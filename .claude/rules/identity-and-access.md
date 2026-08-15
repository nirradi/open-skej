---
description: Identity, access control, and the multi-tenant Space model.
glob: "app/**/*"
---

# Identity & Access

Who a user is, what Spaces exist, what Resources each Space holds, and who is allowed into them.
Booking mechanics — the calendar, the overlap constraint, the rule engine — are not this component's
business, but the `resources` table a booking is made against is.

**Lives in:** `app/backend/app/{auth,identity,db}/`, `app/backend/alembic/`,
`app/backend/scripts/`, and the root `docker-compose.yml`. This domain defines the production
database schema.

`app/backend/app/identity/models.py` and `authz.py` are the authoritative statements of the schema
and the authorization rule, and both carry their reasoning inline.

## Auth0 proves identity; we decide permissions

`space_memberships` is the source of truth for authorization. Auth0 is an identity provider and
nothing more.

Per-Space roles stored in Auth0 would mean a Management API round-trip on every membership change,
and an outage there would become an outage here. The cost of this split is that a user's permissions
are not visible in the Auth0 dashboard — that is intended.

**Users are provisioned just-in-time** on the first authenticated request, keyed on the JWT `sub`.
No Auth0 webhooks or Actions are involved. `sub` is the stable external identifier; **email is
mutable** and refreshed from the token on every login.

Email is deliberately **not unique**. Auth0 issues the same address under different `sub` values when
one person signs up with a password and later with Google — ordinary behavior unless account linking
is configured. A unique constraint on email would turn that into a hard login failure. Uniqueness
lives on `auth0_sub` alone.

**Tokens are verified** with `PyJWT` + `PyJWKClient` against the tenant JWKS, **RS256 only**. The
explicit algorithm allowlist is what rejects a forged `alg: none` or an HS256 token signed with the
public key.

## Sandbox auth mode is an explicit, mutually-exclusive alternative to Auth0

`SANDBOX_AUTH=true` (`settings.sandbox_auth`) swaps `TokenVerifier` onto an in-process RSA keypair —
generated lazily and never persisted — instead of the real Auth0 tenant, so Playwright and manual QA
can authenticate deterministically with no hosted login page. It reuses `TokenVerifier` itself: the
sandbox path differs only in which key, issuer and audience it trusts, never in the RS256 allowlist
or the claim checks. `app.auth.sandbox` holds the keypair and the minter; `app.auth.jwt.get_token_verifier`
is where the mode is selected and its guardrails enforced, because that is the one place a verifier
for the running process comes into existence.

This is test fixture apparatus — the same in-process-keypair, stub-JWKS shape the JWT test suite
already used — promoted to something a running backend does, which is exactly how an auth bypass
ships if the promotion is not disciplined. Three properties hold as a consequence:

* **Off by default, and never inferred.** The switch is a dedicated boolean, false unless set
  explicitly. An unconfigured Auth0 tenant is not read as permission to fall back to the sandbox key;
  with neither configured, verification fails closed exactly as it always has.
* **Mutually exclusive with a real tenant.** Enabling the sandbox switch while `auth0_domain` /
  `auth0_api_audience` are also set raises at verifier construction rather than picking one config to
  prefer. A backend willing to trust either a sandbox-signed token or a real Auth0 one is strictly
  worse than a backend with no sandbox at all — it would accept whichever credential an attacker
  could obtain first.
* **A sandbox token carries an issuer and audience no real tenant can match**, so even a verifier
  built for the wrong config rejects it on those claims, not merely on an unfamiliar signature.

**The sandbox login endpoint exists only when sandbox mode is on.** It is registered by a conditional
`include_router` rather than guarded inside the handler, so a caller against a normally-configured
backend gets a genuine 404 for the route — the same oracle-free posture Spaces use for `public_id`,
applied here to whether sandbox mode is even present, not just whether a request to it succeeds.

**The frontend has a matching sandbox-auth mode, selected by `VITE_SANDBOX_AUTH`.** `AuthProvider`
renders `SandboxAuthProvider` in place of `Auth0Provider` when the switch is on, installing an
access-token provider backed by `POST /sandbox/token` instead of `getAccessTokenSilently` — the same
`setAccessTokenProvider` seam either path hands the api client, so nothing downstream of it knows
which one is installed. It carries the backend's guardrails rather than a second copy of them: off by
default and never inferred, and mutually exclusive with a real tenant — `VITE_SANDBOX_AUTH` set
alongside any `VITE_AUTH0_*` variable fails loudly rather than one config silently winning, the same
bypass the backend's mutual exclusion exists to prevent.

## Roles are per-Space; there is no superuser

`owner | admin | member`, scoped to one Space. Anyone may create a Space and becomes its owner.
Two tenants on one deployment are genuinely independent because no role spans them.

`owner` archives the Space, `admin` manages members and invitations and access requests, `member`
books. **A member cancels only their own booking; admin and owner cancel any booking in the
Space** — the same ladder `require_space_role` already enforces everywhere else, applied to one
more decision rather than a new permission concept.

That check is enforced in the cancel handler itself, not by `require_space_role`, because the
answer depends on whose row is being cancelled and `require_space_role` only ever knows the caller
and the Space. A member refused this way gets **403, not 404** — the one bounded exception to this
document's 404-not-403 rule. That rule exists to stop an unguessable `public_id` becoming an
existence oracle for an outsider; this caller is a *proven member* looking at a booking they can
already see on the calendar they just loaded, so there is nothing left to conceal, and a 404 would
tell them a booking they can plainly see does not exist. This is the identical reasoning already
given above for a member who lacks a role: at the point a caller is confirmed to be inside the
Space, refusing them plainly is the honest answer and 404 has nothing left to hide.

**`BookingRead` carries `mine` for every caller and `user_id` only for admin and owner.** The
question a plain member needs answered is "may I cancel this", never "whose is it" — `mine`
(`booking.user_id == caller.id`) is exactly that answer. The week payload is otherwise the one
response an ordinary member fetches that enumerates the Space's user ids, and no screen renders
them. Admin and owner keep `user_id` because cancelling someone else's booking without knowing whose
it is leaves nobody accountable for the decision. Neither field is an attribute of the booking row —
one depends on who is asking and the other on their role — so a `BookingRead` cannot be built by
validating an ORM row, and every route returning one goes through a single helper that takes the
caller. That is what stops a route added later from serving every `user_id` to a member.

What the calendar offers is downstream of this and is **advisory, never the boundary**: the client
hides a cancel control it can tell the server would refuse, and the cancel handler's ownership check
above is what actually refuses it.

**Role ordering is an explicit rank table (`_ROLE_RANK`), never enum comparison.** `MembershipRole`
is a `str` enum, so comparing two roles compares their strings — under which `"admin" < "member" <
"owner"`, putting member above admin and granting every member admin authority. Declaration order is
no safer: invisible at the comparison site and one reordered line from the same bug.

## A Space is the unit of configuration; a Resource is capacity

A Space is not itself the thing booked. It is a **venue** — a club, a lab — that holds many
**Resources**, and a Resource is one of N **indistinguishable courts**: a unit of bookable capacity
carrying **no configuration of its own**. `bookings.resource_id` is a foreign key onto `resources`
and the overlap constraint is keyed on it, so two courts booked at the same hour do not collide while
the same court twice does — and that overlap constraint is the *only* thing that still distinguishes
two Resources in a Space. Everything else — operating hours, slot interval, every rule limit — lives
on the Space, and every court in it shares that one configuration. Creating a Space **auto-creates
its first Resource**, so a fresh venue is never a dead end and no primary flow meets an empty state;
the schema can represent a Space with no Resource, but nothing in the product produces one. It also
seeds **two `space_rules` rows** — an unscoped `availability_hours` (09:00–17:00) and an unscoped
`session_length` (60 minutes) — for the same reason: a venue is bookable on arrival rather than one an
admin must visit the rules page to make usable. Those two and nothing else, so every limit a venue has
not asked for stays absent. A Space genuinely meant to enforce no hours holds no such row and has to
have the seeded one deleted — which is what `sandbox_seed` does to Space A, since "not enforced" is
the absence of a row and never a row with an empty bound.

**Membership and roles stay at the Space, never the Resource.** You are admitted to the venue, not to
one court, and a member may book any Resource in the Space. This is deliberate and load-bearing: the
entire authorization model above — roles, access requests, invitations, the unguessable `public_id`,
404-not-403 — is untouched by the venue/Resource split. A Resource therefore carries **no `public_id`
of its own**: admission is Space-level, nothing reaches a Resource without first being inside its
Space, so there is no capability URL to protect. Access to a Resource is therefore decided at its
Space and nowhere else, through `require_space_role` on the parent — which extends the same
oracle-free **404, never 403** rule to a Resource id belonging to another tenant, resolved in one
query so the timing does not leak either.

**The timezone is the one genuinely per-Space column left; everything else a Space enforces is a rule
instance.** A venue is in one physical place, so its timezone (an IANA name like `Europe/Berlin`,
never a fixed offset that is right in July and wrong in January) is a column on `Space`. Operating
hours, slot interval, and every rule-engine limit — a maximum booking duration, a booking horizon, a
weekly or monthly frequency cap — are rows in `space_rules` instead: one row per *instance* of a rule
type declared in `rules.registry.REGISTRY` (`rule-engine.md`, "The canon"), carrying that type's own
JSON `params`, an `applies_to` narrowing which weekdays or dates it governs (`null` means always), and
an `enabled` flag that is the entire pause mechanism — a disabled row is never assembled into the
canon. A Space can hold any number of instances of a type; nothing here caps it to one. Because a
frequency cap belongs to the Space rather than any one row's `applies_to`, it counts every booking the
user holds anywhere in the venue, across all its courts, never per court.

`PATCH /spaces/{public_id}` (admin+) edits `name`, `description` and `timezone`, and nothing else;
`SpaceRead` serves the same three. No schedule value is reachable from either — a Space's rules are
read and written only through the rules API below, and **there is deliberately no second way in**. A
scalar field can only ever name a type's one *unscoped* (`applies_to IS NULL`) instance: it has
nowhere to say which of two `availability_hours` rows it means, or to name one scoped to Saturdays,
so keeping one alongside the rules API would leave two paths answering "what does this Space
enforce" — the bug class this schema keeps writing down. `SpaceSchedulePanel` edits only the Space's
`timezone`, the one property its owner calls truly configurable and not a rule; every rule instance,
including the six that were once columns on `spaces`, is created and edited at `/s/{public_id}/rules`
(`SpaceRulesPage`). A Resource has no configuration to edit;
`PATCH /spaces/{public_id}/resources/{resource_id}` renames it and nothing more. `POST
/spaces/{public_id}/resources`, admin+, adds one — its own panel on `/admin` (`ResourcesPanel`),
not a second surface on `SpacePage`, for the same reason every other write on this list has one
home: `SpacePage` is where a *member* picks a Resource to book against, and venue management
belongs with the rest of it on the console a member never reaches.

**A Resource retires; it is never deleted.** `POST
/spaces/{public_id}/resources/{resource_id}/archive`, admin+, is the whole removal mechanism, and
there is no `DELETE`: `bookings.resource_id` points at the row, so a Resource that vanished would
take every booking made against it with it. Archiving only stamps `archived_at` — the row, and its
booking history, survive — matching `archive_space`. There is deliberately no un-archive endpoint
either, so archiving is a one-way door; a Resource retired by mistake is recreated under a new row,
not restored. `ResourcesPanel` is this lifecycle's only UI: it calls `updateResource` to rename and
`archiveResource` to retire, both behind the same two-step confirm-in-place `SpaceRulesPage`'s
`RuleRow` uses for its own delete, and its copy says "Retire" — never "Delete" or "Remove" — because
the row is kept. A retired Resource stays in the admin's list, marked, offering neither control:
both endpoints reject an already-archived Resource with `conflict`, so there is nothing left on that
row to do. This is a decision, not a gap left for a future delete: if a genuine destructive delete is
wanted later it is a data-retention decision made deliberately, not one smuggled in as a Resource
lifecycle default. `listResources` excludes archived Resources unless asked otherwise, so the
single-Resource redirect on `SpacePage` (below) counts only active ones — retiring the second of two
Resources in a Space turns it into a redirecting single-Resource Space, which is correct and needs no
special case.

**`opens_at_minutes` and `closes_at_minutes` are stored together, in one `availability_hours` row,
required together.** Both are `required` parameters of that rule type, so a row holding one bound
without the other is not a state the type can build from, and it is refused rather than stored. A
Space enforcing no hours at all holds no `availability_hours` row at all: "not enforced" is the
*absence* of a row here, exactly as it is for every other rule type, never a row with a bound left
empty. Both are minutes from the Space's own local midnight (`rule-engine.md`), and
`opens_at_minutes` must be earlier than `closes_at_minutes` by at most 24 hours — `0 <=
opens_at_minutes < 1440` and `opens_at_minutes < closes_at_minutes <= opens_at_minutes + 1440` — and
that range is enforced here or nowhere: a pair failing it is refused with **422**. This is not
tidiness: `AvailabilityHoursRule`'s own constructor enforces the identical range, so a submission
failing it here would otherwise be stored and only discovered as `RULE_ERROR_MESSAGE` denying every
booking the next time someone books. This layer is what turns that into a 422 naming the problem
while an admin is still looking at the form, instead of a Space that silently refuses everyone.

The check is made on the **effective pair after the patch is applied**, not on the payload: a PATCH
naming only `opens_at_minutes` is a legal partial update, and whether the pair still satisfies the
range depends on the `closes_at_minutes` the row already holds. A venue open past its own local
midnight — `closes_at_minutes > 1440` — is admitted by this same check today, not by relaxing it
further: it is exactly as valid a value as any same-day window, since the range is measured in
minutes rather than compared as clock times with a date stripped off
(`ops/done/stream-7/passed-midnight.md`).

A `timezone` is validated as a real IANA name at the boundary — an unknown name or a fixed offset
(`+02:00`) is rejected, never stored — because a bad zone would only surface later as a broken
operating-hours resolution far from where it was set.

**The rules API is the only way a Space's rules are read or written.**
`GET /rule-types` describes the product's registered rule types (`rules.REGISTRY`) — authenticated,
but not Space-scoped, since it names nothing about any one tenant's configuration. Every other route
is Space-scoped and reads or writes one Space's own `space_rules` rows directly:
`GET`/`POST /spaces/{public_id}/rules` are member+/admin+, and
`PATCH`/`DELETE /spaces/{public_id}/rules/{id}` are admin+. `POST` and `PATCH` validate `params`
against the row's own registered type's schema at the boundary — an unknown `rule_type`, a missing
required parameter, an unknown parameter, or one of the wrong kind or below its declared `minimum`
is refused with **422** naming the specific parameter, so a later admin form can attach the message
to the right field rather than a generic complaint. `session_length`'s `session_minutes` must divide
1440, enforced here rather than left to surface only at booking time. `availability_hours` carries
the range check above — `opens_at_minutes` and `closes_at_minutes` failing `0 <= opens_at_minutes <
1440` or `opens_at_minutes < closes_at_minutes <= opens_at_minutes + 1440` is a **422**, mirroring
`AvailabilityHoursRule`'s own constructor bound exactly as `session_length`'s check mirrors
`SessionLengthRule`'s. A `PATCH` naming only one of `opens_at_minutes`/`closes_at_minutes` resolves
the effective pair against what the row already has stored, rather than failing "missing required" on
a bound the caller never meant to touch. A rule id that names nothing, or names a row
in another Space, gets the identical **404** on `PATCH`/`DELETE` — the same 404-not-403 treatment a
foreign Resource id gets, since the lookup is scoped to `space_id` in one query and a foreign id
discloses nothing about being live elsewhere. `DELETE` is a real delete, unlike everywhere else in
this schema: `enabled` is already the pause mechanism, so a row nobody wants paused forever should
not have to exist at all.

**No `ON DELETE CASCADE` on the booking foreign keys.** `bookings.resource_id` and `bookings.user_id`
reference `resources.id` and `users.id`, and neither cascades — nothing here is deleted, and a
cascade would destroy booking history the moment a Resource or user was removed. A Resource retires
via `archived_at`, matching the Space's own end-state.

## `generated_rule_types` is global, and it retires rather than deletes

The rule types the AI generation loop has authored are rows in `generated_rule_types`, part of this
production schema. A generated type is **not scoped to a Space**: it joins the catalog every Space
can pick an instance from, exactly as the eleven hand-written types in `rules.REGISTRY` do. Being
available to a Space is not the same as being enforced on it — nothing is enforced until an admin
adds a `space_rules` instance through the rules API above, and *that* act, not the generation, is the
human gate.

**Provenance is columns, not a comment.** `created_by_space_id` (**NOT NULL**), `created_by_user_id`,
`created_at` and the author's original `prompt` are recorded because scoping generated types down to
their creating Space later must be a migration over columns that already hold the answer rather than
an archaeology exercise. NOT NULL is the load-bearing half: a nullable provenance column is empty for
exactly the rows a later migration would need it for.

**`rule_type` is unique and is never reused.** `space_rules.rule_type` stores this string, so handing
the same id to a second rule would silently repoint every instance already naming the first one.

**Retire, never delete — and this does not contradict the rules API's real `DELETE` above.** `status`
(`active` | `retired`) is the whole removal mechanism. The distinction is what points at what: a rule
*instance* is a configuration choice nobody references, so deleting its row strands nothing, while a
rule *type* is named by string id from every `space_rules` row that uses it, and deleting it would
turn each of those into a live reference to nothing. A retired type simply stops being hoisted, and
an instance still naming it denies with the engine's generic copy through the fail-closed path an
unregistered `rule_type` already takes (`rule-engine.md`) — refusing, never silently skipping the
constraint the Space configured.

The source, its `sha256`, the compiled `executable_bytecode`, the `python_version` and the
`bytecode_magic` all live on the row; `rule-engine.md` owns why both version and magic are stored and
what the load path re-proves before executing any of it.

## Generation is a job, and the API over it does not exist unless it is switched on

Writing one of those rows takes minutes and up to eight model calls, so an admin submits a prompt and
polls. Four tables and one router carry it.

`rule_generation_jobs` holds the request: `space_id`, `user_id`, the `prompt`, a status of
`queued | running | succeeded | failed`, the per-attempt `attempts` history, an `error` for a human
to read, and the `generated_rule_types` row it produced, if any. `prompt_versions` and
`rule_generation_exchanges` are the recording — one exchange row per model call, pointing at a system
prompt stored **once, keyed on its sha256** rather than copied onto every call. `rule-engine.md` owns
why the exchanges are kept at all and what the retry turn contains.

**A partial unique index makes the constraint precise**: `uq_rule_generation_jobs_in_flight` is
unique on `space_id` `WHERE status IN ('queued','running')`. The same shape and the same reasoning as
`uq_space_access_requests_pending` — uniqueness over the *live* rows only, so one Space cannot queue
five generations at once, while a Space that generated yesterday can generate again today. A plain
`UNIQUE (space_id)` would permit exactly one generation per Space ever.

That index is also why **`app.main`'s lifespan sweeps orphaned jobs to `failed` at boot**. The runner
is in-process and does not survive a restart, so a `running` row at boot describes a job nobody is
executing — and with this index, one stale row is not merely stale, it is a permanent block on that
Space ever generating again. The sweep is the release valve, not housekeeping. It runs in its own
`try`/`except`, the same posture as the catalog reload beside it: a database slow to accept
connections at boot must not stop the backend coming up.

**The routes are `admin+`, and on a normally-configured backend they do not exist.**
`POST /spaces/{public_id}/rule-drafts` (202, or 409 when one is already in flight), `GET` on the
collection, and `GET` on one id. `app.main` registers the router only when `RULE_GENERATION_ENABLED`
is set — a conditional `include_router`, exactly as for sandbox auth mode, so a caller gets a genuine
404 rather than a 403 that would first require the route to exist in order to refuse it. Whether the
capability is present is not discoverable by whether a request to it succeeds. There is a second
reason here that sandbox mode does not have: every job spends real model calls, so an unconfigured
deployment cannot be induced to spend money by anyone who guesses the URL. `RULE_GENERATION_ENABLED`
is a dedicated boolean and is **never inferred** from an API key being present.

A job id belonging to another Space returns the **identical 404** as one that names nothing, resolved
in one query scoped to `space_id` — the standing rule; the difference between "no such job" and "not
your job" is itself information about another tenant.

**These three routes are absent from `test_spaces_api.py`'s `ROLE_TABLE`**, which is compared for
equality against the app's OpenAPI schema and therefore cannot list a conditionally-registered route.
The equivalent sweep lives in `test_rule_drafts_api.py`, over an app built with generation enabled.

Note the asymmetry the route docstrings state: **the job is Space-scoped, the rule type it produces
is global.** Authorisation to *generate* is a Space authorisation; the artifact is not scoped to that
Space, and the provenance columns above are what a later migration would use to scope it.

## Spaces are not discoverable

There is no endpoint listing Spaces. The only way to reach one you are not in is to be handed its
`public_id` — a 22-character `secrets.token_urlsafe` value, 128 bits, generated with `secrets` and
never `random` (a Mersenne Twister's sequence is reconstructible from a handful of outputs, which for
capability URLs would mean deriving every Space's link from one legitimately received).

**A caller with no membership gets 404, never 403.** A 403 confirms that a Space with that id exists,
making every capability URL an oracle. This matters most in the cases that actually happen — a link
forwarded to the wrong person, an id lifted from browser history or a proxy log — where the question
is not "can this be guessed?" but "is this id still live?". Both paths raise the identical exception
with the identical body, resolved in **one outer-joined query** so they also take the same time; two
queries would return early on a missing Space and leak the same oracle to a stopwatch.

A caller who **is** a member but lacks the role gets a genuine **403** — they already know the Space
exists, so there is nothing left to conceal.

`require_space_role` is a **factory** taking the minimum role, not a dependency taking a role
parameter: FastAPI resolves dependency parameters from the request, so a plain argument would become
a query parameter and let the caller choose which role to require. It returns a `SpaceContext`
(space, membership, user) that handlers use instead of re-querying — a second lookup is a wasted
round-trip and a chance for the two to disagree.

## Access paths into a Space

* **Cold link-holder** — sees a minimal preview (name, description, own status) and may request
  access; an admin approves. The preview is deliberately thin: no member list, no bookings.
  `/preview` is the one route reachable without a **membership** — it still requires a session, since
  the status it returns is the caller's own and means nothing without an identity.
* **Invitee** — an invitation **pre-approves**. Membership is granted on first login, matched on
  verified email, with no request step.

Invitations are keyed on **email, not `user_id`**, because the invitee usually has no account when
the row is written. The address is stored **lowercased** and a CHECK constraint enforces it: matching
case-insensitively at query time would mean a `lower(email)` scan or a silently missed invitation for
`Alice@Example.com`.

**Uniqueness is over pending rows only** — a partial unique index, not a plain `UNIQUE (space_id,
user_id)`. Decided rows are retained so an admin can see a user was denied last month before
approving today; a full unique constraint would permit exactly one request ever, so a user denied
once could never ask again. Same for invitations: an address invited and revoked can be invited
again.

**CHECK constraints enforce decision completeness.** A decided request records both when and by whom;
a pending one records neither. Approval creates the membership and flips the status in one
transaction, and the constraint is what stops a half-applied decision from persisting.

## Schema conventions

**Enums are `native_enum=False` with `create_constraint=True`** — stored as plain strings behind a
CHECK. There is no Postgres `TYPE` to `ALTER` when a role or status is added later (an in-place enum
change is among the more painful migrations to write; swapping a CHECK is not), and it keeps partial
index predicates like `WHERE status = 'pending'` as ordinary string comparisons.

**One declarative `Base`**, imported from `app.db.models` rather than redefined. One metadata
registry is what lets `bookings.resource_id` / `bookings.user_id` be real foreign keys onto
`resources.id` / `users.id` without a cross-base reference. The booking store is folded into
Alembic alongside the identity tables, so **a single migration history owns the whole schema** and
autogenerate manages both halves — there is no table-scoping filter.

**Timestamps use `UtcDateTime`**, which rejects naive datetimes outright, so a local time cannot be
stored as if it were UTC.

Postgres is the only target. `postgresql_where` predicates are not a portability compromise.

## Frontend

**Login is the front door.** `/` is authenticated: a signed-out visitor sees a sign-in card rendered
in place, and the destination once signed in is the Space list — the user's memberships, not a
generic calendar. There is no anonymous view of anything, `/s/{public_id}` included; every route
requires a session before it shows a person anything about a Space. The calendar is not a route of
its own: it exists only per-Resource, at `/s/{public_id}/resources/{id}`, reached by a member who
lands in a Space and picks one of its Resources. It is behind the same guard as everything else and
is not this domain's concern beyond that it, too, requires a session to reach.

**The week that calendar shows is in its URL, as `?week=`, and the page owns it.** The grid is handed
a week and reports where it wants to go; it holds none of its own. That is what makes a refresh, a
bookmark and a forwarded link all land on the week the sender was looking at, and two sources of
truth for the visible week is exactly the bug the shape rules out. A malformed or out-of-range value
falls back to the current week in silence — it is a URL a person can type, so it is not an error they
can act on. Paging pushes rather than replaces, so Back walks week by week; the single-Resource
redirect above replaces, and the two differing is what stops Back walking into that redirect.

**The grid's layout is resolved by the server, per date, and the frontend only renders it.**
`GET /spaces/{public_id}/schedule?from=&days=` (`app.rules_stub.resolve_day_schedule`) reports, for
every date in the requested range, the session length, the operating window, and the grid anchor a
booking on that date would actually be judged against — the flat-AND of that date's own
matching `space_rules` rows (every matching row of a type combines rather than one being picked,
exactly as the engine itself combines rules), in the Space's own local wall clock.
`DayScheduleRead.session_minutes` and `anchor_minutes` are plain minute counts, not wall-clock
`time`s: one names a duration and the other a point measured from local midnight, so neither is
folded through the minutes-to-wire-time conversion `opens_at` / `closes_at` go through.
`anchor_minutes` is that date's own resolved opening time, reported rather than left for the client
to derive from `opens_at` — which rows govern a date is the server's question alone. This exists
because a rule's `applies_to`
(`rule-engine.md`) can narrow it to particular weekdays or dates, so a Space no longer has one
session length or one operating window good for the whole week — a single `CalendarConfig` covering
the whole week cannot express "Tuesdays are different". The frontend never re-derives this resolution
itself: a second implementation of "which rules govern this date" in TypeScript is exactly the
duplication `DEFERRED.md` item 13 warns against, since the engine must stay the sole validator and
the grid stays advisory. A week's own days can resolve to different session lengths and different
anchors; the grid's shared time axis renders at the finest session length configured anywhere in the
visible week, and each day lays out its own rows at its own resolved length, offset to its own
anchor, and is greyed against its own resolved window, never a Space-wide value. A day whose anchor
does not agree with the shared axis renders rows that do not sit flush with it — the same
degradation a day whose session length is coarser than the axis already shows, and reconciling the
two is deferred with the rest of the week-axis work.

The grid always renders the whole day regardless of what any date resolves to — there are no
compile-time slot or opening-hour constants, because an admin edits the rules behind this endpoint
and a build-time value could only ever be a stale copy. Hours outside a date's own resolved window
are **greyed, never absent**: clipping the day to `[opens_at, closes_at)` would leave a booking made
before the hours were narrowed with no row to sit on, so it would vanish from a calendar it is still
on. A date with no matching row of a type resolves that field to `null`, meaning the corresponding
rule is not enforced on that date at all, so it renders the whole day bookable rather than falling
back to an invented window. The greying is the same advisory line everything else on this screen
draws: it must never offer what the server would refuse, and it is never what refuses a booking.

**Every clock the grid draws is the Space's own, never the viewer's.** The day columns, the slot
axis, the greyed hours, and the instant a click submits all resolve through the Space's own
`timezone` — the same zone the backend resolves `LocalFrame` against (`rule-engine.md`) — via
`Intl.DateTimeFormat`
with an explicit zone, asked fresh per date rather than cached as an offset, for the identical reason
the backend conversion is repeated per date rather than done once at write time. A viewer whose own
zone differs from the Space's sees that only as a secondary hint alongside the grid, never a second
version of it: every member looking at a Space sees one grid, in one clock, so two members can compare
what a slot means without translating. A per-viewer clock was considered and rejected — it would let
two members read different times for the same slot, and it makes the operating window wrap midnight
for anyone far enough from the venue.

**A resolved session length longer than the resolved operating window gets an advisory note, never a
blocked calendar.** A real (non-zero-width) window paired with a session length that exceeds its own
length means nothing on that date could ever be booked at all, which `resolve_day_schedule` reports
as that date's own `coherence_issue` rather than refusing to describe the date. The calendar
surfaces it as a small note in that date's own header and changes nothing else about that day.
This is the **only** coherence case there is, and a misaligned bound is not another one: the grid is
anchored on the opening time itself, so an opening time cannot miss a grid built on it, and a closing
time leaving a tail too short for one more session is ordinary wasted capacity rather than a
misconfiguration worth reporting.

This check runs server-side, per date, inside
`resolve_day_schedule` rather than at boot — it once threw at import time, which was right for a
constant nobody could mistype and is wrong for data an admin typed: one bad Space would white-screen
the app for everyone, including the members of every other Space. That reasoning still holds one
level up: a `/schedule` request that fails outright (the server unreachable, not a per-date coherence
issue) is the one case left that still degrades to a notice replacing the *whole* calendar — with no
resolved schedule at all there is nothing honest to render as a grid — and even then it is scoped to
that Space's own calendar, never a page that takes the rest of the app down with it.

**One session seam, two implementations.** `useSession()` returns `{ status: 'loading' |
'authenticated' | 'unauthenticated', login, logout }` — the shape every route reads, regardless of
which of Auth0 or sandbox mode (`SANDBOX_AUTH`'s frontend counterpart, `VITE_SANDBOX_AUTH`) is
actually installed. An Auth0-backed implementation adapts `useAuth0`; a sandbox-backed one holds
signed-in/signed-out state in React, backed by its own `localStorage` flag, distinct from the flag
that merely selects which seeded identity a sandbox token mint is for — selecting an identity and
committing to it are different actions, which is what lets a test open a deep link already
"holding" an identity but still signed out, then sign in through the same control a real visitor
would use. No route may call `useAuth0()` (or read sandbox internals) directly any more; a hook that
assumes one mode's provider is in the tree throws under the other, which is exactly the failure this
seam exists to prevent — sandbox mode has no `Auth0Provider`, and the Auth0 build has no sandbox
session.

**A session that stops working ends for every screen at once.** Neither implementation can discover
this on its own: the Auth0 SDK's `isAuthenticated` and sandbox mode's signed-in flag both keep
answering "signed in" after the token backing them has stopped working, because both are reads of
what someone once did, not of whether it still holds. The api client is where the failure is actually
observed — a rejected token provider, or a 401 the server returned to a token that was sent — so it
publishes that fact as a store the session implementations subscribe to and let override their own
state. That is `setAccessTokenProvider`'s seam pointed the other way: React hands the api client the
token, the api client hands React the news that it stopped working, and neither module imports the
other. Without it, a guarded screen stays on display filling with 401s and every panel prints its own
apology with no way back in, which is the state a person can only escape by clearing browser storage.

**Only an explicit sign-in clears it.** Not the next successful request, and not a timer: either
would re-arm silent auth the instant a guarded screen fell through to the login controls, which is a
loop. The screens themselves need no new gate — `ProtectedRoute` already renders `LoginControls` for
an unauthenticated session, and those controls already return the user to the URL they were on.

Copy for this states the fact and never the cause. A lapsed refresh token, a revoked grant, a rotated
signing key and a changed tenant all arrive here, the person's next move is identical in all of them,
and naming the wrong one is worse than naming none. The diagnosis goes to the console.

An admin dashboard for Space creation, share links, member management, and Resource creation
(`ResourcesPanel`, admin+). Role menus offer only roles at or below the actor's own, which is a
convenience — the server's 403 is the boundary.

**`/admin` is reached from three screens, never typed cold.** The Space list (`/`, the post-login
destination every signed-in user lands on), a Space page (`/s/{public_id}`), and a Resource calendar
(`/s/{public_id}/resources/{id}`) each carry a link onto it, shown only to a caller whose `my_role` on
the relevant Space is `admin` or `owner`. The calendar page carries its own rather than relying on the
Space page's, because a Space with exactly one active Resource redirects straight past the Space page
to that Resource's calendar and never renders it at all — the one venue most likely to need the link
would otherwise never see one. As with every role-gated control on this dashboard, the check is a
convenience that decides what renders, never what is allowed: `require_space_role` is what actually
governs the console once a caller is on it, so a role read gone stale hides a link at worst, and never
stands in for the 403 the server would still return.

**`SpaceRulesPage` carries a rule-authoring panel alongside the generic rule editor, admin+.** An
admin types a booking constraint in plain English and submits it; the panel does not hold a
request open for the minutes generation takes, it holds a job id and polls
`GET /spaces/{public_id}/rule-drafts/{id}` — every two seconds at first, backing off after the
first minute, paused while the tab is hidden and resumed the instant it is visible again. On
mount it calls `GET .../rule-drafts` to resume an in-flight job, so a page reload during a
three-minute generation does not read as the job having vanished. A `not_found` on either route is
read as "this backend does not have `RULE_GENERATION_ENABLED` set" (`rule-engine.md`) and the panel
renders nothing at all, the same absent-not-broken posture the conditionally-registered route
itself takes — a normally-configured backend without the capability should look like a product
without the feature. A succeeded job shows the generated type's own label and description and an
offer to add it to the Space, which preselects the type in the existing "Add a rule" panel rather
than opening a second path to the same action; its `human_code` sits behind a disclosure, since an
admin about to enforce a rule on their members should be able to see what it does. A failed job
never renders an attempt's own failure text — that is pytest output written for the model to read,
not for an admin — and leaves the prompt in the box, editable and resubmittable. The "Add a rule"
picker itself renders the selected type's description underneath the `<select>` rather than
growing into a combobox: a list of labels alone stops being enough to choose from once generated
types sit beside the eight hand-written ones, and a description is enough to fix that at any list
length this product expects.

`/s/{public_id}` serves both sides of the door. A non-member sees the cold-link preview and the
access request; a member lands **in the Space** — its name, description, and a picker onto its
Resources, each linking to that Resource's calendar at `/s/{public_id}/resources/{id}` — rather than
being bounced to the generic Space list, which would cost them a click back to the very link they
just opened.

**A Space with exactly one active Resource opens it instead of offering a picker of one.** The
member is navigated to that Resource's calendar — navigated, not rendered through, so the URL ends
up naming the Resource and a refresh, a bookmark or a forward of that screen all land where the
sender was. The navigation **replaces** rather than pushes: a pushed entry leaves the Space URL
behind it, and Back would return to a page that immediately redirects forward again, which is a loop
with no way out of it. Archived Resources do not count toward the one, which needs no code — the
Resource listing already excludes them unless asked otherwise. The calendar's "back to the Space"
control is hidden in this case for the same reason, since it could only lead to a screen that sends
the visitor straight back.

**Both routes a shared link can name sit outside `ProtectedRoute` and behind one Space door.** That
guard's "you need an account to see this page" is right for a members-only screen and wrong for a
link somebody was handed, so `/s/{public_id}` and `/s/{public_id}/resources/{id}` alike require a
session by their own gate instead. A Resource link is forwarded exactly like a Space link and its
holder may have no membership and no account, and since admission is Space-level a Resource id is not
a second capability to protect — so the stranger gets the identical door: the same sign-in card, the
same preview, the same access request. One component serves both, with each route supplying only its
own `returnTo` and what to render once the caller turns out to be a member.

**The Resource route renders that door at its own URL and never redirects to `/s/{public_id}`.** The
URL the visitor is sitting on already names the Resource, so once the membership exists the same URL
resolves straight to the calendar — which is what makes an approved request land them on the Resource
they were sent rather than on the Space's picker, with no further machinery. Redirecting would throw
away the only thing that carries them there.

Four properties follow and are load-bearing:

* **It checks its own auth mode before any session hook runs.** With neither Auth0 nor sandbox mode
  configured there is no session provider in the tree at all, and reading one in that state throws.
  The check lives in an outer component and the session read in an inner one, since a hook cannot be
  called conditionally.
* **A signed-out visitor gets a sign-in card, not the Space.** `/preview` is authenticated, so there
  is nothing to show until they hold a session. Login renders *in place* rather than redirecting, and
  carries `returnTo`: a visitor who followed a share link and was deposited on the Space list
  afterwards would have lost the only handle to that Space that exists. The round trip survives
  signup as well as login — a brand-new identity is just as often behind a forwarded link as a
  returning one, and `get_current_user` provisions it just-in-time on the same first call either way.

  **The router owns the destination, not the address bar.** Auth0's redirect callback runs outside
  the React tree entirely, and by the time it fires `BrowserRouter` has already mounted at the
  callback URL — so rewriting `window.history` there changes what the address bar says and tells the
  router nothing, leaving the URL naming one screen while another is rendered. The callback therefore
  publishes the destination to a module-level store and a component inside the router subscribes and
  navigates. It must *subscribe* rather than read once on mount, because the callback fires after that
  mount; a one-time read sees nothing. That single navigate also strips Auth0's `?code=&state=`, which
  must not survive into the address bar — it is single-use, it breaks a refresh, and it gets pasted
  into bug reports — and it replaces rather than pushes, so back does not walk into a spent code.

  This is the same shape the session-lost store above uses, for the same underlying reason: a fact
  discovered outside React that a component below the router has to act on.
* **404 copy names the link, never the Space.** "That link doesn't work" — never "you don't have
  access to this Space", which would confirm the id is live and turn the capability URL into the
  oracle the 404 exists to prevent. This is the one piece of copy on the route that is a security
  decision rather than a wording choice.
* **A denied user may ask again.** The status is rendered as a state to act from, matching the
  partial unique index that constrains pending rows only.

**A not-found *page* and a not-found *Space* are two different views, making two different claims,
and neither stands in for the other.** `App.tsx`'s catch-all — `path="*"` for any address, and
`path="/s/:publicId/*"` for one that names a Space but none of its defined routes — renders
`NotFoundPage`. Its claim is unambiguous: this application has no screen at this address, a fact
that concerns nobody's access to anything. `SpaceAccessGate`'s `NotFoundCard`, above, is a narrower
and deliberately *ambiguous* claim about one specific screen, `/s/{public_id}` and
`/s/{public_id}/resources/{resource_id}`: "no such Space, or you can't see it" — the ambiguity is
load-bearing, since a plain "you can't see it" would confirm the id is live. `NotFoundPage` is what
renders for every address neither of those two routes matches, `NotFoundCard` only for one of those
two routes failing to resolve a Space; widening either to cover the other's job would either blunt
`NotFoundCard`'s deliberate ambiguity into a page that leaks nothing, or narrow `NotFoundPage`'s
honest claim into one that pretends to conceal something it does not. Reached under a Space's own
URL, `NotFoundPage` also offers a way back to that Space and to `/admin` — the one thing the
Space-scoped route can say that a bare `path="*"` cannot — and neither checks any Space or
membership state to decide whether to offer them: both links go through the ordinary access gate
exactly as if the visitor had typed them, so nothing is skipped by offering them. Like every route
above except `/s/:publicId/rules`, it is not wrapped in `ProtectedRoute`: a wrong address is wrong
whether or not the visitor is signed in.

Deployment is local only: compose plus localhost callbacks.
