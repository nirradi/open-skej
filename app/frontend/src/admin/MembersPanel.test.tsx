// @vitest-environment jsdom
/**
 * Tests for the member list, role changes and removal.
 *
 * ## The test this file exists for
 *
 * Demoting or removing the last owner is refused by the server with a 409 whose
 * body says what to do instead. Two tests below assert that **that exact
 * sentence** reaches the screen.
 *
 * This is not decorative. Before the client modelled `conflict` as its own
 * outcome, a 409 fell through to the generic "Something went wrong on our end",
 * which is both false — nothing went wrong, the server enforced a rule — and
 * useless, because it names no remedy. An admin reading it retries the identical
 * click forever. The server is the only thing that knows the rule, so it owns
 * the copy, and the client's whole job is to not lose it.
 *
 * The other half of the file is the `forbidden` handling. Every action here
 * re-checks it rather than assuming the panel's admin-only rendering was
 * sufficient, because the UI is never the security boundary and the picture it
 * drew can be stale by the time a button is clicked.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { listMembers, removeMember, updateMemberRole } from '../api'
import { conflict, forbidden, LAST_OWNER_MESSAGE, makeMember, makeSpace, ok } from './fixtures'
import { MembersPanel } from './MembersPanel'

vi.mock('../api', () => ({
  listMembers: vi.fn(),
  removeMember: vi.fn(),
  updateMemberRole: vi.fn(),
}))

const OWNER = makeMember({ user_id: 1, email: 'ada@example.com', role: 'owner' })
const MEMBER = makeMember({
  user_id: 2,
  email: 'grace@example.com',
  name: 'Grace Hopper',
  role: 'member',
})

beforeEach(() => {
  vi.mocked(listMembers).mockResolvedValue(ok([OWNER, MEMBER]))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPanel(space = makeSpace(), onMembershipChanged = vi.fn()) {
  render(<MembersPanel space={space} refreshToken={0} onMembershipChanged={onMembershipChanged} />)
  return { onMembershipChanged }
}

describe('MembersPanel', () => {
  it('shows a loading state before the list arrives', () => {
    vi.mocked(listMembers).mockReturnValue(new Promise(() => {}))

    renderPanel()

    expect(screen.getByTestId('members-loading')).toBeTruthy()
  })

  it('reports an error instead of an empty list', async () => {
    vi.mocked(listMembers).mockResolvedValue(forbidden("You don't have permission to do that."))

    renderPanel()

    const error = await screen.findByTestId('members-error')
    expect(error.textContent).toBe("You don't have permission to do that.")
    expect(screen.queryByTestId('members-empty')).toBeNull()
  })

  it('renders each member with their role', async () => {
    renderPanel()

    await screen.findByTestId('member-1')
    expect(screen.getByTestId('member-2').textContent).toContain('Grace Hopper')
    expect((screen.getByTestId('member-role-1') as HTMLSelectElement).value).toBe('owner')
    expect((screen.getByTestId('member-role-2') as HTMLSelectElement).value).toBe('member')
  })

  it('changes a role and refreshes the list', async () => {
    vi.mocked(updateMemberRole).mockResolvedValue(ok({ ...MEMBER, role: 'admin' }))
    const { onMembershipChanged } = renderPanel()

    fireEvent.change(await screen.findByTestId('member-role-2'), { target: { value: 'admin' } })

    expect(vi.mocked(updateMemberRole)).toHaveBeenCalledWith('sp_7f3a9c', 2, 'admin')
    await vi.waitFor(() => expect(onMembershipChanged).toHaveBeenCalledTimes(1))
  })

  it('removes a member', async () => {
    // `null`, not `undefined`: `removeMember` is declared `MutatingResult<null>`,
    // and a 204 carries no body to distinguish them at runtime — so only the
    // typecheck catches the mismatch, which `vitest run` alone does not perform.
    vi.mocked(removeMember).mockResolvedValue(ok(null))
    const { onMembershipChanged } = renderPanel()

    fireEvent.click(await screen.findByTestId('member-remove-2'))

    expect(vi.mocked(removeMember)).toHaveBeenCalledWith('sp_7f3a9c', 2)
    await vi.waitFor(() => expect(onMembershipChanged).toHaveBeenCalledTimes(1))
  })

  it('shows the last-owner refusal verbatim when demoting', async () => {
    vi.mocked(updateMemberRole).mockResolvedValue(conflict())
    renderPanel()

    fireEvent.change(await screen.findByTestId('member-role-1'), { target: { value: 'member' } })

    const error = await screen.findByTestId('member-error-1')
    // The server's sentence, not a paraphrase and not generic failure copy. It
    // names the remedy — promote someone else first — which is the only thing
    // that gets the admin unstuck.
    expect(error.textContent).toBe(LAST_OWNER_MESSAGE)
    expect(error.textContent).not.toContain('went wrong')
  })

  it('shows the last-owner refusal verbatim when removing', async () => {
    vi.mocked(removeMember).mockResolvedValue(conflict())
    renderPanel()

    fireEvent.click(await screen.findByTestId('member-remove-1'))

    const error = await screen.findByTestId('member-error-1')
    expect(error.textContent).toBe(LAST_OWNER_MESSAGE)
  })

  it('keeps a refusal on the row that caused it', async () => {
    vi.mocked(removeMember).mockResolvedValue(conflict())
    renderPanel()

    fireEvent.click(await screen.findByTestId('member-remove-1'))

    await screen.findByTestId('member-error-1')
    expect(screen.queryByTestId('member-error-2')).toBeNull()
  })

  it('lets an admin see an owner without offering to assign that role', async () => {
    // An admin may not grant `owner`, but must still see that Ada *is* one — a
    // select that silently displayed the wrong value would be worse than one
    // with a disabled option.
    renderPanel(makeSpace({ my_role: 'admin' }))

    const select = (await screen.findByTestId('member-role-1')) as HTMLSelectElement
    expect(select.value).toBe('owner')
    const offered = Array.from(select.options).map((option) => option.value)
    expect(offered).toContain('owner')
    // Present exactly once: as the current value, not as something to pick.
    expect(offered.filter((value) => value === 'owner')).toHaveLength(1)

    const memberSelect = screen.getByTestId('member-role-2') as HTMLSelectElement
    expect(Array.from(memberSelect.options).map((o) => o.value)).toEqual(['admin', 'member'])
  })

  it('offers owner as assignable to an owner', async () => {
    renderPanel(makeSpace({ my_role: 'owner' }))

    const select = (await screen.findByTestId('member-role-2')) as HTMLSelectElement
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['owner', 'admin', 'member'])
  })

  it('disables every control on an archived Space', async () => {
    renderPanel(makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' }))

    await screen.findByTestId('member-1')
    expect(screen.getByTestId('member-role-2').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('member-remove-2').hasAttribute('disabled')).toBe(true)
  })
})
