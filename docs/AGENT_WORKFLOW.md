# Agent Workflow

How the **Planner**, **Builder**, **Reviewer**, **CI**, and **Human Owner**
interact to build the product autonomously from a GitHub issue queue. The
governing rules are in [`CLAUDE.md`](../CLAUDE.md); role prompts are in
[`agents/`](../agents).

---

## Actors at a glance

| Actor | Trigger | Writes code? | Merges? | Output |
| --- | --- | --- | --- | --- |
| Planner | daily cron (queue refill) + Builder-dispatch when empty + manual | No | No | New `agent:ready` issues |
| Builder | push to main + Reviewer-dispatch (on changes-needed) + manual | Yes (one PR) | No | Branch + PR, `agent:needs-review` |
| Reviewer | PR events + manual | No | No | PR comment + `agent:approved`/`agent:changes-needed` |
| CI | every PR / push to main | No | No | Pass/fail status checks |
| Auto-merge | `agent:approved` added + manual | No | **Yes** (deterministic) | Squash-merges green, approved PRs |
| Human owner | as needed | Optionally | Optionally | Provides secrets; can intervene anytime |

> **Autonomous (auto-merge) mode is enabled.** The loop is self-advancing and
> needs no human merge. The `Builder` (`agents/builder.md`) and `Reviewer`
> (`agents/reviewer.md`) **agents** still never merge — only the deterministic
> `auto-merge.yml` workflow does, and only when CI is green *and* the Reviewer
> approved. To restore a human merge gate, delete `auto-merge.yml` (or add
> branch protection requiring CODEOWNERS review).

### The self-advancing loop

```
merge to main ─▶ Builder (push:main) ─▶ opens 1 PR ─▶ CI + Reviewer run
      ▲                                                        │
      │                                  ┌─── approved + CI green
      │                                  │                     │
      │                                  ▼                     ▼
      └──── Auto-merge (squash) ◀── approved          changes-needed
                                                              │
                                          Reviewer dispatches Builder to fix
                                          the same PR ─▶ re-review ─▶ …
```

The loop is **event-driven (hybrid model)**: there is no idle Builder cron.
Merges, approvals, and change-requests each dispatch the next step; the only
time-based trigger is a once-daily Planner that tops up the queue (the Builder
also dispatches the Planner immediately if it ever finds the queue empty).

Each step's GitHub action is performed with a **dedicated PAT (`AGENT_GH_TOKEN`)**,
not the default `GITHUB_TOKEN`. This is essential: GitHub suppresses workflow
triggers for events made by `GITHUB_TOKEN`, so without the PAT a Builder-opened
PR would never start CI/Reviewer and the loop would stall. The PAT is a
fine-grained token scoped to this repo with **Contents, Pull requests, Issues,
and Actions** all set to *Read and write* (Actions is needed for the
`gh workflow run` dispatches). Only **one PR is open at a time** (the Builder
exits early if work is in flight), so dependent issues land in order.

## Labels and their meaning

**Lifecycle (`agent:*`)**
- `agent:ready` — specified and ready for a Builder to pick up.
- `agent:in-progress` — a Builder has claimed it (prevents double-pickup).
- `agent:needs-review` — a PR is open and awaiting Reviewer.
- `agent:changes-needed` — Reviewer requested changes; back to the Builder.
- `agent:approved` — Reviewer approved; awaiting human merge.

**Risk (`risk:*`)** — `risk:low` / `risk:medium` / `risk:high`. Set by Planner;
raises human scrutiny. Anything touching auth, migrations, CI, or security is at
least `risk:medium`.

**Type (`type:*`)** — `type:setup`, `type:feature`, `type:bug`, `type:refactor`,
`type:docs`, `type:test`. Drives the branch prefix and commit type.

## Issue lifecycle

```
(Planner creates)            (Builder picks)          (Builder opens PR)
   agent:ready  ───────▶  agent:in-progress  ───────▶  agent:needs-review
       ▲                                                      │
       │                                              (Reviewer decides)
       │                                        ┌─────────────┴─────────────┐
       │                                        ▼                           ▼
       └──────────  agent:changes-needed  ◀── changes needed          agent:approved
                                                                            │
                                                                  (Human merges PR;
                                                                   issue auto-closes)
```

1. **Planner** runs (cron/manual). If fewer than **5** open issues are labeled
   `agent:ready`, it reads the spec + roadmap + open issues and creates up to
   **3** new small, non-duplicate issues, each with Goal, Context, Acceptance
   criteria, Implementation notes, Verification commands, and labels. It writes
   no code.
2. **Builder** runs. It picks the **oldest** issue labeled `agent:ready` and not
   `agent:in-progress`, labels it `agent:in-progress`, and works that one issue.

## PR lifecycle

1. Builder creates a branch (`type/summary-<issue#>`), implements the smallest
   complete solution, and runs all four checks locally
   (`ruff format --check .`, `ruff check .`, `mypy app tests`, `pytest`).
2. Builder commits (Conventional Commits), pushes the branch, and opens **one**
   PR that `Closes #<issue>` and includes the required PR body sections
   (see `CLAUDE.md` §6). It labels the PR `agent:needs-review`. It never merges
   and never pushes to `main`.
3. **CI** runs the four checks on the PR.
4. **Reviewer** runs (on PR open/synchronize/reopen/ready, or manual). It is
   review-only: it assesses correctness, tests, scope, security, reliability, and
   maintainability, leaves a specific PR comment, and applies exactly one of
   `agent:approved` or `agent:changes-needed`. It edits nothing.
5. **Human owner** reviews approved + green PRs and merges. CODEOWNERS approval is
   required, so the human is always the final gate.

## What happens when CI fails

- The PR is not auto-merged (auto-merge requires the CI check to be green).
- The Reviewer treats failing CI as an automatic `agent:changes-needed` and
  points at the failing check.
- This dispatches the Builder (see below), which fixes the root cause and pushes
  to the **same branch** (re-triggering CI and the Reviewer). Builders must fix
  the cause — never weaken or skip checks.

## What happens when the Reviewer requests changes

- The Reviewer labels the PR `agent:changes-needed` (removing `agent:approved`)
  and **dispatches the Builder** (`gh workflow run agent-builder.yml`).
- The Builder sees the `agent:changes-needed` PR is its top priority: it checks
  out that branch, addresses **every** blocking point in new commits, re-runs the
  four checks, pushes to the same branch, updates the PR's Verification section,
  then swaps the label back to `agent:needs-review`.
- Pushing to the PR branch re-triggers the Reviewer. The cycle repeats until the
  Reviewer approves (→ auto-merge) — no human or cron needed.

## What happens when an agent is blocked

If an agent cannot proceed (ambiguous requirements, missing access, a check that
cannot run, or a decision that needs a human):

- It must **stop and surface the blocker** — a comment on the issue/PR describing
  exactly what is blocking and what it needs — rather than guess, hack around it,
  or weaken safeguards.
- The Builder leaves the issue labeled so a human can intervene, and does **not**
  open a half-broken PR claiming success.

## What the human should approve

- Whether a Reviewer-approved, CI-green PR should merge into `main`.
- Anything labeled `risk:high`, and any change touching auth, security,
  migrations, dependencies, CI, or workflow permissions — even if agents approved
  it.

## What the human should NOT need to do

- Write the issues (Planner does).
- Implement features or open PRs (Builder does).
- Perform first-pass review (Reviewer does).
- Re-run or babysit checks (CI does).

The human's steady-state job is to **review and merge** good PRs. One-time setup
the human must do (secrets, branch protection, app install) is listed in the
README and the setup PR.
