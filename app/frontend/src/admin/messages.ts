/**
 * Turning a non-`ok` API outcome into something worth showing a person.
 *
 * Every panel in this dashboard needs the same translation, and doing it once
 * here is what stops six components from each inventing their own phrasing for
 * the same refusal.
 */

import type { ApiOk, MutatingResult } from '../api'

/** Any outcome other than success. */
export type FailedOutcome = Exclude<MutatingResult<unknown>, ApiOk<unknown>>

/**
 * Copy for a client bug, shown in place of `invalid_request`'s `detail`.
 *
 * `detail` is a flattened Pydantic error — "body.email: value is not a valid
 * email address" — which is diagnostic text for whoever wrote the request, not
 * for the admin who clicked a button. It goes to the console instead.
 */
const CLIENT_BUG_MESSAGE = 'Something went wrong on our end. Please try again.'

/**
 * The user-facing sentence for a failed result.
 *
 * Every variant except `invalid_request` already carries copy written for a
 * person: `conflict` carries the server's own explanation of which rule refused
 * (and, for the last-owner case, what to do about it), and the access outcomes
 * carry deliberately generic text from the client.
 *
 * **`not_found` is the one to be careful with.** On a Space route it means "no
 * such Space, *or* not yours" — the backend spends a 404 rather than a 403
 * precisely so an outsider cannot confirm that an unguessable id exists. The
 * client's copy for it says only "We couldn't find that", and this function
 * passes it through unchanged. Nothing here may sharpen it into "you don't have
 * access to this Space", which would leak the fact the 404 is paid for.
 */
export function messageFor(result: FailedOutcome): string {
  if (result.outcome === 'invalid_request') {
    // Kept out of the UI but not out of existence: a contract drift between this
    // client and `schemas.py` is a real bug and should be findable.
    console.error('Space API rejected a request as malformed:', result.detail, result.raw)
    return CLIENT_BUG_MESSAGE
  }

  return result.message
}

/** Human-readable label for a role, for buttons and selects. */
export const ROLE_LABELS = {
  owner: 'Owner',
  admin: 'Admin',
  member: 'Member',
} as const

/**
 * The roles a caller may assign, given their own.
 *
 * Mirrors the server rule that nobody may grant a role above their own, and
 * that only an owner may hand out `owner`. **This is a convenience, not a
 * boundary** — `update_member_role` and `create_invitation` both re-check it,
 * and every caller of this function still handles the `forbidden` outcome.
 * Hiding an option the server would reject just spares the admin a pointless
 * refusal; it is not what stops them.
 */
export function assignableRoles(actorRole: 'owner' | 'admin' | 'member') {
  if (actorRole === 'owner') return ['owner', 'admin', 'member'] as const
  if (actorRole === 'admin') return ['admin', 'member'] as const
  return [] as const
}
