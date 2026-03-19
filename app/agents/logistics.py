"""
Pydantic AI logistics agent: Qwen 2.5 via Groq + tools (shipments, tracking) + Mem0 memory.

Use from API or WhatsApp webhook: run the agent with user message and deps (session, user_id),
then store the turn in Mem0 for future context.

Every agent run is automatically traced by Logfire (instrument_pydantic_ai).
"""

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import orm
from app.services.tracking import fetch_container_tracking_data

logger = logging.getLogger(__name__)

# Lazy agent creation so we don't require groq/pydantic-ai at import if not used
_agent = None


@dataclass
class AgentDeps:
    """Dependencies for the logistics agent: DB session and current user."""

    session: AsyncSession
    user_id: str  # User identifier (e.g. str(user.id) or email) for DB and Mem0


def _build_agent():
    if not settings.groq_api_key:
        return None
    try:
        from pydantic_ai import Agent, RunContext
        from pydantic_ai.models.groq import GroqModel
        from pydantic_ai.providers.groq import GroqProvider

        model = GroqModel(
            settings.groq_model_agent,
            provider=GroqProvider(api_key=settings.groq_api_key),
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
        logger.warning("Pydantic AI / Groq not available: %s", e)
        return None


def _strip_thinking(text: str) -> str:
    """
    Remove Qwen3 thinking blocks from agent output.
    Handles both complete <think>...</think> and incomplete blocks (token cutoff).
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
    return text.strip()


def get_logistics_agent():
    """Return the shared logistics agent, or None if Groq is not configured."""
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
        await agent_memory.add(
            user_id,
            [{"role": "user", "content": message}, {"role": "assistant", "content": output}],
            metadata={"source": "tydline_agent"},
        )
        return output
    except Exception as e:
        logger.exception("Agent run failed: %s", e)
        return None
