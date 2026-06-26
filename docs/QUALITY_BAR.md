# Quality Bar

The engineering standard every PR is held to. The Reviewer agent checks against
this; the human owner uses it as a merge checklist. "Good enough to merge" means
**all** of the following are satisfied (or a deviation is explicitly justified in
the PR).

---

## Correctness
- The change does what the issue's acceptance criteria require — no more, no less.
- Edge cases are handled: empty input, duplicates, concurrent access, partial
  failure, and the unhappy paths the feature implies.
- No obvious logic errors, off-by-ones, or unhandled `None`/error branches.

## Tests
- New behavior has tests; bug fixes have a regression test that fails before and
  passes after.
- Tests are deterministic and isolated — no reliance on external networks or wall
  clock; outbound HTTP is mocked or pointed at a local receiver.
- Tests assert behavior, not implementation detail. Failing tests are never
  deleted or skipped to go green.

## Type safety
- `mypy app tests` passes under the strict config. Public functions are fully
  annotated. `type: ignore` is rare and justified with a reason.

## Error handling
- Failures are handled explicitly and surfaced with actionable messages; errors
  are never silently swallowed.
- External calls (DB, HTTP) have timeouts and defined failure behavior.
- User-facing errors use correct HTTP status codes and don't leak internals or
  secrets.

## Security
- No secrets in code, logs, or fixtures. Secrets come from config/env only.
- API keys and signing secrets are hashed/encrypted; comparisons of secrets/
  signatures are constant-time.
- Input is validated and bounded via Pydantic; SQL is parameterized via
  SQLAlchemy. Untrusted URLs are treated as SSRF risks.

## Database migration safety
- Every schema change is a reviewable Alembic migration with a working
  `downgrade` where feasible.
- Migrations are backward-compatible with the previous app version where
  practical (additive first; no destructive change without explicit note).
- No `create_all` in production paths; indexes exist for hot query patterns
  (e.g. claiming due deliveries).

## API design
- Resource-oriented, consistent naming, correct status codes, and stable response
  shapes. Pagination/filtering for list endpoints. Documented request/response
  models.

## Observability
- Meaningful structured logs with correlation ids (event/delivery/attempt), no
  secrets. Every network delivery attempt is recorded as an inspectable row.

## Simplicity
- The smallest solution that fully solves the issue. No speculative abstraction,
  no unused config, no gold-plating. Prefer clarity over cleverness.

## Documentation
- Docstrings on non-trivial modules/functions explaining *why*. Relevant docs
  (`README`, `ARCHITECTURE`, `ROADMAP`) updated when behavior or structure
  changes. The PR body explains the change and how it was verified.

## Reviewability
- The diff is focused on one issue — no unrelated refactors or churn. Commits are
  logical and well-messaged. The PR is small enough to review carefully; if it
  isn't, it should have been split.
