// @vitest-environment jsdom
/**
 * Tests for listing and creating Resources.
 *
 * `createResource` and the panel are new in this task; the coverage mirrors
 * `InvitationsPanel.test.tsx`'s shape since a list-plus-create-form panel is
 * exactly what both are.
 *
 * Two behaviours are pinned deliberately, because both are easy to lose:
 *
 * 1. **A created Resource joins the list in place.** No refetch of the whole
 *    panel — the new row appears from the create response alone.
 * 2. **`forbidden` is handled explicitly**, not merely assumed away by the
 *    fact that the panel is only rendered for an admin — `messageFor` renders
 *    it like any other refusal.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createResource, listResources } from '../api'
import { conflict, failed, forbidden, makeResource, makeSpace, ok } from './fixtures'
import { ResourcesPanel } from './ResourcesPanel'

vi.mock('../api', () => ({
  listResources: vi.fn(),
  createResource: vi.fn(),
}))

beforeEach(() => {
  vi.mocked(listResources).mockResolvedValue(ok([]))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPanel(space = makeSpace(), onChanged = vi.fn()) {
  render(<ResourcesPanel space={space} onChanged={onChanged} />)
  return { onChanged }
}

describe('ResourcesPanel', () => {
  it('shows a loading state before the list arrives', () => {
    vi.mocked(listResources).mockReturnValue(new Promise(() => {}))

    renderPanel()

    expect(screen.getByTestId('resources-loading')).toBeTruthy()
  })

  it('asks for archived Resources too', () => {
    renderPanel(makeSpace({ public_id: 'sp_abc' }))

    expect(vi.mocked(listResources)).toHaveBeenCalledWith('sp_abc', { includeArchived: true })
  })

  it('says so when there are no Resources yet', async () => {
    renderPanel()

    expect(await screen.findByTestId('resources-empty')).toBeTruthy()
  })

  it('reports an error instead of an empty list', async () => {
    vi.mocked(listResources).mockResolvedValue(failed('The network went away.'))

    renderPanel()

    const error = await screen.findByTestId('resources-error')
    expect(error.textContent).toBe('The network went away.')
    expect(screen.queryByTestId('resources-empty')).toBeNull()
  })

  it('lists existing Resources, marking archived ones', async () => {
    vi.mocked(listResources).mockResolvedValue(
      ok([
        makeResource({ id: 1, name: 'Court A' }),
        makeResource({ id: 2, name: 'Court B', archived_at: '2026-07-10T09:00:00.000Z' }),
      ]),
    )

    renderPanel()

    expect(await screen.findByTestId('resource-1')).toBeTruthy()
    expect(screen.getByTestId('resource-2').textContent).toContain('Court B')
    expect(screen.getByTestId('resource-archived-2')).toBeTruthy()
    expect(screen.queryByTestId('resource-archived-1')).toBeNull()
  })

  it('creates a Resource and shows it immediately, with no refetch', async () => {
    const created = makeResource({ id: 9, name: 'Court C' })
    vi.mocked(createResource).mockResolvedValue(ok(created))
    const { onChanged } = renderPanel()
    await screen.findByTestId('resources-empty')

    fireEvent.change(screen.getByTestId('resource-name'), { target: { value: 'Court C' } })
    fireEvent.click(screen.getByTestId('resource-create-submit'))

    expect(vi.mocked(createResource)).toHaveBeenCalledWith('sp_7f3a9c', { name: 'Court C' })
    expect(await screen.findByTestId('resource-9')).toBeTruthy()
    expect(vi.mocked(listResources)).toHaveBeenCalledTimes(1)
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('refuses an empty name without asking the server', async () => {
    renderPanel()
    await screen.findByTestId('resources-empty')

    fireEvent.click(screen.getByTestId('resource-create-submit'))

    expect(await screen.findByTestId('resource-create-error')).toBeTruthy()
    expect(vi.mocked(createResource)).not.toHaveBeenCalled()
  })

  it('refuses a whitespace-only name without asking the server', async () => {
    renderPanel()
    await screen.findByTestId('resources-empty')

    fireEvent.change(screen.getByTestId('resource-name'), { target: { value: '   ' } })
    fireEvent.click(screen.getByTestId('resource-create-submit'))

    expect(await screen.findByTestId('resource-create-error')).toBeTruthy()
    expect(vi.mocked(createResource)).not.toHaveBeenCalled()
  })

  it('trims the name before sending it', async () => {
    vi.mocked(createResource).mockResolvedValue(ok(makeResource()))
    renderPanel()
    await screen.findByTestId('resources-empty')

    fireEvent.change(screen.getByTestId('resource-name'), { target: { value: '  Court D  ' } })
    fireEvent.click(screen.getByTestId('resource-create-submit'))

    expect(vi.mocked(createResource)).toHaveBeenCalledWith('sp_7f3a9c', { name: 'Court D' })
  })

  it('shows a forbidden create as an error on the form', async () => {
    vi.mocked(createResource).mockResolvedValue(forbidden())
    renderPanel()
    await screen.findByTestId('resources-empty')

    fireEvent.change(screen.getByTestId('resource-name'), { target: { value: 'Court E' } })
    fireEvent.click(screen.getByTestId('resource-create-submit'))

    const error = await screen.findByTestId('resource-create-error')
    expect(error.textContent).toBe("You don't have permission to do that.")
  })

  it('shows a conflict (archived Space) as an error on the form', async () => {
    vi.mocked(createResource).mockResolvedValue(
      conflict('This Space is archived and can no longer be changed.'),
    )
    renderPanel()
    await screen.findByTestId('resources-empty')

    fireEvent.change(screen.getByTestId('resource-name'), { target: { value: 'Court F' } })
    fireEvent.click(screen.getByTestId('resource-create-submit'))

    const error = await screen.findByTestId('resource-create-error')
    expect(error.textContent).toBe('This Space is archived and can no longer be changed.')
  })

  it('disables the form on an archived Space', async () => {
    vi.mocked(listResources).mockResolvedValue(ok([makeResource({ id: 1 })]))

    renderPanel(makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' }))

    await screen.findByTestId('resource-1')
    expect(screen.getByTestId('resource-name').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('resource-create-submit').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('resources-archived')).toBeTruthy()
  })
})
