from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Core
    app_name: str = "Tydline Core"
    environment: str = "development"

    # Database (Supabase Postgres URI, e.g. postgres:// or postgresql+asyncpg://)
    database_url: str

    # Supabase (for future auth/integration use)
    supabase_url: str | None = None
    supabase_anon_key: str | None = None

    # Outbound email provider: "postmark" (default) or "resend"
    email_provider: str = "postmark"
    email_from: str | None = None

    # Postmark (outbound + inbound webhook)
    postmark_server_token: str | None = None
    # HTTP Basic Auth secret for the Postmark inbound webhook (format: "user:password")
    postmark_inbound_secret: str | None = None

    # Resend (outbound only — use when EMAIL_PROVIDER=resend)
    resend_api_key: str | None = None

    # Legacy SMTP (optional fallback)
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None

    whatsapp_api_key: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    # Shared secret — sent as X-Webhook-Secret on both inbound forwards and outbound pushes
    whatsapp_webhook_secret: str | None = None
    # Full URL for the async push endpoint on the proxy
    # e.g. https://proxy.tydline.com/whatsapp/external/send
    whatsapp_proxy_url: str | None = None
    sms_api_key: str | None = None

    # OpenAI (used for mem0 embeddings — text-embedding-3-small)
    openai_api_key: str | None = None

    # Groq (AI alert generation + agent)
    groq_api_key: str | None = None
    groq_model: str = "llama-3.1-8b-instant"
    # Agent: Pydantic AI with Qwen 3 via Groq
    groq_model_agent: str = "qwen/qwen3-32b"

    # Mem0 (conversation/shipment memory for the agent)
    mem0_api_key: str | None = None

    # Logfire (LLM observability and tracing)
    logfire_token: str | None = None

    # API security (optional; if set, X-API-Key required on protected routes)
    api_key: str | None = None

    # Frontend URL (used in magic link emails)
    frontend_url: str = "http://localhost:5173"

    # Moolre MoMo payment integration
    moolre_webhook_secret: str | None = None
    moolre_api_user: str | None = None
    moolre_public_key: str | None = None
    moolre_amount: str = "99.00"

    # Postmark display name for outbound emails
    postmark_from_name: str = "Tydline"

    # CORS — comma-separated list of allowed origins, e.g. https://app.tydline.com
    # Defaults to no external origins (same-origin only). Use ["*"] only in dev.
    cors_origins: list[str] = []

    # ShipsGo container tracking API
    shipsgo_api_base_url: str = "https://api.shipsgo.com"
    shipsgo_api_key: str | None = None

    # Fallback generic tracking vars (optional; ShipsGo takes precedence)
    tracking_api_base_url: str | None = None
    tracking_api_key: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
