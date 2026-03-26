"""
Mem0 self-hosted memory backed by Supabase pgvector.

Uses mem0's Memory class (not MemoryClient) so everything stays in our own
Supabase database — no Mem0 cloud account needed.

Stack:
  Vector store : pgvector on Supabase (same Postgres instance we already use)
  LLM          : Groq     (fact extraction — llama-3.1-8b-instant)
  Embedder     : OpenAI   text-embedding-3-small (API-based, 1536 dims)

API-based embeddings are used because Cloud Run scales to zero — a local model
would re-download on every cold start.

mem0 will automatically create a `tydline_memories` table with a vector
column in Supabase the first time it runs.

Prerequisite: run this once in the Supabase SQL editor:
  create extension if not exists vector;
"""

import asyncio
import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_memory: Any = None  # mem0.Memory instance, built once on first use


def _get_sync_db_url() -> str:
    """
    Convert the async DATABASE_URL to a psycopg2-compatible sync URL.
    postgresql+asyncpg://...  →  postgresql://...?sslmode=require
    """
    url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


def _build_memory() -> Any:
    """Build and return a configured mem0.Memory instance.

    LLM priority: OpenAI (primary) → Groq (fallback if OpenAI unavailable).
    Embeddings always use OpenAI text-embedding-3-small.
    """
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY not set — mem0 Memory unavailable")
        return None
    try:
        from mem0 import Memory
        from mem0.configs.base import (
            EmbedderConfig,
            LlmConfig,
            MemoryConfig,
            VectorStoreConfig,
        )

        # Use OpenAI as primary LLM; fall back to Groq if OpenAI key is absent
        if settings.openai_api_key:
            llm_config = LlmConfig(
                provider="openai",
                config={
                    "model": settings.openai_model,
                    "api_key": settings.openai_api_key,
                    "temperature": 0.0,
                    "max_tokens": 1500,
                },
            )
            logger.info("mem0 using OpenAI as LLM for fact extraction")
        else:
            llm_config = LlmConfig(
                provider="groq",
                config={
                    "model": settings.groq_model,
                    "api_key": settings.groq_api_key,
                    "temperature": 0.0,
                    "max_tokens": 1500,
                },
            )
            logger.info("mem0 using Groq as fallback LLM for fact extraction")

        config = MemoryConfig(
            vector_store=VectorStoreConfig(
                provider="pgvector",
                config={
                    "connection_string": _get_sync_db_url(),
                    "collection_name": "tydline_memories",
                    "embedding_model_dims": 1536,
                    "hnsw": True,
                },
            ),
            llm=llm_config,
            embedder=EmbedderConfig(
                provider="openai",
                config={
                    "model": "text-embedding-3-small",
                    "api_key": settings.openai_api_key,
                    "embedding_dims": 1536,
                },
            ),
        )
        memory = Memory(config=config)
        logger.info("mem0 Memory initialised (pgvector @ Supabase + OpenAI embeddings)")
        return memory
    except Exception as e:
        logger.warning("mem0 Memory init failed: %s", e)
        return None


def _get_memory() -> Any:
    global _memory
    if _memory is not None:
        return _memory
    _memory = _build_memory()
    return _memory


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def add_memory(
    user_id: str,
    messages: list[dict[str, str]],
    metadata: dict[str, Any] | None = None,
) -> bool:
    """
    Store a conversation turn or email context for the user.

    Mem0 processes the messages through Groq to extract discrete facts, then
    embeds and stores them as vectors in the Supabase pgvector table.
    Pass natural user/assistant message pairs — avoid role=system.

    Returns True if the data was successfully stored, False if mem0 is
    unavailable or an error occurred.
    """
    memory = _get_memory()
    if not memory:
        return False
    try:
        await asyncio.to_thread(
            memory.add,
            messages,
            user_id=user_id,
            metadata=metadata or {},
        )
        return True
    except Exception as e:
        logger.warning("mem0 add_memory failed: %s", e)
        return False


def search_memory(user_id: str, query: str, limit: int = 8) -> list[str]:
    """
    Search the user's memories for context relevant to *query*.
    Returns a list of fact strings. Returns [] on failure or if not configured.
    """
    memory = _get_memory()
    if not memory:
        return []
    try:
        results = memory.search(query, user_id=user_id, limit=limit)
        # mem0 Memory.search returns {"results": [{"memory": "...", ...}, ...]}
        if isinstance(results, dict):
            items = results.get("results", [])
        elif isinstance(results, list):
            items = results
        else:
            return []

        out: list[str] = []
        for item in items:
            if isinstance(item, dict):
                text = item.get("memory") or item.get("text") or item.get("content")
                if isinstance(text, str):
                    out.append(text)
            elif isinstance(item, str):
                out.append(item)
        return out[:limit]
    except Exception as e:
        logger.warning("mem0 search_memory failed: %s", e)
        return []


class AgentMemory:
    """Thin wrapper used by the agent: add after each turn, search before reply."""

    async def add(
        self,
        user_id: str,
        messages: list[dict[str, str]],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        return await add_memory(user_id, messages, metadata)

    def search(self, user_id: str, query: str, limit: int = 8) -> list[str]:
        return search_memory(user_id, query, limit)


agent_memory = AgentMemory()
