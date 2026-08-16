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
 *
 * ## The console is now sections behind a side nav (task 9.6)
 *
 * `SpaceAdmin` no longer renders every panel in one flat stack — it renders a
 * left-hand nav of three sections (People, Resources, Settings) plus a fourth
 * entry, Rules, that is a `<Link>` out to `/s/{public_id}/rules` rather than a
 * section that swaps in place. Only the selected section's panels are mounted
 * at a time, so most of the assertions below are about *which* panels are
 * present for a given section, not just that the dashboard renders at all.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import { listAccessRequests, listInvitations, listMembers, listResources, listSpaces } from '../api'
import { AdminPage } from './AdminPage'
import { failed, makeMember, makeSpace, ok } from './fixtures'

/**
 * `AdminPage` links to `/s/{public_id}/rules` (the "Manage rules" panel added
 * in task 6.8), so it needs a router in the tree the same way any other
 * screen with a `<Link>` does — rendering it bare throws on the `useContext`
 * that `<Link>` reads.
 */
function renderAdminPage() {
  return render(
    <MemoryRouter>
      <AdminPage />
    </MemoryRouter>,
  )
}

vi.mock('../api', () => ({
  listSpaces: vi.fn(),
  createSpace: vi.fn(),
  updateSpace: vi.fn(),
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
  listResources: vi.fn(),
  createResource: vi.fn(),
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
  vi.mocked(listResources).mockResolvedValue(ok([]))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AdminPage', () => {
  it('shows a loading state before the Spaces arrive', () => {
    vi.mocked(listSpaces).mockReturnValue(new Promise(() => {}))

    renderAdminPage()

    expect(screen.getByTestId('spaces-loading')).toBeTruthy()
  })

  it('reports an error instead of an empty dashboard', async () => {
    vi.mocked(listSpaces).mockResolvedValue(failed('The network went away.'))

    renderAdminPage()

    const error = await screen.findByTestId('spaces-error')
    expect(error.textContent).toBe('The network went away.')
    // "You have no Spaces" and "we could not find out" are different facts, and
    // showing the first for the second invites the admin to create a duplicate.
    expect(screen.queryByTestId('spaces-empty')).toBeNull()
  })

  it('explains the empty case without making it look broken', async () => {
    vi.mocked(listSpaces).mockResolvedValue(ok([]))

    renderAdminPage()

    const empty = await screen.findByTestId('spaces-empty')
    expect(empty.textContent).toContain('not in any Spaces')
    // The one control that still makes sense with no Spaces stays available.
    expect(screen.getByTestId('create-space-panel')).toBeTruthy()
    expect(screen.queryByTestId('space-picker')).toBeNull()
  })

  it('includes archived Spaces so they can still be seen', async () => {
    renderAdminPage()
    await screen.findByTestId('space-picker')

    // An archived Space that vanished from the picker would look deleted, and
    // there is no way to bring one back.
    expect(vi.mocked(listSpaces)).toHaveBeenCalledWith({ includeArchived: true })
  })

  it('opens on the People section, which holds the access-request queue', async () => {
    renderAdminPage()

    expect(await screen.findByTestId('space-admin')).toBeTruthy()

    // Each panel is awaited separately: they fetch independently, so `space-admin`
    // appearing means a Space is selected, not that any panel has loaded. A
    // synchronous `getByTestId` here would race the panel's own loading state and
    // is what made this test fail against a perfectly correct dashboard.
    expect(await screen.findByTestId('requests-panel')).toBeTruthy()
    expect(await screen.findByTestId('members-panel')).toBeTruthy()
    expect(await screen.findByTestId('invitations-panel')).toBeTruthy()

    // Only People is mounted at a time — the flat stack is gone.
    expect(screen.queryByTestId('resources-panel')).toBeNull()
    expect(screen.queryByTestId('space-settings-panel')).toBeNull()
    expect(screen.queryByTestId('share-link')).toBeNull()
    expect(screen.queryByTestId('archive-panel')).toBeNull()
  })

  it('shows only the Resources panel once Resources is selected', async () => {
    renderAdminPage()
    await screen.findByTestId('requests-panel')

    fireEvent.click(screen.getByTestId('admin-nav-resources'))

    expect(await screen.findByTestId('resources-panel')).toBeTruthy()
    expect(screen.queryByTestId('requests-panel')).toBeNull()
    expect(screen.queryByTestId('members-panel')).toBeNull()
    expect(screen.queryByTestId('invitations-panel')).toBeNull()
    expect(screen.queryByTestId('space-settings-panel')).toBeNull()
  })

  it('shows Settings — including the share link and archive — only once selected', async () => {
    vi.mocked(listSpaces).mockResolvedValue(ok([makeSpace({ my_role: 'owner' })]))

    renderAdminPage()
    await screen.findByTestId('requests-panel')

    fireEvent.click(screen.getByTestId('admin-nav-settings'))

    expect(await screen.findByTestId('space-settings-panel')).toBeTruthy()
    const link = await screen.findByTestId('share-link')
    expect(link.textContent).toContain('/s/sp_7f3a9c')
    expect(screen.getByTestId('archive-panel')).toBeTruthy()

    // People's own panels are gone now that Settings is showing.
    expect(screen.queryByTestId('requests-panel')).toBeNull()
    expect(screen.queryByTestId('members-panel')).toBeNull()
    expect(screen.queryByTestId('invitations-panel')).toBeNull()
    expect(screen.queryByTestId('resources-panel')).toBeNull()
  })

  it('switches content when the section changes, with the picker staying above the nav', async () => {
    renderAdminPage()
    await screen.findByTestId('requests-panel')

    const picker = screen.getByTestId('space-picker-panel')
    const nav = screen.getByTestId('admin-nav')
    // `DOCUMENT_POSITION_FOLLOWING` on the nav (relative to the picker) means
    // the picker precedes the nav in the DOM, i.e. renders above it.
    expect(picker.compareDocumentPosition(nav) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    fireEvent.click(screen.getByTestId('admin-nav-resources'))
    expect(await screen.findByTestId('resources-panel')).toBeTruthy()

    // The picker and nav are both still there after switching sections.
    expect(screen.getByTestId('space-picker-panel')).toBeTruthy()
    expect(screen.getByTestId('admin-nav')).toBeTruthy()

    fireEvent.click(screen.getByTestId('admin-nav-people'))
    expect(await screen.findByTestId('requests-panel')).toBeTruthy()
  })

  it('renders Rules as a link out, not a section that swaps in place', async () => {
    renderAdminPage()
    await screen.findByTestId('requests-panel')

    const rulesLink = screen.getByTestId('space-rules-link')
    expect(rulesLink.tagName).toBe('A')
    expect(rulesLink.getAttribute('href')).toBe('/s/sp_7f3a9c/rules')

    fireEvent.click(rulesLink)

    // Clicking it must not swap the main-area content the way a section click
    // does — People's panels (or whatever was showing) are unaffected, because
    // this is a real navigation, not a state change `SpaceAdmin` reacts to.
    expect(screen.getByTestId('requests-panel')).toBeTruthy()
  })

  it('hides every admin control from a plain member, including the section nav', async () => {
    vi.mocked(listSpaces).mockResolvedValue(ok([makeSpace({ my_role: 'member' })]))

    renderAdminPage()

    expect(await screen.findByTestId('member-notice')).toBeTruthy()
    // Absent, not disabled. In particular the invitation list names people who
    // are not in the Space at all, and there is no nav list to click into.
    expect(screen.queryByTestId('space-admin')).toBeNull()
    expect(screen.queryByTestId('admin-nav')).toBeNull()
    expect(screen.queryByTestId('members-panel')).toBeNull()
    expect(screen.queryByTestId('requests-panel')).toBeNull()
    expect(screen.queryByTestId('invitations-panel')).toBeNull()
    expect(screen.queryByTestId('space-settings-panel')).toBeNull()
    expect(screen.queryByTestId('resources-panel')).toBeNull()
    expect(screen.queryByTestId('archive-panel')).toBeNull()
    expect(screen.queryByTestId('space-rules-link')).toBeNull()
  })

  it('does not even ask the server for what a member may not see', async () => {
    vi.mocked(listSpaces).mockResolvedValue(ok([makeSpace({ my_role: 'member' })]))

    renderAdminPage()
    await screen.findByTestId('member-notice')

    // Rendering the panels and letting them 403 would work, but it would fill a
    // member's screen with errors and the audit log with refusals.
    expect(vi.mocked(listMembers)).not.toHaveBeenCalled()
    expect(vi.mocked(listAccessRequests)).not.toHaveBeenCalled()
    expect(vi.mocked(listInvitations)).not.toHaveBeenCalled()
    expect(vi.mocked(listResources)).not.toHaveBeenCalled()
  })

  it('shows Archive inside Settings for an owner but not for an admin', async () => {
    vi.mocked(listSpaces).mockResolvedValue(ok([makeSpace({ my_role: 'owner' })]))

    renderAdminPage()
    await screen.findByTestId('requests-panel')
    fireEvent.click(screen.getByTestId('admin-nav-settings'))

    expect(await screen.findByTestId('archive-panel')).toBeTruthy()
  })

  it('does not offer archiving to an admin who is not the owner', async () => {
    vi.mocked(listSpaces).mockResolvedValue(ok([makeSpace({ my_role: 'admin' })]))

    renderAdminPage()
    await screen.findByTestId('requests-panel')

    // Archiving is owner-only on the server, so offering it would be a button
    // that always 403s. Settings still renders for an admin — just without it.
    fireEvent.click(screen.getByTestId('admin-nav-settings'))
    expect(await screen.findByTestId('space-settings-panel')).toBeTruthy()
    expect(screen.queryByTestId('archive-panel')).toBeNull()
  })

  it('says plainly when a Space is archived', async () => {
    vi.mocked(listSpaces).mockResolvedValue(
      ok([makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' })]),
    )

    renderAdminPage()

    expect(await screen.findByTestId('archived-banner')).toBeTruthy()
  })

  it('marks archived Spaces in the picker', async () => {
    vi.mocked(listSpaces).mockResolvedValue(
      ok([
        makeSpace({ public_id: 'sp_live', name: 'Court A' }),
        makeSpace({
          public_id: 'sp_old',
          name: 'Court B',
          archived_at: '2026-07-20T09:00:00.000Z',
        }),
      ]),
    )

    renderAdminPage()

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

    renderAdminPage()
    await screen.findByTestId('members-panel')

    fireEvent.change(screen.getByTestId('space-picker'), { target: { value: 'sp_other' } })

    // The second Space is one this user is only a member of, so the panels have
    // to re-decide rather than carrying the first Space's role over.
    expect(await screen.findByTestId('member-notice')).toBeTruthy()
    expect(screen.queryByTestId('members-panel')).toBeNull()
  })

  it('keeps the selected section when the Space changes in the picker', async () => {
    vi.mocked(listSpaces).mockResolvedValue(
      ok([
        makeSpace({ public_id: 'sp_live', name: 'Court A' }),
        makeSpace({ public_id: 'sp_other', name: 'Court B', my_role: 'owner' }),
      ]),
    )

    renderAdminPage()
    await screen.findByTestId('requests-panel')

    fireEvent.click(screen.getByTestId('admin-nav-resources'))
    expect(await screen.findByTestId('resources-panel')).toBeTruthy()

    fireEvent.change(screen.getByTestId('space-picker'), { target: { value: 'sp_other' } })

    // Still on Resources for the new Space — the section is a view of the
    // console, not of one Space, so switching Spaces must not reset it back
    // to People.
    expect(await screen.findByTestId('resources-panel')).toBeTruthy()
    expect(screen.queryByTestId('requests-panel')).toBeNull()
  })

  it('renders without ever calling useAuth0', async () => {
    // The mocked SDK throws on any call. Reaching a rendered dashboard is the
    // proof: with Auth0 unconfigured there is no provider in the tree, and a
    // hook call would take this page down exactly as it took the calendar down
    // in 2.8.
    renderAdminPage()

    expect(await screen.findByTestId('space-admin')).toBeTruthy()
  })
})
