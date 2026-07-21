/**
 * Shared fixtures for the admin dashboard tests.
 *
 * Test-only: nothing in the application imports this, and the bundler drops it.
 * It lives beside the components rather than under a `__fixtures__` directory so
 * that a drift between `Space` here and `Space` in `types.ts` is a compile error
 * in the same `tsc -b` that builds the app.
 *
 * The builders take overrides rather than being constants because almost every
 * assertion cares about exactly one field — `my_role`, `archived_at`, a
 * `status` — and spelling out the other five each time buries which one the test
 * is actually about.
 */

import type {
  AccessRequest,
  ApiConflict,
  ApiFailure,
  ApiForbidden,
  ApiOk,
  Invitation,
  Member,
  Space,
} from '../api'

export function ok<T>(data: T): ApiOk<T> {
  return { outcome: 'ok', data }
}

/**
 * The server's verbatim copy for refusing to unseat the final owner.
 *
 * Copied from `LAST_OWNER_DETAIL` in `app/backend/app/identity/router.py`. It is
 * duplicated here on purpose: the point of the tests that use it is that this
 * *specific sentence* reaches the screen instead of generic failure copy, and
 * asserting on a value the test itself invented would prove only that a string
 * survives a round trip. If the backend rewords it, these tests should be
 * updated deliberately, not silently follow along.
 */
export const LAST_OWNER_MESSAGE =
  'This Space must always have at least one owner.' +
  ' Promote another member to owner before changing this one.'

export function conflict(message: string = LAST_OWNER_MESSAGE): ApiConflict {
  return { outcome: 'conflict', message }
}

export function forbidden(message = "You don't have permission to do that."): ApiForbidden {
  return { outcome: 'forbidden', message }
}

export function failed(message = 'Something went wrong. Please try again.'): ApiFailure {
  return { outcome: 'failed', message }
}

export function makeSpace(overrides: Partial<Space> = {}): Space {
  return {
    public_id: 'sp_7f3a9c',
    name: 'Tennis Court',
    description: null,
    created_at: '2026-07-01T10:00:00.000Z',
    archived_at: null,
    my_role: 'owner',
    ...overrides,
  }
}

export function makeMember(overrides: Partial<Member> = {}): Member {
  return {
    user_id: 1,
    email: 'ada@example.com',
    name: 'Ada Lovelace',
    role: 'owner',
    created_at: '2026-07-01T10:00:00.000Z',
    ...overrides,
  }
}

export function makeAccessRequest(overrides: Partial<AccessRequest> = {}): AccessRequest {
  return {
    id: 10,
    user_id: 2,
    email: 'grace@example.com',
    name: 'Grace Hopper',
    status: 'pending',
    message: null,
    created_at: '2026-07-02T10:00:00.000Z',
    decided_at: null,
    decided_by_user_id: null,
    ...overrides,
  }
}

export function makeInvitation(overrides: Partial<Invitation> = {}): Invitation {
  return {
    id: 20,
    email: 'alan@example.com',
    role: 'member',
    status: 'pending',
    invited_by_user_id: 1,
    created_at: '2026-07-03T10:00:00.000Z',
    accepted_at: null,
    ...overrides,
  }
}
