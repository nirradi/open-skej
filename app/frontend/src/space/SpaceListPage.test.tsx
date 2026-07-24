// @vitest-environment jsdom
/**
 * Tests for the minimal Space-list landing page.
 *
 * Deliberately thin, matching the component: this exists to prove the three
 * states render (loading, error, populated/empty) and that each Space links
 * to its `/s/{public_id}`, not to cover navigation or picking a Resource —
 * that is the next task's surface.
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import { listSpaces } from '../api'
import type { ApiOk, Space } from '../api'
import { SpaceListPage } from './SpaceListPage'

vi.mock('../api', () => ({ listSpaces: vi.fn() }))

function ok<T>(data: T): ApiOk<T> {
  return { outcome: 'ok', data }
}

function makeSpace(overrides: Partial<Space> = {}): Space {
  return {
    public_id: 'aBcDeFgHiJkLmNoPqRsTuV',
    name: 'Tennis Club',
    description: null,
    created_at: '2026-07-01T00:00:00Z',
    archived_at: null,
    my_role: 'member',
    ...overrides,
  }
}

beforeEach(() => {
  vi.mocked(listSpaces).mockResolvedValue(ok([]))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPage() {
  return render(
    <MemoryRouter>
      <SpaceListPage />
    </MemoryRouter>,
  )
}

describe('SpaceListPage', () => {
  it('shows a loading state while the request is in flight', () => {
    vi.mocked(listSpaces).mockReturnValue(new Promise(() => {}))

    renderPage()

    expect(screen.getByTestId('space-list-loading')).toBeTruthy()
  })

  it('shows an empty state for a user in no Spaces', async () => {
    renderPage()

    expect(await screen.findByTestId('space-list-empty')).toBeTruthy()
  })

  it('lists each Space with a link to its preview route', async () => {
    const space = makeSpace()
    vi.mocked(listSpaces).mockResolvedValue(ok([space]))

    renderPage()

    const link = await screen.findByTestId(`space-list-item-${space.public_id}`)
    expect(link.textContent).toBe('Tennis Club')
    expect(link.getAttribute('href')).toBe(`/s/${space.public_id}`)
  })

  it('reports a failure rather than an empty list', async () => {
    vi.mocked(listSpaces).mockResolvedValue({ outcome: 'failed', message: 'The network went away.' })

    renderPage()

    const error = await screen.findByTestId('space-list-error')
    expect(error.textContent).toContain('The network went away.')
  })
})
