"""Thin client for the Fly.io Machines API.

Used only by the idle watchdog (:mod:`app.services.idle_watchdog`) to start and
stop the `producer`/`worker` process-group machines. Kept thin and typed so it
can be exercised against an ``httpx.MockTransport`` in tests without a live Fly
app, mirroring :mod:`producer.client`.

Every call is best-effort: a non-2xx response or transport error is logged and
swallowed, never raised. This is a cost-control convenience, not part of the
webhook-delivery correctness path — it must never be able to take the app down.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.machines.dev/v1"


class FlyMachinesClient:
    """Lists, starts, and stops machines for one Fly app by process group."""

    def __init__(self, *, app_name: str, api_token: str, client: httpx.AsyncClient) -> None:
        self._app_name = app_name
        self._headers = {"Authorization": f"Bearer {api_token}"}
        self._client = client

    async def list_machine_ids(self, *, process_group: str, started: bool) -> list[str]:
        """Return machine ids in *process_group* currently started (or not).

        The Machines API has no server-side process-group filter, so this
        fetches the app's full machine list and filters client-side — cheap,
        since a demo app like this only ever has a handful of machines.
        Returns an empty list (rather than raising) on any failure.
        """
        try:
            resp = await self._client.get(
                f"{_API_BASE}/apps/{self._app_name}/machines", headers=self._headers
            )
            resp.raise_for_status()
            machines: list[dict[str, Any]] = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("fly machines: list failed for %s: %s", self._app_name, exc)
            return []

        matches = []
        for machine in machines:
            metadata = machine.get("config", {}).get("metadata", {})
            if metadata.get("fly_process_group") != process_group:
                continue
            is_started = machine.get("state") == "started"
            if is_started == started:
                matches.append(machine["id"])
        return matches

    async def start(self, machine_id: str) -> bool:
        """Start one machine. Returns whether the call succeeded."""
        return await self._post(f"machines/{machine_id}/start")

    async def stop(self, machine_id: str) -> bool:
        """Stop one machine. Returns whether the call succeeded."""
        return await self._post(f"machines/{machine_id}/stop")

    async def _post(self, path: str) -> bool:
        try:
            resp = await self._client.post(
                f"{_API_BASE}/apps/{self._app_name}/{path}", headers=self._headers
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("fly machines: %s failed: %s", path, exc)
            return False
        return True
