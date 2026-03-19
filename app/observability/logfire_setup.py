"""
Logfire observability for Tydline.

Auto-instruments:
  - Pydantic AI  — agent runs, tool calls, model requests, token counts
  - OpenAI       — embedding calls (mem0 uses text-embedding-3-small)
  - HTTPX        — outbound HTTP calls (ShipsGo, proxy, etc.)
"""

import logging

logger = logging.getLogger(__name__)

_initialized = False


def configure_logfire() -> bool:
    """
    Configure Logfire. Returns True if successfully initialised.

    Local dev:  uses ~/.logfire/default.toml credentials (logfire auth).
                LOGFIRE_TOKEN can be left empty.
    Production: set LOGFIRE_TOKEN to a project write token.
    """
    global _initialized
    if _initialized:
        return True

    try:
        import logfire
        from app.core.config import settings

        kwargs = {}
        if settings.logfire_token:
            kwargs["token"] = settings.logfire_token

        logfire.configure(service_name="tydline-backend", **kwargs)

        # Pydantic AI — agent runs, tool calls, model name, input/output tokens
        logfire.instrument_pydantic_ai()

        # OpenAI — mem0 embedding calls (text-embedding-3-small), token counts
        try:
            logfire.instrument_openai()
        except Exception as exc:
            logger.debug("Logfire OpenAI instrumentation skipped: %s", exc)

        # HTTPX — outbound HTTP (ShipsGo, Moolre, Groq raw calls, proxy)
        try:
            logfire.instrument_httpx(capture_request_body=True, capture_response_body=False)
        except Exception as exc:
            logger.debug("Logfire HTTPX instrumentation skipped: %s", exc)

        _initialized = True
        logger.info("Logfire observability initialised")
        return True

    except Exception as exc:
        logger.warning("Logfire initialisation failed: %s", exc)
        return False
