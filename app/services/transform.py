"""Outbound payload transformation.

Given an endpoint's ``PayloadFormat``, build the exact request body the worker
POSTs. This is pure (no I/O) so it is trivially unit-testable and reused by the
delivery worker. ``raw`` preserves the platform's native envelope; ``discord``
maps the event onto a Discord webhook message so it renders as a chat embed.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.models.endpoint import PayloadFormat

# Accent colour matching the dashboard (0x6C8CFF) as a Discord integer colour.
_DISCORD_COLOR = 0x6C8CFF
_DISCORD_MAX_FIELDS = 25  # Discord caps embeds at 25 fields
_DISCORD_MAX_VALUE = 1024  # Discord caps a field value at 1024 chars
_DISCORD_MAX_NAME = 256


def build_delivery_body(
    payload_format: PayloadFormat,
    event_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> bytes:
    """Return the compact-encoded JSON body to POST for this endpoint format."""
    if payload_format == PayloadFormat.discord:
        document: dict[str, Any] = to_discord_message(event_id, event_type, payload)
    else:
        document = {"event_id": str(event_id), "type": event_type, "payload": payload}
    return json.dumps(document, separators=(",", ":")).encode()


def to_discord_message(
    event_id: uuid.UUID, event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Map an event onto a Discord webhook message with a single embed.

    Every top-level payload key becomes an inline embed field. Discord rejects
    empty messages and empty field values, so the embed always carries a title
    and description, field values fall back to ``—``, and both the field count
    and value length are clamped to Discord's limits.
    """
    fields: list[dict[str, Any]] = []
    for key, value in list(payload.items())[:_DISCORD_MAX_FIELDS]:
        rendered = value if isinstance(value, str) else json.dumps(value)
        fields.append(
            {
                "name": str(key)[:_DISCORD_MAX_NAME],
                "value": (rendered or "—")[:_DISCORD_MAX_VALUE],
                "inline": True,
            }
        )

    embed: dict[str, Any] = {
        "title": f"📨 {event_type}",
        "description": "Delivered by the Reliable Webhook Platform",
        "color": _DISCORD_COLOR,
        "footer": {"text": f"event {event_id}"},
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if fields:
        embed["fields"] = fields

    return {"username": "HookIt", "embeds": [embed]}
