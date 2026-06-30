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
    max_event_payload_bytes: int = Field(
        default=65_536,
        description="Maximum JSON payload size in bytes for POST /events.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    Cached so configuration is parsed once per process. Tests may clear the
    cache via ``get_settings.cache_clear()`` to inject overrides.
    """

    return Settings()
