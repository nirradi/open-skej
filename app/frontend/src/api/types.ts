/**
 * Wire types and result types for the booking API.
 *
 * The types here mirror `app/backend/app/schemas.py`. When that file changes,
 * this one changes with it.
 */

/** Mirrors `BookingStatus` in `app/backend/app/db/models.py`. */
export type BookingStatus = 'confirmed' | 'cancelled'

/**
 * Mirrors `BookingRead` in `app/backend/app/schemas.py`.
 *
 * Timestamps are left as the ISO-8601 strings the server sends rather than being
 * parsed into `Date`s. The field names are snake_case for the same reason: this
 * is the wire shape verbatim, so a mismatch with the backend is a visible diff
 * against `schemas.py` rather than something hidden behind a mapping layer.
 */
export interface Booking {
  id: number
  resource_id: number
  user_id: number
  start_at: string
  end_at: string
  status: BookingStatus
  created_at: string
  /** Set only when `status` is `cancelled`. */
  cancelled_at: string | null
}

/**
 * The result variants a call can produce.
 *
 * ## Why these are shaped this way
 *
 * The status code alone cannot identify an outcome, and the API contract in
 * `stream-1-plan.md` is explicit about it:
 *
 * - **Two different 422s.** A rule denial carries `error: "rule_denied"` and a
 *   message written for a human. A malformed request is FastAPI's own validation
 *   failure and carries no `error` key at all. Rendering the second as if it were
 *   the first puts a Pydantic error dump in front of the user.
 * - **Two different 409s.** `overlap` means somebody else took the slot and the
 *   calendar on screen is stale. `already_cancelled` means the user's own cancel
 *   already landed — a double-clicked button, which the UI should treat as
 *   success, not as a collision.
 *
 * So callers never see a status code. Each request function returns a union of
 * only the variants that request can actually produce, discriminated on
 * `outcome`. A caller that `switch`es on `outcome` gets exhaustiveness checking
 * from TypeScript, and cannot write the `status === 409` branch that would
 * conflate the two conflicts, because there is no status to branch on.
 */

/** The request succeeded. */
export interface ApiOk<T> {
  outcome: 'ok'
  data: T
}

/** 422 + `error: "rule_denied"` — the rule engine refused. Nothing was written. */
export interface ApiRuleDenied {
  outcome: 'rule_denied'
  /** The rule engine's copy, written to be shown to the user verbatim. */
  message: string
}

/** 409 + `error: "overlap"` — the interval is already taken. Refetch the calendar. */
export interface ApiOverlap {
  outcome: 'overlap'
  message: string
}

/**
 * The thing addressed by the URL is not there — **or is not yours**.
 *
 * Produced two ways, deliberately collapsed into one variant because the caller
 * can do nothing different about them:
 *
 * - 404 + `error: "not_found"` — no booking with that id.
 * - a bare 404 with no discriminator, which is how every Space route answers a
 *   caller with no relationship to that Space.
 *
 * ## Do not phrase this as "you lack access"
 *
 * `require_space_role` in `app/backend/app/identity/authz.py` returns **404, not
 * 403**, for a Space the caller is not a member of. That is not sloppiness: a
 * Space's `public_id` link *is* the capability, so a 403 would confirm that an
 * unguessable id exists and turn the id space into something worth probing.
 *
 * The consequence for the UI is that a 404 from a Space route means "no such
 * Space, **or** not yours" and the two are indistinguishable from here. Copy
 * must therefore stay agnostic — "We couldn't find that Space" is right;
 * "This Space exists but you don't have access" both leaks the fact the backend
 * is spending a status code to hide, and is wrong half the time. Membership is
 * something the user *asks* for via the preview route, not something this
 * variant should assert.
 */
export interface ApiNotFound {
  outcome: 'not_found'
  message: string
}

/**
 * 401 — the server did not accept us as anyone.
 *
 * Means the access token was absent, expired, or rejected. Distinct from
 * `forbidden` because the remedy is different and the UI branch is different:
 * this one is fixed by logging in again, so it should send the user to the
 * login controls rather than telling them they are not allowed.
 *
 * Also produced **without a round trip** when the token provider itself fails —
 * see `setAccessTokenProvider` in `client.ts`. A silent-auth failure and a
 * rejected token are the same fact from the caller's side ("we could not prove
 * who you are"), and folding them together means a caller cannot forget one.
 */
export interface ApiUnauthenticated {
  outcome: 'unauthenticated'
  /** Generic copy safe to show the user. */
  message: string
}

/**
 * 403 — we know who you are, and the answer is still no.
 *
 * Reached when the caller *is* a member of the Space but not at a high enough
 * role: an ordinary member hitting an admin-only route. Unlike `not_found`,
 * this one confirms the Space exists — which is fine, because to get a 403 at
 * all the caller must already be a member of it.
 *
 * Logging in again will not help, so the UI must not offer that as the fix.
 */
export interface ApiForbidden {
  outcome: 'forbidden'
  message: string
}

/**
 * 409 + `error: "already_cancelled"` — that booking was already cancelled.
 *
 * Benign: the desired end state already holds. Task 1.8 treats this as success.
 */
export interface ApiAlreadyCancelled {
  outcome: 'already_cancelled'
  message: string
}

/**
 * 409 + `error: "space_archived"` — a create against a Resource whose Space is
 * archived.
 *
 * Distinct from `overlap` even though both are 409 on the same route: an
 * overlap means someone else got the slot and a refetch is worth trying, this
 * means the whole venue is closed and no slot on it will ever open. Terminal,
 * not a race — the UI should stop offering the form rather than inviting a
 * retry. Mirrors `BookingSpaceArchived` in `app/backend/app/schemas.py`.
 */
export interface ApiSpaceArchived {
  outcome: 'space_archived'
  message: string
}

/**
 * 409 + `error: "already_started"` — a cancel against a booking whose start
 * time has already passed.
 *
 * Distinct from `already_cancelled`: that one is benign (the desired end state
 * already holds), this one has no remedy at all — the interval is under way
 * and cannot be released. Mirrors `BookingAlreadyStarted` in
 * `app/backend/app/schemas.py`.
 */
export interface ApiAlreadyStarted {
  outcome: 'already_started'
  message: string
}

/**
 * The server rejected the request as malformed — a 422 with no `error` key
 * (FastAPI request validation) or a 400 (bad window, naive datetime).
 *
 * This is a bug in the client, not something the user did, so `detail` is
 * diagnostic text for a developer and should **not** be rendered as friendly
 * copy the way `rule_denied.message` is.
 */
export interface ApiInvalidRequest {
  outcome: 'invalid_request'
  /** A flattened, human-readable rendering of the server's `detail`. */
  detail: string
  /** The raw body, for logging. */
  raw: unknown
}

/**
 * 409 with no `error` key — the request was understood and refused on a rule.
 *
 * Every domain refusal in `app/backend/app/identity/router.py` is one of these:
 * demoting the last owner, mutating an archived Space, inviting an address that
 * is already a member, revoking an invitation that was already accepted. They
 * are raised as a bare `HTTPException`, so unlike the booking endpoints' two
 * 409s they carry **no discriminator** — the status plus the prose is the whole
 * message.
 *
 * ## Why `message` forwards the server's `detail`, when 401/403/404 do not
 *
 * `classifyByStatus` deliberately drops the server's `detail` for the three
 * access statuses, because there it is diagnostic text about a token aimed at
 * whoever holds it. The 409s are the opposite: `LAST_OWNER_DETAIL` reads "This
 * Space must always have at least one owner. Promote another member to owner
 * before changing this one." That is product copy, written for the admin who
 * just clicked the button, and it names the remedy. Replacing it with
 * "Something went wrong on our end" would turn a precise, actionable refusal
 * into a bug report — the user would retry the same click forever.
 *
 * So the rule is per-status rather than global: the server owns the copy where
 * the server is the only thing that knows the rule, and this client owns it
 * where the server's text is about plumbing.
 */
export interface ApiConflict {
  outcome: 'conflict'
  /** The server's own copy, written for the user and shown verbatim. */
  message: string
}

/**
 * The request never produced a recognised answer: the network failed, the
 * response was not JSON, or the server returned a status this client does not
 * model (a 500, say).
 */
export interface ApiFailure {
  outcome: 'failed'
  /** Generic copy safe to show the user; the specifics are in `cause`. */
  message: string
  cause?: unknown
}

/** `GET /bookings` — nothing user-facing can go wrong, only client or infra bugs. */
export type ListBookingsResult = ApiOk<Booking[]> | ApiInvalidRequest | ApiFailure

/** `POST /bookings` — both flavours of "no" are distinct variants. */
export type CreateBookingResult =
  ApiOk<Booking> | ApiRuleDenied | ApiOverlap | ApiInvalidRequest | ApiFailure

/** `DELETE /bookings/{id}` — note `already_cancelled` is separate from `not_found`. */
export type CancelBookingResult =
  ApiOk<Booking> | ApiNotFound | ApiAlreadyCancelled | ApiInvalidRequest | ApiFailure

/**
 * The outcomes any **authenticated** endpoint can produce.
 *
 * Every Stream 2 route sits behind `get_current_user`, so all three access
 * outcomes are reachable on all of them and there is nothing to gain from
 * hand-writing the same five-member union per endpoint. An endpoint that adds a
 * genuine domain outcome (a discriminated `error`) declares its own union
 * instead, the way `CreateBookingResult` does — this alias is the floor, not a
 * ceiling.
 *
 * The *unscoped* booking endpoints (`listBookings`, `createBooking`,
 * `cancelBooking`) deliberately do **not** use it and never will: they are the
 * single-user Stream 1 contract, unauthenticated, and task 4.11 deletes them.
 * Folding `unauthenticated` into `ListBookingsResult` would add a branch the
 * server cannot produce and a component (`BookingPanel`, `CancelPanel`) would
 * have to be edited to ignore.
 *
 * The *resource-scoped* routes below (`listResourceBookings` and friends) are
 * the ones Stream 4 actually authenticates, and this is the floor they build
 * on — `ListResourceBookingsResult` is exactly this alias, and
 * `CreateResourceBookingResult` / `CancelResourceBookingResult` widen it with
 * their own domain discriminators the same way `CreateBookingResult` does.
 */
export type AuthenticatedResult<T> =
  ApiOk<T> | ApiUnauthenticated | ApiForbidden | ApiNotFound | ApiInvalidRequest | ApiFailure

/**
 * `AuthenticatedResult` plus the domain refusal, for routes that **write**.
 *
 * A separate alias rather than folding `conflict` into `AuthenticatedResult`
 * itself, because that alias documents itself as "the floor, not a ceiling" and
 * says an endpoint with a genuine extra outcome should declare its own union.
 * A 409 is exactly that: no `GET` in the Space API can produce one, so widening
 * the floor would put a permanently dead branch into `getCurrentUser`,
 * `listMembers` and every other read — and a dead branch in an exhaustive
 * `switch` is a branch a reader has to prove is dead before they can ignore it.
 */
export type MutatingResult<T> = AuthenticatedResult<T> | ApiConflict

/** Mirrors `MembershipRole` in `app/backend/app/identity/models.py`. */
export type MembershipRole = 'owner' | 'admin' | 'member'

/** Mirrors `AccessRequestStatus` in `app/backend/app/identity/models.py`. */
export type AccessRequestStatus = 'pending' | 'approved' | 'denied'

/** Mirrors `InvitationStatus` in `app/backend/app/identity/models.py`. */
export type InvitationStatus = 'pending' | 'accepted' | 'revoked'

/** Mirrors `PreviewStatus` in `app/backend/app/identity/schemas.py`. */
export type PreviewStatus = 'none' | 'pending' | 'denied' | 'member'

/**
 * Mirrors `SpaceRead` — a Space as seen from inside, by a member.
 *
 * **There is no `id` field, and adding one would be a security bug.** The
 * backend's integer primary key is sequential and therefore enumerable;
 * `public_id` is a 128-bit random token that *is* the capability granting access
 * to the Space. `schemas.py` structurally refuses to serialise the integer, and
 * this type mirrors that refusal so nothing downstream can start depending on
 * one appearing.
 *
 * `my_role` travels with the Space so the UI can decide which controls to render
 * without a second round trip. It is a convenience and never a security
 * boundary — every privileged route re-checks the role server-side, so hiding a
 * button on it is a tidiness measure, not an access control.
 *
 * `archived_at` non-null means the Space is closed: the server rejects every
 * mutation on it with a 409, so the UI should stop offering them.
 */
export interface Space {
  public_id: string
  name: string
  description: string | null
  created_at: string
  archived_at: string | null
  my_role: MembershipRole
}

/**
 * Mirrors `SpacePreview` — the thin view for someone holding the link.
 *
 * Deliberately carries no member list and no counts. Task 2.10 owns the screen
 * that renders it; it lives here because it is part of the same wire contract.
 */
export interface SpacePreview {
  public_id: string
  name: string
  description: string | null
  status: PreviewStatus
}

/** Mirrors `MemberRead` — one membership, visible to people inside the Space. */
export interface Member {
  user_id: number
  email: string
  name: string | null
  role: MembershipRole
  created_at: string
}

/**
 * Mirrors `AccessRequestRead` — one request in the admin review queue.
 *
 * `email` and `name` are joined in by the server precisely because this is the
 * screen where an admin decides whether a stranger gets in, and a bare
 * `user_id` gives them nothing to decide on.
 */
export interface AccessRequest {
  id: number
  user_id: number
  email: string
  name: string | null
  status: AccessRequestStatus
  message: string | null
  created_at: string
  decided_at: string | null
  decided_by_user_id: number | null
}

/**
 * Mirrors `InvitationRead` — one invitation, visible to admins only.
 *
 * There is no invitation token or per-invitation link here, and that is not an
 * omission: the invitee is admitted by the verified address on their token, so
 * the thing an inviter shares is the Space's ordinary `public_id` link. A
 * per-invitation secret would be a second capability to leak.
 */
export interface Invitation {
  id: number
  email: string
  role: MembershipRole
  status: InvitationStatus
  invited_by_user_id: number
  created_at: string
  accepted_at: string | null
}

/**
 * Mirrors the `GET /me` body in `app/backend/app/main.py`.
 *
 * `id` is our own users-table id, not the Auth0 `sub`. The `sub` stays on the
 * server: it is the join key for a just-in-time upsert and the UI has no use
 * for it, so it is not sent.
 */
export interface CurrentUser {
  id: number
  email: string | null
  name: string | null
  /** ISO-8601, or `null` for a user who has somehow never completed a login. */
  last_login_at: string | null
}

/**
 * `GET /me` — the "is my token still good?" probe.
 *
 * `not_found` and `forbidden` are structurally possible but should never occur:
 * the route upserts the user it is asked about, so a verified token always has a
 * row. They are present because the union describes the transport, and a caller
 * that `switch`es exhaustively is better off with a dead branch than with a
 * `default` that would also swallow a real one.
 */
export type GetCurrentUserResult = AuthenticatedResult<CurrentUser>

/**
 * The resource-scoped booking routes — `/spaces/{public_id}/resources/{id}/bookings`.
 *
 * Mirrors `app/backend/app/routers/resource_bookings.py`. Where the unscoped
 * `ListBookingsResult` / `CreateBookingResult` / `CancelBookingResult` above are
 * frozen at the single-user Stream 1 contract, these are authenticated behind
 * `require_space_role` and so carry the full access floor — `unauthenticated`,
 * `forbidden`, `not_found` — on top of each route's own domain outcomes.
 *
 * `not_found` here is doubly collapsed, the same way `ApiNotFound` already
 * documents for the Space API: it is the *Resource's* parent Space not being
 * the caller's (a bare 404 from `require_space_role`), a `resource_id` that
 * exists but belongs to another Space (also a bare 404, resolved in the same
 * query), **and** — on the cancel route only — a `booking_id` that does not
 * exist or belongs to a different Resource (a discriminated 404). All three
 * collapse into one variant because the UI's remedy is identical for each:
 * there is nothing here for you to act on.
 */

/** `GET .../bookings` — a read, so only the access floor, nothing domain-specific. */
export type ListResourceBookingsResult = AuthenticatedResult<Booking[]>

/**
 * `POST .../bookings` — the access floor plus the three ways a create is
 * refused: the rule engine denies it, the slot is already taken, or the
 * Resource's Space is archived and takes no new bookings.
 */
export type CreateResourceBookingResult =
  | ApiOk<Booking>
  | ApiRuleDenied
  | ApiOverlap
  | ApiSpaceArchived
  | ApiUnauthenticated
  | ApiForbidden
  | ApiNotFound
  | ApiInvalidRequest
  | ApiFailure

/**
 * `DELETE .../bookings/{id}` — the access floor plus the three ways a cancel is
 * refused. `already_cancelled` is benign (the desired end state already
 * holds); `already_started` has no remedy at all, unlike the unscoped routes,
 * which have no such check.
 */
export type CancelResourceBookingResult =
  | ApiOk<Booking>
  | ApiNotFound
  | ApiAlreadyCancelled
  | ApiAlreadyStarted
  | ApiUnauthenticated
  | ApiForbidden
  | ApiInvalidRequest
  | ApiFailure
