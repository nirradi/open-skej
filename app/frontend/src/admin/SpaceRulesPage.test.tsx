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
import { failed, makeRuleParam, makeRuleType, makeSpace, makeSpaceRule, ok } from './fixtures'
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
    // `AddRulePanel` now carries its own `AppliesToEditor`, defaulted to
    // "always" (`{ mode: 'always' }`), which converts to `null` on the wire
    // (`appliesToFromDraft`) — task 9.7 made create send `applies_to` always,
    // not only once a scope is chosen.
    expect(vi.mocked(createSpaceRule)).toHaveBeenCalledWith('sp_7f3a9c', {
      rule_type: 'max_duration',
      params: { max_duration_minutes: 90 },
      applies_to: null,
    })
  })

  it('creates a rule scoped to a weekday, and the scope reaches the wire body', async () => {
    // Task 9.7's task-1: scope is editable at create time, not only as a
    // later edit on the row. This proves the create call itself carries the
    // chosen `applies_to` rather than always defaulting to "always".
    const created = makeSpaceRule({
      id: 6,
      rule_type: 'max_duration',
      params: { max_duration_minutes: 90 },
      applies_to: { weekdays: [2] },
    })
    vi.mocked(createSpaceRule).mockResolvedValue(ok(created))

    renderPage()

    fireEvent.change(await screen.findByTestId('add-rule-type-select'), {
      target: { value: 'max_duration' },
    })
    fireEvent.change(await screen.findByTestId('add-rule-param-max_duration_minutes'), {
      target: { value: '90' },
    })
    fireEvent.click(screen.getByTestId('add-rule-applies-mode-weekdays'))
    fireEvent.click(screen.getByTestId('add-rule-applies-weekday-2'))
    fireEvent.click(screen.getByTestId('add-rule-submit'))

    expect(await screen.findByTestId('rule-6')).toBeTruthy()
    expect(vi.mocked(createSpaceRule)).toHaveBeenCalledWith('sp_7f3a9c', {
      rule_type: 'max_duration',
      params: { max_duration_minutes: 90 },
      applies_to: { weekdays: [2] },
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

/**
 * Task 9.7 — the page-level save model: `RulesManager` holds one draft per
 * configured row, `rules-save` writes only the dirty ones (one `PATCH` each,
 * carrying `params` and `applies_to` together), `rules-cancel` discards every
 * draft with no request, and a dirty row is visibly marked
 * (`rule-{id}-changed`). This replaces the old per-row `-params-save` /
 * `-applies-save` buttons, which no longer exist anywhere in this component.
 */
describe('SpaceRulesPage — page-level save', () => {
  it('disables Save on arrival, enables it once a row is edited, and disables it again once the edit is undone', async () => {
    vi.mocked(listSpaceRules).mockResolvedValue(
      ok([makeSpaceRule({ id: 100, params: { max_duration_minutes: 90 } })]),
    )

    renderPage()

    const saveButton = (await screen.findByTestId('rules-save')) as HTMLButtonElement
    expect(saveButton.disabled).toBe(true)

    const paramInput = screen.getByTestId('rule-100-param-max_duration_minutes')
    fireEvent.change(paramInput, { target: { value: '120' } })
    expect(saveButton.disabled).toBe(false)
    expect(screen.getByTestId('rule-100-changed')).toBeTruthy()

    fireEvent.change(paramInput, { target: { value: '90' } })
    expect(saveButton.disabled).toBe(true)
    expect(screen.queryByTestId('rule-100-changed')).toBeNull()
  })

  it('writes only the dirty row, leaving the untouched rows alone', async () => {
    vi.mocked(listSpaceRules).mockResolvedValue(
      ok([
        makeSpaceRule({ id: 100, params: { max_duration_minutes: 60 } }),
        makeSpaceRule({ id: 101, params: { max_duration_minutes: 90 } }),
        makeSpaceRule({ id: 102, params: { max_duration_minutes: 120 } }),
      ]),
    )
    vi.mocked(updateSpaceRule).mockResolvedValue(
      ok(makeSpaceRule({ id: 101, params: { max_duration_minutes: 75 } })),
    )

    renderPage()

    fireEvent.change(await screen.findByTestId('rule-101-param-max_duration_minutes'), {
      target: { value: '75' },
    })
    fireEvent.click(screen.getByTestId('rules-save'))

    await vi.waitFor(() => expect(vi.mocked(updateSpaceRule)).toHaveBeenCalledTimes(1))
    expect(vi.mocked(updateSpaceRule)).toHaveBeenCalledWith('sp_7f3a9c', 101, {
      applies_to: null,
      params: { max_duration_minutes: 75 },
    })
  })

  it('sends params and applies_to together in one PATCH when both were edited on the same row', async () => {
    vi.mocked(listSpaceRules).mockResolvedValue(
      ok([makeSpaceRule({ id: 100, params: { max_duration_minutes: 90 }, applies_to: null })]),
    )
    vi.mocked(updateSpaceRule).mockResolvedValue(
      ok(
        makeSpaceRule({
          id: 100,
          params: { max_duration_minutes: 120 },
          applies_to: { weekdays: [3] },
        }),
      ),
    )

    renderPage()

    fireEvent.change(await screen.findByTestId('rule-100-param-max_duration_minutes'), {
      target: { value: '120' },
    })
    fireEvent.click(screen.getByTestId('rule-100-applies-mode-weekdays'))
    fireEvent.click(screen.getByTestId('rule-100-applies-weekday-3'))
    fireEvent.click(screen.getByTestId('rules-save'))

    await vi.waitFor(() => expect(vi.mocked(updateSpaceRule)).toHaveBeenCalledTimes(1))
    expect(vi.mocked(updateSpaceRule)).toHaveBeenCalledWith('sp_7f3a9c', 100, {
      applies_to: { weekdays: [3] },
      params: { max_duration_minutes: 120 },
    })
  })

  it('cancel restores every field to its stored value and makes no request', async () => {
    vi.mocked(listSpaceRules).mockResolvedValue(
      ok([makeSpaceRule({ id: 100, params: { max_duration_minutes: 90 }, applies_to: null })]),
    )

    renderPage()

    const paramInput = (await screen.findByTestId(
      'rule-100-param-max_duration_minutes',
    )) as HTMLInputElement
    fireEvent.change(paramInput, { target: { value: '120' } })
    fireEvent.click(screen.getByTestId('rule-100-applies-mode-weekdays'))
    fireEvent.click(screen.getByTestId('rule-100-applies-weekday-1'))
    expect(screen.getByTestId('rule-100-changed')).toBeTruthy()

    fireEvent.click(screen.getByTestId('rules-cancel'))

    expect(paramInput.value).toBe('90')
    expect((screen.getByTestId('rule-100-applies-mode-always') as HTMLInputElement).checked).toBe(
      true,
    )
    expect(screen.queryByTestId('rule-100-changed')).toBeNull()
    expect(vi.mocked(updateSpaceRule)).not.toHaveBeenCalled()
  })

  it('marks only the row that changed', async () => {
    vi.mocked(listSpaceRules).mockResolvedValue(
      ok([
        makeSpaceRule({ id: 100, params: { max_duration_minutes: 60 } }),
        makeSpaceRule({ id: 101, params: { max_duration_minutes: 90 } }),
      ]),
    )

    renderPage()

    fireEvent.change(await screen.findByTestId('rule-100-param-max_duration_minutes'), {
      target: { value: '75' },
    })

    expect(screen.getByTestId('rule-100-changed')).toBeTruthy()
    expect(screen.queryByTestId('rule-101-changed')).toBeNull()
  })

  it('on partial failure, clears the row that saved and keeps the row that failed dirty and named', async () => {
    vi.mocked(listSpaceRules).mockResolvedValue(
      ok([
        makeSpaceRule({ id: 100, rule_type: 'max_duration', params: { max_duration_minutes: 60 } }),
        makeSpaceRule({ id: 101, rule_type: 'max_duration', params: { max_duration_minutes: 90 } }),
      ]),
    )
    // `updateSpaceRule` is called once per dirty row (`Promise.allSettled`),
    // never a bulk request — row 100 succeeds and echoes the edited value
    // back, row 101 fails.
    vi.mocked(updateSpaceRule).mockImplementation(async (_publicId, ruleId) => {
      if (ruleId === 100) {
        return ok(makeSpaceRule({ id: 100, params: { max_duration_minutes: 65 } }))
      }
      return failed('The server had a problem.')
    })

    renderPage()

    fireEvent.change(await screen.findByTestId('rule-100-param-max_duration_minutes'), {
      target: { value: '65' },
    })
    fireEvent.change(await screen.findByTestId('rule-101-param-max_duration_minutes'), {
      target: { value: '95' },
    })
    fireEvent.click(screen.getByTestId('rules-save'))

    await vi.waitFor(() => expect(vi.mocked(updateSpaceRule)).toHaveBeenCalledTimes(2))

    // The succeeded row goes clean — its "changed" marker disappears.
    await vi.waitFor(() => expect(screen.queryByTestId('rule-100-changed')).toBeNull())
    // The failed row stays dirty and marked, its edited value intact.
    expect(screen.getByTestId('rule-101-changed')).toBeTruthy()
    expect(
      (screen.getByTestId('rule-101-param-max_duration_minutes') as HTMLInputElement).value,
    ).toBe('95')

    const errorBanner = await screen.findByTestId('rules-save-error')
    expect(errorBanner.textContent).toContain('rule 101')
    expect(errorBanner.textContent).toContain('The server had a problem.')
    expect(errorBanner.textContent).not.toContain('rule 100')
  })

  it('disables Save on an archived Space', async () => {
    vi.mocked(getSpace).mockResolvedValue(
      ok(makeSpace({ my_role: 'admin', archived_at: '2026-07-20T09:00:00.000Z' })),
    )
    vi.mocked(listSpaceRules).mockResolvedValue(
      ok([makeSpaceRule({ id: 100, params: { max_duration_minutes: 90 } })]),
    )

    renderPage()

    expect(await screen.findByTestId('rules-archived-banner')).toBeTruthy()
    const saveButton = (await screen.findByTestId('rules-save')) as HTMLButtonElement
    expect(saveButton.disabled).toBe(true)
  })
})
