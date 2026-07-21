// @vitest-environment jsdom
/**
 * Tests for archiving a Space.
 *
 * The api client is mocked wholesale — no network, no tenant. What is under test
 * is the confirmation gate, and the assertion that carries the weight is the
 * negative one: **cancelling must archive nothing.**
 *
 * That is worth a dedicated test rather than being assumed from reading the
 * component, because archiving has no inverse. There is no un-archive endpoint,
 * and an archived Space refuses every later mutation with a 409, so a
 * confirmation that did not actually gate the call would destroy a Space on a
 * misclick and no amount of happy-path coverage would notice. A test that only
 * clicked through to "yes" passes just as happily against a component with no
 * confirmation step at all.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { archiveSpace } from '../api'
import { ArchiveSpacePanel } from './ArchiveSpacePanel'
import { conflict, makeSpace, ok } from './fixtures'

vi.mock('../api', () => ({ archiveSpace: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPanel(space = makeSpace(), onArchived = vi.fn()) {
  render(<ArchiveSpacePanel space={space} onArchived={onArchived} />)
  return { onArchived }
}

describe('ArchiveSpacePanel', () => {
  it('does not offer to archive until the first button is pressed', () => {
    renderPanel()

    expect(screen.getByTestId('archive-start')).toBeTruthy()
    expect(screen.queryByTestId('archive-confirm')).toBeNull()
    expect(screen.queryByTestId('archive-confirm-yes')).toBeNull()
  })

  it('asks for confirmation, naming the Space', () => {
    renderPanel(makeSpace({ name: 'Centre Court' }))

    fireEvent.click(screen.getByTestId('archive-start'))

    expect(screen.getByTestId('archive-confirm')).toBeTruthy()
    // The name is in the prompt so an admin with several Spaces open cannot
    // confirm the wrong one from muscle memory.
    expect(screen.getByTestId('archive-confirm').textContent).toContain('Centre Court')
    // Still nothing sent: reaching the prompt is not consent.
    expect(vi.mocked(archiveSpace)).not.toHaveBeenCalled()
  })

  it('archives nothing when the confirmation is cancelled', () => {
    const { onArchived } = renderPanel()

    fireEvent.click(screen.getByTestId('archive-start'))
    fireEvent.click(screen.getByTestId('archive-cancel'))

    // The whole point of the gate. If this ever fails, a misclick is
    // unrecoverable — there is no endpoint that undoes it.
    expect(vi.mocked(archiveSpace)).not.toHaveBeenCalled()
    expect(onArchived).not.toHaveBeenCalled()
    // And the panel is back to its resting state, not stuck mid-prompt.
    expect(screen.queryByTestId('archive-confirm')).toBeNull()
    expect(screen.getByTestId('archive-start')).toBeTruthy()
  })

  it('archives once the confirmation is accepted', async () => {
    const archived = makeSpace({ archived_at: '2026-07-21T09:00:00.000Z' })
    vi.mocked(archiveSpace).mockResolvedValue(ok(archived))
    const { onArchived } = renderPanel()

    fireEvent.click(screen.getByTestId('archive-start'))
    fireEvent.click(screen.getByTestId('archive-confirm-yes'))

    expect(vi.mocked(archiveSpace)).toHaveBeenCalledWith('sp_7f3a9c')
    // The parent is handed the updated Space rather than a bare signal, so the
    // list it owns reflects the new state without a refetch.
    await vi.waitFor(() => expect(onArchived).toHaveBeenCalledWith(archived))
  })

  it('shows the server refusal rather than swallowing it', async () => {
    vi.mocked(archiveSpace).mockResolvedValue(conflict('This Space is archived.'))
    renderPanel()

    fireEvent.click(screen.getByTestId('archive-start'))
    fireEvent.click(screen.getByTestId('archive-confirm-yes'))

    const error = await screen.findByTestId('archive-error')
    expect(error.textContent).toBe('This Space is archived.')
  })

  it('offers nothing to archive on an already-archived Space', () => {
    renderPanel(makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' }))

    expect(screen.getByTestId('archive-already')).toBeTruthy()
    // Not merely disabled — absent. A second archive would be a guaranteed 409.
    expect(screen.queryByTestId('archive-start')).toBeNull()
  })
})
