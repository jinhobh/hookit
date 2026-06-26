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

    # --- Delivery / worker defaults ---------------------------------------
    # These are intentionally present as placeholders so later phases have a
    # single, typed home for tuning. They are not yet consumed by any code.
    delivery_timeout_seconds: float = Field(
        default=10.0,
        description="Per-attempt HTTP timeout when delivering a webhook.",
    )
    max_delivery_attempts: int = Field(
        default=6,
        description="Total attempts before a delivery is dead-lettered.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    Cached so configuration is parsed once per process. Tests may clear the
    cache via ``get_settings.cache_clear()`` to inject overrides.
    """

    return Settings()
