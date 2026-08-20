// @vitest-environment jsdom
/** Tests for the separate chat-and-preview calendar-shape studio. */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import {
  createShapeConversation,
  createShapeConversationTurn,
  discardCalendarShapeDraft,
  getOpenShapeConversation,
  getShapeConversation,
  getSpace,
  getSpaceCalendar,
  publishCalendarShape,
  type ShapeConversationRead,
} from '../api'
import { openDayRead } from '../calendar/fixtures'
import { addDays, toDateKey } from '../calendar'
import { makeSpace, ok } from './fixtures'
import { SpaceShapePage } from './SpaceShapePage'

vi.mock('../api', () => ({
  getSpace: vi.fn(),
  getSpaceCalendar: vi.fn(),
  createShapeConversation: vi.fn(),
  getOpenShapeConversation: vi.fn(),
  getShapeConversation: vi.fn(),
  createShapeConversationTurn: vi.fn(),
  publishCalendarShape: vi.fn(),
  discardCalendarShapeDraft: vi.fn(),
  listResourceBookings: vi.fn(),
}))

const PUBLIC_ID = 'sp_7f3a9c'

function conversation(overrides: Partial<ShapeConversationRead> = {}): ShapeConversationRead {
  return {
    id: 31,
    status: 'open',
    created_at: '2026-08-20T09:00:00Z',
    closed_at: null,
    messages: [],
    draft: null,
    live: {
      id: 1,
      document: {
        version: 1,
        operating_blocks: [
          {
            days_of_week: [0],
            start_time: '10:00',
            end_time: '11:00',
            slot_interval_mins: 60,
            allowed_durations_mins: [60],
          },
        ],
        blackout_windows: [],
      },
      status: 'live',
      created_at: '2026-08-20T08:00:00Z',
      source_conversation_id: null,
    },
    ...overrides,
  }
}

function draft() {
  return {
    id: 44,
    document: { version: 1, operating_blocks: [], blackout_windows: [] },
    status: 'draft' as const,
    created_at: '2026-08-20T09:00:00Z',
    source_conversation_id: 31,
  }
}

function calendarResponse(from: Date, to: Date, startMinutes = 600) {
  return Array.from(
    { length: Math.round((to.getTime() - from.getTime()) / 86_400_000) + 1 },
    (_, index) => {
      const entry = openDayRead(toDateKey(addDays(from, index)))
      return {
        ...entry,
        offered_starts: [{ start_minutes: startMinutes, durations_mins: [60] }],
      }
    },
  )
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/s/${PUBLIC_ID}/shape`]}>
      <Routes>
        <Route path="/s/:publicId/shape" element={<SpaceShapePage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  window.localStorage.clear()
  vi.mocked(getSpace).mockResolvedValue(ok(makeSpace({ public_id: PUBLIC_ID, my_role: 'admin' })))
  vi.mocked(getOpenShapeConversation).mockResolvedValue(ok(null))
  vi.mocked(createShapeConversation).mockResolvedValue(ok(conversation()))
  vi.mocked(getSpaceCalendar).mockImplementation(async (_id, from, to, options) =>
    ok(calendarResponse(from, to, options?.draft ? 240 : 600)),
  )
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('SpaceShapePage', () => {
  it('sends one synchronous turn, renders its summary, and switches the shared preview to draft', async () => {
    vi.mocked(createShapeConversationTurn).mockResolvedValue(
      ok({ summary: 'The venue now opens at 04:00.', question: null, draft: draft() }),
    )

    renderPage()
    const input = await screen.findByTestId('shape-message-input')
    fireEvent.change(input, { target: { value: 'open at 4' } })
    fireEvent.click(screen.getByTestId('shape-send'))

    expect(await screen.findByText('The venue now opens at 04:00.')).toBeTruthy()
    await waitFor(() =>
      expect(vi.mocked(getSpaceCalendar)).toHaveBeenCalledWith(
        PUBLIC_ID,
        expect.any(Date),
        expect.any(Date),
        { draft: true },
      ),
    )
    expect(vi.mocked(createShapeConversationTurn)).toHaveBeenCalledWith(PUBLIC_ID, 31, 'open at 4')
  })

  it('fetches and displays the replacement draft after every normal turn', async () => {
    let draftFetches = 0
    vi.mocked(getSpaceCalendar).mockImplementation(async (_id, from, to, options) => {
      if (!options?.draft) return ok(calendarResponse(from, to, 600))
      draftFetches += 1
      return ok(calendarResponse(from, to, draftFetches === 1 ? 240 : 300))
    })
    vi.mocked(createShapeConversationTurn)
      .mockResolvedValueOnce(ok({ summary: 'Open at 04:00.', question: null, draft: draft() }))
      .mockResolvedValueOnce(
        ok({
          summary: 'Open at 05:00.',
          question: null,
          draft: { ...draft(), created_at: '2026-08-20T09:01:00Z' },
        }),
      )

    renderPage()
    fireEvent.change(await screen.findByTestId('shape-message-input'), {
      target: { value: 'open at 4' },
    })
    fireEvent.click(screen.getByTestId('shape-send'))
    await screen.findByText('Open at 04:00.')
    expect((await screen.findAllByLabelText(/04:00$/)).length).toBeGreaterThan(0)

    fireEvent.change(screen.getByTestId('shape-message-input'), { target: { value: 'open at 5' } })
    fireEvent.click(screen.getByTestId('shape-send'))
    await screen.findByText('Open at 05:00.')
    expect((await screen.findAllByLabelText(/05:00$/)).length).toBeGreaterThan(0)
    expect(draftFetches).toBe(2)
  })

  it('uses the exact live and draft documents, not the displayed week, to disable publish', async () => {
    const identical = { ...draft(), status: 'live' as const, id: 1, source_conversation_id: null }
    vi.mocked(createShapeConversation).mockResolvedValue(
      ok(conversation({ draft: draft(), live: identical })),
    )

    renderPage()

    expect(((await screen.findByTestId('shape-publish')) as HTMLButtonElement).disabled).toBe(true)
  })

  it('renders an unbookable question without replacing the useful preview', async () => {
    vi.mocked(createShapeConversationTurn).mockResolvedValue(
      ok({
        summary: 'I closed every offered block.',
        question: 'Should the venue be closed all week?',
        draft: draft(),
      }),
    )

    renderPage()
    fireEvent.change(await screen.findByTestId('shape-message-input'), {
      target: { value: 'close it' },
    })
    fireEvent.click(screen.getByTestId('shape-send'))

    expect((await screen.findByTestId('shape-question')).textContent).toContain(
      'Should the venue be closed all week?',
    )
    expect(screen.getByTestId('shape-preview').textContent).toContain(
      'Previewing the live calendar.',
    )
  })

  it('disables publish without a draft, enables it for a changed one, and confirms discard', async () => {
    vi.mocked(createShapeConversationTurn).mockResolvedValue(
      ok({ summary: 'The venue now opens at 04:00.', question: null, draft: draft() }),
    )
    vi.mocked(discardCalendarShapeDraft).mockResolvedValue(ok(null))

    renderPage()
    expect(((await screen.findByTestId('shape-publish')) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByTestId('shape-discard-start') as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByTestId('shape-message-input'), { target: { value: 'open at 4' } })
    fireEvent.click(screen.getByTestId('shape-send'))
    await screen.findByText('The venue now opens at 04:00.')
    expect((screen.getByTestId('shape-publish') as HTMLButtonElement).disabled).toBe(false)

    fireEvent.click(screen.getByTestId('shape-discard-start'))
    expect(screen.getByTestId('shape-discard-confirm')).toBeTruthy()
    fireEvent.click(screen.getByTestId('shape-discard-confirm-yes'))
    await waitFor(() =>
      expect(vi.mocked(discardCalendarShapeDraft)).toHaveBeenCalledWith(PUBLIC_ID),
    )
    expect((await screen.findByTestId('shape-success')).textContent).toContain(
      'Discarded the draft',
    )
    expect(window.localStorage.getItem(`skej.shape-conversation.${PUBLIC_ID}`)).toBeNull()
    expect(screen.getByTestId('shape-preview').textContent).toContain(
      'Previewing the live calendar.',
    )
    expect((await screen.findAllByLabelText(/10:00$/)).length).toBeGreaterThan(0)
  })

  it('renders forbidden and archived write failures instead of dropping the click', async () => {
    vi.mocked(createShapeConversationTurn).mockResolvedValue(
      ok({ summary: 'The venue now opens at 04:00.', question: null, draft: draft() }),
    )
    vi.mocked(publishCalendarShape).mockResolvedValue({
      outcome: 'forbidden',
      message: "You don't have permission to do that.",
    })

    renderPage()
    fireEvent.change(await screen.findByTestId('shape-message-input'), {
      target: { value: 'open at 4' },
    })
    fireEvent.click(screen.getByTestId('shape-send'))
    await screen.findByText('The venue now opens at 04:00.')
    fireEvent.click(screen.getByTestId('shape-publish'))
    expect((await screen.findByTestId('shape-action-error')).textContent).toContain(
      "don't have permission",
    )

    vi.mocked(discardCalendarShapeDraft).mockResolvedValue({
      outcome: 'conflict',
      message: 'This Space is archived and can no longer be changed.',
    })
    fireEvent.click(screen.getByTestId('shape-discard-start'))
    fireEvent.click(screen.getByTestId('shape-discard-confirm-yes'))
    await waitFor(() =>
      expect(screen.getByTestId('shape-action-error').textContent).toContain('archived'),
    )
  })

  it('refreshes the live preview after publishing', async () => {
    let published = false
    vi.mocked(getSpaceCalendar).mockImplementation(async (_id, from, to, options) =>
      ok(calendarResponse(from, to, options?.draft || published ? 240 : 600)),
    )
    vi.mocked(createShapeConversationTurn).mockResolvedValue(
      ok({ summary: 'The venue now opens at 04:00.', question: null, draft: draft() }),
    )
    vi.mocked(publishCalendarShape).mockImplementation(async () => {
      published = true
      return ok({ ...draft(), status: 'live' })
    })
    renderPage()
    fireEvent.change(await screen.findByTestId('shape-message-input'), {
      target: { value: 'open at 4' },
    })
    fireEvent.click(screen.getByTestId('shape-send'))
    await screen.findByText('The venue now opens at 04:00.')
    fireEvent.click(screen.getByTestId('shape-publish'))
    await screen.findByTestId('shape-success')
    expect(screen.getByTestId('shape-preview').textContent).toContain(
      'Previewing the live calendar.',
    )
    expect((await screen.findAllByLabelText(/04:00$/)).length).toBeGreaterThan(0)
  })

  it('accepts a legacy saved pointer by id and clears a missing one before opening another', async () => {
    window.localStorage.setItem(
      `skej.shape-conversation.${PUBLIC_ID}`,
      JSON.stringify({ id: 91, question: 'obsolete browser state' }),
    )
    vi.mocked(getShapeConversation).mockResolvedValue({
      outcome: 'not_found',
      message: "We couldn't find that.",
    })

    renderPage()

    await waitFor(() => expect(vi.mocked(getShapeConversation)).toHaveBeenCalledWith(PUBLIC_ID, 91))
    await waitFor(() => expect(vi.mocked(getOpenShapeConversation)).toHaveBeenCalledWith(PUBLIC_ID))
    await waitFor(() => expect(vi.mocked(createShapeConversation)).toHaveBeenCalledWith(PUBLIC_ID))
    expect(window.localStorage.getItem(`skej.shape-conversation.${PUBLIC_ID}`)).toBe(
      JSON.stringify({ id: 31 }),
    )
  })

  it('recovers the server-side open conversation when no browser pointer exists', async () => {
    vi.mocked(getOpenShapeConversation).mockResolvedValue(
      ok(
        conversation({
          messages: [
            {
              ordinal: 1,
              role: 'assistant',
              content: 'What hours should the venue offer?',
              question: null,
              resulting_shape_version_id: null,
              created_at: '2026-08-20T09:00:00Z',
            },
          ],
        }),
      ),
    )

    renderPage()

    expect(await screen.findByText('What hours should the venue offer?')).toBeTruthy()
    expect(vi.mocked(createShapeConversation)).not.toHaveBeenCalled()
    expect(window.localStorage.getItem(`skej.shape-conversation.${PUBLIC_ID}`)).toBe(
      JSON.stringify({ id: 31 }),
    )
  })

  it('recovers an unbookable question from the server transcript and falls back to live', async () => {
    vi.mocked(getOpenShapeConversation).mockResolvedValue(
      ok(
        conversation({
          draft: draft(),
          messages: [
            {
              ordinal: 2,
              role: 'assistant',
              content: 'I closed every offered block.',
              question: 'Should the venue be closed all week?',
              resulting_shape_version_id: 44,
              created_at: '2026-08-20T09:00:00Z',
            },
          ],
        }),
      ),
    )

    renderPage()

    expect((await screen.findByTestId('shape-question')).textContent).toContain(
      'Should the venue be closed all week?',
    )
    expect(screen.getByTestId('shape-preview').textContent).toContain(
      'Previewing the live calendar.',
    )
    expect((await screen.findAllByLabelText(/10:00$/)).length).toBeGreaterThan(0)
  })

  it('anchors the initial preview week in the Space timezone once the Space resolves', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date('2026-08-17T01:00:00Z'))
    vi.mocked(getSpace).mockResolvedValue(
      ok(makeSpace({ public_id: PUBLIC_ID, my_role: 'admin', timezone: 'Pacific/Honolulu' })),
    )

    renderPage()
    await waitFor(() => expect(vi.mocked(getSpaceCalendar)).toHaveBeenCalled())
    const liveCall = vi
      .mocked(getSpaceCalendar)
      .mock.calls.find(([, , , options]) => !options?.draft)
    expect(liveCall).toBeDefined()
    expect(toDateKey(liveCall![1])).toBe('2026-08-10')
  })
})
