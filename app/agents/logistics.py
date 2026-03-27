"""
Pydantic AI logistics agent: GPT-4o via OpenAI + tools (shipments, tracking) + Mem0 memory.

Use from API or WhatsApp webhook: run the agent with user message and deps (session, user_id),
then store the turn in Mem0 for future context.

Every agent run is automatically traced by Logfire (instrument_pydantic_ai).
"""

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
                "You are Tydline's logistics assistant. You help importers track containers and avoid demurrage. "
                "Use the tools to look up the user's shipments and container status when needed. "
                "Be concise and actionable. If you don't have data, say so and suggest they add a container or try again later. "
                "IMPORTANT: If the user's message includes extracted BL or container numbers (shown as [EXTRACTED: ...]), "
                "acknowledge them immediately and confirm that tracking has begun. "
                "Never ask the user to provide container numbers if BL numbers or containers were already extracted from their message."
            ),
        )

        @agent.system_prompt
        async def system_prompt(ctx: RunContext[AgentDeps]) -> str:
            base = (
                "You are Tydline's logistics assistant. Help the user with container tracking and demurrage risk. "
                "Use list_my_shipments to see their shipments and get_shipment_status for live status of a container."
            )
            from app.agents.memory import agent_memory

            memories = agent_memory.search(ctx.deps.user_id, "shipments containers tracking bill of lading email", limit=8)
            if memories:
                base += "\n\nRelevant context from past conversations:\n" + "\n".join(f"- {m}" for m in memories)

            recent = _get_recent_turns(ctx.deps.user_id)
            if recent:
                history_lines = []
                for turn in recent:
                    history_lines.append(f"User: {turn.user}")
                    history_lines.append(f"Assistant: {turn.assistant}")
                base += "\n\nRecent conversation (most relevant for short follow-up replies):\n" + "\n".join(history_lines)

            return base

        @agent.tool
        async def list_my_shipments(ctx: RunContext[AgentDeps]) -> str:
            """List the current user's shipments (container number, status, ETA). Use this when the user asks about their containers or shipments."""
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
                    eta = s.eta.isoformat() if s.eta else "—"
                    risk = f", risk: {s.demurrage_risk}" if s.demurrage_risk else ""
                    lines.append(f"- {s.container_number}: {s.status}, ETA {eta}{risk}")
                return "\n".join(lines)
            except Exception as e:
                logger.exception("list_my_shipments failed")
                return f"Error loading shipments: {e!s}"

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
                logger.exception("get_shipment_status failed")
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
    Returns None if the agent is not available (e.g. no Groq key).
    """
    agent = get_logistics_agent()
    if not agent:
        return None

    from app.agents.memory import agent_memory

    deps = AgentDeps(session=session, user_id=user_id)
    try:
        result = await agent.run(message, deps=deps)
        output = result.output if hasattr(result, "output") else str(result)
        output = _strip_thinking(output)
        _save_turn(user_id, message, output)
        await agent_memory.add(
            user_id,
            [{"role": "user", "content": message}, {"role": "assistant", "content": output}],
            metadata={"source": "tydline_agent"},
        )
        return output
    except Exception as e:
        logger.exception("Agent run failed: %s", e)
        return None
