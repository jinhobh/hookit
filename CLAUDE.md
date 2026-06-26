# CLAUDE.md — Constitution for Claude Agents

This file is the **authoritative operating contract** for every Claude agent
(Planner, Builder, Reviewer) and for any interactive Claude Code session working
in this repository. If any instruction elsewhere conflicts with this file, this
file wins unless a human owner overrides it explicitly.

---

## 1. Project summary

The **Reliable Webhook Delivery Platform** is a production-style backend service
that ingests events from authenticated API clients and delivers them
asynchronously to registered webhook endpoints — reliably. It demonstrates
backend fundamentals: async job processing, database modeling, idempotency,
failure handling (retries, backoff, dead-lettering, redrive), HMAC signing,
testing, and production-style engineering judgment.

It is **not** a CRUD demo. See `docs/PROJECT_SPEC.md` for the full product
definition and `docs/ROADMAP.md` for the phased build plan.

Current status: **project skeleton only** (FastAPI health endpoint + agent OS).

## 2. Tech stack

- Python 3.12
- FastAPI (HTTP API)
- PostgreSQL (persistence and, initially, the delivery job queue)
- SQLAlchemy 2.x (ORM / Core)
- Alembic (migrations)
- Pydantic Settings (config)
- httpx (outbound delivery + test client)
- pytest, ruff, mypy (quality gates)
- Docker Compose (local infra)
- GitHub Actions (CI + agent automation)

Do not introduce other frameworks, brokers (Redis/Kafka/SQS), or languages unless
an issue explicitly authorizes it. The MVP uses Postgres-backed persistence and a
simple worker before any external queue is considered.

## 3. Required commands

Every Builder change and every Reviewer check must run these, in order. They are
also the CI gates — CI must mirror them exactly.

```bash
ruff format --check .     # formatting must be clean
ruff check .              # lint must pass
mypy app tests            # static types must pass
pytest                    # tests must pass
```

Local setup:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

If a command **cannot be run** (missing tool, environment failure, network
restriction), the agent MUST say so explicitly and clearly in its PR body or
review comment — state which command failed and why. Never claim a check passed
when it did not run.

## 4. Branch naming rules

Branches are `type/short-kebab-summary`, optionally suffixed with the issue
number. Use the issue's `type:*` label as the prefix:

- `setup/...`, `feature/...`, `bug/...`, `refactor/...`, `docs/...`, `test/...`

Examples: `feature/event-ingestion-12`, `bug/retry-backoff-overflow-41`.

Never work directly on `main`.

## 5. Commit message rules

Use Conventional Commits: `type(scope): summary` in the imperative mood.

- Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `ci`.
- Keep the subject ≤ 72 chars; add a body explaining *why* when non-trivial.
- Reference the issue in the body (`Refs #<n>`), not just the subject.
- Make focused commits; do not bundle unrelated changes.
- Co-author trailer for agent commits:
  `Co-Authored-By: Claude <noreply@anthropic.com>`

## 6. PR body requirements

Every PR opened by an agent MUST include these sections:

1. **Summary** — what changed and why, in 2–4 sentences.
2. **Linked issue** — `Closes #<n>` (or `Refs #<n>` if it only partially
   addresses the issue).
3. **Acceptance criteria** — copied from the issue, each checked or explained.
4. **Verification** — the exact commands run and their result (pass/fail). If a
   command could not run, say so.
5. **Scope** — confirmation the change is limited to the issue; note anything
   intentionally left out.
6. **Risks / follow-ups** — known limitations and suggested next issues.

## 7. Testing expectations

- New behavior ships with tests. Bug fixes ship with a regression test.
- Tests must be deterministic and not depend on network access to third
  parties. Mock outbound HTTP (use a local receiver / httpx mock).
- Prefer fast unit/integration tests; use the Postgres service for DB-touching
  tests rather than mocking the database away.
- Never delete or skip a failing test to make CI green. Fix the cause.
- Maintain or improve coverage of the code you touch.

## 8. Architecture rules

- Keep the layering clean: API router → service/domain logic → persistence.
  HTTP concerns stay in routers; business rules do not leak into them.
- All configuration goes through `app/core/config.py` (Pydantic Settings). No
  `os.getenv` scattered through the code; no hardcoded secrets or URLs.
- Database schema changes are made **only** through Alembic migrations. Never
  edit the database out-of-band or rely on `create_all` in production paths.
- Idempotency, retry, and signing logic must be pure and unit-testable, isolated
  from I/O where practical.
- Keep modules small and cohesive. See `docs/ARCHITECTURE.md` before adding
  structure.

## 9. Security rules

- Never commit secrets. Secrets come from environment variables only; the GitHub
  Actions secret is `ANTHROPIC_API_KEY`.
- API keys and webhook signing secrets must be stored hashed/encrypted, never in
  plaintext logs. Never log full secrets, tokens, or signatures.
- Sign outbound webhooks with HMAC-SHA256; verify signatures with a constant-time
  comparison. Include a timestamp to mitigate replay.
- Validate and constrain all inbound input via Pydantic. Use parameterized
  queries (SQLAlchemy) — never string-build SQL.
- Treat webhook target URLs as untrusted (SSRF risk); document and, where an
  issue requires, enforce egress constraints.

## 10. Agent role boundaries

Authoritative role prompts live in `agents/`. Summary:

- **Planner** (`agents/planner.md`): reads spec/roadmap/issues, creates small,
  non-duplicate issues with acceptance criteria. **Writes no code.**
- **Builder** (`agents/builder.md`): takes exactly one `agent:ready` issue,
  implements the smallest complete solution on a branch, runs checks, opens one
  PR, links the issue, labels `agent:needs-review`. **Does not merge or push to
  main.**
- **Reviewer** (`agents/reviewer.md`): review-only on PRs. Comments specific
  feedback and labels `agent:approved` or `agent:changes-needed`. **Edits no
  files, makes no commits, does not merge.**
- **Human owner**: the only actor who approves and merges PRs (enforced via
  `.github/CODEOWNERS` + branch protection).

See `docs/AGENT_WORKFLOW.md` for the full lifecycle.

## 11. Prohibited actions

These apply to all agents, always:

- Never push to `main`.
- Never force push.
- Never merge PRs.
- Never approve your own PR.
- Never change, add, or remove secrets.
- Never weaken CI (don't remove/disable checks or make them non-blocking).
- Never weaken branch protection.
- Never change GitHub Actions permissions unless the issue explicitly asks for
  workflow/CI setup.
- Never add dependencies without explicit justification in the PR body.
- Never perform broad, unrelated refactors outside the issue's scope.
- Never hide, silence, or work around failing tests.
- Never delete tests to make CI pass.

When in doubt, stop and leave a comment asking for human guidance rather than
guessing.
