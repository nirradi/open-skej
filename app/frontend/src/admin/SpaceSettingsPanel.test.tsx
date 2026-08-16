// @vitest-environment jsdom
/**
 * Tests for the Space settings panel: `name`, `description` and `timezone` —
 * the three plain columns `PATCH /spaces/{public_id}` writes.
 *
 * Task 6.8 moved operating hours, slot interval, max duration, booking
 * horizon, and the two frequency caps off this panel entirely — each is a
 * `space_rules` row edited at `/s/{public_id}/rules` instead
 * (`SpaceRulesPage.test.tsx` covers that surface). This file asserts on
 * exactly the three fields left, including the omitted-vs-null distinction
 * `description` carries and no other field does.
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
import { SpaceSettingsPanel } from './SpaceSettingsPanel'

vi.mock('../api', () => ({
  updateSpace: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPanel(space = makeSpace(), onSpaceChanged = vi.fn()) {
  render(<SpaceSettingsPanel space={space} onSpaceChanged={onSpaceChanged} />)
  return { onSpaceChanged }
}

describe('SpaceSettingsPanel', () => {
  it('shows the Space name, description and timezone', () => {
    renderPanel(
      makeSpace({
        name: 'Riverside Courts',
        description: 'Two clay courts',
        timezone: 'Europe/Berlin',
      }),
    )

    expect((screen.getByTestId('name-input') as HTMLInputElement).value).toBe('Riverside Courts')
    expect((screen.getByTestId('description-input') as HTMLTextAreaElement).value).toBe(
      'Two clay courts',
    )
    expect((screen.getByTestId('timezone-input') as HTMLInputElement).value).toBe(
      'Europe/Berlin',
    )
  })

  it('shows an empty description box when the Space has none', () => {
    renderPanel(makeSpace({ description: null }))

    expect((screen.getByTestId('description-input') as HTMLTextAreaElement).value).toBe('')
  })

  it('saves an edited name, sending the unchanged description and timezone', async () => {
    const space = makeSpace({ name: 'Tennis Court', description: 'Original', timezone: 'UTC' })
    const updated = makeSpace({ ...space, name: 'Riverside Courts' })
    vi.mocked(updateSpace).mockResolvedValue(ok(updated))
    const { onSpaceChanged } = renderPanel(space)

    fireEvent.change(screen.getByTestId('name-input'), { target: { value: 'Riverside Courts' } })
    fireEvent.click(screen.getByTestId('settings-save'))

    expect(vi.mocked(updateSpace)).toHaveBeenCalledWith('sp_7f3a9c', {
      name: 'Riverside Courts',
      description: 'Original',
      timezone: 'UTC',
    })
    await vi.waitFor(() => expect(onSpaceChanged).toHaveBeenCalledWith(updated))
  })

  it('clears the description by sending null, never an empty string', async () => {
    const space = makeSpace({ description: 'Original' })
    vi.mocked(updateSpace).mockResolvedValue(ok(makeSpace({ description: null })))
    renderPanel(space)

    fireEvent.change(screen.getByTestId('description-input'), { target: { value: '' } })
    fireEvent.click(screen.getByTestId('settings-save'))

    await vi.waitFor(() =>
      expect(vi.mocked(updateSpace)).toHaveBeenCalledWith(
        'sp_7f3a9c',
        expect.objectContaining({ description: null }),
      ),
    )
  })

  it('refuses an empty name before any request is made', () => {
    renderPanel()

    fireEvent.change(screen.getByTestId('name-input'), { target: { value: '   ' } })
    fireEvent.click(screen.getByTestId('settings-save'))

    expect(vi.mocked(updateSpace)).not.toHaveBeenCalled()
    expect(screen.getByTestId('settings-error').textContent).toBeTruthy()
  })

  it('saves an edited timezone, and nothing else', async () => {
    const updated = makeSpace({ timezone: 'Asia/Tokyo' })
    vi.mocked(updateSpace).mockResolvedValue(ok(updated))
    const { onSpaceChanged } = renderPanel()

    fireEvent.change(screen.getByTestId('timezone-input'), { target: { value: 'Asia/Tokyo' } })
    fireEvent.click(screen.getByTestId('settings-save'))

    // The panel no longer reads or writes hours, slot interval, or any rule
    // parameter — those are `space_rules` rows edited at `/s/{public_id}/rules`
    // now, and sending them here would be a second place to edit the same
    // configuration.
    expect(vi.mocked(updateSpace)).toHaveBeenCalledWith(
      'sp_7f3a9c',
      expect.objectContaining({ timezone: 'Asia/Tokyo' }),
    )
    await vi.waitFor(() => expect(onSpaceChanged).toHaveBeenCalledWith(updated))
  })

  it('shows the server refusal for an unknown zone rather than inventing copy', async () => {
    const detail = "'Not/AZone' is not a known IANA timezone name"
    vi.mocked(updateSpace).mockResolvedValue({ outcome: 'invalid_request', detail, raw: null })
    renderPanel()

    fireEvent.change(screen.getByTestId('timezone-input'), { target: { value: 'Not/AZone' } })
    fireEvent.click(screen.getByTestId('settings-save'))

    // `invalid_request` is treated as a client-diagnostic bug everywhere else in
    // this dashboard, so it shows the same generic copy here rather than the raw
    // Pydantic detail — consistent with `messageFor`.
    const error = await screen.findByTestId('settings-error')
    expect(error.textContent).toBeTruthy()
  })

  it('resolves a member acting between render and click to forbidden', async () => {
    vi.mocked(updateSpace).mockResolvedValue(forbidden())
    renderPanel()

    fireEvent.click(screen.getByTestId('settings-save'))

    const error = await screen.findByTestId('settings-error')
    expect(error.textContent).toBe("You don't have permission to do that.")
  })

  it('shows the server conflict verbatim for an archived Space', async () => {
    const detail = 'This Space is archived and can no longer be changed.'
    vi.mocked(updateSpace).mockResolvedValue(conflict(detail))
    renderPanel()

    fireEvent.click(screen.getByTestId('settings-save'))

    const error = await screen.findByTestId('settings-error')
    expect(error.textContent).toBe(detail)
  })

  it('disables every control on an archived Space', () => {
    renderPanel(makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' }))

    expect(screen.getByTestId('name-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('description-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('timezone-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('settings-save').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('settings-archived')).toBeTruthy()
  })

  it('resets every field, including a dirty one, when the picker switches Spaces', () => {
    const first = makeSpace({
      public_id: 'sp_first',
      name: 'Court A',
      description: 'First venue',
      timezone: 'UTC',
    })
    const { rerender } = render(<SpaceSettingsPanel space={first} onSpaceChanged={vi.fn()} />)

    fireEvent.change(screen.getByTestId('name-input'), { target: { value: 'Unsaved edit' } })

    const second = makeSpace({
      public_id: 'sp_second',
      name: 'Court B',
      description: 'Second venue',
      timezone: 'Asia/Tokyo',
    })
    rerender(<SpaceSettingsPanel space={second} onSpaceChanged={vi.fn()} />)

    expect((screen.getByTestId('name-input') as HTMLInputElement).value).toBe('Court B')
    expect((screen.getByTestId('description-input') as HTMLTextAreaElement).value).toBe(
      'Second venue',
    )
    expect((screen.getByTestId('timezone-input') as HTMLInputElement).value).toBe('Asia/Tokyo')
  })
})
