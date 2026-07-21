// @vitest-environment jsdom
/**
 * Tests for the `/admin` dashboard as a whole.
 *
 * ## The two assertions that are really requirements
 *
 * **A plain member sees no admin controls.** Not disabled ones — absent ones.
 * This is a usability guarantee rather than a security one: `require_space_role`
 * re-checks every call behind this page and is the only thing that actually
 * stops anybody, since a determined member can edit the bundle they were served.
 * What the hiding buys is that a member is not shown six controls that would
 * each fail with a 403. The invitation list matters most, because it names
 * people who are *not* in the Space — who is being recruited is not every
 * member's business.
 *
 * **Nothing here calls `useAuth0()`.** With `VITE_AUTH0_*` unset there is no
 * `Auth0Provider` in the tree at all — `AuthProvider` deliberately keeps
 * rendering the app so the unauthenticated calendar at `/` survives a missing
 * tenant, which is the regression that took twelve Playwright tests down during
 * task 2.8. Calling the hook in that state throws. The test below proves the
 * property directly by mocking the SDK so that *any* call to `useAuth0` throws,
 * then rendering the page: that fails loudly if someone later reaches for the
 * hook, in a way that reading the imports would not, since the call could arrive
 * through any child.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { listAccessRequests, listInvitations, listMembers, listSpaces } from '../api'
import { AdminPage } from './AdminPage'
import { failed, makeMember, makeSpace, ok } from './fixtures'

vi.mock('../api', () => ({
  listSpaces: vi.fn(),
  createSpace: vi.fn(),
  listMembers: vi.fn(),
  updateMemberRole: vi.fn(),
  removeMember: vi.fn(),
  listAccessRequests: vi.fn(),
  approveAccessRequest: vi.fn(),
  denyAccessRequest: vi.fn(),
  listInvitations: vi.fn(),
  createInvitation: vi.fn(),
  revokeInvitation: vi.fn(),
  archiveSpace: vi.fn(),
}))

/**
 * A tripwire, not a stub. If anything under `/admin` ever calls `useAuth0`, it
 * throws here — and would throw for real users whenever Auth0 is unconfigured.
 */
vi.mock('@auth0/auth0-react', () => ({
  useAuth0: () => {
    throw new Error('useAuth0() must not be called from the admin dashboard')
  },
}))

beforeEach(() => {
  vi.mocked(listSpaces).mockResolvedValue(ok([makeSpace()]))
  vi.mocked(listMembers).mockResolvedValue(ok([makeMember()]))
  vi.mocked(listAccessRequests).mockResolvedValue(ok([]))
  vi.mocked(listInvitations).mockResolvedValue(ok([]))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AdminPage', () => {
  it('shows a loading state before the Spaces arrive', () => {
    vi.mocked(listSpaces).mockReturnValue(new Promise(() => {}))

    render(<AdminPage />)

    expect(screen.getByTestId('spaces-loading')).toBeTruthy()
  })

  it('reports an error instead of an empty dashboard', async () => {
    vi.mocked(listSpaces).mockResolvedValue(failed('The network went away.'))

    render(<AdminPage />)

    const error = await screen.findByTestId('spaces-error')
    expect(error.textContent).toBe('The network went away.')
    // "You have no Spaces" and "we could not find out" are different facts, and
    // showing the first for the second invites the admin to create a duplicate.
    expect(screen.queryByTestId('spaces-empty')).toBeNull()
  })

  it('explains the empty case without making it look broken', async () => {
    vi.mocked(listSpaces).mockResolvedValue(ok([]))

    render(<AdminPage />)

    const empty = await screen.findByTestId('spaces-empty')
    expect(empty.textContent).toContain('not in any Spaces')
    // The one control that still makes sense with no Spaces stays available.
    expect(screen.getByTestId('create-space-panel')).toBeTruthy()
    expect(screen.queryByTestId('space-picker')).toBeNull()
  })

  it('includes archived Spaces so they can still be seen', async () => {
    render(<AdminPage />)
    await screen.findByTestId('space-picker')

    // An archived Space that vanished from the picker would look deleted, and
    // there is no way to bring one back.
    expect(vi.mocked(listSpaces)).toHaveBeenCalledWith({ includeArchived: true })
  })

  it('renders the full set of panels for an owner', async () => {
    render(<AdminPage />)

    expect(await screen.findByTestId('space-admin')).toBeTruthy()

    // Each panel is awaited separately: they fetch independently, so `space-admin`
    // appearing means a Space is selected, not that any panel has loaded. A
    // synchronous `getByTestId` here would race the panel's own loading state and
    // is what made this test fail against a perfectly correct dashboard.
    expect(await screen.findByTestId('requests-panel')).toBeTruthy()
    expect(await screen.findByTestId('members-panel')).toBeTruthy()
    expect(await screen.findByTestId('invitations-panel')).toBeTruthy()
    expect(await screen.findByTestId('archive-panel')).toBeTruthy()
  })

  it('hides every admin control from a plain member', async () => {
    vi.mocked(listSpaces).mockResolvedValue(ok([makeSpace({ my_role: 'member' })]))

    render(<AdminPage />)

    expect(await screen.findByTestId('member-notice')).toBeTruthy()
    // Absent, not disabled. In particular the invitation list names people who
    // are not in the Space at all.
    expect(screen.queryByTestId('space-admin')).toBeNull()
    expect(screen.queryByTestId('members-panel')).toBeNull()
    expect(screen.queryByTestId('requests-panel')).toBeNull()
    expect(screen.queryByTestId('invitations-panel')).toBeNull()
    expect(screen.queryByTestId('archive-panel')).toBeNull()
  })

  it('does not even ask the server for what a member may not see', async () => {
    vi.mocked(listSpaces).mockResolvedValue(ok([makeSpace({ my_role: 'member' })]))

    render(<AdminPage />)
    await screen.findByTestId('member-notice')

    // Rendering the panels and letting them 403 would work, but it would fill a
    // member's screen with errors and the audit log with refusals.
    expect(vi.mocked(listMembers)).not.toHaveBeenCalled()
    expect(vi.mocked(listAccessRequests)).not.toHaveBeenCalled()
    expect(vi.mocked(listInvitations)).not.toHaveBeenCalled()
  })

  it('does not offer archiving to an admin who is not the owner', async () => {
    vi.mocked(listSpaces).mockResolvedValue(ok([makeSpace({ my_role: 'admin' })]))

    render(<AdminPage />)

    // Archiving is owner-only on the server, so offering it would be a button
    // that always 403s.
    expect(await screen.findByTestId('members-panel')).toBeTruthy()
    expect(screen.queryByTestId('archive-panel')).toBeNull()
  })

  it('says plainly when a Space is archived', async () => {
    vi.mocked(listSpaces).mockResolvedValue(
      ok([makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' })]),
    )

    render(<AdminPage />)

    expect(await screen.findByTestId('archived-banner')).toBeTruthy()
  })

  it('marks archived Spaces in the picker', async () => {
    vi.mocked(listSpaces).mockResolvedValue(
      ok([
        makeSpace({ public_id: 'sp_live', name: 'Court A' }),
        makeSpace({ public_id: 'sp_old', name: 'Court B', archived_at: '2026-07-20T09:00:00.000Z' }),
      ]),
    )

    render(<AdminPage />)

    const picker = (await screen.findByTestId('space-picker')) as HTMLSelectElement
    const labels = Array.from(picker.options).map((option) => option.textContent)
    expect(labels).toEqual(['Court A', 'Court B (archived)'])
  })

  it('switches the panels when another Space is picked', async () => {
    vi.mocked(listSpaces).mockResolvedValue(
      ok([
        makeSpace({ public_id: 'sp_live', name: 'Court A' }),
        makeSpace({ public_id: 'sp_other', name: 'Court B', my_role: 'member' }),
      ]),
    )

    render(<AdminPage />)
    await screen.findByTestId('members-panel')

    fireEvent.change(screen.getByTestId('space-picker'), { target: { value: 'sp_other' } })

    // The second Space is one this user is only a member of, so the panels have
    // to re-decide rather than carrying the first Space's role over.
    expect(await screen.findByTestId('member-notice')).toBeTruthy()
    expect(screen.queryByTestId('members-panel')).toBeNull()
  })

  it('shows the share link for the selected Space', async () => {
    render(<AdminPage />)

    const link = await screen.findByTestId('share-link')
    expect(link.textContent).toContain('/s/sp_7f3a9c')
  })

  it('renders without ever calling useAuth0', async () => {
    // The mocked SDK throws on any call. Reaching a rendered dashboard is the
    // proof: with Auth0 unconfigured there is no provider in the tree, and a
    // hook call would take this page down exactly as it took the calendar down
    // in 2.8.
    render(<AdminPage />)

    expect(await screen.findByTestId('space-admin')).toBeTruthy()
  })
})
