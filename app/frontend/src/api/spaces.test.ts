/**
 * Tests for the Space API client — the calls task 2.9's dashboard is built on.
 *
 * Two things are under test here, and the second is the interesting one.
 *
 * The first is ordinary: URL, method and body for each call, so a typo in a path
 * fails here rather than as a mystery 404 in the UI.
 *
 * The second is the **409 story**. Every domain refusal in the Space API is a
 * bare `HTTPException` — no `error` discriminator, just a status and prose — so
 * the client has to read the status to find them, which is exactly the thing
 * `client.test.ts` says the client refuses to do for the booking endpoints. The
 * two are reconciled by ordering: the discriminator is checked first, so a
 * booking's `overlap` (409 *with* an `error` key) is claimed before the status
 * check can see it. `keeps a booking conflict away from the Space conflict`
 * below pins that, because it is the assertion that fails if someone
 * "simplifies" the classification by moving the status check earlier.
 *
 * `fetch` is mocked throughout — nothing here touches a real server.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  API_BASE_URL,
  approveAccessRequest,
  archiveSpace,
  createInvitation,
  createSpace,
  denyAccessRequest,
  getSpace,
  listAccessRequests,
  listInvitations,
  listMembers,
  listSpaces,
  removeMember,
  revokeInvitation,
  updateMemberRole,
} from './client'
import { createBooking } from './client'
import type { AccessRequest, Invitation, Member, Space } from './types'

const space: Space = {
  public_id: 'aBcDeFgHiJkLmNoPqRsTuV',
  name: 'Tennis court',
  description: 'The one by the car park',
  created_at: '2026-07-01T09:00:00Z',
  archived_at: null,
  my_role: 'owner',
}

const member: Member = {
  user_id: 3,
  email: 'alice@example.com',
  name: 'Alice',
  role: 'member',
  created_at: '2026-07-02T09:00:00Z',
}

const accessRequest: AccessRequest = {
  id: 11,
  user_id: 4,
  email: 'bob@example.com',
  name: 'Bob',
  status: 'pending',
  message: "I'm on the Tuesday team",
  created_at: '2026-07-03T09:00:00Z',
  decided_at: null,
  decided_by_user_id: null,
}

const invitation: Invitation = {
  id: 5,
  email: 'carol@example.com',
  role: 'member',
  status: 'pending',
  invited_by_user_id: 1,
  created_at: '2026-07-04T09:00:00Z',
  accepted_at: null,
}

/** The last-owner refusal, exactly as `router.py` raises it: 409, bare `detail`. */
const LAST_OWNER_DETAIL =
  'This Space must always have at least one owner.' +
  ' Promote another member to owner before changing this one.'

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

/** A 204: no body at all, so `json()` throws the way a real empty response does. */
function noContentResponse(): Response {
  return {
    ok: true,
    status: 204,
    json: async () => {
      throw new SyntaxError('Unexpected end of JSON input')
    },
  } as unknown as Response
}

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

/** The URL and `RequestInit` the client passed to `fetch` on its last call. */
function lastRequest(): { url: string; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit]
  return { url, init }
}

describe('createSpace', () => {
  it('posts the name and description and returns the Space', async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, space))

    const result = await createSpace('Tennis court', 'The one by the car park')

    const { url, init } = lastRequest()
    expect(url).toBe(`${API_BASE_URL}/spaces`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({
      name: 'Tennis court',
      description: 'The one by the car park',
    })
    expect(result).toEqual({ outcome: 'ok', data: space })
  })

  it('sends an explicit null description when none is given', async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, space))

    await createSpace('Tennis court')

    expect(JSON.parse(lastRequest().init.body as string)).toEqual({
      name: 'Tennis court',
      description: null,
    })
  })
})

describe('listSpaces', () => {
  it('asks only for live Spaces by default', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, [space]))

    const result = await listSpaces()

    expect(lastRequest().url).toBe(`${API_BASE_URL}/spaces`)
    expect(result).toEqual({ outcome: 'ok', data: [space] })
  })

  it('opts into archived Spaces when asked', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    await listSpaces({ includeArchived: true })

    expect(lastRequest().url).toBe(`${API_BASE_URL}/spaces?include_archived=true`)
  })

  it('resolves to unauthenticated on a 401 rather than throwing', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'Signature verification failed' }))

    const result = await listSpaces()

    expect(result.outcome).toBe('unauthenticated')
    // The server's diagnostic text is about a token, not about the person who
    // left a tab open overnight, so it must not reach the screen.
    if (result.outcome === 'unauthenticated') {
      expect(result.message).not.toContain('Signature')
    }
  })
})

describe('getSpace', () => {
  it('resolves a 404 to not_found without claiming the Space exists', async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { detail: 'No such Space.' }))

    const result = await getSpace('nope')

    expect(result.outcome).toBe('not_found')
  })

  it('percent-encodes the public id', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, space))

    await getSpace('a/b?c')

    expect(lastRequest().url).toBe(`${API_BASE_URL}/spaces/a%2Fb%3Fc`)
  })
})

describe('listMembers', () => {
  it('returns the member list', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, [member]))

    const result = await listMembers(space.public_id)

    expect(lastRequest().url).toBe(`${API_BASE_URL}/spaces/${space.public_id}/members`)
    expect(result).toEqual({ outcome: 'ok', data: [member] })
  })
})

describe('updateMemberRole', () => {
  it('patches the role', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ...member, role: 'admin' }))

    const result = await updateMemberRole(space.public_id, 3, 'admin')

    const { url, init } = lastRequest()
    expect(url).toBe(`${API_BASE_URL}/spaces/${space.public_id}/members/3`)
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(init.body as string)).toEqual({ role: 'admin' })
    expect(result.outcome).toBe('ok')
  })

  it('surfaces the last-owner refusal as a conflict carrying the server copy', async () => {
    // The point of the `conflict` variant. Before it existed this fell through
    // to `failed` and the admin was told "Something went wrong on our end" —
    // which is not what happened, gives them nothing to act on, and invites them
    // to retry the identical click forever. The server's own sentence names the
    // remedy, so it is the thing worth showing.
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: LAST_OWNER_DETAIL }))

    const result = await updateMemberRole(space.public_id, 3, 'member')

    expect(result).toEqual({ outcome: 'conflict', message: LAST_OWNER_DETAIL })
  })

  it('surfaces an owner-authority refusal as forbidden, not as a conflict', async () => {
    // A different refusal with a different remedy: this admin will never be able
    // to do it, whereas the last-owner conflict clears once someone else is
    // promoted. Collapsing the two would mislead in both directions.
    fetchMock.mockResolvedValue(
      jsonResponse(403, { detail: 'Only an owner can grant the owner role.' }),
    )

    const result = await updateMemberRole(space.public_id, 3, 'owner')

    expect(result.outcome).toBe('forbidden')
  })

  it('falls back to failed for a 409 with no readable detail', async () => {
    // `conflict` promises a real explanation. A 409 that carries none has
    // nothing to say, and inventing "something went wrong" while labelling it a
    // conflict would be a failure wearing a conflict's clothes.
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: { unexpected: 'shape' } }))

    const result = await updateMemberRole(space.public_id, 3, 'member')

    expect(result.outcome).toBe('failed')
  })
})

describe('removeMember', () => {
  it('resolves a 204 to ok rather than choking on the empty body', async () => {
    // Regression guard. The client parses every response as JSON, so before 204
    // was handled explicitly a removal that fully succeeded surfaced as
    // `failed` — the admin would be told it broke and click again.
    fetchMock.mockResolvedValue(noContentResponse())

    const result = await removeMember(space.public_id, 3)

    const { url, init } = lastRequest()
    expect(url).toBe(`${API_BASE_URL}/spaces/${space.public_id}/members/3`)
    expect(init.method).toBe('DELETE')
    expect(result).toEqual({ outcome: 'ok', data: null })
  })

  it('surfaces the last-owner refusal as a conflict', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: LAST_OWNER_DETAIL }))

    const result = await removeMember(space.public_id, 3)

    expect(result).toEqual({ outcome: 'conflict', message: LAST_OWNER_DETAIL })
  })
})

describe('access requests', () => {
  it('lists the queue unfiltered by default', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, [accessRequest]))

    const result = await listAccessRequests(space.public_id)

    expect(lastRequest().url).toBe(`${API_BASE_URL}/spaces/${space.public_id}/access-requests`)
    expect(result).toEqual({ outcome: 'ok', data: [accessRequest] })
  })

  it('filters by status when asked', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    await listAccessRequests(space.public_id, { status: 'pending' })

    expect(lastRequest().url).toBe(
      `${API_BASE_URL}/spaces/${space.public_id}/access-requests?status=pending`,
    )
  })

  it('resolves a plain member to forbidden on the queue, not not_found', async () => {
    // They are already inside the Space and know it exists, so there is nothing
    // left for a 404 to conceal — and telling them "no such Space" would be a
    // lie they can immediately disprove.
    fetchMock.mockResolvedValue(jsonResponse(403, { detail: 'Not allowed.' }))

    const result = await listAccessRequests(space.public_id)

    expect(result.outcome).toBe('forbidden')
  })

  it('approves by POSTing to the approve URL with no body', async () => {
    // No role travels with an approval: it grants `member`, and promotion is a
    // separate call so the owner-authority rules live in exactly one place.
    fetchMock.mockResolvedValue(jsonResponse(200, { ...accessRequest, status: 'approved' }))

    const result = await approveAccessRequest(space.public_id, 11)

    const { url, init } = lastRequest()
    expect(url).toBe(`${API_BASE_URL}/spaces/${space.public_id}/access-requests/11/approve`)
    expect(init.method).toBe('POST')
    expect(init.body).toBeUndefined()
    expect(result.outcome).toBe('ok')
  })

  it('denies by POSTing to the deny URL', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ...accessRequest, status: 'denied' }))

    const result = await denyAccessRequest(space.public_id, 11)

    expect(lastRequest().url).toBe(
      `${API_BASE_URL}/spaces/${space.public_id}/access-requests/11/deny`,
    )
    expect(result.outcome).toBe('ok')
  })

  it('reports an already-decided request as a conflict', async () => {
    const detail = 'This access request has already been decided.'
    fetchMock.mockResolvedValue(jsonResponse(409, { detail }))

    const result = await approveAccessRequest(space.public_id, 11)

    expect(result).toEqual({ outcome: 'conflict', message: detail })
  })
})

describe('invitations', () => {
  it('creates an invitation with the email and role', async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, invitation))

    const result = await createInvitation(space.public_id, 'carol@example.com', 'member')

    const { url, init } = lastRequest()
    expect(url).toBe(`${API_BASE_URL}/spaces/${space.public_id}/invitations`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({
      email: 'carol@example.com',
      role: 'member',
    })
    expect(result).toEqual({ outcome: 'ok', data: invitation })
  })

  it('surfaces inviting above your own role as forbidden', async () => {
    // The UI does not offer `owner` to an admin, but the UI is not the boundary
    // and this is the branch that proves the client handles the server saying so.
    fetchMock.mockResolvedValue(
      jsonResponse(403, { detail: 'You cannot invite someone at a role above your own.' }),
    )

    const result = await createInvitation(space.public_id, 'carol@example.com', 'owner')

    expect(result.outcome).toBe('forbidden')
  })

  it('reports a duplicate invitation as a conflict', async () => {
    const detail = 'That address already has a pending invitation to this Space.'
    fetchMock.mockResolvedValue(jsonResponse(409, { detail }))

    const result = await createInvitation(space.public_id, 'carol@example.com', 'member')

    expect(result).toEqual({ outcome: 'conflict', message: detail })
  })

  it('revokes by DELETE and returns the revoked invitation, not an empty 204', async () => {
    // The row survives as `revoked`, and its status is the evidence the
    // revocation landed — which is why this route answers 200 with a body.
    const revoked: Invitation = { ...invitation, status: 'revoked' }
    fetchMock.mockResolvedValue(jsonResponse(200, revoked))

    const result = await revokeInvitation(space.public_id, 5)

    const { url, init } = lastRequest()
    expect(url).toBe(`${API_BASE_URL}/spaces/${space.public_id}/invitations/5`)
    expect(init.method).toBe('DELETE')
    expect(result).toEqual({ outcome: 'ok', data: revoked })
  })

  it('reports revoking an accepted invitation as a conflict', async () => {
    const detail = 'This invitation has already been accepted or revoked.'
    fetchMock.mockResolvedValue(jsonResponse(409, { detail }))

    const result = await revokeInvitation(space.public_id, 5)

    expect(result).toEqual({ outcome: 'conflict', message: detail })
  })

  it('lists invitations filtered by status', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, [invitation]))

    await listInvitations(space.public_id, { status: 'pending' })

    expect(lastRequest().url).toBe(
      `${API_BASE_URL}/spaces/${space.public_id}/invitations?status=pending`,
    )
  })
})

describe('archiveSpace', () => {
  it('posts to the archive URL and returns the archived Space', async () => {
    const archived: Space = { ...space, archived_at: '2026-07-21T09:00:00Z' }
    fetchMock.mockResolvedValue(jsonResponse(200, archived))

    const result = await archiveSpace(space.public_id)

    const { url, init } = lastRequest()
    expect(url).toBe(`${API_BASE_URL}/spaces/${space.public_id}/archive`)
    expect(init.method).toBe('POST')
    expect(result).toEqual({ outcome: 'ok', data: archived })
  })

  it('reports archiving an archived Space as a conflict', async () => {
    const detail = 'This Space is archived and can no longer be changed.'
    fetchMock.mockResolvedValue(jsonResponse(409, { detail }))

    const result = await archiveSpace(space.public_id)

    expect(result).toEqual({ outcome: 'conflict', message: detail })
  })

  it('resolves an admin (not owner) to forbidden', async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, { detail: 'Owner only.' }))

    const result = await archiveSpace(space.public_id)

    expect(result.outcome).toBe('forbidden')
  })
})

describe('the two kinds of 409 stay apart', () => {
  it('keeps a booking conflict away from the Space conflict', async () => {
    // `overlap` is a 409 *with* an `error` key. The discriminator is read before
    // the status, so it is claimed as `overlap` and never reaches the bare-409
    // branch that produces `conflict`. If someone reorders the classification so
    // the status is checked first, this is the test that fails — and the bug it
    // would otherwise ship is a booking collision rendered with a Space's
    // membership copy.
    fetchMock.mockResolvedValue(
      jsonResponse(409, { error: 'overlap', message: 'That slot is taken.' }),
    )

    const result = await createBooking(new Date('2026-07-20T09:00:00Z'), new Date('2026-07-20T10:00:00Z'))

    expect(result).toEqual({ outcome: 'overlap', message: 'That slot is taken.' })
  })

  it('leaves an undiscriminated 409 on a booking route as failed', async () => {
    // The booking result unions were deliberately not widened — `BookingPanel`
    // and `CancelPanel` switch exhaustively with no `default`, so a new variant
    // there would force edits to Stream 1 components. A bare 409 on a booking
    // route was `failed` before this task and stays `failed` after it.
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: 'Some future rule.' }))

    const result = await createBooking(new Date('2026-07-20T09:00:00Z'), new Date('2026-07-20T10:00:00Z'))

    expect(result.outcome).toBe('failed')
  })
})
