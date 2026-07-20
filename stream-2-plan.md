# Stream 2: Auth, Access & Admin — Implementation Plan

## Context

Open-Skej books time on shared resources. Stream 1 is building the end-to-end booking flow against a
hardcoded single user and single resource — `app/backend/app/db/constants.py` says so explicitly:

```python
DEFAULT_USER_ID = "default-user"
DEFAULT_RESOURCE_ID = "default-resource"
```

Stream 2 replaces that fiction with reality: real users authenticated by Auth0, real multi-tenant
Spaces, and the real Postgres database. Per `.claude/rules/stream-2-auth.md` this stream owns User and
Space relationships and the production schema, and must **not** touch calendar or booking mechanics.

**A Space is one bookable thing** — one calendar, one tennis court, one piece of equipment.

**Current repo state.** `app/backend` has a booking data layer (`app/db/models.py`, `driver.py`,
`sqlite.py`) and booking endpoints from Stream 1's tasks 1.3/1.4. There is no Alembic, no Postgres,
no settings module, and `requirements.txt` has no auth or database-driver packages.

**Stream 1 is COMPLETE** as of `13339a5` — all of 1.1–1.10 are merged, including the calendar grid,
booking flow, cancel UI, Playwright suite and runbook. Its Tailwind config, `src/config.ts` and typed
`src/api/` client are all on `main` and Phase B builds directly on them. The gate on Phase B is
therefore lifted; the boundary rules below still apply so Stream 4's integration stays clean.

**Stream 2 runs in a separate git worktree** at `/Users/nir.radian/nirdev/skej-stream2` on branch
`stream-2/base`, so neither stream's working tree can disturb the other. All Stream 2 work happens
there, never in the primary checkout.

**Check `DEFERRED.md` before every task.** Note in particular that *Resource Configuration Admin UI*
(editing open/close times, timezone, slot intervals) is explicitly deferred — Stream 2's admin
dashboard covers members, invitations and access requests **only**.

**A Stream 4 will integrate Streams 1 and 2.** Stream 2 therefore stops at well-marked seams rather
than reaching into booking code. Those seams are enumerated in the handoff section at the end.

## Prerequisites — all verified

- `app/backend/.env` (gitignored) holds working Auth0 M2M credentials for tenant
  `dev-oag8ojxnvqp4okwn.us.auth0.com`. A Management API token was fetched successfully and all eight
  required scopes are granted: `create/read/update:clients`, `create/read/update:resource_servers`,
  `read/update:connections`.
- Docker daemon running; `gh` active as `nirradi`; `origin` uses the `github-nirradi` SSH alias.
- **No Auth0 secrets are needed in CI.** Task 2.3 tests JWT verification against a locally-generated
  RSA keypair and an in-process JWKS. Only the provisioning script and a real browser login touch the
  live tenant.

Known risk: enabling Google social login requires the tenant's default `google-oauth2` connection to
already exist. `update:connections` can attach it to the SPA client, but if no such connection exists
the `create:connections` scope must be added in the Auth0 dashboard.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Stream isolation | Stream 2 runs in a **separate git worktree** on its own branch | Stream 1 has uncommitted work in the primary checkout. A shared working tree means either stream's branch switch can destroy the other's in-progress edits. |
| Authorization source of truth | Our Postgres `space_memberships` table | Auth0 proves *identity*; we decide *permissions*. Per-Space roles in Auth0 would mean a Management API round-trip on every membership change. |
| Role model | `owner \| admin \| member` scoped per Space | Anyone can create a Space and becomes its owner. No global superuser, so two tenants stay genuinely independent. |
| Space discovery | **Not discoverable.** No listing endpoint. Access is via an unguessable link | Per your steer. There is no "browse all spaces" route to build or to leak. |
| Space URL identity | Random opaque `public_id` (22-char urlsafe token), never a sequential integer | The link *is* the capability, so it must not be guessable or enumerable. |
| Cold user with a link | Sees a **minimal preview** (name, description, own status) and can request access; approval required | Your "unknown cold user requires approval". The preview is deliberately thin — no member list, no bookings. |
| Invited user | Invitation **pre-approves**; membership is granted on first login, no request needed | Your steer. Invitations are matched on verified email. |
| User provisioning | Just-in-time upsert on first authenticated request, keyed on JWT `sub` | Avoids Auth0 webhooks/Actions entirely. `sub` is the stable external id; email is mutable and refreshed each login. |
| Token verification | `PyJWT` + `PyJWKClient` against the tenant JWKS, RS256 only | Explicit algorithm allowlist so a forged `alg: none` or an HS256-signed-with-the-public-key token is rejected. |
| Login methods | Auth0 database connection **plus Google** | Your steer. Enabled by the provisioning script, not by dashboard clicks. |
| Space lifecycle | Create and **archive** (`archived_at`). No delete, no ownership transfer | Your steer. Deleting would have to decide the fate of existing bookings, which is Stream 1/4 territory. |
| Migrations | Alembic, **filtered to identity tables only** | Stream 1's `Booking` model is still changing across tasks 1.2–1.4. Migrating it now would go stale underneath them. |
| Postgres booking constraint | Deferred to Stream 4 | The `EXCLUDE USING gist` constraint that `db/driver.py` documents lands when the Booking model has settled. |
| Deployment | Local only | Nothing is deployed soon; compose + localhost callbacks are sufficient. |

## Stream boundary contract

Three files are unavoidably shared. The rule for each:

| File | Rule |
|---|---|
| `app/backend/requirements.txt` | Append only, never reorder or reformat. A trivial conflict at worst. |
| `app/backend/app/main.py` | **Additive lines only** — `include_router` and the exception handler. Stream 1's tasks 1.3/1.4 add booking routes here. CORS middleware: add only if absent, since 1.3 may land it first. |
| `app/frontend/*` | **Untouched during Phase A.** Phase B starts only after Stream 1's 1.5–1.8 have merged. |

Everything else Stream 2 writes lives in new directories Stream 1 never opens: `app/backend/app/auth/`,
`app/backend/app/identity/`, `app/backend/alembic/`, `app/backend/scripts/`, and the root
`docker-compose.yml`.

---

## Phase A — Backend (start now; zero Stream 1 file overlap)

- [x] **2.1 — Postgres + Alembic foundation.** _(DONE — merged as `7fbdbbf`)_ Root `docker-compose.yml` with Postgres 16 (named
  volume, dev credentials). Append `psycopg[binary]`, `alembic`, `pydantic-settings` to
  `requirements.txt`. New `app/backend/app/settings.py` — a `pydantic-settings` `Settings` reading
  `DATABASE_URL`, `AUTH0_DOMAIN`, `AUTH0_API_AUDIENCE`, CORS origins from env/`.env`. New
  `app/backend/app/db/session.py` with engine, `sessionmaker`, and a `get_session` dependency.
  Alembic in `app/backend/alembic/`, `env.py` wired to `Base.metadata` **with an `include_object`
  filter restricting autogenerate to Stream 2's tables** — so `bookings` is never picked up. Add a
  `postgres:16` service to the `backend` job in `.github/workflows/ci.yml`.
  Tests: `alembic upgrade head` then `downgrade base` runs clean. Postgres tests **skip** when
  `DATABASE_URL` is unset, so Stream 1's SQLite suite still runs standalone.

- [x] **2.2 — Identity schema.** _(DONE — merged as `20a301c`)_ New `app/backend/app/identity/models.py`, sharing `Base` from
  `app/db/models.py` and reusing its `UtcDateTime` and `utcnow` rather than redefining them:
  - `users` — `id`, `auth0_sub` (unique), `email`, `name`, `created_at`, `last_login_at`.
  - `spaces` — `id`, `public_id` (unique, unguessable), `name`, `description`,
    `created_by_user_id`, `created_at`, `archived_at` (nullable).
  - `space_memberships` — `space_id`, `user_id`, `role`, `created_at`; unique `(space_id, user_id)`.
  - `space_access_requests` — `space_id`, `user_id`, `status` (`pending|approved|denied`), `message`,
    `decided_by_user_id`, timestamps; **partial unique index** allowing at most one *pending* request
    per user per Space while preserving decided ones as history.
  - `space_invitations` — `space_id`, `email` (lowercased), `role`, `status`
    (`pending|accepted|revoked`), `invited_by_user_id`, timestamps; partial unique on pending.

  Alembic migration. Tests cover every uniqueness constraint and both partial indexes — specifically
  that a second pending row is rejected but a new pending row *after* a denial is allowed.

- [x] **2.3 — JWT verification _(DONE — merged as `c547ba6`)_ + current-user dependency.** Append `pyjwt[crypto]`. New
  `app/backend/app/auth/jwt.py`: cached JWKS fetch, RS256 only, `aud` checked against
  `AUTH0_API_AUDIENCE` and `iss` against the tenant, `exp`/`nbf` with small leeway. New
  `app/backend/app/auth/dependencies.py` with `get_current_user` — verifies the bearer token, upserts
  the `users` row, refreshes `last_login_at`, and **claims any pending invitation matching the
  verified email**, creating the membership at the invited role. Additive wiring in `main.py`: an
  auth-error handler returning **401 not 500**, and `GET /me`.

  **Security requirement — an invitation may only be claimed when the token's `email_verified` claim
  is `true`.** Task 2.2 deliberately made `users.email` non-unique, and correctly so: Auth0 issues
  distinct `sub` values for a database signup and a Google login of the same address, so a unique
  constraint would turn an ordinary second login into a hard failure. The consequence is that an
  email address does **not** identify a person. Without an `email_verified` gate, anyone could sign
  up through the database connection using a victim's address, never confirm it, and inherit every
  Space that victim was invited to — a full account-takeover path into private Spaces. An unverified
  address must claim nothing and fall back to the normal access-request flow. A test must assert
  exactly this: an unverified token matching a pending invitation yields **no** membership and
  leaves the invitation `pending`.
  Tests generate an RSA keypair in-process and serve a stub JWKS — no network, no secrets. Cover as
  **separate assertions**: valid; expired; wrong audience; wrong issuer; unknown signing key;
  malformed header; missing header; `alg: none` rejected; HS256-signed-with-the-RSA-public-key
  rejected. A single blanket "401 on bad token" test would pass against a verifier that rejects
  everything, so each rejection reason is asserted independently.

- [x] **2.4 — Auth0 tenant provisioning script.** _(DONE — merged as `e65f3b7`)_ `app/backend/scripts/auth0_provision.py`, driven by
  the M2M credentials in `.env`. Idempotent — read, then create *or* update; never blind-create:
  - Resource server / API `https://api.open-skej.dev`, RS256, RBAC enabled.
  - SPA application `open-skej-web` with `http://localhost:5173` callback, logout and web-origin URLs.
  - Enable the database connection **and `google-oauth2`** for that client.
  - Print the resulting client id and audience as paste-ready `.env` lines.

  Ships with `--dry-run` printing intended calls without issuing them. Unit tests run against a
  mocked Management API and must prove idempotency: a second run issues `PATCH`, never a duplicate
  `POST`. The client secret is never logged or echoed.

- [x] **2.5 — Space endpoints + authorization dependency.** _(DONE — merged as `cfac771`)_ New `app/backend/app/identity/router.py`:
  - `POST /spaces` — creator becomes `owner`; returns the shareable `public_id` link.
  - `GET /spaces` — only Spaces I belong to. Archived excluded unless `?include_archived=true`.
  - `GET /spaces/{public_id}` — full detail, **members only**; `404` otherwise.
  - `GET /spaces/{public_id}/preview` — the link-holder view: name, description, and my own status
    (`none | pending | denied | member`). No member list, no bookings.
  - `PATCH /spaces/{public_id}` — admin+. `POST /spaces/{public_id}/archive` — **owner only**.
  - `GET /spaces/{public_id}/members`, plus `PATCH`/`DELETE` on a membership (admin+).

  A reusable `require_space_role(minimum)` dependency returning **404, not 403**, for a Space the
  caller has no relationship with — a 403 confirms the Space exists and undermines the unguessable
  link. Enforce the **last-owner invariant**: the final owner cannot be demoted or removed.
  Tests include a parametrised **cross-tenant isolation** case: a member of Space A gets 404 on every
  Space B route except `/preview`, and archived Spaces reject mutations.

- [x] **2.6 — Access requests.** _(DONE — merged as `dc30e77`)_ `POST /spaces/{public_id}/access-requests` — any authenticated user
  holding the link; rejects a duplicate pending request, an existing member, and an archived Space.
  `GET /spaces/{public_id}/access-requests?status=` (admin+) and
  `POST /spaces/{public_id}/access-requests/{id}/approve|deny` (admin+). Approval creates the
  membership and stamps `decided_by_user_id` in **one transaction** — a test must confirm no request
  is ever left approved without its membership row. Tests cover the full lifecycle, re-requesting
  after a denial, and a plain member getting 403 on the decision routes.

  **Atomicity is proven by a negative control, not by the happy path.** The first implementation
  asserted the request/membership pairing only after a *successful* approval — which two separate
  commits satisfy just as well, since both writes land either way. `POST /access-requests` is
  deliberately reachable without membership (like `/preview`): requiring membership to ask for
  membership would make the door unopenable. The cross-tenant isolation sweep in `test_spaces_api.py`
  therefore moved from a path-suffix exclusion to an explicit `(method, path)` allowlist — `GET` on
  the access-request queue is admin-only while `POST` on the *same path* is not, and a suffix rule
  would have silently dropped that admin route out of the sweep.

  **Open product question:** approval grants `member` only; an admin cannot choose a higher role at
  approval time (promotion uses the existing membership route). Revisit if approval should take an
  optional role.

- [ ] **2.7 — Invitations.** `POST /spaces/{public_id}/invitations` (admin+, email + role),
  `GET .../invitations`, `DELETE .../invitations/{id}` to revoke. Emails stored lowercased, matched
  case-insensitively. No email is sent — the inviter shares the link. Tests prove the pre-approval
  path from 2.3 end to end: invite `x@y.com` → that user logs in → membership exists at the invited
  role with **no access request**, invitation marked `accepted`; and a revoked invitation grants
  nothing.

## Phase B — Frontend (gated: starts only once Stream 1's 1.6–1.8 have merged to `main`)

Stream 1's task 1.5 has already landed Tailwind, `src/config.ts` and a typed `src/api/` client on
`main`. Phase B **reuses** those rather than duplicating them, and rebases onto `main` before
starting. It waits on 1.6–1.8 (calendar grid, booking flow, cancel UI) so the auth shell wraps a
finished calendar instead of racing it.

- [ ] **2.8 — Frontend auth foundation.** Add `@auth0/auth0-react` and `react-router-dom`. Reuse
  Stream 1's Tailwind setup and extend its existing `src/api/` client — do **not** create a parallel
  one. New `src/auth/` with `Auth0Provider` wiring from `import.meta.env`, a `ProtectedRoute`, and
  login/logout controls (Google and email/password). Extend the api client to attach the access token
  via `getAccessTokenSilently` and surface 401/403/404 as distinguishable typed results. Append
  `VITE_AUTH0_*` to Stream 1's `.env.example`.

- [ ] **2.9 — Admin dashboard.** Route `/admin`: create a Space and copy its share link; view members
  and manage roles; review pending access requests with approve/deny; send and revoke invitations;
  archive a Space with confirmation. Admin controls hidden for plain members **and** enforced
  server-side — the UI is never the security boundary. Loading, empty and error states throughout,
  with stable `data-testid` hooks.

- [ ] **2.10 — Link-holder view + runbook.** A `/s/{public_id}` route rendering the preview for a cold
  link-holder with a "Request access" action and pending/denied/member states, redirecting members
  into the Space. Then a root `README.md` section: compose up Postgres, run migrations, run the
  provisioning script, required env vars, and the local login flow. Update `CLAUDE.md`'s stream table.

## Verification

- **Per task:** CI green — `black --check .`, `flake8 .`, `pytest` (line length 100 per
  `pyproject.toml`/`.flake8`); frontend `npm run lint` and `npm run build` in Phase B.
- **Stream 1 must stay green after every task.** Its SQLite suite runs unchanged; Postgres-only tests
  skip without `DATABASE_URL`. **If a Stream 2 change ever requires editing a Stream 1 test or a file
  in `app/db/`, that is a boundary violation — stop and surface it rather than editing.**
- **Auth cannot be bypassed (2.3):** applying Stream 1's negative-control discipline, once the suite
  is green, temporarily neuter signature verification and confirm the forged-token tests **fail**. A
  test suite that passes both with and without verification is proving nothing.
- **Link is the capability (2.5):** a test asserting `public_id` values are high-entropy and that no
  endpoint enumerates Spaces.
- **Invitation pre-approval (2.7):** invited login yields membership with zero access requests —
  distinguishing genuine pre-approval from an auto-approved request.
- **Live end-to-end (after 2.10):** compose up → `alembic upgrade head` → run the provisioning script
  → start both apps → log in via Google → create a Space → open its link as a second cold user →
  request access → approve → second user sees the Space. Then invite a third user and confirm they
  land inside with no request. This is the only step touching the live tenant.

## Handoff to Stream 4 (integration)

Deliberately left undone, so Stream 4 has a clean seam rather than a merge conflict:

1. **`bookings.resource_id` → `spaces.id` and `bookings.user_id` → `users.id`** as real foreign keys.
   Both stay free-text `String(64)` here; `app/db/constants.py` documents them as Stream 2's to
   replace, but doing it now would break Stream 1's in-flight tests.
2. **Bookings folded into Alembic**, including the
   `EXCLUDE USING gist (resource_id WITH =, tstzrange(start_at, end_at) WITH &&) WHERE (status = 'confirmed')`
   constraint that `app/db/driver.py` already specifies, plus the `PostgresBookingDriver`.
3. **Space-scoping `GET`/`POST /bookings`** behind `require_space_role` — booking mechanics belong to
   Stream 1, authorization to Stream 2, so the join is Stream 4's.
4. **What archiving a Space does to its future bookings** — Stream 2 only sets `archived_at`.
5. **Merging the calendar UI with the auth shell** so a Space's calendar renders inside its route.
