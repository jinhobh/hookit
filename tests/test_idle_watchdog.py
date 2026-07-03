"""Tests for the idle watchdog: the pure decision function and the orchestration
around it, with the Fly Machines API faked out (no real network, no live Fly app).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import Settings, get_settings
from app.services import idle_watchdog
from app.services.idle_watchdog import _stop_if_idle, is_idle, wake_showcase_machines


def _settings(**overrides: object) -> Settings:
    base = get_settings()
    defaults: dict[str, object] = {
        "database_url": base.database_url,
        "fly_api_token": "tok",
        "fly_app_name": "hookit",
        "visitor_idle_minutes": 10.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class _FakeFlyClient:
    """Records start/stop calls; state seeded per process group."""

    def __init__(self, machines: dict[str, dict[str, bool]]) -> None:
        self._machines = machines
        self.started: list[str] = []
        self.stopped: list[str] = []

    async def list_machine_ids(self, *, process_group: str, started: bool) -> list[str]:
        return [
            mid
            for mid, is_started in self._machines.get(process_group, {}).items()
            if is_started == started
        ]

    async def start(self, machine_id: str) -> bool:
        self.started.append(machine_id)
        return True

    async def stop(self, machine_id: str) -> bool:
        self.stopped.append(machine_id)
        return True


# ===========================================================================
# is_idle: pure decision function
# ===========================================================================


@pytest.mark.parametrize(
    ("minutes_since_seen", "threshold_minutes", "expected"),
    [
        (0.0, 10.0, False),
        (9.9, 10.0, False),
        (10.0, 10.0, True),
        (10.1, 10.0, True),
        (60.0, 10.0, True),
    ],
)
def test_is_idle(minutes_since_seen: float, threshold_minutes: float, expected: bool) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    last_seen_at = now - timedelta(minutes=minutes_since_seen)
    assert is_idle(
        last_seen_at=last_seen_at, now=now, idle_threshold_minutes=threshold_minutes
    ) is (expected)


# ===========================================================================
# wake_showcase_machines
# ===========================================================================


async def test_wake_starts_only_stopped_producer_and_worker_machines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeFlyClient(
        {
            "producer": {"p-started": True, "p-stopped": False},
            "worker": {"w-started": True, "w-stopped": False},
        }
    )
    monkeypatch.setattr(idle_watchdog, "_client", lambda settings, http_client: fake)

    await wake_showcase_machines(_settings())

    assert sorted(fake.started) == ["p-stopped", "w-stopped"]
    assert fake.stopped == []


async def test_wake_is_noop_without_fly_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeFlyClient({"producer": {"p-stopped": False}})
    monkeypatch.setattr(idle_watchdog, "_client", lambda settings, http_client: fake)

    await wake_showcase_machines(_settings(fly_api_token=""))

    assert fake.started == []


# ===========================================================================
# _stop_if_idle
# ===========================================================================


async def test_stop_if_idle_stops_started_machines_past_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        idle_watchdog, "_last_visitor_seen", lambda: datetime.now(UTC) - timedelta(minutes=15)
    )
    fake = _FakeFlyClient(
        {
            "producer": {"p-started": True},
            "worker": {"w-started": True},
        }
    )

    await _stop_if_idle(_settings(), fake)  # type: ignore[arg-type]

    assert sorted(fake.stopped) == ["p-started", "w-started"]


async def test_stop_if_idle_noop_when_recently_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        idle_watchdog, "_last_visitor_seen", lambda: datetime.now(UTC) - timedelta(minutes=1)
    )
    fake = _FakeFlyClient({"producer": {"p-started": True}, "worker": {"w-started": True}})

    await _stop_if_idle(_settings(), fake)  # type: ignore[arg-type]

    assert fake.stopped == []


async def test_stop_if_idle_noop_when_no_visitor_ever_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(idle_watchdog, "_last_visitor_seen", lambda: None)
    fake = _FakeFlyClient({"producer": {"p-started": True}})

    await _stop_if_idle(_settings(), fake)  # type: ignore[arg-type]

    assert fake.stopped == []
