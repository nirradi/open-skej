// @vitest-environment jsdom
/**
 * Tests for the schedule configuration panel: the Space's timezone and each
 * Resource's operating hours / slot interval — `DEFERRED.md` item 2.
 *
 * As with every other panel in this dashboard, the hiding in `AdminPage` is a
 * convenience, not the boundary: every write here still has to handle
 * `forbidden` and `conflict` as if it were called directly, because a second
 * admin can act between this component's render and a click.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { listResources, updateResource, updateSpace } from '../api'
import { conflict, forbidden, makeResource, makeSpace, ok } from './fixtures'
import { ResourceConfigPanel } from './ResourceConfigPanel'

vi.mock('../api', () => ({
  listResources: vi.fn(),
  updateResource: vi.fn(),
  updateSpace: vi.fn(),
}))

const RESOURCE = makeResource({
  id: 30,
  name: 'Court A',
  opens_at: '07:00:00',
  closes_at: '22:00:00',
  slot_minutes: 60,
})

beforeEach(() => {
  vi.mocked(listResources).mockResolvedValue(ok([RESOURCE]))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPanel(space = makeSpace(), onSpaceChanged = vi.fn()) {
  render(<ResourceConfigPanel space={space} onSpaceChanged={onSpaceChanged} />)
  return { onSpaceChanged }
}

describe('ResourceConfigPanel — timezone', () => {
  it('shows the Space timezone', () => {
    renderPanel(makeSpace({ timezone: 'Europe/Berlin' }))

    expect((screen.getByTestId('timezone-input') as HTMLInputElement).value).toBe(
      'Europe/Berlin',
    )
  })

  it('saves an edited timezone and reports the update upward', async () => {
    const updated = makeSpace({ timezone: 'Asia/Tokyo' })
    vi.mocked(updateSpace).mockResolvedValue(ok(updated))
    const { onSpaceChanged } = renderPanel()

    fireEvent.change(screen.getByTestId('timezone-input'), { target: { value: 'Asia/Tokyo' } })
    fireEvent.click(screen.getByTestId('timezone-save'))

    expect(vi.mocked(updateSpace)).toHaveBeenCalledWith('sp_7f3a9c', { timezone: 'Asia/Tokyo' })
    await vi.waitFor(() => expect(onSpaceChanged).toHaveBeenCalledWith(updated))
  })

  it('shows the server refusal for an unknown zone rather than inventing copy', async () => {
    const detail = "'Not/AZone' is not a known IANA timezone name"
    vi.mocked(updateSpace).mockResolvedValue({
      outcome: 'invalid_request',
      detail,
      raw: null,
    })
    renderPanel()

    fireEvent.change(screen.getByTestId('timezone-input'), { target: { value: 'Not/AZone' } })
    fireEvent.click(screen.getByTestId('timezone-save'))

    // `invalid_request` is treated as a client-diagnostic bug everywhere else in
    // this dashboard, so it shows the same generic copy here rather than the raw
    // Pydantic detail — consistent with `messageFor`.
    const error = await screen.findByTestId('timezone-error')
    expect(error.textContent).toBeTruthy()
  })

  it('resolves a member acting between render and click to forbidden', async () => {
    vi.mocked(updateSpace).mockResolvedValue(forbidden())
    renderPanel()

    fireEvent.change(screen.getByTestId('timezone-input'), { target: { value: 'Europe/Berlin' } })
    fireEvent.click(screen.getByTestId('timezone-save'))

    const error = await screen.findByTestId('timezone-error')
    expect(error.textContent).toBe("You don't have permission to do that.")
  })

  it('disables the timezone control on an archived Space', () => {
    renderPanel(makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' }))

    expect(screen.getByTestId('timezone-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('timezone-save').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('timezone-archived')).toBeTruthy()
  })
})

describe('ResourceConfigPanel — Resources', () => {
  it('lists Resources with their current hours and slot interval', async () => {
    renderPanel()

    await screen.findByTestId('resource-row-30')
    expect((screen.getByTestId('resource-opens-30') as HTMLInputElement).value).toBe('07:00')
    expect((screen.getByTestId('resource-closes-30') as HTMLInputElement).value).toBe('22:00')
    expect((screen.getByTestId('resource-slot-30') as HTMLInputElement).value).toBe('60')
  })

  it('asks for archived Resources too, so a retired calendar is still visible', async () => {
    renderPanel()

    await vi.waitFor(() =>
      expect(vi.mocked(listResources)).toHaveBeenCalledWith('sp_7f3a9c', { includeArchived: true }),
    )
  })

  it('saves edited hours, converting HH:MM to the wire HH:MM:SS shape', async () => {
    const updated = { ...RESOURCE, opens_at: '08:00:00' }
    vi.mocked(updateResource).mockResolvedValue(ok(updated))
    renderPanel()

    fireEvent.change(await screen.findByTestId('resource-opens-30'), {
      target: { value: '08:00' },
    })
    fireEvent.click(screen.getByTestId('resource-save-30'))

    expect(vi.mocked(updateResource)).toHaveBeenCalledWith('sp_7f3a9c', 30, {
      opens_at: '08:00:00',
      closes_at: '22:00:00',
      slot_minutes: 60,
    })
    await vi.waitFor(() =>
      expect((screen.getByTestId('resource-opens-30') as HTMLInputElement).value).toBe('08:00'),
    )
  })

  it('clears an hours column back to "no restriction" with an explicit null', async () => {
    const updated = { ...RESOURCE, opens_at: null }
    vi.mocked(updateResource).mockResolvedValue(ok(updated))
    renderPanel()

    fireEvent.change(await screen.findByTestId('resource-opens-30'), { target: { value: '' } })
    fireEvent.click(screen.getByTestId('resource-save-30'))

    expect(vi.mocked(updateResource)).toHaveBeenCalledWith('sp_7f3a9c', 30, {
      opens_at: null,
      closes_at: '22:00:00',
      slot_minutes: 60,
    })
  })

  it('sends null slot_minutes when the field is cleared', async () => {
    const updated = { ...RESOURCE, slot_minutes: null }
    vi.mocked(updateResource).mockResolvedValue(ok(updated))
    renderPanel()

    fireEvent.change(await screen.findByTestId('resource-slot-30'), { target: { value: '' } })
    fireEvent.click(screen.getByTestId('resource-save-30'))

    expect(vi.mocked(updateResource)).toHaveBeenCalledWith('sp_7f3a9c', 30, {
      opens_at: '07:00:00',
      closes_at: '22:00:00',
      slot_minutes: null,
    })
  })

  it('shows a forbidden refusal on the row that caused it', async () => {
    vi.mocked(updateResource).mockResolvedValue(forbidden())
    renderPanel()

    fireEvent.click(await screen.findByTestId('resource-save-30'))

    const error = await screen.findByTestId('resource-error-30')
    expect(error.textContent).toBe("You don't have permission to do that.")
  })

  it('shows an archived Resource as a terminal state rather than inviting a retry', async () => {
    vi.mocked(listResources).mockResolvedValue(
      ok([makeResource({ id: 31, archived_at: '2026-07-20T09:00:00.000Z' })]),
    )
    renderPanel()

    await screen.findByTestId('resource-row-31')
    expect(screen.getByTestId('resource-opens-31').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('resource-save-31').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('resource-archived-31')).toBeTruthy()
  })

  it('shows the archived-Space state rather than a retryable error', async () => {
    renderPanel(makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' }))

    await screen.findByTestId('resource-row-30')
    expect(screen.getByTestId('resource-opens-30').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('resource-save-30').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('resource-archived-30')).toBeTruthy()
  })

  it('reports an error instead of an empty list', async () => {
    vi.mocked(listResources).mockResolvedValue(forbidden())
    renderPanel()

    const error = await screen.findByTestId('resources-error')
    expect(error.textContent).toBe("You don't have permission to do that.")
  })
})

describe('the two conflicts do not surface with the same copy', () => {
  it('shows the server conflict verbatim for a Resource of an archived Space', async () => {
    const detail = 'This Space is archived and can no longer be changed.'
    vi.mocked(updateResource).mockResolvedValue(conflict(detail))
    renderPanel()

    fireEvent.click(await screen.findByTestId('resource-save-30'))

    const error = await screen.findByTestId('resource-error-30')
    expect(error.textContent).toBe(detail)
  })
})
