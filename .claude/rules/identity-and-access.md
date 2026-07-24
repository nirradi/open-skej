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

## Roles are per-Space; there is no superuser

`owner | admin | member`, scoped to one Space. Anyone may create a Space and becomes its owner.
Two tenants on one deployment are genuinely independent because no role spans them.

`owner` archives the Space, `admin` manages members and invitations and access requests, `member`
books.

**Role ordering is an explicit rank table (`_ROLE_RANK`), never enum comparison.** `MembershipRole`
is a `str` enum, so comparing two roles compares their strings — under which `"admin" < "member" <
"owner"`, putting member above admin and granting every member admin authority. Declaration order is
no safer: invisible at the comparison site and one reordered line from the same bug.

## A Space is a venue; a Resource is the calendar

A Space is not itself the thing booked. It is a **venue** — a club, a lab — that holds many
**Resources**, and a Resource is the bookable calendar: `bookings.resource_id` is a foreign key onto
`resources`, and the overlap constraint is keyed on it, so two courts booked at the same hour do not
collide while the same court twice does. Creating a Space **auto-creates its first Resource**, so a
fresh venue is never a dead end and no primary flow meets an empty state; the schema can represent a
Space with no Resource, but nothing in the product produces one.

**Membership and roles stay at the Space, never the Resource.** You are admitted to the venue, not to
one court, and a member may book any Resource in the Space. This is deliberate and load-bearing: the
entire authorization model above — roles, access requests, invitations, the unguessable `public_id`,
404-not-403 — is untouched by the venue/Resource split. A Resource therefore carries **no `public_id`
of its own**: admission is Space-level, nothing reaches a Resource without first being inside its
Space, so there is no capability URL to protect. Access to a Resource is therefore decided at its
Space and nowhere else, through `require_space_role` on the parent — which extends the same
oracle-free **404, never 403** rule to a Resource id belonging to another tenant, resolved in one
query so the timing does not leak either.

**Timezone lives on the Space, not the Resource.** A venue is in one physical place, and the zone
(an IANA name like `Europe/Berlin`, never a fixed offset that is right in July and wrong in January)
exists only to resolve a Resource's *operating hours* — local wall-clock config — to a UTC instant
per date at the boundary. That is the one place a zone is a property of the data; stored instants
carry none. Operating hours (`opens_at`, `closes_at`, `slot_minutes`) are per-Resource columns.

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
  `/preview` is the one route reachable without a membership.
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

Auth0 React SDK for login/logout. An admin dashboard for Space creation, share links, and member
management. Role menus offer only roles at or below the actor's own, which is a convenience — the
server's 403 is the boundary.

`/s/{public_id}` renders the cold-link preview and the access request. It is **the only public entry
point**, and the one route outside the session guard, because everything it exists to serve is a
person who is not yet a member. Four properties follow from that and are load-bearing:

* **It checks its own Auth0 configuration before any hook runs.** With the tenant unset there is no
  `Auth0Provider` in the tree at all — the app keeps rendering so the unauthenticated calendar
  survives a missing tenant — and calling `useAuth0()` in that state throws. The check lives in an
  outer component and the hook in an inner one, since a hook cannot be called conditionally.
* **A signed-out visitor gets a sign-in card, not the Space.** `/preview` is authenticated, so there
  is nothing to show until they hold a token. Login renders *in place* rather than redirecting, and
  carries `returnTo`: a visitor who followed a share link and was deposited on the calendar afterwards
  would have lost the only handle to that Space that exists.
* **404 copy names the link, never the Space.** "That link doesn't work" — never "you don't have
  access to this Space", which would confirm the id is live and turn the capability URL into the
  oracle the 404 exists to prevent. This is the one piece of copy on the route that is a security
  decision rather than a wording choice.
* **A denied user may ask again.** The status is rendered as a state to act from, matching the
  partial unique index that constrains pending rows only.

Deployment is local only: compose plus localhost callbacks.
