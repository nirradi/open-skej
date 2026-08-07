# Open-Skej: Architecture

This file and `.claude/rules/*.md` describe **what the system is and why**.

## What it is

Open-Skej books time on shared resources — a tennis court, a piece of expensive equipment. Each such
resource is a **Resource**: a unit of bookable capacity, one of N indistinguishable courts in its
**Space**. A Space is the venue (a club, a lab) that owns many Resources and is the boundary of both
who may book and how: membership, roles, and every booking constraint are Space-level, not
per-Resource — a member of the venue may book any Resource in it, all governed by the same rules. A
booking is always against one Resource, and the overlap constraint keyed on it is what still tells
two Resources apart. The differentiator is **AI-driven rule configuration**: booking constraints are
authored in natural language ("only 1 hour sessions", "no more than twice a week") and stored as
parameterized Python snippets that the rule engine executes, per Space.

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
  identity/      Users, Spaces, memberships, access requests, invitations, and each Space's own
                   rule configuration (`space_rules` rows, read and written through the
                   `/spaces/{public_id}/rules` API). Also `generated_rule_types` — the rule types
                   the AI generation loop has authored, global rather than Space-scoped, stored as
                   source plus compiled bytecode with the provenance of who created them
                   authz.py — require_space_role, the per-Space authorization dependency
                   router.py — also carries `rule_types_router` (`GET /rule-types`), the one route
                     here that is deliberately not Space-scoped: it describes the product's
                     registered rule types, not one tenant's configuration of them
  rule_catalog.py  Every rule type this process knows: `rules.REGISTRY` plus the generated types
                   *hoisted* out of `generated_rule_types`. Backend-owned precisely so `REGISTRY`
                   is never written into, and the one place a stored rule is turned back into
                   executable code — re-validated and run in a restricted namespace each time
  db/            Declarative Base, session, UtcDateTime, driver abstraction
  routers/       Booking endpoints. `resource_bookings.py` is Space-scoped and authenticated: a
                   booking is made against a Resource inside a Space the caller belongs to, resolved
                   through `require_space_role`. It is the only booking router — there is no unscoped
                   route and no default Resource or user for one to carry
  rules_stub.py  Adapter onto `rules/`: converts to UTC, supplies the allow-path copy, and
                   assembles the canon from the Space's own configuration rather than running a
                   module-level one. Name is historical; it holds no rule logic
app/frontend/    React SPA
  src/auth/      Auth0 and sandbox wiring behind one `useSession()` seam, and the route guard
                   built on it — `ProtectedRoute` is what makes `/` the front door: login rendered
                   in place for a signed-out visitor, the api client's token bridge
  src/space/     The Space list at `/`; `/s/{public_id}` — the cold link-holder preview and access
                   request for a non-member, and for a member the Space itself with a picker onto its
                   Resources; and `/s/{public_id}/resources/{id}` — the calendar for one Resource
  src/admin/     Space creation, members, invitations, the access-request queue, the Space's
                   timezone, and — at `/s/{public_id}/rules` — a generic editor over the Space's
                   own rule instances, built from each rule type's declared parameter schema rather
                   than one form per type. The same page also carries a panel to author a rule
                   type in English rather than only configure one from a declared schema: a
                   prompt, a polled generation job, and a hand-off into the same "Add a rule" flow
                   once it succeeds
app/e2e/         Playwright suite driving the real backend, not a mock
rules/rules/
  interfaces.py  The rule contract — authoritative, read before writing any rule
  controller.py  evaluate_request(): fail-fast canon execution and error containment
  registry.py    Each rule type's runtime identity: the stable string id a `space_rules` row
                   stores, its parameter schema, its priority, and the function that builds it
```

## Cross-cutting invariants

These hold everywhere and are not any one component's private business.

**UTC everywhere.** Every datetime crossing a module boundary is timezone-aware with a **zero** UTC
offset. Naive datetimes and non-zero offsets are rejected at construction (`UtcDateTime` in the
schema, the interface dataclasses in the engine). Timezone is a UI presentation concern and no
backend entity carries one. This is not pedantry: rules read `.hour` to enforce opening windows, so
a `+02:00` value would yield a *local* hour and silently mis-enforce them. Convert at the boundary.

**Instants carry no zone; recurring configuration does.** A stored datetime — a booking's `start_at`,
`created_at`, any instant — is UTC and carries no timezone, full stop: an instant is an absolute
point, and a zone on it would add ambiguity without adding information. The one exception is
recurring wall-clock configuration, not an instant at all — a Space's operating hours are authored
as local clock times against its own IANA zone (`.claude/rules/identity-and-access.md` has the full
model) — and that split is exactly why it is the exception: a rule resolving to a different UTC
instant per date is the one thing that needs a zone to resolve at all.

**Conversion happens at the boundary, per date, never once at write time.** Resolving a Space's local
calendar — its day, week and month bounds, a slot grid's anchor, a frequency cap's counting window —
to UTC instants is repeated for every date the question is asked about, not computed once and cached
as a fixed offset. A cached offset is correct for the day it was computed and silently wrong the next
time the zone's DST rule flips — the version of this bug that looks right in July and wrong in
January. Doing the conversion at the boundary, on demand, is what keeps every entity past that
point — a booking, a rule reading a bound — timezone-free and correct in both months. This is
strictest for the rule engine itself: the only conversion left by the time a rule runs is the one the
adapter performs once, per booking, to build `LocalFrame` (`.claude/rules/rule-engine.md`); no rule
receives a converted clock time of its own to re-derive anything from.

**Fail closed.** Any failure to positively establish that a booking is permitted results in **no
booking**. See `.claude/rules/rule-engine.md` for the three containment paths.

**The link is the capability.** A Space is reachable only by its unguessable `public_id`. There is no
listing endpoint, and a caller outside a Space gets **404, never 403** — a 403 would confirm the id
exists and turn every capability URL into an oracle. The integer primary key is never exposed.
`public_id` lives on the Space alone: a Resource carries no such id, because admission is Space-level
and a Resource is reachable only once you are already inside its Space. Access to a Resource is
decided at its Space and nowhere else, so the same oracle-free 404 covers a Resource that is not
yours.

**Nothing is deleted.** Spaces archive (`archived_at`); access requests and invitations retain their
decided rows as history; a generated rule type retires (`status`). Consequently no foreign key
carries `ON DELETE CASCADE` — there is no delete to cascade, and one added later would quietly
destroy the audit trail. The one deliberate exception is a *rule instance*, which is really deleted,
and the line between it and a retiring rule type is what anything else points at: nothing references
an instance, while a `space_rules` row names its type by string id and would be left pointing at
nothing.

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
