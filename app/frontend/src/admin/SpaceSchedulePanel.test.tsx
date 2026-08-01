// @vitest-environment jsdom
/**
 * Tests for the schedule configuration panel: the Space's timezone —
 * `DEFERRED.md` item 2.
 *
 * Task 6.8 moved operating hours, slot interval, max duration, booking
 * horizon, and the two frequency caps off this panel entirely — each is a
 * `space_rules` row edited at `/s/{public_id}/rules` instead
 * (`SpaceRulesPage.test.tsx` covers that surface). `timezone` is the one
 * property left here, so this file no longer asserts on the seven removed
 * fields at all.
 *
 * As with every other panel in this dashboard, the hiding in `AdminPage` is a
 * convenience, not the boundary: the write here still has to handle
 * `forbidden` and `conflict` as if it were called directly, because a second
 * admin can act between this component's render and a click.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { updateSpace } from '../api'
import { conflict, forbidden, makeSpace, ok } from './fixtures'
import { SpaceSchedulePanel } from './SpaceSchedulePanel'

vi.mock('../api', () => ({
  updateSpace: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPanel(space = makeSpace(), onSpaceChanged = vi.fn()) {
  render(<SpaceSchedulePanel space={space} onSpaceChanged={onSpaceChanged} />)
  return { onSpaceChanged }
}

describe('SpaceSchedulePanel', () => {
  it('shows the Space timezone', () => {
    renderPanel(makeSpace({ timezone: 'Europe/Berlin' }))

    expect((screen.getByTestId('timezone-input') as HTMLInputElement).value).toBe(
      'Europe/Berlin',
    )
  })

  it('saves an edited timezone, and nothing else', async () => {
    const updated = makeSpace({ timezone: 'Asia/Tokyo' })
    vi.mocked(updateSpace).mockResolvedValue(ok(updated))
    const { onSpaceChanged } = renderPanel()

    fireEvent.change(screen.getByTestId('timezone-input'), { target: { value: 'Asia/Tokyo' } })
    fireEvent.click(screen.getByTestId('schedule-save'))

    // The panel no longer reads or writes hours, slot interval, or any rule
    // parameter — those are `space_rules` rows edited at `/s/{public_id}/rules`
    // now, and sending them here would be a second place to edit the same
    // configuration.
    expect(vi.mocked(updateSpace)).toHaveBeenCalledWith('sp_7f3a9c', { timezone: 'Asia/Tokyo' })
    await vi.waitFor(() => expect(onSpaceChanged).toHaveBeenCalledWith(updated))
  })

  it('shows the server refusal for an unknown zone rather than inventing copy', async () => {
    const detail = "'Not/AZone' is not a known IANA timezone name"
    vi.mocked(updateSpace).mockResolvedValue({ outcome: 'invalid_request', detail, raw: null })
    renderPanel()

    fireEvent.change(screen.getByTestId('timezone-input'), { target: { value: 'Not/AZone' } })
    fireEvent.click(screen.getByTestId('schedule-save'))

    // `invalid_request` is treated as a client-diagnostic bug everywhere else in
    // this dashboard, so it shows the same generic copy here rather than the raw
    // Pydantic detail — consistent with `messageFor`.
    const error = await screen.findByTestId('schedule-error')
    expect(error.textContent).toBeTruthy()
  })

  it('resolves a member acting between render and click to forbidden', async () => {
    vi.mocked(updateSpace).mockResolvedValue(forbidden())
    renderPanel()

    fireEvent.click(screen.getByTestId('schedule-save'))

    const error = await screen.findByTestId('schedule-error')
    expect(error.textContent).toBe("You don't have permission to do that.")
  })

  it('shows the server conflict verbatim for an archived Space', async () => {
    const detail = 'This Space is archived and can no longer be changed.'
    vi.mocked(updateSpace).mockResolvedValue(conflict(detail))
    renderPanel()

    fireEvent.click(screen.getByTestId('schedule-save'))

    const error = await screen.findByTestId('schedule-error')
    expect(error.textContent).toBe(detail)
  })

  it('disables every control on an archived Space', () => {
    renderPanel(makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' }))

    expect(screen.getByTestId('timezone-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('schedule-save').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('schedule-archived')).toBeTruthy()
  })
})
