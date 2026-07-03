"""Tests for the Fly Machines API client using an in-memory transport.

No real network: ``httpx.MockTransport`` answers requests, mirroring
``tests/test_producer_client.py``'s pattern for the producer's HTTP clients.
"""

from __future__ import annotations

import httpx
from app.services.fly_machines import FlyMachinesClient

_MACHINES = [
    {"id": "m1", "state": "started", "config": {"metadata": {"fly_process_group": "producer"}}},
    {"id": "m2", "state": "stopped", "config": {"metadata": {"fly_process_group": "producer"}}},
    {"id": "m3", "state": "started", "config": {"metadata": {"fly_process_group": "worker"}}},
    {"id": "m4", "state": "started", "config": {"metadata": {"fly_process_group": "app"}}},
]


async def test_list_machine_ids_filters_by_group_and_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/apps/hookit/machines"
        assert request.headers.get("authorization") == "Bearer tok"
        return httpx.Response(200, json=_MACHINES)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = FlyMachinesClient(app_name="hookit", api_token="tok", client=http)
        assert await client.list_machine_ids(process_group="producer", started=True) == ["m1"]
        assert await client.list_machine_ids(process_group="producer", started=False) == ["m2"]
        assert await client.list_machine_ids(process_group="worker", started=True) == ["m3"]
        assert await client.list_machine_ids(process_group="app", started=True) == ["m4"]
        assert await client.list_machine_ids(process_group="app", started=False) == []


async def test_list_machine_ids_returns_empty_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = FlyMachinesClient(app_name="hookit", api_token="tok", client=http)
        assert await client.list_machine_ids(process_group="producer", started=True) == []


async def test_start_posts_to_machine_start_and_returns_true() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = FlyMachinesClient(app_name="hookit", api_token="tok", client=http)
        assert await client.start("m1") is True

    assert seen["path"] == "/v1/apps/hookit/machines/m1/start"
    assert seen["auth"] == "Bearer tok"


async def test_stop_posts_to_machine_stop_and_returns_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/apps/hookit/machines/m1/stop"
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = FlyMachinesClient(app_name="hookit", api_token="tok", client=http)
        assert await client.stop("m1") is True


async def test_start_and_stop_swallow_non_2xx_and_return_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "nope"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = FlyMachinesClient(app_name="hookit", api_token="tok", client=http)
        assert await client.start("m1") is False
        assert await client.stop("m1") is False
