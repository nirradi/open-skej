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

/** 404 + `error: "not_found"` — no booking with that id. */
export interface ApiNotFound {
  outcome: 'not_found'
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
