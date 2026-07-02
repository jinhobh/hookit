"""Application configuration via Pydantic Settings.

All runtime configuration is read from environment variables (or a local ``.env``
file during development). Never hardcode secrets — see ``.env.example`` for the
full set of supported variables.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Values are loaded, in order of precedence, from: real environment variables,
    then a ``.env`` file (development only). Unknown environment variables are
    ignored so the process can run in shared environments.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------
    app_name: str = Field(
        default="reliable-webhook-platform",
        description="Human-readable service name used in logs and responses.",
    )
    app_env: str = Field(
        default="local",
        description="Deployment environment: local | test | staging | production.",
    )
    debug: bool = Field(
        default=False,
        description="Enable verbose error output. Must be False in production.",
    )

    # --- Database ----------------------------------------------------------
    # Postgres connection URL. The MVP uses Postgres-backed persistence for both
    # data and the delivery job queue before any external broker is considered.
    database_url: str = Field(
        default="postgresql+psycopg://webhook:webhook@localhost:5432/webhook",
        description="SQLAlchemy database URL (psycopg v3 driver).",
    )

    # --- Endpoint secret encryption ----------------------------------------
    # URL-safe base64-encoded 32-byte Fernet key used to encrypt webhook signing
    # secrets at rest.  Must be overridden in staging/production with a secret
    # value sourced from a secrets manager.  The default is a zero-byte key
    # suitable for local development only — never use it in production.
    endpoint_secret_key: str = Field(
        default="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        description="Fernet key (URL-safe base64, 32 bytes) for encrypting endpoint secrets.",
    )

    # --- Logging -----------------------------------------------------------
    log_level: str = Field(
        default="INFO",
        description="Logging level: DEBUG | INFO | WARNING | ERROR | CRITICAL.",
    )

    # --- Delivery / worker defaults ---------------------------------------
    delivery_timeout_seconds: float = Field(
        default=10.0,
        description="Per-attempt HTTP timeout when delivering a webhook.",
    )
    max_delivery_attempts: int = Field(
        default=6,
        description="Total attempts before a delivery is dead-lettered.",
    )
    retry_base_seconds: float = Field(
        default=10.0,
        description="Base delay in seconds for exponential backoff retry scheduling.",
    )
    retry_cap_seconds: float = Field(
        default=3600.0,
        description="Maximum delay in seconds for exponential backoff retry scheduling.",
    )
    worker_batch_size: int = Field(
        default=10,
        description="Max deliveries claimed per worker loop tick.",
    )
    worker_lease_seconds: int = Field(
        default=60,
        description="Lease duration in seconds before a crashed worker's claims expire.",
    )
    worker_name: str = Field(
        default="",
        description=(
            "Stable name identifying this worker process in claim/attempt "
            "attribution (deliveries.claimed_by, delivery_attempts.worker_id). "
            "Empty means derive '<hostname>:<pid>' at runtime."
        ),
    )
    worker_concurrency: int = Field(
        default=1,
        ge=1,
        le=32,
        description=(
            "Independent claim loops the worker entrypoint runs in one process, "
            "each with its own DB session and worker name — exercising "
            "FOR UPDATE SKIP LOCKED across real concurrent sessions."
        ),
    )
    worker_listen_channel: str = Field(
        default="new_delivery",
        description="PostgreSQL LISTEN/NOTIFY channel name for worker wake-up.",
    )
    worker_fallback_poll_seconds: float = Field(
        default=5.0,
        description="Fallback poll interval in seconds when no NOTIFY arrives.",
    )
    max_event_payload_bytes: int = Field(
        default=65_536,
        description="Maximum JSON payload size in bytes for POST /events.",
    )

    # --- Interactive dashboard demo ----------------------------------------
    public_base_url: str = Field(
        default="http://localhost:8000",
        description=(
            "Base URL this process is publicly reachable at, used to build the "
            "self-referential receiver URL for the dashboard demo. Must be a "
            "hostname (e.g. 'https://hookit.fly.dev' or 'http://localhost:8000'), "
            "never an IP literal — validate_url_not_ssrf only blocks IP-literal "
            "loopback/private/link-local hosts, so an IP-literal value here would "
            "cause every demo delivery to dead-letter instantly on the SSRF "
            "check instead of reaching the receiver."
        ),
    )

    # --- Live showcase (real producer → real Discord) ----------------------
    # The dashboard's live demo is fed by the separate `producer` service and
    # delivers real price alerts to a real Discord channel. One shared, seeded
    # "showcase" project backs it (see app/services/showcase.py + app/seed_showcase.py).
    showcase_project_name: str = Field(
        default="__showcase__",
        description="Stable, unique name of the seeded shared showcase project.",
    )
    showcase_api_key: str = Field(
        default="",
        description=(
            "Shared API key for the showcase project (secret). The seeder stores its "
            "hash so the `producer` service can authenticate with this same value. "
            "Empty disables seeding of the key."
        ),
    )
    showcase_discord_webhook_url: str = Field(
        default="",
        description=(
            "Real Discord webhook URL the showcase delivers price alerts to (secret). "
            "Empty disables the Discord endpoint (the reliability demo still works)."
        ),
    )
    producer_base_url: str = Field(
        default="http://localhost:8100",
        description=(
            "Internal base URL of the `producer` control server. POST /showcase/burst "
            "proxies to '{producer_base_url}/burst' so the producer need not be public."
        ),
    )
    discord_widget_server_id: str = Field(
        default="",
        description="Public Discord server (guild) id for the dashboard's embedded channel widget.",
    )
    discord_widget_channel_id: str = Field(
        default="",
        description="Public Discord channel id for the dashboard's embedded channel widget.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    Cached so configuration is parsed once per process. Tests may clear the
    cache via ``get_settings.cache_clear()`` to inject overrides.
    """

    return Settings()
