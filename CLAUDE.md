# Open-Skej: Architecture

This file and `.claude/rules/*.md` describe **what the system is and why**. They are not a plan and
carry no task list, status, or schedule — that lives in `ops/` (see *Where plan state lives*). If you
are looking for what to do next, you are in the wrong file.

## What it is

Open-Skej books time on shared resources — a tennis court, a piece of expensive equipment. The
differentiator is **AI-driven rule configuration**: booking constraints are authored in natural
language ("only 1 hour sessions", "no more than twice a week") and stored as parameterized Python
snippets that the rule engine executes.

Rule evaluation is bounded to **at most one calendar month of history**. That bound is a design
constraint, not a tuning knob: it caps the work any single booking attempt can cause, so a rule
cannot degrade the whole system by asking a broader question than the engine promised to answer.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | React + Vite, TailwindCSS, calendar library | — |
| Backend | Python, FastAPI | Runs the Python rule engine in-process — no cross-language boundary between the API and rule evaluation |
| Database | PostgreSQL | Partial indexes and `EXCLUDE USING gist` are load-bearing in the schema; both are Postgres-specific and deliberately so |
| Identity | Auth0 (free tier) | Proves *identity* only. Authorization is ours — see below |

## System map

```
app/backend/app/
  auth/          JWT verification (Auth0 JWKS, RS256) and the current-user dependency
  identity/      Users, Spaces, memberships, access requests, invitations
                   authz.py — require_space_role, the per-Space authorization dependency
  db/            Declarative Base, session, UtcDateTime, driver abstraction
  routers/       Booking endpoints
  rules_stub.py  Placeholder engine; replaced by `rules/` at integration
app/frontend/    React SPA
app/e2e/         Playwright suite driving the real backend, not a mock
rules/rules/
  interfaces.py  The rule contract — authoritative, read before writing any rule
  controller.py  evaluate_request(): fail-fast canon execution and error containment
```

## Cross-cutting invariants

These hold everywhere and are not any one component's private business. Breaking one is a change to
the architecture, not a local implementation choice.

**UTC everywhere.** Every datetime crossing a module boundary is timezone-aware with a **zero** UTC
offset. Naive datetimes and non-zero offsets are rejected at construction (`UtcDateTime` in the
schema, the interface dataclasses in the engine). Timezone is a UI presentation concern and no
backend entity carries one. This is not pedantry: rules read `.hour` to enforce opening windows, so
a `+02:00` value would yield a *local* hour and silently mis-enforce them. Convert at the boundary.

**Fail closed.** Any failure to positively establish that a booking is permitted results in **no
booking**. See `.claude/rules/rule-engine.md` for the three containment paths.

**The link is the capability.** A Space is reachable only by its unguessable `public_id`. There is no
listing endpoint, and a caller outside a Space gets **404, never 403** — a 403 would confirm the id
exists and turn every capability URL into an oracle. The integer primary key is never exposed.

**Nothing is deleted.** Spaces archive (`archived_at`); access requests and invitations retain their
decided rows as history. Consequently no foreign key carries `ON DELETE CASCADE` — there is no delete
to cascade, and one added later would quietly destroy the audit trail.

**Code outranks prose.** Where these documents and the code disagree, the code is correct and the
document is stale. Fix the document.

## Component ownership

Work proceeds in vertical slices that own disjoint directories, so parallel agents never collide.
Ownership is architectural — it says who may change what — and outlives whatever task is in flight.

* **Booking core** — calendar UI and the booking flow. Shipped against a stubbed engine and a local
  SQLite abstraction. Closed; see `ops/archive/stream-1/SUMMARY.md`.
* **Identity & access** (`.claude/rules/identity-and-access.md`) — the production schema,
  multi-tenant Space management, and Auth0 integration. Owns `app/backend/app/{auth,identity,db}/`
  and `alembic/`.
* **Rule engine** (`.claude/rules/rule-engine.md`) — owns `rules/` entirely: the isolated execution
  environment, the rule contract, and the AI generation loop.

Three files are unavoidably shared. `app/backend/requirements.txt` is **append-only**.
`app/backend/app/main.py` takes **additive lines only** (`include_router`, exception handlers).
`app/frontend/*` belongs to whichever slice is actively building UI — check before editing.

## Keeping the architecture live

**These documents are part of the deliverable, not commentary on it.** An agent working a task
updates them *in the same PR as the code*, never as a follow-up — a doc fixed later is a doc that
describes a system nobody is running anymore.

Update the architecture when a task:

* establishes or changes an **invariant**, a contract, or an interface shape;
* makes a **decision with a rationale** worth not re-litigating (why 404 and not 403, why this
  index is partial, why this model is the default);
* **contradicts** something written here — including a decision reversed after the doc was written;
* moves ownership of a directory, or adds a component to the system map.

Do **not** update it for: task status, PR numbers, what is coming next, or anything true only until
the next merge. That is plan state and belongs in `ops/plans/stream-N-plan.md`.

**Write in the present indicative.** "Spaces are not discoverable", not "Stream 2 will make Spaces
non-discoverable" or "Task 2.5 made Spaces non-discoverable". If a sentence needs a task number, a
PR link, or a future tense to make sense, it is plan text and does not belong here. The reader is an
agent six months from now who has no idea what task 2.5 was and cannot look it up.

**Record the reversal, not the history.** When a decision changes, rewrite the claim and state the
current rationale. Do not append "previously we did X" — an architecture doc is a description of the
present, and a changelog embedded in it is read as a live description of a system that no longer
exists. Git holds the history.

**Prune.** When a slice closes, move its rules doc to `ops/archive/stream-N/` and lift any invariant
it established that still binds other components into the invariants section above. A rules doc left
behind after its component is closed costs every future agent context for policy nobody can act on.

## Where plan state lives — `ops/`

**Plans are not in this repo.** They live in the private `skej-ops` repo, reached from every checkout
through a gitignored `ops` symlink:

| What | Path |
|---|---|
| Active plans | `ops/plans/stream-N-plan.md` |
| Deferred / out-of-scope features | `ops/DEFERRED.md` |
| Live obligations from closed slices | `ops/plans/integration-carry-forward.md` |
| Completed slices | `ops/archive/stream-N/` |
| Harness scripts | `ops/scripts/` |
| Git & GitHub access setup | `ops/git-access.md` |

This split exists so parallel agents share plan state **live** — an edit is visible to every other
agent immediately, with no commit/PR/merge/pull round-trip. Never copy a plan into this repo, and
never commit one here.

If `ops/` is missing from your working directory, stop and report it rather than reconstructing a
plan — the symlink is per-worktree and needs `ln -sfn ../skej-ops ops`.

**Why the two halves live apart:** a plan changes on *every task*, so keeping it outside git avoids a
commit/merge round-trip per checkbox. An architecture doc changes per decision, not per task, so the
git ceremony is cheap and the review is worth having. Architecture docs additionally must stay under
`.claude/`, which is auto-loaded into every agent session — moving one to `ops/` would silently stop
it loading, and a spec that fails to load fails *quietly*: the agent simply never learns the policy.

**Naming follows the same split.** Plans are named for the slice that executes them
(`stream-N-plan.md`) because a slice is a unit of scheduling. Architecture docs are named for the
**domain they describe** (`identity-and-access.md`, `rule-engine.md`) because the identity model
outlives the slice that happened to build it. A doc named for its stream is one closed stream away
from looking obsolete when it is still the live description of a running component.

Before planning or building anything, check `ops/DEFERRED.md`. If a feature is listed there, or would
be subsumed by something listed there, note it and move on. When the user calls something a good idea
for later, add it there.

## Worktrees

Each slice works in its own worktree on its own long-lived branch:

```
~/nirdev/skej/           main checkout    (orchestration, review, merges)
~/nirdev/skej-stream2/   stream-2/base
~/nirdev/skej-stream3/   stream-3/base
~/nirdev/skej-ops/       plans + scripts  (separate private repo)
```

Task branches are cut **inside** the slice's worktree and each merges to `main` via its own reviewed
PR. Create a worktree with `ops/scripts/new_worktree.sh N` — never `git worktree add` by hand, or the
`ops` symlink will be missing.

## Git & GitHub access

This machine has more than one GitHub account and **only one can push to this repo.** Two separate
things must both point at the repo owner: the **git remote** (via an SSH host alias) and the **`gh`
active account** (global, machine-wide, and easily left wrong). If a `git push` or a `gh` write
(`pr create`, `pr merge`, `api -X POST`) fails with a permissions or collaborator error, read
`ops/git-access.md` before retrying — do not "fix" it by rewriting the remote to a plain `github.com`
URL, which silently breaks pushing.

## Autonomous agent protocol (Planner–Doer–Reviewer)

When acting as Lead Architect (Opus), follow this loop without asking for manual intervention:

1. **State check.** Read `ops/plans/stream-N-plan.md` for the next pending task.
2. **Delegate.** Spawn a Sonnet sub-agent headless via Bash, passing `--allowedTools` so it is not
   blocked on permissions.
   * **Never unset or override `CLAUDE_CONFIG_DIR`.** The harness exports it so the whole agent tree
     runs under one config; sub-agents inherit it. If it is unset, stop and report — the fallback
     config bills and logs against a different account.
   * Example: `claude -p "Complete Task 3.3 from ops/plans/stream-3-plan.md. Write the code, update
     the architecture docs if the task changed an invariant or interface, commit, and open a PR."
     --allowedTools "Read,Edit,Bash"`
3. **Review.** The Bash call blocks until Sonnet finishes; then review the PR with `gh`. Check that
   any architectural change in the diff is reflected in `CLAUDE.md` or the relevant rules doc — an
   interface change shipped with a stale doc is an incomplete task, not a nit.
4. **Iterate or record.** If flawed, comment on the PR and re-run the sub-agent against that
   feedback. If approved, merge (no approval flow needed) and mark the task `[x]` in the plan.
5. **Proceed** to the next pending task.

Continue until a hard permission blocker, or until 2–3 product-facing questions have accumulated.
Collect open questions and keep working other tasks rather than blocking the whole flow.

If no plan file exists you may be the first planner: write the plan and wait for approval before
starting the implementation loop.
