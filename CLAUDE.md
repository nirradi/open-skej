# Open-Skej: Global Project Context

## Project Overview
Open-Skej is a scheduling application for booking time on shared resources (e.g., a shared tennis court or expensive physical equipment). The core differentiator is its AI-driven rule configuration. The rule engine enforces constraints saved as parameterized Python snippets (e.g., "only 1 hour sessions", "no booking more than twice a week"). The rule scope is bounded to a maximum of one month to keep computational overhead light.

## Architecture & Tech Stack
* **Frontend:** React (Next.js or Vite), TailwindCSS, Calendar Library.
* **Backend:** Python (FastAPI). Chosen to natively run the Python-based rule engine without cross-language overhead.
* **Database:** PostgreSQL (Stream 2 manages production schema, Stream 1 uses SQLite locally).
* **Authentication:** Auth0 (Free Tier).

## Development Strategy
Work is divided into vertical, feature-based slices (Streams) that operate independently before a final integration phase. 

* **Stream 1 (Core E2E Booking):** ✅ **COMPLETE / ARCHIVED.** Built the full-stack calendar UI and booking flow using a stubbed rule engine and a local SQLite abstraction. See `ops/archive/stream-1/SUMMARY.md` — do not reopen or replan it.
* **Stream 2 (Auth, Access & Admin):** Owns the real database schema, multi-tenant Space management, and Auth0 integration.
* **Stream 3 (Rule Engine Phase 1):** Develops the pure Python isolated execution environment and the initial hardcoded canon of rules.

## Where orchestration state lives — `ops/`

**Plans are not in this repo.** They live in the private `skej-ops` repo, reached from every
checkout through a gitignored `ops` symlink:

| What | Path |
|---|---|
| Active plans | `ops/plans/stream-N-plan.md` |
| Deferred / out-of-scope features | `ops/DEFERRED.md` |
| Live obligations from closed streams | `ops/plans/integration-carry-forward.md` |
| Completed streams | `ops/archive/stream-N/` |
| Harness scripts | `ops/scripts/` |
| Git & GitHub access setup | `ops/git-access.md` |

This exists so parallel agents share plan state **live** — an edit is visible to every other agent
immediately, with no commit/PR/merge/pull round-trip. Never copy a plan into this repo, and never
commit one here.

If `ops/` is missing from your working directory, stop and report it rather than recreating a plan
from scratch — the symlink is per-worktree and simply needs `ln -sfn ../skej-ops ops`.

### What goes where when you add a stream

Two files, two homes — this split is deliberate:

* **`ops/plans/stream-N-plan.md`** — the task breakdown. Changes on *every task*, so it lives
  outside git to avoid a commit/merge round-trip per checkbox.
* **`.claude/rules/stream-N-<topic>.md`** — the stream's objective, boundaries, and policy. Stays
  **in this repo** because files under `.claude/` are auto-loaded into every agent session. Moving
  one to `ops/` would silently stop it loading, and a stream spec that fails to load fails
  *quietly* — the agent just never learns the policy. Rules change per policy revision, not per
  task, so the git ceremony is cheap.

### Git & GitHub access

This machine has more than one GitHub account and **only one can push to this repo.** Two separate
things must both point at the repo owner: the **git remote** (via an SSH host alias) and the **`gh`
active account** (global, machine-wide, and easily left wrong). If a `git push` or a `gh` write
operation (`pr create`, `pr merge`, `api -X POST`) fails with a permissions or collaborator error,
read `ops/git-access.md` before retrying — do not "fix" it by rewriting the remote to a plain
`github.com` URL, which silently breaks pushing.

## Worktrees — one per stream

Each stream works in its own worktree on its own long-lived branch, so streams never collide in the
working tree:

```
~/nirdev/skej/           main checkout    (orchestration, review, merges)
~/nirdev/skej-stream2/   stream-2/base
~/nirdev/skej-stream3/   stream-3/base
~/nirdev/skej-ops/       plans + scripts  (separate private repo)
```

Task branches are cut **inside the stream's worktree**, and each task still merges upstream to
`main` via its own reviewed PR. Create a new stream's worktree with `ops/scripts/new_worktree.sh N`
— never `git worktree add` by hand, or the `ops` symlink will be missing.

## Autonomous Agent Protocol (Planner-Doer-Reviewer)
When acting as the Lead Architect (Opus), you must strictly follow this loop without asking the user for manual intervention:

1. **State Check:** Read the relevant plan file (`ops/plans/stream-N-plan.md`) to identify the next pending task.
2. **Task Delegation (Headless Sub-agent):** Use your Bash tool to spawn a Sonnet sub-agent in non-interactive mode using the `-p` flag. You must pass `--allowedTools` so the sub-agent doesn't get blocked asking for permissions. Tasks are merged via PRs which you will review.
   * **Never unset or override `CLAUDE_CONFIG_DIR`.** The harness exports it so the whole agent tree runs under one intended Claude config; sub-agents inherit it automatically, so just spawn `claude -p` normally. If you find it unset, stop and report rather than proceeding — the fallback config bills and logs against a different account.
   * *Example Command:* `claude -p "Complete Task 3.3 from ops/plans/stream-3-plan.md. Write the code and commit the changes and create a PR." --allowedTools "Read,Edit,Bash"`
3. **Wait & Review:** The bash command will block until Sonnet finishes. Once the bash command returns successfully, use gh commands to review the code Sonnet just wrote.
4. **Iterate or Update State:** * If the work is flawed, comment on the PR, use the Bash tool to run the headless Sonnet agent again, to read the feedback and fix the specific issues.
   * If the work is approved, merge the PR (no need for PR approval flow), update the relevant `ops/plans/stream-X-plan.md` file to mark the task as `[x]` DONE.
5. **Proceed:** Automatically move to the next pending task in the plan and repeat the loop.
NOTES: 
A. If there's no plan file, it could be that you are also the first to create the plan. Go ahead and do that, and wait for approval before starting implementation loop.  
B. Continue with the plan until hitting hard permission blockers, or at least 2-3 product facing question. Continue on other tasks as much as possible and collect open questions instead of blocking the entire flow. 

Out of Scope / Deferred: Always check `ops/DEFERRED.md` before planning a task or writing a feature. If a major feature is listed there or similarly will be covered by a deferred implementation, make a not of it and move on. If the user states that something is a good idea for later, add it to the deffered md.