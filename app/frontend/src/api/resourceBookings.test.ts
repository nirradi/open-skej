/**
 * Tests for the resource-scoped booking client — `listResourceBookings`,
 * `createResourceBooking`, `cancelResourceBooking`.
 *
 * `client.test.ts` already proves the shared classification machinery (the
 * `overlap`/`already_cancelled` and `rule_denied`/validation-error pairs), so
 * this file's job is narrower: prove the URL these functions build, prove the
 * two *new* discriminated outcomes (`space_archived`, `already_started`) land
 * on their own variants rather than being absorbed by a neighbour, and prove
 * that — unlike the unscoped routes — `unauthenticated` / `forbidden` /
 * `not_found` are first-class outcomes here rather than folded into `failed`.
 *
 * `fetch` is mocked throughout — nothing here touches a real server.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  API_BASE_URL,
  cancelResourceBooking,
  createResourceBooking,
  listResourceBookings,
} from './client'
import type { Booking } from './types'

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'
const RESOURCE_ID = 3

/** A `BookingRead` as the backend sends it — snake_case, ISO-8601 strings. */
const confirmedBooking: Booking = {
  id: 42,
  resource_id: RESOURCE_ID,
  user_id: 9,
  start_at: '2026-07-20T10:00:00Z',
  end_at: '2026-07-20T11:00:00Z',
  status: 'confirmed',
  created_at: '2026-07-19T12:00:00Z',
  cancelled_at: null,
}

const cancelledBooking: Booking = {
  ...confirmedBooking,
  status: 'cancelled',
  cancelled_at: '2026-07-19T13:00:00Z',
}

/** Minimal stand-in for `Response`; the client only reads `ok`, `status`, `json`. */
function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  fetchMock.mockReset()
})

/** The URL the client called, for asserting path and query construction. */
function calledUrl(): string {
  return String(fetchMock.mock.calls[0]?.[0])
}

const BASE_PATH = `/spaces/${PUBLIC_ID}/resources/${RESOURCE_ID}/bookings`

describe('listResourceBookings', () => {
  const from = new Date('2026-07-20T00:00:00Z')
  const to = new Date('2026-07-27T00:00:00Z')

  it('resolves to ok with the booking array', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, [confirmedBooking]))

    const result = await listResourceBookings(PUBLIC_ID, RESOURCE_ID, from, to)

    expect(result).toEqual({ outcome: 'ok', data: [confirmedBooking] })
  })

  it('scopes the URL to the Space and the Resource and serialises the window as UTC', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    await listResourceBookings(PUBLIC_ID, RESOURCE_ID, from, to)

    const url = new URL(calledUrl())
    expect(url.pathname).toBe(BASE_PATH)
    expect(url.searchParams.get('from')).toBe('2026-07-20T00:00:00.000Z')
    expect(url.searchParams.get('to')).toBe('2026-07-27T00:00:00.000Z')
    expect(url.searchParams.has('include_cancelled')).toBe(false)
  })

  it('percent-encodes the public id', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    await listResourceBookings('a/b?c', RESOURCE_ID, from, to)

    expect(new URL(calledUrl()).pathname).toBe(
      `/spaces/a%2Fb%3Fc/resources/${RESOURCE_ID}/bookings`,
    )
  })

  it('sends include_cancelled when asked', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    await listResourceBookings(PUBLIC_ID, RESOURCE_ID, from, to, { includeCancelled: true })

    expect(new URL(calledUrl()).searchParams.get('include_cancelled')).toBe('true')
  })

  it('rejects an invalid Date as invalid_request without calling fetch', async () => {
    const result = await listResourceBookings(PUBLIC_ID, RESOURCE_ID, new Date('nonsense'), to)

    expect(result.outcome).toBe('invalid_request')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('maps a 401 to unauthenticated', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'Signature verification failed' }))

    const result = await listResourceBookings(PUBLIC_ID, RESOURCE_ID, from, to)

    expect(result.outcome).toBe('unauthenticated')
  })

  it('maps a 403 to forbidden', async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, { detail: 'Not enough.' }))

    const result = await listResourceBookings(PUBLIC_ID, RESOURCE_ID, from, to)

    expect(result.outcome).toBe('forbidden')
  })

  it('maps a bare 404 to not_found — a non-member gets no confirmation the Space exists', async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { detail: 'Space not found' }))

    const result = await listResourceBookings(PUBLIC_ID, RESOURCE_ID, from, to)

    expect(result.outcome).toBe('not_found')
  })

  it('maps an unmodelled discriminated error to failed — a read has no domain outcome', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'space_archived', message: 'Closed.' }))

    const result = await listResourceBookings(PUBLIC_ID, RESOURCE_ID, from, to)

    expect(result.outcome).toBe('failed')
  })
})

describe('createResourceBooking', () => {
  const start = new Date('2026-07-20T10:00:00Z')
  const end = new Date('2026-07-20T11:00:00Z')

  it('resolves to ok with the created booking', async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, confirmedBooking))

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result).toEqual({ outcome: 'ok', data: confirmedBooking })
  })

  it('posts to the scoped URL with UTC ISO-8601 timestamps', async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, confirmedBooking))

    await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    const init = fetchMock.mock.calls[0]?.[1]
    expect(calledUrl()).toBe(`${API_BASE_URL}${BASE_PATH}`)
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({
      start_at: '2026-07-20T10:00:00.000Z',
      end_at: '2026-07-20T11:00:00.000Z',
    })
  })

  it('maps 422 + rule_denied to rule_denied', async () => {
    const message = 'Bookings can be at most 2 hours long.'
    fetchMock.mockResolvedValue(jsonResponse(422, { error: 'rule_denied', message }))

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result).toEqual({ outcome: 'rule_denied', message })
  })

  it('maps 409 + overlap to overlap', async () => {
    const message = 'That time was just taken by someone else.'
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'overlap', message }))

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result).toEqual({ outcome: 'overlap', message })
  })

  it('maps 409 + space_archived to space_archived, distinct from overlap', async () => {
    const message = 'This Space is archived and is no longer taking new bookings.'
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'space_archived', message }))

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result).toEqual({ outcome: 'space_archived', message })
    expect(result.outcome).not.toBe('overlap')
  })

  it('keeps the two new-in-4.4 outcomes apart from each other and from overlap', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'overlap', message: 'Taken.' }))
    const overlap = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'space_archived', message: 'Closed.' }))
    const archived = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(overlap.outcome).toBe('overlap')
    expect(archived.outcome).toBe('space_archived')
    expect(overlap.outcome).not.toBe(archived.outcome)
  })

  it('maps a 401 to unauthenticated', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'Signature verification failed' }))

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result.outcome).toBe('unauthenticated')
  })

  it('maps a 403 to forbidden', async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, { detail: 'Not a member.' }))

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result.outcome).toBe('forbidden')
  })

  it('maps a bare 404 to not_found', async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { detail: 'Resource not found' }))

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result.outcome).toBe('not_found')
  })

  it('maps a 422 validation body (no error key) to invalid_request, not rule_denied', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { detail: [{ loc: ['body', 'start_at'], msg: 'Invalid' }] }),
    )

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result.outcome).toBe('invalid_request')
  })

  it('maps an unrecognised discriminator to failed rather than mis-mapping it', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'something_new', message: 'Future.' }))

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result.outcome).toBe('failed')
  })

  it('maps an undiscriminated 409 to failed — this route has no bare conflict', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: 'Some domain refusal.' }))

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result.outcome).toBe('failed')
  })

  it('rejects an invalid Date as invalid_request without calling fetch', async () => {
    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, new Date('nonsense'), end)

    expect(result.outcome).toBe('invalid_request')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('maps a rejected fetch to failed', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    const result = await createResourceBooking(PUBLIC_ID, RESOURCE_ID, start, end)

    expect(result.outcome).toBe('failed')
    if (result.outcome !== 'failed') throw new Error('unreachable')
    expect(result.message).toMatch(/couldn't reach the server/i)
  })
})

describe('cancelResourceBooking', () => {
  it('resolves to ok with the cancelled booking, not an empty 204', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, cancelledBooking))

    const result = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 42)

    expect(calledUrl()).toBe(`${API_BASE_URL}${BASE_PATH}/42`)
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('DELETE')
    if (result.outcome !== 'ok') throw new Error('unreachable')
    expect(result.data.status).toBe('cancelled')
    expect(result.data.cancelled_at).toBe('2026-07-19T13:00:00Z')
  })

  it('maps 404 + not_found (the discriminated form) to not_found', async () => {
    const message = 'That booking no longer exists.'
    fetchMock.mockResolvedValue(jsonResponse(404, { error: 'not_found', message }))

    const result = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 999)

    expect(result).toEqual({ outcome: 'not_found', message })
  })

  it('maps a bare 404 (the Space/Resource form) to the same not_found variant', async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { detail: 'Space not found' }))

    const result = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 999)

    expect(result.outcome).toBe('not_found')
  })

  it('maps 409 + already_cancelled to already_cancelled', async () => {
    const message = 'That booking was already cancelled.'
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'already_cancelled', message }))

    const result = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 42)

    expect(result).toEqual({ outcome: 'already_cancelled', message })
  })

  it('maps 409 + already_started to already_started', async () => {
    const message = 'This booking has already started and can no longer be cancelled.'
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'already_started', message }))

    const result = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 42)

    expect(result).toEqual({ outcome: 'already_started', message })
  })

  it('keeps already_started apart from already_cancelled despite sharing 409', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(409, { error: 'already_cancelled', message: 'Already cancelled.' }),
    )
    const cancelled = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 42)

    fetchMock.mockResolvedValue(
      jsonResponse(409, { error: 'already_started', message: 'Under way.' }),
    )
    const started = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 42)

    expect(cancelled.outcome).toBe('already_cancelled')
    expect(started.outcome).toBe('already_started')
    expect(cancelled.outcome).not.toBe(started.outcome)
  })

  it('maps a 401 to unauthenticated', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'Signature verification failed' }))

    const result = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 42)

    expect(result.outcome).toBe('unauthenticated')
  })

  it('maps a 403 to forbidden', async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, { detail: 'Not a member.' }))

    const result = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 42)

    expect(result.outcome).toBe('forbidden')
  })

  it('maps an unrecognised discriminator to failed, not to already_cancelled', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'locked_by_admin', message: 'Nope.' }))

    const result = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 42)

    expect(result.outcome).toBe('failed')
  })

  it('maps an undiscriminated 409 to failed — this route has no bare conflict', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: 'Some domain refusal.' }))

    const result = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 42)

    expect(result.outcome).toBe('failed')
  })

  it('maps a rejected fetch to failed', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    const result = await cancelResourceBooking(PUBLIC_ID, RESOURCE_ID, 42)

    expect(result.outcome).toBe('failed')
  })
})
