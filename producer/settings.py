"""Configuration for the live crypto producer service.

Read from environment variables (or a local ``.env`` during development), same
pattern as ``app.core.config``. The producer is a separate process with its own
environment; ``PLATFORM_API_KEY`` is a secret and must be injected, never
committed.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from producer.prices import DEFAULT_SYMBOLS


class ProducerSettings(BaseSettings):
    """Strongly-typed producer settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    platform_api_url: str = Field(
        default="http://localhost:8000",
        description="Base URL of the webhook platform's public API (POST /events).",
    )
    platform_api_key: str = Field(
        default="",
        description="Bearer API key for the seeded showcase project. Secret — inject via env.",
    )
    price_api_url: str = Field(
        default="https://api.coinbase.com/v2/prices",
        description="Base URL of the keyless spot-price API (Coinbase v2 prices).",
    )
    symbols: str = Field(
        default=",".join(DEFAULT_SYMBOLS),
        description="Comma-separated product ids to poll, e.g. 'BTC-USD,ETH-USD'.",
    )
    poll_interval_seconds: float = Field(
        default=4.0,
        ge=1.0,
        description="Seconds between polling cycles. Kept gentle to respect upstream limits.",
    )
    alert_threshold_pct: float = Field(
        default=0.5,
        gt=0.0,
        description="Percent move from a symbol's anchor that triggers a price.alert.",
    )
    burst_count: int = Field(
        default=20,
        ge=1,
        le=200,
        description="How many rapid tick events one /burst request fires.",
    )
    request_timeout_seconds: float = Field(
        default=10.0,
        description="HTTP timeout for both upstream price fetches and event publishing.",
    )
    control_host: str = Field(
        default="::",
        description=(
            "Bind host for the tiny control server exposing POST /burst. Defaults "
            "to '::' (all IPv6 + IPv4-mapped) so it is reachable over Fly's private "
            "networking (6PN), which is IPv6-only — binding '0.0.0.0' would refuse "
            "those connections."
        ),
    )
    control_port: int = Field(
        default=8100,
        description="Bind port for the control server.",
    )
    log_level: str = Field(default="INFO", description="Logging level.")

    @property
    def symbol_list(self) -> list[str]:
        """Parsed, trimmed, non-empty symbols."""
        return [s.strip() for s in self.symbols.split(",") if s.strip()]


@lru_cache(maxsize=1)
def get_producer_settings() -> ProducerSettings:
    """Return a cached :class:`ProducerSettings` instance."""
    return ProducerSettings()
