// @vitest-environment jsdom
/**
 * Tests for `/s/{public_id}/rules`.
 *
 * ## The acceptance-critical property: generic over the registry
 *
 * `SpaceRulesPage` and `RuleParamsForm` are supposed to know nothing about any
 * one rule type — only `RuleTypeRead.params`, fetched at runtime from
 * `GET /rule-types`. "invents a rule type" below proves that directly: it
 * mocks the registry with a `rule_type` (`test_invented_thing`) that does not
 * exist in `rules/rules/registry.py`, mixing an `"integer"` and a
 * `"local_time"` param, and asserts the "Add a rule" picker offers it and its
 * params form renders real, working inputs for it. If this page ever grew an
 * `if (rule_type === 'max_duration')` branch anywhere, the invented type would
 * render nothing and this test would fail — a type registered later (by a
 * future task, or Stream 7's generation loop) is exactly this case.
 *
 * Everything else here is the ordinary admin-screen shape every other panel
 * in this dashboard already follows: a member sees a notice and no writes are
 * attempted, an admin sees the manager, and every write handles its outcomes
 * the same way the mocked API can produce them.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import {
  createSpaceRule,
  deleteSpaceRule,
  getSpace,
  listRuleDrafts,
  listRuleTypes,
  listSpaceRules,
  updateSpaceRule,
  type RuleTypeRead,
} from '../api'
import { makeRuleParam, makeRuleType, makeSpace, makeSpaceRule, ok } from './fixtures'
import { SpaceRulesPage } from './SpaceRulesPage'

vi.mock('../api', () => ({
  getSpace: vi.fn(),
  listRuleTypes: vi.fn(),
  listSpaceRules: vi.fn(),
  createSpaceRule: vi.fn(),
  updateSpaceRule: vi.fn(),
  deleteSpaceRule: vi.fn(),
  createRuleDraft: vi.fn(),
  listRuleDrafts: vi.fn(),
  getRuleDraft: vi.fn(),
}))

/** A rule type the real registry does not have — see the module docstring. */
const INVENTED_RULE_TYPE: RuleTypeRead = {
  rule_type: 'test_invented_thing',
  label: 'Invented Thing (test only)',
  description: 'Refuses whatever this invented test type is configured to refuse.',
  priority: 999,
  reads_history: false,
  needs_local_resolution: false,
  is_single: false,
  params: [
    makeRuleParam({
      name: 'threshold',
      kind: 'integer',
      label: 'Threshold',
      unit: null,
      required: true,
      minimum: 1,
    }),
    makeRuleParam({
      name: 'cutoff_time',
      kind: 'local_time',
      label: 'Cutoff time',
      unit: null,
      required: false,
      minimum: null,
    }),
  ],
}

beforeEach(() => {
  vi.mocked(getSpace).mockResolvedValue(ok(makeSpace({ my_role: 'admin' })))
  vi.mocked(listRuleTypes).mockResolvedValue(ok([makeRuleType()]))
  vi.mocked(listSpaceRules).mockResolvedValue(ok([]))
  // `RuleAuthoringPanel` is not this file's subject — see
  // `RuleAuthoringPanel.test.tsx` — so it is defaulted out of the way here by
  // reading as "generation disabled", the same 404 a normally-configured
  // backend serves for these routes.
  vi.mocked(listRuleDrafts).mockResolvedValue({
    outcome: 'not_found',
    message: "We couldn't find that.",
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPage(publicId = 'sp_7f3a9c') {
  return render(
    <MemoryRouter initialEntries={[`/s/${publicId}/rules`]}>
      <Routes>
        <Route path="/s/:publicId/rules" element={<SpaceRulesPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('SpaceRulesPage — access', () => {
  it('shows a member notice for a plain member and asks the server for nothing else', async () => {
    vi.mocked(getSpace).mockResolvedValue(ok(makeSpace({ my_role: 'member' })))

    renderPage()

    expect(await screen.findByTestId('rules-member-notice')).toBeTruthy()
    expect(vi.mocked(listRuleTypes)).not.toHaveBeenCalled()
    expect(vi.mocked(listSpaceRules)).not.toHaveBeenCalled()
  })

  it('shows the rules manager for an admin', async () => {
    renderPage()

    expect(await screen.findByTestId('add-rule-panel')).toBeTruthy()
    expect(await screen.findByTestId('rules-list')).toBeTruthy()
    expect(screen.getByTestId('rules-empty')).toBeTruthy()
  })
})

describe('SpaceRulesPage — generic over the registry', () => {
  it('offers, and renders real inputs for, a rule type the test invents', async () => {
    vi.mocked(listRuleTypes).mockResolvedValue(ok([makeRuleType(), INVENTED_RULE_TYPE]))

    renderPage()

    const select = (await screen.findByTestId('add-rule-type-select')) as HTMLSelectElement
    const optionLabels = Array.from(select.options).map((option) => option.textContent)
    expect(optionLabels).toContain('Invented Thing (test only)')

    fireEvent.change(select, { target: { value: 'test_invented_thing' } })

    const integerInput = (await screen.findByTestId('add-rule-param-threshold')) as HTMLInputElement
    expect(integerInput.type).toBe('number')

    const timeInput = screen.getByTestId('add-rule-param-cutoff_time') as HTMLInputElement
    expect(timeInput.type).toBe('time')
  })

  it("renders the selected type's description, proving the picker is not a hardcoded list", async () => {
    vi.mocked(listRuleTypes).mockResolvedValue(ok([makeRuleType(), INVENTED_RULE_TYPE]))

    renderPage()

    const select = await screen.findByTestId('add-rule-type-select')
    expect(screen.queryByTestId('add-rule-type-description')).toBeNull()

    fireEvent.change(select, { target: { value: 'test_invented_thing' } })

    const description = await screen.findByTestId('add-rule-type-description')
    expect(description.textContent).toBe(INVENTED_RULE_TYPE.description)
  })
})

describe('SpaceRulesPage — create, pause, delete', () => {
  it('creates a rule and shows it in the list', async () => {
    const created = makeSpaceRule({
      id: 5,
      rule_type: 'max_duration',
      params: { max_duration_minutes: 90 },
    })
    vi.mocked(createSpaceRule).mockResolvedValue(ok(created))

    renderPage()

    fireEvent.change(await screen.findByTestId('add-rule-type-select'), {
      target: { value: 'max_duration' },
    })
    fireEvent.change(await screen.findByTestId('add-rule-param-max_duration_minutes'), {
      target: { value: '90' },
    })
    fireEvent.click(screen.getByTestId('add-rule-submit'))

    expect(await screen.findByTestId('rule-5')).toBeTruthy()
    expect(vi.mocked(createSpaceRule)).toHaveBeenCalledWith('sp_7f3a9c', {
      rule_type: 'max_duration',
      params: { max_duration_minutes: 90 },
    })
  })

  it('pauses a configured rule', async () => {
    vi.mocked(listSpaceRules).mockResolvedValue(ok([makeSpaceRule({ id: 5, enabled: true })]))
    vi.mocked(updateSpaceRule).mockResolvedValue(ok(makeSpaceRule({ id: 5, enabled: false })))

    renderPage()

    fireEvent.click(await screen.findByTestId('rule-5-toggle-enabled'))

    expect(await screen.findByTestId('rule-5-paused-badge')).toBeTruthy()
    expect(vi.mocked(updateSpaceRule)).toHaveBeenCalledWith('sp_7f3a9c', 5, { enabled: false })
  })

  it('deletes a rule after the confirm step', async () => {
    vi.mocked(listSpaceRules).mockResolvedValue(ok([makeSpaceRule({ id: 5 })]))
    vi.mocked(deleteSpaceRule).mockResolvedValue(ok(null))

    renderPage()

    fireEvent.click(await screen.findByTestId('rule-5-delete-start'))
    fireEvent.click(await screen.findByTestId('rule-5-delete-confirm-yes'))

    await vi.waitFor(() => expect(screen.queryByTestId('rule-5')).toBeNull())
    expect(vi.mocked(deleteSpaceRule)).toHaveBeenCalledWith('sp_7f3a9c', 5)
  })
})

describe('SpaceRulesPage — the fail-closed row made visible', () => {
  it('renders the broken banner for a row whose stored params fail its own type schema', async () => {
    vi.mocked(listRuleTypes).mockResolvedValue(ok([makeRuleType()]))
    vi.mocked(listSpaceRules).mockResolvedValue(
      ok([makeSpaceRule({ id: 9, rule_type: 'max_duration', params: {} })]),
    )

    renderPage()

    const banner = await screen.findByTestId('rule-9-broken')
    expect(banner.textContent).toContain('Missing required parameter')
  })
})
