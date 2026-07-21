/**
 * Role vocabulary for the admin dashboard.
 *
 * The result-to-copy translation every panel here uses moved to
 * `src/ui/messages.ts` when the link-holder screen started needing it too, and
 * is re-exported below so the panels' imports stay pointed at their own
 * directory. One implementation, one statement of the `not_found` rule.
 */

export { messageFor } from '../ui/messages'
export type { FailedOutcome } from '../ui/messages'

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
