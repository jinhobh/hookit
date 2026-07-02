"""Realistic GitHub/CI event payloads for the dashboard demo.

Pure and deterministic given an injected ``random.Random`` — no I/O, no clock
reads baked into the shape — so it is trivially unit-testable. The dashboard's
"deploy pipeline" story emits three familiar GitHub webhook event types
(``push``, ``pull_request``, ``workflow_run``); the payloads mirror the real
GitHub shapes closely enough to be recognizable without copying them verbatim.

These are the *event* payloads the platform ingests. The worker still wraps and
HMAC-signs them exactly like any other event — the demo runs the real pipeline.
"""

from __future__ import annotations

import random
from typing import Any

# The event types the demo endpoint subscribes to. Ordinary (non-reserved)
# strings, so fan-out matches them like any real event type.
DEMO_EVENT_TYPES: tuple[str, ...] = ("push", "pull_request", "workflow_run")

# Shared default source of randomness when the caller doesn't inject a seeded one.
_DEFAULT_RNG = random.Random()

_REPOS: tuple[tuple[str, str], ...] = (
    ("acme", "checkout-service"),
    ("acme", "web-storefront"),
    ("acme", "billing-api"),
    ("acme", "notifications"),
    ("acme", "infra"),
)

_ACTORS: tuple[str, ...] = (
    "ava-thompson",
    "liam-nakamura",
    "sofia-reyes",
    "noah-okafor",
    "mia-petrov",
)

_BRANCHES: tuple[str, ...] = (
    "main",
    "release/2026.07",
    "feature/idempotency-keys",
    "fix/retry-jitter",
    "chore/bump-deps",
)

_COMMIT_MESSAGES: tuple[str, ...] = (
    "Fix flaky retry backoff test",
    "Add HMAC signature verification helper",
    "Tune worker batch size for throughput",
    "Handle 429 from downstream with jittered backoff",
    "Refactor delivery worker claim query",
    "Document dead-letter redrive flow",
)

_PR_TITLES: tuple[str, ...] = (
    "Add per-endpoint rate limiting",
    "Cursor pagination for GET /deliveries",
    "Encrypt endpoint secrets at rest",
    "LISTEN/NOTIFY worker wake-up",
    "SSRF guard for outbound delivery URLs",
)

_WORKFLOWS: tuple[str, ...] = ("CI", "Deploy", "Integration Tests", "Lint & Types")


def _sha(rng: random.Random) -> str:
    """A 40-hex-character commit SHA."""
    return "".join(rng.choice("0123456789abcdef") for _ in range(40))


def build_demo_event(
    event_type: str | None = None,
    rng: random.Random | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return ``(event_type, payload)`` for a realistic GitHub/CI event.

    *event_type* is chosen at random from :data:`DEMO_EVENT_TYPES` when omitted.
    Pass a seeded *rng* for deterministic output in tests.
    """
    rng = rng or _DEFAULT_RNG
    if event_type is None:
        event_type = rng.choice(DEMO_EVENT_TYPES)
    if event_type not in DEMO_EVENT_TYPES:
        raise ValueError(f"unknown demo event_type: {event_type!r}")

    owner, name = rng.choice(_REPOS)
    repository = {"id": rng.randint(10_000, 99_999), "name": name, "full_name": f"{owner}/{name}"}
    actor = rng.choice(_ACTORS)
    branch = rng.choice(_BRANCHES)

    if event_type == "push":
        after = _sha(rng)
        message = rng.choice(_COMMIT_MESSAGES)
        return event_type, {
            "ref": f"refs/heads/{branch}",
            "before": _sha(rng),
            "after": after,
            "repository": repository,
            "pusher": {"name": actor, "email": f"{actor}@users.noreply.github.com"},
            "head_commit": {
                "id": after,
                "message": message,
                "author": {"name": actor, "email": f"{actor}@example.com"},
            },
            "commit_count": rng.randint(1, 5),
        }

    if event_type == "pull_request":
        action = rng.choice(("opened", "synchronize", "closed"))
        merged = action == "closed" and rng.random() < 0.8
        return event_type, {
            "action": action,
            "number": rng.randint(100, 999),
            "repository": repository,
            "pull_request": {
                "title": rng.choice(_PR_TITLES),
                "state": "closed" if action == "closed" else "open",
                "merged": merged,
                "user": {"login": actor},
                "head": {"ref": branch},
                "base": {"ref": "main"},
            },
        }

    # workflow_run
    conclusion = rng.choices(("success", "failure"), weights=(4, 1))[0]
    return event_type, {
        "action": "completed",
        "repository": repository,
        "workflow_run": {
            "name": rng.choice(_WORKFLOWS),
            "status": "completed",
            "conclusion": conclusion,
            "head_branch": branch,
            "head_sha": _sha(rng),
            "run_number": rng.randint(200, 5000),
            "event": "push",
            "actor": {"login": actor},
        },
    }
