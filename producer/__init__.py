"""Live crypto price producer — a *separate* real API client for the platform.

This package is a standalone service (run with ``python -m producer``). It polls
a real, keyless public crypto price API, turns each observation into a
``price.tick`` / ``price.alert`` event, and POSTs it to the platform's real
public ingestion API (``POST /events``) over HTTP with a real API key — exactly
like any external customer would. Nothing here reaches into the platform's
database or internals; it is a genuine outside producer, which is what makes the
showcase real rather than simulated.

See ``producer/README.md`` for how to run it.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
