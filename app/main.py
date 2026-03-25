import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1.router import router as v1_router
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.observability.logfire_setup import configure_logfire

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("tydline")


async def _log_raw_request(request: Request) -> None:
    """Log every incoming request with headers and body for local debugging."""
    body = await request.body()
    logger.info(
        "\n─── INCOMING REQUEST ───────────────────────────────\n"
        "  %s %s\n"
        "  Headers: %s\n"
        "  Body: %s\n"
        "────────────────────────────────────────────────────",
        request.method,
        request.url,
        dict(request.headers),
        body.decode("utf-8", errors="replace")[:2000],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle."""
    configure_logfire()
    try:
        import logfire
        logfire.instrument_fastapi(app, capture_headers=False)
    except Exception:
        pass
    yield


def create_app() -> FastAPI:
    """
    Application factory for FastAPI.

    Keeps construction logic isolated so we can reuse in tests,
    ASGI servers, and serverless handlers.
    """
    is_production = settings.environment == "production"

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS — restrict to configured origins; empty list = same-origin only
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # In development: log every raw request so webhook payloads are visible
    if not is_production:
        @app.middleware("http")
        async def debug_request_logger(request: Request, call_next):
            await _log_raw_request(request)
            return await call_next(request)

    # Register routers
    app.include_router(v1_router)

    @app.get("/", include_in_schema=False)
    async def root():
        return {"message": "Tydline backend is running"}

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        """
        Basic health probe for Cloud Run / monitoring.
        Checks DB connectivity, ShipsGo, and Groq.
        """
        db_status = "unknown"
        shipsgo_status = "unknown"
        groq_status = "unknown"

        # DB
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1"))
            db_status = "connected"
        except Exception as exc:
            logger.warning("Health check DB failed: %s", exc)
            db_status = "error"

        # ShipsGo
        if settings.shipsgo_api_key:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(
                        settings.shipsgo_api_base_url + "/health",
                        timeout=3.0,
                    )
                shipsgo_status = "reachable" if resp.status_code < 500 else "error"
            except Exception as exc:
                logger.warning("Health check ShipsGo failed: %s", exc)
                shipsgo_status = "error"
        else:
            shipsgo_status = "not_configured"

        # Groq (optional, only if configured)
        groq_api_key = getattr(settings, "groq_api_key", None)
        if groq_api_key:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(
                        "https://api.groq.com/openai/v1/models",
                        headers={"Authorization": f"Bearer {groq_api_key}"},
                    )
                groq_status = "reachable" if resp.status_code < 500 else "error"
            except Exception as exc:
                logger.warning("Health check Groq failed: %s", exc)
                groq_status = "error"
        else:
            groq_status = "not_configured"

        overall = "ok" if db_status == "connected" else "degraded"

        return {
            "status": overall,
            "environment": settings.environment,
            "database": db_status,
            "shipsgo_api": shipsgo_status,
            "groq_api": groq_status,
            "logfire": "configured" if settings.logfire_token else "not_configured",
        }

    # Catch-all: log unmatched routes clearly instead of silent 404
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def catch_all(path: str, request: Request):
        from fastapi.responses import JSONResponse
        logger.warning("No route matched: %s %s — check your proxy webhook URL", request.method, request.url)
        return JSONResponse(
            status_code=404,
            content={"detail": f"No route matched: {request.method} /{path}", "registered_webhook": "/api/v1/whatsapp/webhook"},
        )

    return app


app = create_app()
