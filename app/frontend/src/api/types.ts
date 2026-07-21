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
  resource_id: string
  user_id: string
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
 * The booking endpoints deliberately do **not** use it. They are still the
 * single-user Stream 1 contract and none of them is authenticated yet; folding
 * `unauthenticated` into `ListBookingsResult` today would add a branch the
 * server cannot currently produce. Stream 4 space-scopes bookings behind
 * `require_space_role`, and that is the change that should widen those unions.
 */
export type AuthenticatedResult<T> =
  ApiOk<T> | ApiUnauthenticated | ApiForbidden | ApiNotFound | ApiInvalidRequest | ApiFailure

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
