// @vitest-environment jsdom
/**
 * Tests for the Space share link and its copy button.
 *
 * The failure path is the interesting one. Clipboard access can be refused — an
 * insecure origin, a withheld permission, a headless browser — and a copy button
 * whose only feedback is "Copied!" strands the admin when it silently did not.
 * Since this link *is* the capability, and the only way anyone else ever reaches
 * the Space, a swallowed clipboard error means a Space nobody can be invited to.
 * So the rejection is asserted to produce a visible, different message, with the
 * link still on screen to select by hand.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ShareLink } from './ShareLink'
import { spaceShareUrl } from './shareUrl'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

/** Installs a clipboard that either resolves or rejects. jsdom ships none. */
function stubClipboard(writeText: () => Promise<void>) {
  const spy = vi.fn(writeText)
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: spy },
    configurable: true,
  })
  return spy
}

describe('spaceShareUrl', () => {
  it('points at the link-holder route on an absolute origin', () => {
    // Absolute because the entire purpose is to be pasted into a chat window,
    // where a relative path means nothing.
    expect(spaceShareUrl('sp_7f3a9c', 'https://skej.example')).toBe(
      'https://skej.example/s/sp_7f3a9c',
    )
  })

  it('defaults to this origin', () => {
    expect(spaceShareUrl('sp_7f3a9c')).toBe(`${window.location.origin}/s/sp_7f3a9c`)
  })
})

describe('ShareLink', () => {
  it('shows the link as text, not only behind the button', () => {
    render(<ShareLink publicId="sp_7f3a9c" />)

    // Selectable on screen is what makes the button a shortcut rather than the
    // sole route to the link.
    expect(screen.getByTestId('share-link').textContent).toBe(
      `${window.location.origin}/s/sp_7f3a9c`,
    )
  })

  it('says nothing until the button is pressed', () => {
    render(<ShareLink publicId="sp_7f3a9c" />)

    expect(screen.queryByTestId('share-link-status')).toBeNull()
  })

  it('confirms a successful copy', async () => {
    const writeText = stubClipboard(() => Promise.resolve())
    render(<ShareLink publicId="sp_7f3a9c" />)

    fireEvent.click(screen.getByTestId('share-link-copy'))

    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/s/sp_7f3a9c`)
    const status = await screen.findByTestId('share-link-status')
    expect(status.textContent).toContain('copied')
  })

  it('admits it when the clipboard refuses', async () => {
    stubClipboard(() => Promise.reject(new Error('denied')))
    render(<ShareLink publicId="sp_7f3a9c" />)

    fireEvent.click(screen.getByTestId('share-link-copy'))

    const status = await screen.findByTestId('share-link-status')
    // Not "Copied!" — the admin has to know to copy it by hand.
    expect(status.textContent).toContain("couldn't copy")
    // And the link is still there to do that with.
    expect(screen.getByTestId('share-link').textContent).toContain('/s/sp_7f3a9c')
  })
})
