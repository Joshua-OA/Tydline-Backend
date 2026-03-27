"""
Pydantic AI logistics agent: GPT-4o via OpenAI + tools (shipments, tracking) + Mem0 memory.

Use from API or WhatsApp webhook: run the agent with user message and deps (session, user_id),
then store the turn in Mem0 for future context.

Every agent run is automatically traced by Logfire (instrument_pydantic_ai).
"""

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import orm
from app.services.tracking import fetch_container_tracking_data

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Short-term conversation history (sliding window per user, in-memory)
# ---------------------------------------------------------------------------

_HISTORY_TTL = 30 * 60  # 30 minutes
_HISTORY_MAX_TURNS = 5  # number of (user, assistant) pairs to retain

@dataclass
class _Turn:
    user: str
    assistant: str
    ts: float = field(default_factory=time.monotonic)

# user_id → deque of recent turns
_recent_turns: dict[str, deque] = {}


def _get_recent_turns(user_id: str) -> list[_Turn]:
    """Return recent turns for user, evicting stale ones."""
    turns = _recent_turns.get(user_id)
    if not turns:
        return []
    now = time.monotonic()
    while turns and now - turns[0].ts > _HISTORY_TTL:
        turns.popleft()
    return list(turns)


def _save_turn(user_id: str, user_msg: str, assistant_msg: str) -> None:
    """Append a turn to the user's recent history."""
    if user_id not in _recent_turns:
        _recent_turns[user_id] = deque(maxlen=_HISTORY_MAX_TURNS)
    _recent_turns[user_id].append(_Turn(user=user_msg, assistant=assistant_msg))


# Lazy agent creation so we don't require openai/pydantic-ai at import if not used
_agent = None


@dataclass
class AgentDeps:
    """Dependencies for the logistics agent: DB session and current user."""

    session: AsyncSession
    user_id: str  # User identifier (e.g. str(user.id) or email) for DB and Mem0


def _build_agent():
    if not settings.openai_api_key:
        return None
    try:
        from pydantic_ai import Agent, RunContext
        from pydantic_ai.models.openai import OpenAIModel
        from pydantic_ai.providers.openai import OpenAIProvider

        model = OpenAIModel(
            settings.openai_model_agent,
            provider=OpenAIProvider(api_key=settings.openai_api_key),
        )
        agent = Agent(
            model,
            deps_type=AgentDeps,
            instructions=(
                "You are TASA, Tydline's AI logistics assistant. Your name is TASA. "
                "Always introduce yourself as TASA when starting a conversation or when context makes it helpful (e.g. 'Hi, TASA here.'). "
                "You help importers track containers and avoid demurrage. "
                "Speak in first person as TASA. Be concise and actionable.\n\n"
                "TOOL USAGE RULES:\n"
                "- If the user mentions a BL number or container number not yet in the system, call add_shipment immediately.\n"
                "- If the user says 'approve', 'yes', 'start tracking', 'go ahead', or similar after a shipment was just added, call approve_shipment.\n"
                "- If the user asks about their shipments, call list_my_shipments.\n"
                "- If the user asks for live status of a specific container, call get_shipment_status.\n\n"
                "IMPORTANT: If the user's message includes [EXTRACTED: ...], those shipments are already saved. "
                "Tell the user they've been added and ask if they'd like to approve them to begin tracking. "
                "Never ask the user to re-provide container or BL numbers that were already extracted."
            ),
        )

        @agent.system_prompt
        async def system_prompt(ctx: RunContext[AgentDeps]) -> str:
            base = (
                "You are TASA, Tydline's AI logistics assistant. Help the user with container tracking and demurrage risk. "
                "Always speak as TASA in first person. "
                "Use list_my_shipments to see their shipments, get_shipment_status for live tracking, "
                "add_shipment to save a new BL or container, and approve_shipment to start tracking a pending shipment."
            )
            from app.agents.memory import agent_memory

            try:
                memories = agent_memory.search(
                    ctx.deps.user_id,
                    "shipments containers tracking bill of lading email",
                    limit=8,
                )
                if memories:
                    base += "\n\nRelevant context from past conversations:\n" + "\n".join(f"- {m}" for m in memories)
            except Exception:
                logger.warning("agent: memory search failed for user %s", ctx.deps.user_id, exc_info=True)

            recent = _get_recent_turns(ctx.deps.user_id)
            if recent:
                history_lines = []
                for turn in recent:
                    history_lines.append(f"User: {turn.user}")
                    history_lines.append(f"Assistant: {turn.assistant}")
                base += "\n\nRecent conversation (use this for short follow-up replies like 'yes' or 'approve'):\n" + "\n".join(history_lines)

            return base

        @agent.tool
        async def list_my_shipments(ctx: RunContext[AgentDeps]) -> str:
            """List the current user's shipments with BL, container, status, and ETA."""
            uid = ctx.deps.user_id
            try:
                user_uuid = UUID(uid)
            except ValueError:
                return "Could not identify user. Please try again."
            try:
                result = await ctx.deps.session.execute(
                    select(orm.Shipment)
                    .where(orm.Shipment.user_id == user_uuid)
                    .order_by(orm.Shipment.created_at.desc())
                )
                shipments = result.scalars().all()
                if not shipments:
                    return "No shipments found for this user."
                lines = []
                for s in shipments:
                    ref = s.container_number or s.bill_of_lading or "—"
                    bl = f", BL: {s.bill_of_lading}" if s.bill_of_lading and s.container_number else ""
                    eta = s.eta.isoformat() if s.eta else "—"
                    risk = f", risk: {s.demurrage_risk}" if s.demurrage_risk else ""
                    lines.append(f"- {ref}{bl}: {s.status}, ETA {eta}{risk}")
                return "\n".join(lines)
            except Exception as e:
                logger.exception("list_my_shipments failed for user %s", uid)
                return f"Error loading shipments: {e!s}"

        @agent.tool
        async def add_shipment(
            ctx: RunContext[AgentDeps],
            bl_number: str | None,
            container_number: str | None,
            carrier: str | None,
        ) -> str:
            """
            Save a new shipment (BL or container number) to the user's account with status
            pending_approval. Call this as soon as the user provides a BL or container number
            that isn't already being tracked. Returns a confirmation with the shipment ID.
            """
            uid = ctx.deps.user_id
            bl = (bl_number or "").strip().upper() or None
            container = (container_number or "").strip().upper() or None
            if not bl and not container:
                return "Please provide a BL number or container number to add."
            try:
                user_uuid = UUID(uid)
            except ValueError:
                return "Could not identify user."

            try:
                # Check for existing shipment
                filters = []
                if bl:
                    filters.append(orm.Shipment.bill_of_lading == bl)
                if container:
                    filters.append(orm.Shipment.container_number == container)

                from sqlalchemy import or_
                existing = (await ctx.deps.session.execute(
                    select(orm.Shipment).where(
                        orm.Shipment.user_id == user_uuid,
                        or_(*filters),
                    )
                )).scalar_one_or_none()

                if existing:
                    ref = container or bl
                    if existing.status == "pending_approval":
                        return (
                            f"Shipment {ref} is already saved and pending your approval (ID: {existing.id}). "
                            "Say 'approve' to start tracking."
                        )
                    return (
                        f"Shipment {ref} is already in your account with status: {existing.status}. "
                        f"ETA: {existing.eta.isoformat() if existing.eta else 'not yet available'}."
                    )

                shipment = orm.Shipment(
                    container_number=container,
                    bill_of_lading=bl,
                    carrier=carrier or None,
                    user_id=user_uuid,
                    status="pending_approval",
                )
                ctx.deps.session.add(shipment)
                await ctx.deps.session.commit()
                await ctx.deps.session.refresh(shipment)
                ref = container or bl
                logger.info(
                    "agent: add_shipment — created shipment id=%s bl=%s container=%s carrier=%s user_id=%s",
                    shipment.id, bl, container, carrier, uid,
                )
                return (
                    f"I've added {ref} to your account (ID: {shipment.id}). "
                    "It's pending approval. Would you like me to approve it and start tracking now?"
                )
            except Exception as e:
                logger.exception("add_shipment failed for user %s", uid)
                return f"Error saving shipment: {e!s}"

        @agent.tool
        async def approve_shipment(
            ctx: RunContext[AgentDeps],
            reference: str,
        ) -> str:
            """
            Approve a pending shipment and immediately start tracking it.
            'reference' can be a BL number, container number, or shipment ID.
            Call this when the user says 'approve', 'yes', 'start tracking', or similar.
            """
            uid = ctx.deps.user_id
            ref = (reference or "").strip().upper()
            if not ref:
                return "Please specify which shipment to approve (BL number or container number)."
            try:
                user_uuid = UUID(uid)
            except ValueError:
                return "Could not identify user."

            try:
                # Try matching by BL, container number, or shipment ID
                from sqlalchemy import or_
                conditions = [
                    orm.Shipment.bill_of_lading == ref,
                    orm.Shipment.container_number == ref,
                ]
                try:
                    conditions.append(orm.Shipment.id == UUID(reference.strip()))
                except (ValueError, AttributeError):
                    pass

                result = await ctx.deps.session.execute(
                    select(orm.Shipment).where(
                        orm.Shipment.user_id == user_uuid,
                        or_(*conditions),
                    )
                )
                shipment = result.scalar_one_or_none()

                if not shipment:
                    return (
                        f"I couldn't find a shipment matching '{reference}' on your account. "
                        "Use list_my_shipments to see what's available."
                    )

                if shipment.status != "pending_approval":
                    status_msg = {
                        "tracking_started": "already approved and being tracked",
                        "delivered": "already marked as delivered",
                        "cancelled": "cancelled",
                    }.get(shipment.status, f"already in status '{shipment.status}'")
                    ref_label = shipment.container_number or shipment.bill_of_lading or reference
                    return f"Shipment {ref_label} is {status_msg}. No action needed."

                shipment.status = "tracking_started"
                ctx.deps.session.add(shipment)
                await ctx.deps.session.commit()
                await ctx.deps.session.refresh(shipment)

                ref_label = shipment.container_number or shipment.bill_of_lading or reference
                logger.info(
                    "agent: approve_shipment — approved id=%s ref=%s user_id=%s — triggering tracking",
                    shipment.id, ref_label, uid,
                )

                # Fire-and-forget tracking lookup (same pattern as dashboard endpoint)
                from app.services.tracking import initial_track_shipment
                asyncio.create_task(initial_track_shipment(shipment.id))

                return (
                    f"Approved! I'm now starting to track {ref_label}. "
                    "I'll notify you as soon as I have live tracking data."
                )
            except Exception as e:
                logger.exception("approve_shipment failed for user %s ref=%s", uid, reference)
                return f"Error approving shipment: {e!s}"

        @agent.tool
        async def get_shipment_status(ctx: RunContext[AgentDeps], container_number: str) -> str:
            """Get live tracking status for a container from ShipsGo. Use when the user asks about a specific container number."""
            if not container_number or not container_number.strip():
                return "Please provide a container number."
            try:
                data = await fetch_container_tracking_data(container_number.strip())
                if not data:
                    return f"No tracking data found for container {container_number}."
                status = data.get("status") or "unknown"
                location = data.get("location") or "—"
                eta = data.get("eta")
                eta_str = eta.isoformat() if hasattr(eta, "isoformat") else str(eta) if eta else "—"
                vessel = data.get("vessel") or "—"
                return f"Container {data.get('container_number', container_number)}: status={status}, location={location}, ETA={eta_str}, vessel={vessel}"
            except Exception as e:
                logger.exception("get_shipment_status failed for %s", container_number)
                return f"Error fetching status: {e!s}"

        return agent
    except ImportError as e:
        logger.warning("Pydantic AI / OpenAI not available: %s", e)
        return None


def _strip_thinking(text: str) -> str:
    """Remove any thinking blocks from agent output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
    return text.strip()


def get_logistics_agent():
    """Return the shared logistics agent, or None if OpenAI is not configured."""
    global _agent
    if _agent is None:
        _agent = _build_agent()
    return _agent


async def run_agent(user_id: str, message: str, session: AsyncSession) -> str | None:
    """
    Run the logistics agent for one user message and return the reply.
    Persists the turn to Mem0 when Mem0 is configured.
    Returns None if the agent is not available (e.g. no OpenAI key).
    """
    agent = get_logistics_agent()
    if not agent:
        logger.warning("run_agent: agent not available (OpenAI not configured) for user %s", user_id)
        return None

    from app.agents.memory import agent_memory

    logger.info("run_agent: starting for user_id=%s message_len=%d", user_id, len(message))
    deps = AgentDeps(session=session, user_id=user_id)
    try:
        result = await agent.run(message, deps=deps)
        output = result.output if hasattr(result, "output") else str(result)
        output = _strip_thinking(output)
        logger.info("run_agent: completed for user_id=%s output_len=%d", user_id, len(output))

        _save_turn(user_id, message, output)

        logger.debug("run_agent: saving turn to Mem0 for user_id=%s", user_id)
        mem_ok = await agent_memory.add(
            user_id,
            [{"role": "user", "content": message}, {"role": "assistant", "content": output}],
            metadata={"source": "tydline_agent"},
        )
        if mem_ok:
            logger.info("run_agent: Mem0 turn saved for user_id=%s", user_id)
        else:
            logger.warning("run_agent: Mem0 save returned False for user_id=%s — memory may be unavailable", user_id)

        return output
    except Exception as e:
        logger.exception("run_agent: agent run failed for user_id=%s: %s", user_id, e)
        return None
