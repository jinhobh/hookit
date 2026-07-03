"""Idle watchdog: scales `producer`/`worker` Fly machines to zero when unwatched.

The showcase dashboard's `producer` service polls Coinbase and posts events to
the platform's own public URL every few seconds — self-generated traffic that
would otherwise defeat Fly's built-in idle-based autostop for the `app` machine
too. This module tracks real dashboard visits (recorded via
``app.services.showcase.touch_visitor_seen``, called only from the dashboard's
own GET routes — never from producer traffic) and stops `producer`/`worker`
once nobody has been watching for a while. Once `producer` is stopped, it stops
generating self-traffic, so `app`'s own existing idle-autostop starts working
too — no separate handling needed for `app`.

Fully inert (both the wake call and the watchdog loop are no-ops) unless
``settings.fly_api_token`` and ``settings.fly_app_name`` are both set, which is
true only in the real Fly deployment — never in local dev or tests.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx

from app.core.config import Settings
from app.db.session import SessionLocal
from app.services.fly_machines import FlyMachinesClient
from app.services.showcase import get_last_visitor_seen, resolve_showcase

logger = logging.getLogger(__name__)

# Process groups (fly.toml [processes] keys) the watchdog manages. `app` is
# deliberately excluded — Fly's own http_service autostop already covers it.
_PROCESS_GROUPS = ("producer", "worker")


def is_idle(*, last_seen_at: datetime, now: datetime, idle_threshold_minutes: float) -> bool:
    """True once *now* is at least *idle_threshold_minutes* past *last_seen_at*."""
    return now - last_seen_at >= timedelta(minutes=idle_threshold_minutes)


async def wake_showcase_machines(settings: Settings) -> None:
    """Best-effort: start any producer/worker machines that aren't already running.

    Called once at app startup. `app` itself only boots on a real inbound
    request (once producer's self-traffic no longer keeps it artificially
    alive), so an `app` boot is a reliable "a real visitor just showed up"
    signal — this is what brings producer/worker back for them.
    """
    if not (settings.fly_api_token and settings.fly_app_name):
        return
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client = _client(settings, http_client)
        for group in _PROCESS_GROUPS:
            for machine_id in await client.list_machine_ids(process_group=group, started=False):
                if await client.start(machine_id):
                    logger.info("idle watchdog: started %s machine %s", group, machine_id)


async def run_idle_watchdog(settings: Settings) -> None:
    """Forever: sleep, then stop producer/worker if no real visitor recently.

    Each tick is independently best-effort — a DB or Fly API hiccup is logged
    and the loop keeps going, matching the "a transient hiccup never stops the
    stream" resilience style already used by the producer's own poll loop.
    """
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client = _client(settings, http_client)
        while True:
            await asyncio.sleep(settings.idle_watchdog_interval_seconds)
            try:
                await _stop_if_idle(settings, client)
            except Exception:
                logger.exception("idle watchdog: tick failed")


def _client(settings: Settings, http_client: httpx.AsyncClient) -> FlyMachinesClient:
    return FlyMachinesClient(
        app_name=settings.fly_app_name, api_token=settings.fly_api_token, client=http_client
    )


async def _stop_if_idle(settings: Settings, client: FlyMachinesClient) -> None:
    last_seen = _last_visitor_seen()
    # No visitor recorded yet (e.g. a fresh deploy nobody has visited): stay up
    # rather than guess — the watchdog only ever acts on a confirmed visit.
    if last_seen is None:
        return
    if not is_idle(
        last_seen_at=last_seen,
        now=datetime.now(UTC),
        idle_threshold_minutes=settings.visitor_idle_minutes,
    ):
        return
    for group in _PROCESS_GROUPS:
        for machine_id in await client.list_machine_ids(process_group=group, started=True):
            if await client.stop(machine_id):
                logger.info("idle watchdog: stopped %s machine %s", group, machine_id)


def _last_visitor_seen() -> datetime | None:
    with SessionLocal() as session:
        handles = resolve_showcase(session)
        if handles is None:
            return None
        return get_last_visitor_seen(session, handles.project_id)
