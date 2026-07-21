# Open-Skej: Architecture

This file and `.claude/rules/*.md` describe **what the system is and why**.

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

These hold everywhere and are not any one component's private business.

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

## Domain documents

Each domain's contracts, decisions and rationale live beside this file and are auto-loaded with it:

* `.claude/rules/identity-and-access.md` — users, Spaces, memberships, authorization.
* `.claude/rules/rule-engine.md` — the rule contract, the execution model, AI rule generation.

## Keeping these documents live

**They are part of the deliverable, not commentary on it.** A change to the system is reflected here
in the same change that makes it, never as a follow-up — a doc fixed later is a doc that describes a
system nobody is running anymore.

Write here when a change:

* establishes or changes an **invariant**, a contract, or an interface shape;
* makes a **decision with a rationale** worth not re-litigating (why 404 and not 403, why this
  index is partial, why this model is the default);
* **contradicts** something written here — including a decision reversed after the doc was written;
* adds a component to the system map.

Do **not** write task status, PR numbers, what is coming next, or anything true only until the next
merge. None of it is architecture, and all of it is wrong within the week.

**Write in the present indicative.** "Spaces are not discoverable", not "we will make Spaces
non-discoverable" or "task 2.5 made Spaces non-discoverable". If a sentence needs a task number, a
PR link, or a future tense to make sense, it does not belong here. The reader is an agent six months
from now who has no idea what task 2.5 was and cannot look it up.

**Record the reversal, not the history.** When a decision changes, rewrite the claim and state the
current rationale. Do not append "previously we did X" — an architecture doc is a description of the
present, and a changelog embedded in it is read as a live description of a system that no longer
exists. Git holds the history.

**Name a domain document for the domain it describes**, never for whatever effort produced it. The
identity model outlives the work that built it, and a document named after that work looks obsolete
the moment the work finishes.

**Where these documents and the code disagree, the code is correct** and the document is stale. Fix
the document.

Everything outside this section describes only what is true now. Guidance on writing these documents
belongs here and nowhere else — a rule stated inside a description is one an editor of that
description will not think to look for.
