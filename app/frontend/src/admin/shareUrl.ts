/**
 * Builds the shareable URL for a Space.
 *
 * Points at `/s/{public_id}`, the link-holder route task 2.10 owns. Naming it
 * here before that route exists is deliberate: this link *is* the capability —
 * it is the only way anyone else reaches the Space, since nothing enumerates
 * Spaces — so the admin needs to be able to copy it the moment the Space is
 * created, and the URL shape is already fixed by the plan.
 *
 * Absolute rather than a bare path, because the entire point is to paste it into
 * a chat window where a relative path means nothing.
 *
 * It lives in its own module rather than beside `ShareLink` because a file that
 * exports both a component and a plain function loses React Fast Refresh for the
 * whole file — the lint rule that says so is the one warning this dashboard
 * would otherwise carry.
 */
export function spaceShareUrl(publicId: string, origin: string = window.location.origin): string {
  return `${origin}/s/${publicId}`
}
