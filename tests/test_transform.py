"""Unit tests for outbound payload transformation (pure, no DB required)."""

from __future__ import annotations

import json
import uuid

from app.models.endpoint import PayloadFormat
from app.services.transform import build_delivery_body, to_discord_message


def test_raw_body_preserves_native_envelope() -> None:
    event_id = uuid.uuid4()
    body = build_delivery_body(PayloadFormat.raw, event_id, "order.created", {"order_id": "abc"})
    assert json.loads(body) == {
        "event_id": str(event_id),
        "type": "order.created",
        "payload": {"order_id": "abc"},
    }


def test_raw_body_is_compact_json() -> None:
    body = build_delivery_body(PayloadFormat.raw, uuid.uuid4(), "t", {"a": 1})
    assert b", " not in body and b": " not in body  # compact separators


def test_discord_body_builds_embed_with_fields() -> None:
    event_id = uuid.uuid4()
    body = build_delivery_body(
        PayloadFormat.discord,
        event_id,
        "order.created",
        {"order_id": "abc", "amount": 42},
    )
    doc = json.loads(body)
    assert doc["username"] == "HookIt"
    assert len(doc["embeds"]) == 1
    embed = doc["embeds"][0]
    assert "order.created" in embed["title"]
    assert embed["description"]
    assert f"event {event_id}" == embed["footer"]["text"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields == {"order_id": "abc", "amount": "42"}


def test_discord_empty_payload_still_valid_message() -> None:
    # Discord rejects empty messages / empty field values; ensure a title-only
    # embed with no empty fields array.
    doc = json.loads(build_delivery_body(PayloadFormat.discord, uuid.uuid4(), "ping", {}))
    embed = doc["embeds"][0]
    assert embed["title"]
    assert "fields" not in embed  # no empty fields list emitted


def test_discord_clamps_field_count_and_value_length() -> None:
    payload = {f"k{i}": "x" * 2000 for i in range(40)}
    embed = to_discord_message(uuid.uuid4(), "t", payload)["embeds"][0]
    assert len(embed["fields"]) == 25  # Discord's max
    assert all(len(f["value"]) <= 1024 for f in embed["fields"])


def test_discord_non_string_values_are_json_encoded() -> None:
    embed = to_discord_message(uuid.uuid4(), "t", {"nested": {"k": 1}, "flag": True})
    fields = {f["name"]: f["value"] for f in embed["embeds"][0]["fields"]}
    assert fields["nested"] == '{"k": 1}'
    assert fields["flag"] == "true"
