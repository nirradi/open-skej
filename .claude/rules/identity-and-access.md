---
description: Identity, access control, and the multi-tenant Space model.
glob: "app/**/*"
---

# Identity & Access

Who a user is, what Spaces exist, and who is allowed into them. Booking mechanics are not this
component's business.

**Lives in:** `app/backend/app/{auth,identity,db}/`, `app/backend/alembic/`,
`app/backend/scripts/`, and the root `docker-compose.yml`. This domain defines the production
database schema.

`app/backend/app/identity/models.py` and `authz.py` are the authoritative statements of the schema
and the authorization rule. Both carry the reasoning inline. Read them before changing either; where
they and this document disagree, the code wins.

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

## Roles are per-Space; there is no superuser

`owner | admin | member`, scoped to one Space. Anyone may create a Space and becomes its owner.
Two tenants on one deployment are genuinely independent because no role spans them.

`owner` archives the Space, `admin` manages members and invitations and access requests, `member`
books.

**Role ordering is an explicit rank table (`_ROLE_RANK`), never enum comparison.** `MembershipRole`
is a `str` enum, so comparing two roles compares their strings — under which `"admin" < "member" <
"owner"`, putting member above admin and granting every member admin authority. Declaration order is
no safer: invisible at the comparison site and one reordered line from the same bug.

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
registry is what lets integration turn `bookings.resource_id` / `bookings.user_id` into real foreign
keys onto `spaces.id` / `users.id` without a cross-base reference. The resulting risk — autogenerate
claiming the booking tables — is handled mechanically by `app.migration_filter`, which scopes
migrations to identity tables.

**Timestamps use `UtcDateTime`**, which rejects naive datetimes outright, so a local time cannot be
stored as if it were UTC.

Postgres is the only target. `postgresql_where` predicates are not a portability compromise.

## Frontend

Auth0 React SDK for login/logout. An admin dashboard for Space creation, share links, and member
management. A `/s/{public_id}` route rendering the cold-link preview and the access request.

Deployment is local only: compose plus localhost callbacks.
