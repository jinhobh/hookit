"""Pydantic schemas for the live simulation ("Simulate load") endpoint."""

from __future__ import annotations

import uuid

from pydantic import BaseModel


class SimulateRunResponse(BaseModel):
    """Returned by POST /simulate/run."""

    endpoint_id: uuid.UUID
    queued_events: int
    queued_deliveries: int
    dead_lettered_delivery_id: uuid.UUID | None = None
