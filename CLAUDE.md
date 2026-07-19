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

* **Stream 1 (Core E2E Booking):** Builds the full-stack calendar UI and booking flow using a stubbed rule engine and a local SQLite abstraction.
* **Stream 2 (Auth, Access & Admin):** Owns the real database schema, multi-tenant Space management, and Auth0 integration.
* **Stream 3 (Rule Engine Phase 1):** Develops the pure Python isolated execution environment and the initial hardcoded canon of rules.

## Autonomous Agent Protocol (Planner-Doer-Reviewer)
When acting as the Lead Architect (Opus), you must strictly follow this loop without asking the user for manual intervention:

1. **State Check:** Read the relevant plan file (e.g., `stream-1-plan.md`) to identify the next pending task.
2. **Task Delegation (Headless Sub-agent):** Use your Bash tool to spawn a Sonnet sub-agent in non-interactive mode using the `-p` flag. You must pass `--allowedTools` so the sub-agent doesn't get blocked asking for permissions. Tasks are merged via PRs which you will review.
   * *Example Command:* `claude -p "Complete Task 1.1 from stream-1-plan.md. Write the code and commit the changes and create a PR." --allowedTools "Read,Edit,Bash"`
3. **Wait & Review:** The bash command will block until Sonnet finishes. Once the bash command returns successfully, use gh commands to review the code Sonnet just wrote.
4. **Iterate or Update State:** * If the work is flawed, comment on the PR, use the Bash tool to run the headless Sonnet agent again, to read the feedback and fix the specific issues.
   * If the work is approved, merge the PR (no need for PR approval flow), update the relevant `stream-X-plan.md` file to mark the task as `[x]` DONE.
5. **Proceed:** Automatically move to the next pending task in the plan and repeat the loop.
NOTES: 
A. If there's no plan file, it could be that you are also the first to create the plan. Go ahead and do that, and wait for approval before starting implementation loop.  
B. Continue with the plan until hitting hard permission blockers, or at least 2-3 product facing question. Continue on other tasks as much as possible and collect open questions instead of blocking the entire flow. 

Out of Scope / Deferred: Always check DEFERRED.md before planning a task or writing a feature. If a major feature is listed there or similarly will be covered by a deferred implementation, make a not of it and move on. If the user states that something is a good idea for later, add it to the deffered md.