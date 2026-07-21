// @vitest-environment jsdom
/**
 * Tests for creating a Space.
 *
 * The assertion doing real work is that the share link appears in the create
 * response and is rendered straight away. `public_id` is the only handle to a
 * new Space that will ever exist — nothing enumerates Spaces and there is no
 * lookup by name — so if this screen dropped it, the creator would keep their
 * own access through their membership while permanently losing the ability to
 * bring anyone else in. The Space would be unshareable, and nothing about it
 * would look broken.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createSpace } from '../api'
import { CreateSpaceForm } from './CreateSpaceForm'
import { failed, makeSpace, ok } from './fixtures'

vi.mock('../api', () => ({ createSpace: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderForm(onCreated = vi.fn()) {
  render(<CreateSpaceForm onCreated={onCreated} />)
  return { onCreated }
}

describe('CreateSpaceForm', () => {
  it('creates a Space with a name and description', async () => {
    const space = makeSpace({ name: 'Centre Court', description: 'The good one' })
    vi.mocked(createSpace).mockResolvedValue(ok(space))
    const { onCreated } = renderForm()

    fireEvent.change(screen.getByTestId('space-name'), { target: { value: 'Centre Court' } })
    fireEvent.change(screen.getByTestId('space-description'), {
      target: { value: 'The good one' },
    })
    fireEvent.click(screen.getByTestId('space-create-submit'))

    expect(vi.mocked(createSpace)).toHaveBeenCalledWith('Centre Court', 'The good one')
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith(space))
  })

  it('sends a null description rather than an empty string', async () => {
    vi.mocked(createSpace).mockResolvedValue(ok(makeSpace()))
    renderForm()

    fireEvent.change(screen.getByTestId('space-name'), { target: { value: 'Centre Court' } })
    fireEvent.click(screen.getByTestId('space-create-submit'))

    // `description` is nullable on the wire, and an empty string is a different
    // value from absent — it would render as a blank description rather than none.
    expect(vi.mocked(createSpace)).toHaveBeenCalledWith('Centre Court', null)
  })

  it('trims the name', async () => {
    vi.mocked(createSpace).mockResolvedValue(ok(makeSpace()))
    renderForm()

    fireEvent.change(screen.getByTestId('space-name'), { target: { value: '  Centre Court  ' } })
    fireEvent.click(screen.getByTestId('space-create-submit'))

    expect(vi.mocked(createSpace)).toHaveBeenCalledWith('Centre Court', null)
  })

  it('refuses a blank name without asking the server', () => {
    renderForm()

    fireEvent.change(screen.getByTestId('space-name'), { target: { value: '   ' } })
    fireEvent.click(screen.getByTestId('space-create-submit'))

    expect(screen.getByTestId('create-space-error')).toBeTruthy()
    expect(vi.mocked(createSpace)).not.toHaveBeenCalled()
  })

  it('shows the share link as soon as the Space exists', async () => {
    // The name comes back from the *server response*, not from what was typed —
    // the server is the source of truth for what was actually created — so the
    // fixture has to carry it too, not just the `public_id` under test.
    vi.mocked(createSpace).mockResolvedValue(
      ok(makeSpace({ name: 'Centre Court', public_id: 'sp_new123' })),
    )
    renderForm()

    fireEvent.change(screen.getByTestId('space-name'), { target: { value: 'Centre Court' } })
    fireEvent.click(screen.getByTestId('space-create-submit'))

    // The one moment this value is guaranteed to be in front of the creator.
    const created = await screen.findByTestId('created-space')
    expect(created.textContent).toContain('Centre Court')
    expect(screen.getByTestId('share-link').textContent).toContain('/s/sp_new123')
  })

  it('clears the form after a success so the next Space starts empty', async () => {
    vi.mocked(createSpace).mockResolvedValue(ok(makeSpace()))
    renderForm()

    fireEvent.change(screen.getByTestId('space-name'), { target: { value: 'Centre Court' } })
    fireEvent.click(screen.getByTestId('space-create-submit'))

    await screen.findByTestId('created-space')
    expect((screen.getByTestId('space-name') as HTMLInputElement).value).toBe('')
  })

  it('reports a failure and creates nothing', async () => {
    vi.mocked(createSpace).mockResolvedValue(failed('The network went away.'))
    const { onCreated } = renderForm()

    fireEvent.change(screen.getByTestId('space-name'), { target: { value: 'Centre Court' } })
    fireEvent.click(screen.getByTestId('space-create-submit'))

    const error = await screen.findByTestId('create-space-error')
    expect(error.textContent).toBe('The network went away.')
    expect(onCreated).not.toHaveBeenCalled()
    // No share link for a Space that does not exist.
    expect(screen.queryByTestId('created-space')).toBeNull()
  })
})
