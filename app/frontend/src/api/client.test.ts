/**
 * Tests for the booking API client's outcome classification.
 *
 * The client's whole reason to exist is that the HTTP status code is not enough
 * to identify an outcome: `overlap` and `already_cancelled` share 409, and
 * `rule_denied` shares 422 with FastAPI's own request-validation failure. So the
 * tests that matter most here are the *pairs* — proving the two members of a
 * shared status land on different variants, rather than merely proving each one
 * lands somewhere.
 *
 * The second promise under test is that **no expected outcome is delivered by a
 * thrown exception**. Every assertion below therefore awaits a resolved value;
 * `describe('never throws')` at the end makes that explicit for the failure
 * paths, where throwing is the tempting implementation.
 *
 * `fetch` is mocked throughout — nothing here touches a real server.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { API_BASE_URL, cancelBooking, createBooking, listBookings } from './client'
import type { Booking } from './types'

/** A `BookingRead` as the backend sends it — snake_case, ISO-8601 strings. */
const confirmedBooking: Booking = {
  id: 7,
  resource_id: 1,
  user_id: 1,
  start_at: '2026-07-20T09:00:00Z',
  end_at: '2026-07-20T10:00:00Z',
  status: 'confirmed',
  created_at: '2026-07-19T12:00:00Z',
  cancelled_at: null,
}

const cancelledBooking: Booking = {
  ...confirmedBooking,
  status: 'cancelled',
  cancelled_at: '2026-07-19T13:00:00Z',
}

/**
 * A FastAPI request-validation body: a `detail` array of Pydantic errors and,
 * crucially, **no `error` key**. This is the 422 that must not be mistaken for a
 * rule denial.
 */
const validationBody = {
  detail: [
    {
      type: 'datetime_parsing',
      loc: ['body', 'start_at'],
      msg: 'Input should be a valid datetime',
      input: 'not-a-datetime',
    },
  ],
}

/** Minimal stand-in for `Response`; the client only reads `ok`, `status`, `json`. */
function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

/** A 200 whose body is not JSON at all — an HTML error page from a proxy, say. */
function nonJsonResponse(status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON at position 0')
    },
    // Double cast: a `json` that only ever throws infers as `Promise<never>`,
    // which TypeScript will not narrow to `Response` in one step.
  } as unknown as Response
}

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  fetchMock.mockReset()
})

/** The URL the client called, for asserting query construction. */
function calledUrl(): string {
  return String(fetchMock.mock.calls[0]?.[0])
}

describe('createBooking', () => {
  const start = new Date('2026-07-20T09:00:00Z')
  const end = new Date('2026-07-20T10:00:00Z')

  it('resolves to ok with the created booking', async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, confirmedBooking))

    const result = await createBooking(start, end)

    expect(result).toEqual({ outcome: 'ok', data: confirmedBooking })
    // Narrowing on `outcome` is how callers reach `data`; if the union ever
    // stops discriminating, this stops compiling.
    if (result.outcome !== 'ok') throw new Error('unreachable')
    expect(result.data.status).toBe('confirmed')
    expect(result.data.id).toBe(7)
  })

  it('sends UTC ISO-8601 timestamps in the body', async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, confirmedBooking))

    await createBooking(start, end)

    const init = fetchMock.mock.calls[0]?.[1]
    expect(calledUrl()).toBe(`${API_BASE_URL}/bookings`)
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({
      start_at: '2026-07-20T09:00:00.000Z',
      end_at: '2026-07-20T10:00:00.000Z',
    })
  })

  it('maps 422 + rule_denied to rule_denied, carrying the friendly copy', async () => {
    const message = 'Bookings can be at most 2 hours long.'
    fetchMock.mockResolvedValue(jsonResponse(422, { error: 'rule_denied', message }))

    const result = await createBooking(start, end)

    expect(result).toEqual({ outcome: 'rule_denied', message })
  })

  it('maps 409 + overlap to overlap, carrying the friendly copy', async () => {
    const message = 'That time was just taken by someone else.'
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'overlap', message }))

    const result = await createBooking(start, end)

    expect(result).toEqual({ outcome: 'overlap', message })
  })

  it('maps a 422 validation body (no error key) to invalid_request, not rule_denied', async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, validationBody))

    const result = await createBooking(start, end)

    expect(result.outcome).toBe('invalid_request')
  })

  it('keeps the two 422s apart', async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, { error: 'rule_denied', message: 'Too long.' }))
    const denied = await createBooking(start, end)

    fetchMock.mockResolvedValue(jsonResponse(422, validationBody))
    const malformed = await createBooking(start, end)

    expect(denied.outcome).toBe('rule_denied')
    expect(malformed.outcome).toBe('invalid_request')
    expect(denied.outcome).not.toBe(malformed.outcome)
  })

  it('never exposes validation detail where a UI would render friendly copy', async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, validationBody))

    const result = await createBooking(start, end)

    // `message` is the field the booking UI renders verbatim to the user. An
    // `invalid_request` has no such field at all — the Pydantic text lives in
    // `detail`, which is documented as developer diagnostics.
    expect(result).not.toHaveProperty('message')
    if (result.outcome !== 'invalid_request') throw new Error('unreachable')
    expect(result.detail).toContain('Input should be a valid datetime')
    expect(result.raw).toEqual(validationBody)
  })

  it('maps an unrecognised discriminator to failed rather than mis-mapping it', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(409, { error: 'something_new', message: 'A future outcome.' }),
    )

    const result = await createBooking(start, end)

    expect(result.outcome).toBe('failed')
    // Specifically not silently absorbed into `overlap`, the client's other 409.
    expect(result.outcome).not.toBe('overlap')
    if (result.outcome !== 'failed') throw new Error('unreachable')
    // The server's copy for an outcome this client does not model must not be
    // shown to the user as if it were understood.
    expect(result.message).not.toContain('A future outcome.')
    expect(String(result.cause)).toContain('something_new')
  })

  it('maps an unmodelled 500 to failed', async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { detail: 'Internal Server Error' }))

    const result = await createBooking(start, end)

    expect(result.outcome).toBe('failed')
  })

  it('maps a rejected fetch to failed', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    const result = await createBooking(start, end)

    expect(result.outcome).toBe('failed')
    if (result.outcome !== 'failed') throw new Error('unreachable')
    expect(result.message).toMatch(/couldn't reach the server/i)
  })

  it('maps a 2xx with an unparseable body to failed', async () => {
    fetchMock.mockResolvedValue(nonJsonResponse(200))

    const result = await createBooking(start, end)

    expect(result.outcome).toBe('failed')
  })

  it('rejects an invalid Date as invalid_request without calling fetch', async () => {
    const result = await createBooking(new Date('nonsense'), end)

    expect(result.outcome).toBe('invalid_request')
    if (result.outcome !== 'invalid_request') throw new Error('unreachable')
    expect(result.detail).toContain('start_at')
    // A request built from `new Date('nonsense').toISOString()` would have
    // thrown a RangeError, so the guard has to run before the call.
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('names both dates when both are invalid', async () => {
    const result = await createBooking(new Date('nonsense'), new Date('also nonsense'))

    if (result.outcome !== 'invalid_request') throw new Error('unreachable')
    expect(result.detail).toContain('start_at')
    expect(result.detail).toContain('end_at')
  })
})

describe('listBookings', () => {
  const from = new Date('2026-07-20T00:00:00Z')
  const to = new Date('2026-07-27T00:00:00Z')

  it('resolves to ok with the booking array', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, [confirmedBooking]))

    const result = await listBookings(from, to)

    expect(result).toEqual({ outcome: 'ok', data: [confirmedBooking] })
    if (result.outcome !== 'ok') throw new Error('unreachable')
    expect(result.data[0]?.start_at).toBe('2026-07-20T09:00:00Z')
  })

  it('resolves to ok with an empty array for an empty window', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    const result = await listBookings(from, to)

    expect(result).toEqual({ outcome: 'ok', data: [] })
  })

  it('serialises the window as UTC and omits include_cancelled by default', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    await listBookings(from, to)

    const url = new URL(calledUrl())
    expect(url.pathname).toBe('/bookings')
    expect(url.searchParams.get('from')).toBe('2026-07-20T00:00:00.000Z')
    expect(url.searchParams.get('to')).toBe('2026-07-27T00:00:00.000Z')
    expect(url.searchParams.has('include_cancelled')).toBe(false)
  })

  it('sends include_cancelled when asked', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    await listBookings(from, to, { includeCancelled: true })

    expect(new URL(calledUrl()).searchParams.get('include_cancelled')).toBe('true')
  })

  it('maps a 400 bad-window body to invalid_request', async () => {
    fetchMock.mockResolvedValue(jsonResponse(400, { detail: 'from must be before to' }))

    const result = await listBookings(to, from)

    expect(result.outcome).toBe('invalid_request')
    if (result.outcome !== 'invalid_request') throw new Error('unreachable')
    expect(result.detail).toBe('from must be before to')
  })

  it('maps a rejected fetch to failed', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    const result = await listBookings(from, to)

    expect(result.outcome).toBe('failed')
  })

  it('maps an unparseable body to failed', async () => {
    fetchMock.mockResolvedValue(nonJsonResponse(200))

    const result = await listBookings(from, to)

    expect(result.outcome).toBe('failed')
  })

  it('maps any discriminated error to failed — GET models no denial outcomes', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'overlap', message: 'Taken.' }))

    const result = await listBookings(from, to)

    expect(result.outcome).toBe('failed')
  })

  it('rejects an invalid Date as invalid_request without calling fetch', async () => {
    const result = await listBookings(new Date('nonsense'), to)

    expect(result.outcome).toBe('invalid_request')
    if (result.outcome !== 'invalid_request') throw new Error('unreachable')
    expect(result.detail).toContain('from')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('cancelBooking', () => {
  it('resolves to ok with the cancelled booking, not an empty 204', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, cancelledBooking))

    const result = await cancelBooking(7)

    expect(calledUrl()).toBe(`${API_BASE_URL}/bookings/7`)
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('DELETE')
    if (result.outcome !== 'ok') throw new Error('unreachable')
    // The contract returns the authoritative row so the calendar can be patched
    // without a refetch — both fields must survive the client.
    expect(result.data.status).toBe('cancelled')
    expect(result.data.cancelled_at).toBe('2026-07-19T13:00:00Z')
  })

  it('maps 404 + not_found to not_found', async () => {
    const message = 'That booking no longer exists.'
    fetchMock.mockResolvedValue(jsonResponse(404, { error: 'not_found', message }))

    const result = await cancelBooking(999)

    expect(result).toEqual({ outcome: 'not_found', message })
  })

  it('maps 409 + already_cancelled to already_cancelled', async () => {
    const message = 'That booking was already cancelled.'
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'already_cancelled', message }))

    const result = await cancelBooking(7)

    expect(result).toEqual({ outcome: 'already_cancelled', message })
  })

  it('keeps the two 409s apart', async () => {
    // Both arrive as 409 on the same resource. `overlap` means the calendar is
    // stale and warrants a warning; `already_cancelled` means the desired end
    // state already holds and task 1.8 treats it as success. Conflating them
    // would warn the user about their own double-click.
    fetchMock.mockResolvedValue(
      jsonResponse(409, { error: 'already_cancelled', message: 'Already cancelled.' }),
    )
    const cancelResult = await cancelBooking(7)

    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'overlap', message: 'Taken.' }))
    const createResult = await createBooking(
      new Date('2026-07-20T09:00:00Z'),
      new Date('2026-07-20T10:00:00Z'),
    )

    expect(cancelResult.outcome).toBe('already_cancelled')
    expect(createResult.outcome).toBe('overlap')
    expect(cancelResult.outcome).not.toBe(createResult.outcome)
  })

  it('maps an unrecognised discriminator to failed, not to already_cancelled', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { error: 'locked_by_admin', message: 'Nope.' }))

    const result = await cancelBooking(7)

    expect(result.outcome).toBe('failed')
    expect(result.outcome).not.toBe('already_cancelled')
  })

  it('maps an unmodelled 500 to failed', async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { detail: 'boom' }))

    expect((await cancelBooking(7)).outcome).toBe('failed')
  })

  it('maps a rejected fetch to failed', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    expect((await cancelBooking(7)).outcome).toBe('failed')
  })
})

describe('the no-exceptions promise', () => {
  const start = new Date('2026-07-20T09:00:00Z')
  const end = new Date('2026-07-20T10:00:00Z')

  /**
   * Every way a call can go wrong, paired with the outcome it must resolve to.
   *
   * A caller doing the right thing — `switch (result.outcome)`, no try/catch —
   * must never get an unhandled rejection instead of a branch, so these are run
   * through a wrapper that fails the test if the promise rejects at all.
   */
  const failureModes: { name: string; call: () => Promise<{ outcome: string }> }[] = [
    {
      name: 'network down',
      call: () => {
        fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
        return createBooking(start, end)
      },
    },
    {
      name: 'fetch throwing synchronously',
      call: () => {
        fetchMock.mockImplementation(() => {
          throw new TypeError('Failed to fetch')
        })
        return createBooking(start, end)
      },
    },
    {
      name: 'unparseable body',
      call: () => {
        fetchMock.mockResolvedValue(nonJsonResponse(200))
        return listBookings(start, end)
      },
    },
    {
      name: 'unparseable error body',
      call: () => {
        fetchMock.mockResolvedValue(nonJsonResponse(500))
        return cancelBooking(7)
      },
    },
    {
      name: 'unmodelled 500',
      call: () => {
        fetchMock.mockResolvedValue(jsonResponse(500, { detail: 'boom' }))
        return createBooking(start, end)
      },
    },
    {
      name: 'unrecognised discriminator',
      call: () => {
        fetchMock.mockResolvedValue(jsonResponse(409, { error: 'something_new', message: 'x' }))
        return cancelBooking(7)
      },
    },
    {
      name: 'invalid Date on create',
      call: () => createBooking(new Date('nonsense'), end),
    },
    {
      name: 'invalid Date on list',
      call: () => listBookings(start, new Date('nonsense')),
    },
    {
      name: 'null body on an error status',
      call: () => {
        fetchMock.mockResolvedValue(jsonResponse(422, null))
        return createBooking(start, end)
      },
    },
    {
      name: 'a discriminated error with no message',
      call: () => {
        fetchMock.mockResolvedValue(jsonResponse(422, { error: 'rule_denied' }))
        return createBooking(start, end)
      },
    },
  ]

  it.each(failureModes)('resolves rather than throwing: $name', async ({ call }) => {
    // Not `expect(...).resolves` — that would still pass if the *call itself*
    // threw synchronously before returning a promise.
    let threw: unknown = null
    let outcome: string | undefined
    try {
      outcome = (await call()).outcome
    } catch (error) {
      threw = error
    }

    expect(threw).toBeNull()
    expect(typeof outcome).toBe('string')
  })

  it('reports a discriminated error with no message as generic copy, not undefined', async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, { error: 'rule_denied' }))

    const result = await createBooking(start, end)

    if (result.outcome !== 'rule_denied') throw new Error('unreachable')
    // A UI renders this verbatim; `undefined` would reach the screen as "undefined".
    expect(typeof result.message).toBe('string')
    expect(result.message.length).toBeGreaterThan(0)
  })
})
