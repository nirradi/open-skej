/**
 * Turning a non-`ok` API outcome into something worth showing a person.
 *
 * Lives here rather than under `src/admin/` because two unrelated screens now
 * need it: the dashboard, and the `/s/{public_id}` link-holder view — which is
 * the one screen in the app that a stranger sees. One implementation is what
 * stops seven components from each inventing their own phrasing for the same
 * refusal, and — more importantly — what keeps the `not_found` rule below stated
 * in exactly one place.
 */

import type { ApiOk, MutatingResult } from '../api'

/** Any outcome other than success. */
export type FailedOutcome = Exclude<MutatingResult<unknown>, ApiOk<unknown>>

/**
 * Copy for a client bug, shown in place of `invalid_request`'s `detail`.
 *
 * `detail` is a flattened Pydantic error — "body.email: value is not a valid
 * email address" — which is diagnostic text for whoever wrote the request, not
 * for the person who clicked a button. It goes to the console instead.
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
