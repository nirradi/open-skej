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
the schema can represent a Space with no Resource, but nothing in the product produces one.

**Membership and roles stay at the Space, never the Resource.** You are admitted to the venue, not to
one court, and a member may book any Resource in the Space. This is deliberate and load-bearing: the
entire authorization model above — roles, access requests, invitations, the unguessable `public_id`,
404-not-403 — is untouched by the venue/Resource split. A Resource therefore carries **no `public_id`
of its own**: admission is Space-level, nothing reaches a Resource without first being inside its
Space, so there is no capability URL to protect. Access to a Resource is therefore decided at its
Space and nowhere else, through `require_space_role` on the parent — which extends the same
oracle-free **404, never 403** rule to a Resource id belonging to another tenant, resolved in one
query so the timing does not leak either.

**All configuration lives on the Space.** A venue is in one physical place, so its timezone (an IANA
name like `Europe/Berlin`, never a fixed offset that is right in July and wrong in January), its
operating hours and slot interval (`opens_at`, `closes_at`, `slot_minutes`), and its rule-engine
parameters (`max_duration_minutes`, `booking_horizon_days`, `max_bookings_per_week`,
`max_bookings_per_month`) are all **per-Space columns**. Operating hours are *local wall-clock*
config, resolved against the Space's `timezone` to a UTC instant per date at the boundary — that is
the one place a zone is a property of the data; stored instants carry none. The rule params are
nullable: unset means the corresponding rule is not enforced. Because a frequency cap belongs to the
Space, it counts every booking the user holds anywhere in the venue, across all its courts, never per
court.

The whole schedule is **admin-editable** via `PATCH /spaces/{public_id}` (admin+), and `SpaceRead`
carries all of it so the config UI reads it without a second call. A `timezone` is validated as a real
IANA name at the boundary — an unknown name or a fixed offset (`+02:00`) is rejected, never stored —
because a bad zone would only surface later as a broken operating-hours resolution far from where it
was set. A Resource has no configuration to edit; `PATCH /spaces/{public_id}/resources/{resource_id}`
renames it and nothing more.

**No `ON DELETE CASCADE` on the booking foreign keys.** `bookings.resource_id` and `bookings.user_id`
reference `resources.id` and `users.id`, and neither cascades — nothing here is deleted, and a
cascade would destroy booking history the moment a Resource or user was removed. A Resource retires
via `archived_at`, matching the Space's own end-state.

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

An admin dashboard for Space creation, share links, and member management. Role menus offer only
roles at or below the actor's own, which is a convenience — the server's 403 is the boundary.

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

Deployment is local only: compose plus localhost callbacks.
