import { useEffect, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'

import {
  createSpaceRule,
  deleteSpaceRule,
  getSpace,
  listRuleTypes,
  listSpaceRules,
  updateSpaceRule,
  type RuleTypeRead,
  type Space,
  type SpaceRuleRead,
  type SpaceRuleUpdateInput,
} from '../api'
import {
  AppliesToEditor,
  appliesToDraftIsValid,
  appliesToFromDraft,
  draftFromAppliesTo,
  type AppliesToDraft,
} from './AppliesToEditor'
import { messageFor } from './messages'
import { RuleAuthoringPanel } from './RuleAuthoringPanel'
import {
  RuleParamsForm,
  emptyRuleParamValues,
  ruleParamValuesFromStored,
  ruleParamValuesToWire,
  ruleParamsAreValid,
  type RuleParamValues,
} from './RuleParamsForm'
import { findRuleTypeFor, ruleBrokenReason } from './ruleValidation'

/**
 * `/s/{public_id}/rules` — the Space rules page, Requirement 2's visible half.
 *
 * ## Generic over the registry, not seven hand-written forms
 *
 * This page (and `RuleParamsForm` it builds each row's form from) knows
 * exactly one thing about a rule type: its declared parameter schema
 * (`RuleTypeRead.params`), fetched once from `GET /rule-types`. It contains
 * no `if (rule_type === 'max_duration')` anywhere — a type registered later
 * gets a working "add rule" option and a working edit form with zero changes
 * here. `SpaceRulesPage.test.tsx` proves this by inventing a rule type the
 * real registry does not have.
 *
 * ## admin+, with its own notice for a member who lands here directly
 *
 * Linked from `/admin`, but reachable by typing the URL too — the same way
 * every other route in this app is. A plain member sees the identical
 * `member-notice` treatment `AdminPage`'s `SpaceAdmin` already uses, and (like
 * every panel in this directory) that hiding is a convenience, not the
 * boundary: every write below still handles `forbidden`, since a second admin
 * can demote the caller between this page loading and a click landing.
 *
 * ## The one place an admin can see a Space silently refusing every booking
 *
 * A rule whose stored `params` no longer satisfy its own type's schema is
 * 6.6's fail-closed path in its silent form — the server enforces it without
 * saying why. `ruleValidation.ts`'s `ruleBrokenReason` re-runs that same
 * schema check client-side against the fetched registry and renders the
 * result as a visible banner per row, which is the only place in the product
 * that reason is shown to anyone.
 *
 * ## One save for the page, not one per row
 *
 * `RulesManager` holds a draft of every configured row's `params` and
 * `applies_to`, edits it in place, and writes it with a single page-level
 * Save (`rules-save`) or discards it with a single Cancel (`rules-cancel`).
 * `RuleRow` itself is presentational for both fields — it renders whatever
 * draft and validity it is handed and reports edits upward; it owns no
 * `params`/`applies_to` state of its own any more. Creating a rule (its own
 * "Add rule" button) and pausing or deleting one (`RuleRow`'s own immediate
 * actions) are unrelated to this draft/save machinery — see each one's own
 * docstring for why.
 */
export function SpaceRulesPage() {
  const { publicId } = useParams<{ publicId: string }>()

  if (!publicId) {
    return (
      <Shell>
        <p className="text-sm text-red-700" role="alert" data-testid="space-rules-invalid">
          That link doesn&rsquo;t work.
        </p>
      </Shell>
    )
  }

  return <SpaceRulesPageInner publicId={publicId} />
}

type SpaceLoad = { kind: 'ok'; space: Space } | { kind: 'error'; message: string } | null

function SpaceRulesPageInner({ publicId }: { publicId: string }) {
  const [spaceLoad, setSpaceLoad] = useState<SpaceLoad>(null)

  useEffect(() => {
    let cancelled = false

    void getSpace(publicId).then((result) => {
      if (cancelled) return
      setSpaceLoad(
        result.outcome === 'ok'
          ? { kind: 'ok', space: result.data }
          : { kind: 'error', message: messageFor(result) },
      )
    })

    return () => {
      cancelled = true
    }
  }, [publicId])

  if (spaceLoad === null) {
    return (
      <Shell>
        <p className="text-sm text-slate-600" data-testid="space-rules-loading" role="status">
          Loading…
        </p>
      </Shell>
    )
  }

  if (spaceLoad.kind === 'error') {
    return (
      <Shell>
        <p className="text-sm text-red-700" data-testid="space-rules-error" role="alert">
          {spaceLoad.message}
        </p>
      </Shell>
    )
  }

  const space = spaceLoad.space
  const isAdmin = space.my_role === 'admin' || space.my_role === 'owner'

  if (!isAdmin) {
    return (
      <Shell space={space}>
        <section
          className="rounded-lg border border-slate-200 bg-white p-4"
          data-testid="rules-member-notice"
        >
          <h2 className="text-sm font-semibold text-slate-900">{space.name}</h2>
          <p className="mt-2 text-sm text-slate-600">
            You are a member of this Space. Only its admins can manage its rules.
          </p>
        </section>
      </Shell>
    )
  }

  return (
    <Shell space={space}>
      <RulesManager space={space} />
    </Shell>
  )
}

/** The page chrome: a back link and heading, shared by every state above. */
function Shell({ space, children }: { space?: Space; children: ReactNode }) {
  return (
    <main className="min-h-screen bg-slate-50 p-8 text-slate-800">
      <div className="mx-auto max-w-3xl" data-testid="space-rules-page">
        <Link
          to={space ? `/s/${space.public_id}` : '/admin'}
          className="text-xs text-slate-500 hover:underline"
          data-testid="rules-back-link"
        >
          ← Back to {space ? space.name : 'admin'}
        </Link>
        <h1 className="mt-2 text-2xl font-semibold text-slate-900">
          Rules{space ? ` — ${space.name}` : ''}
        </h1>
        <p className="mt-2 mb-6 text-sm text-slate-600">
          Every booking constraint configured for this Space, one instance per row.
        </p>
        {children}
      </div>
    </main>
  )
}

type RegistryLoad =
  { kind: 'ok'; ruleTypes: RuleTypeRead[] } | { kind: 'error'; message: string } | null
type RulesLoad = { kind: 'ok'; rules: SpaceRuleRead[] } | { kind: 'error'; message: string } | null

/** One row's in-progress edit of `params` and `applies_to`, held above `RuleRow`. */
type RuleDraft = { paramValues: RuleParamValues; appliesDraft: AppliesToDraft }

/** A key that changes whenever anything about a stored row's editable state changes. */
function ruleRowKey(rule: SpaceRuleRead): string {
  return `${rule.id}:${JSON.stringify(rule.params)}:${JSON.stringify(rule.applies_to)}:${rule.enabled}`
}

/** A fresh draft seeded straight from a stored row — what a clean, untouched row looks like. */
function seedDraft(rule: SpaceRuleRead, ruleTypes: RuleTypeRead[]): RuleDraft {
  const ruleType = findRuleTypeFor(ruleTypes, rule.rule_type)
  return {
    paramValues: ruleType ? ruleParamValuesFromStored(ruleType.params, rule.params) : {},
    appliesDraft: draftFromAppliesTo(rule.applies_to),
  }
}

/**
 * Rebuilds the drafts map for the current `rules` array, one entry per row.
 *
 * A row already present in `previous` keeps its existing draft untouched,
 * clean or mid-edit alike; only a row with no existing entry — a rule just
 * created — gets a fresh one seeded from its stored values. This is
 * deliberately **not** a blind reseed of every row from `rules`: a page-level
 * Save that partially fails must leave the failed row's draft exactly as the
 * admin left it (still dirty, still marked) while the succeeded rows' drafts
 * fall out of the comparison as clean on their own, since a draft that was
 * just submitted and accepted already equals the fresh stored row it is
 * compared against. Reseeding everything unconditionally here would instead
 * discard the failed row's unsaved edit the moment any other row's save
 * landed — exactly the case `SpaceRulesPage.test.tsx`'s partial-failure test
 * guards. The gap this leaves — a row nobody is editing changed by another
 * admin in another tab — is not picked up until Cancel or a reload; this
 * page has never handled cross-session concurrency and this rewrite does not
 * add it.
 */
function buildDrafts(
  rules: SpaceRuleRead[],
  ruleTypes: RuleTypeRead[],
  previous: Record<number, RuleDraft>,
): Record<number, RuleDraft> {
  const next: Record<number, RuleDraft> = {}
  for (const rule of rules) {
    next[rule.id] = previous[rule.id] ?? seedDraft(rule, ruleTypes)
  }
  return next
}

/** Whether a draft differs from its stored rule in `params`, `applies_to`, or both. */
function ruleIsDirty(
  rule: SpaceRuleRead,
  ruleType: RuleTypeRead | undefined,
  draft: RuleDraft,
): boolean {
  const wireApplies = appliesToFromDraft(draft.appliesDraft)
  if (JSON.stringify(wireApplies) !== JSON.stringify(rule.applies_to)) return true
  if (!ruleType) return false
  const wireParams = ruleParamValuesToWire(ruleType.params, draft.paramValues)
  return JSON.stringify(wireParams) !== JSON.stringify(rule.params)
}

/** Fetches the registry and the Space's configured rules, then renders the editor. */
function RulesManager({ space }: { space: Space }) {
  const [registryLoad, setRegistryLoad] = useState<RegistryLoad>(null)
  const [rulesLoad, setRulesLoad] = useState<RulesLoad>(null)
  // The rule type a just-succeeded generation job offered to add — set when
  // the admin clicks "Add this rule to the Space" on `RuleAuthoringPanel`,
  // and what preselects it in `AddRulePanel` below.
  const [preselectedType, setPreselectedType] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErrors, setSaveErrors] = useState<Record<number, string>>({})

  const rules = rulesLoad?.kind === 'ok' ? rulesLoad.rules : []
  const ruleTypes = registryLoad?.kind === 'ok' ? registryLoad.ruleTypes : []

  // The drafts map, and the reset idiom that keeps it in step with `rules`
  // without an effect (`react-hooks/set-state-in-effect`) — see the old
  // per-row `rowKey` (now `ruleRowKey`, moved up here from `RuleRow`) and
  // `SpaceSettingsPanel`'s `settingsKey` for the same pattern elsewhere in
  // this codebase. Compared during render against a remembered value; when
  // it differs, the drafts map is rebuilt (see `buildDrafts` for what
  // "rebuilt" means here) and any stale per-row save error is dropped along
  // with it.
  const rulesKey = rules.map(ruleRowKey).join('|')
  const [seenRulesKey, setSeenRulesKey] = useState(rulesKey)
  const [drafts, setDrafts] = useState<Record<number, RuleDraft>>(() =>
    buildDrafts(rules, ruleTypes, {}),
  )
  if (seenRulesKey !== rulesKey) {
    setSeenRulesKey(rulesKey)
    setDrafts(buildDrafts(rules, ruleTypes, drafts))
    const survivingErrors: Record<number, string> = {}
    for (const rule of rules) {
      if (saveErrors[rule.id] !== undefined) survivingErrors[rule.id] = saveErrors[rule.id]
    }
    setSaveErrors(survivingErrors)
  }

  function fetchRuleTypes() {
    return listRuleTypes().then((result) => {
      setRegistryLoad(
        result.outcome === 'ok'
          ? { kind: 'ok', ruleTypes: result.data }
          : { kind: 'error', message: messageFor(result) },
      )
    })
  }

  useEffect(() => {
    let cancelled = false

    void listRuleTypes().then((result) => {
      if (cancelled) return
      setRegistryLoad(
        result.outcome === 'ok'
          ? { kind: 'ok', ruleTypes: result.data }
          : { kind: 'error', message: messageFor(result) },
      )
    })

    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let cancelled = false

    void listSpaceRules(space.public_id).then((result) => {
      if (cancelled) return
      setRulesLoad(
        result.outcome === 'ok'
          ? { kind: 'ok', rules: result.data }
          : { kind: 'error', message: messageFor(result) },
      )
    })

    return () => {
      cancelled = true
    }
  }, [space.public_id])

  if (registryLoad === null || rulesLoad === null) {
    return (
      <p className="text-sm text-slate-600" data-testid="rules-loading" role="status">
        Loading rules…
      </p>
    )
  }

  if (registryLoad.kind === 'error') {
    return (
      <p className="text-sm text-red-700" data-testid="rules-error" role="alert">
        {registryLoad.message}
      </p>
    )
  }

  if (rulesLoad.kind === 'error') {
    return (
      <p className="text-sm text-red-700" data-testid="rules-error" role="alert">
        {rulesLoad.message}
      </p>
    )
  }

  const archived = space.archived_at !== null

  function handleCreated(rule: SpaceRuleRead) {
    setRulesLoad((current) =>
      current?.kind === 'ok' ? { kind: 'ok', rules: [...current.rules, rule] } : current,
    )
  }

  function handleUpdated(rule: SpaceRuleRead) {
    setRulesLoad((current) =>
      current?.kind === 'ok'
        ? {
            kind: 'ok',
            rules: current.rules.map((existing) => (existing.id === rule.id ? rule : existing)),
          }
        : current,
    )
  }

  function handleDeleted(ruleId: number) {
    setRulesLoad((current) =>
      current?.kind === 'ok'
        ? { kind: 'ok', rules: current.rules.filter((existing) => existing.id !== ruleId) }
        : current,
    )
  }

  function handleParamChange(ruleId: number, name: string, value: string) {
    setDrafts((current) => {
      const existing = current[ruleId]
      if (!existing) return current
      return {
        ...current,
        [ruleId]: { ...existing, paramValues: { ...existing.paramValues, [name]: value } },
      }
    })
  }

  function handleAppliesChange(ruleId: number, next: AppliesToDraft) {
    setDrafts((current) => {
      const existing = current[ruleId]
      if (!existing) return current
      return { ...current, [ruleId]: { ...existing, appliesDraft: next } }
    })
  }

  function handleCancelAll() {
    setDrafts(Object.fromEntries(rules.map((rule) => [rule.id, seedDraft(rule, ruleTypes)])))
    setSaveErrors({})
  }

  async function handleSaveAll() {
    if (dirtyRows.length === 0) return
    setSaving(true)

    const results = await Promise.allSettled(
      dirtyRows.map(({ rule, ruleType, draft }) => {
        const patch: SpaceRuleUpdateInput = { applies_to: appliesToFromDraft(draft.appliesDraft) }
        if (ruleType) patch.params = ruleParamValuesToWire(ruleType.params, draft.paramValues)
        return updateSpaceRule(space.public_id, rule.id, patch)
      }),
    )

    setSaving(false)

    const errors: Record<number, string> = {}
    dirtyRows.forEach(({ rule }, index) => {
      const settled = results[index]
      if (settled.status === 'rejected') {
        errors[rule.id] = 'Something went wrong. Please try again.'
        return
      }
      if (settled.value.outcome === 'ok') {
        handleUpdated(settled.value.data)
        return
      }
      errors[rule.id] = messageFor(settled.value)
    })
    setSaveErrors(errors)
  }

  // One row of derived state per configured rule — its draft (falling back
  // to a fresh seed defensively; every rule id is expected to already have
  // an entry by the time this renders, courtesy of the reset idiom above),
  // its registered type, and whether it is dirty and/or valid. Computed once
  // here rather than re-derived inside `RuleRow`, since both the page-level
  // Save button and every row's own "changed"/"invalid" marker need the
  // identical answer.
  const rowInfos = sortRulesForDisplay(rules, ruleTypes).map((rule) => {
    const ruleType = findRuleTypeFor(ruleTypes, rule.rule_type)
    const draft = drafts[rule.id] ?? seedDraft(rule, ruleTypes)
    const dirty = ruleIsDirty(rule, ruleType, draft)
    const paramsValid = ruleType ? ruleParamsAreValid(ruleType.params, draft.paramValues) : true
    const appliesValid = appliesToDraftIsValid(draft.appliesDraft)
    return { rule, ruleType, draft, dirty, valid: paramsValid && appliesValid }
  })

  const dirtyRows = rowInfos.filter((info) => info.dirty)
  const invalidDirtyRows = dirtyRows.filter((info) => !info.valid)
  const saveDisabled = archived || saving || dirtyRows.length === 0 || invalidDirtyRows.length > 0
  const cancelDisabled = saving || dirtyRows.length === 0

  const saveErrorEntries = Object.entries(saveErrors)

  return (
    <div className="space-y-6">
      {archived ? (
        <p
          className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
          data-testid="rules-archived-banner"
          role="status"
        >
          This Space is archived. Its rules can no longer be changed.
        </p>
      ) : null}

      <RuleAuthoringPanel
        space={space}
        disabled={archived}
        ruleTypes={ruleTypes}
        onJobSucceeded={() => void fetchRuleTypes()}
        onAddToSpace={setPreselectedType}
      />

      <AddRulePanel
        space={space}
        ruleTypes={ruleTypes}
        existingRules={rules}
        onCreated={handleCreated}
        disabled={archived}
        preselectedType={preselectedType}
      />

      <section className="rounded-lg border border-slate-200 bg-white p-4" data-testid="rules-list">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-900">Configured rules</h2>

          <div className="flex items-center gap-2">
            <button
              type="button"
              className="rounded border border-slate-300 px-3 py-1 text-sm disabled:opacity-50"
              data-testid="rules-cancel"
              disabled={cancelDisabled}
              onClick={handleCancelAll}
            >
              Cancel
            </button>
            <button
              type="button"
              className="rounded bg-slate-800 px-3 py-1 text-sm text-white disabled:opacity-50"
              data-testid="rules-save"
              disabled={saveDisabled}
              onClick={() => void handleSaveAll()}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>

        {invalidDirtyRows.length > 0 ? (
          <p className="mt-2 text-xs text-amber-700" data-testid="rules-save-invalid">
            Save is disabled: fix the highlighted row
            {invalidDirtyRows.length > 1 ? 's' : ''} —{' '}
            {invalidDirtyRows
              .map(({ rule, ruleType }) => `${ruleType?.label ?? rule.rule_type} (rule ${rule.id})`)
              .join(', ')}
            .
          </p>
        ) : null}

        {saveErrorEntries.length > 0 ? (
          <p
            className="mt-2 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-800"
            data-testid="rules-save-error"
            role="alert"
          >
            {saveErrorEntries
              .map(([idText, message]) => {
                const id = Number(idText)
                const info = rowInfos.find((candidate) => candidate.rule.id === id)
                const label = info?.ruleType?.label ?? info?.rule.rule_type ?? `rule ${id}`
                return `${label} (rule ${id}) failed to save: ${message}`
              })
              .join(' ')}
          </p>
        ) : null}

        {rules.length === 0 ? (
          <p className="mt-2 text-sm text-slate-600" data-testid="rules-empty">
            No rules configured yet. Every booking is admitted unless a rule below refuses it.
          </p>
        ) : (
          <ul className="mt-3 divide-y divide-slate-100">
            {rowInfos.map(({ rule, ruleType, draft, dirty, valid }) => (
              <RuleRow
                key={rule.id}
                space={space}
                rule={rule}
                ruleType={ruleType}
                disabled={archived || saving}
                draft={draft}
                changed={dirty}
                invalidBlockingSave={dirty && !valid}
                onParamChange={(name, value) => handleParamChange(rule.id, name, value)}
                onAppliesChange={(next) => handleAppliesChange(rule.id, next)}
                onUpdated={handleUpdated}
                onDeleted={handleDeleted}
              />
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

/** Configured rules sorted the way an assembled canon would run them: declared priority, then id. */
function sortRulesForDisplay(rules: SpaceRuleRead[], ruleTypes: RuleTypeRead[]): SpaceRuleRead[] {
  const priorityOf = (rule: SpaceRuleRead) =>
    findRuleTypeFor(ruleTypes, rule.rule_type)?.priority ?? Number.POSITIVE_INFINITY
  return [...rules].sort((a, b) => priorityOf(a) - priorityOf(b) || a.id - b.id)
}

/**
 * "Add a rule": pick a registered type, fill its params, scope it, submit.
 *
 * ## Scope is editable here now, not only afterward
 *
 * `AppliesToEditor` defaults to `{ mode: 'always' }` — the identical default
 * a freshly created rule got before this task, just now a visible, editable
 * field on this form rather than an assumption baked into the create call.
 * This reverses the page's original decision, which read: *"`applies_to`
 * starts at 'always' here — scoping a freshly created rule to a weekday or a
 * set of dates is an edit on the row afterward, not a second copy of that
 * editor on this form."* That shape was the source of
 * `scope-not-appearing-on-create.md`: an admin who wanted a scoped rule had
 * to create it unscoped and then find the row's own scope editor to narrow
 * it, a step nothing on this form hinted was still needed. Reusing
 * `AppliesToEditor` here costs nothing extra to validate or convert — it
 * already exports `appliesToFromDraft` and `appliesToDraftIsValid`, the same
 * two functions `RulesManager`'s page-level Save now uses for every row's
 * draft — so this is the same editor, not a second implementation of it.
 *
 * ## The picker shows a description, not a combobox
 *
 * With generated types in the same list as the seven hand-written ones, a
 * `<select>` of labels alone is not enough to choose from — a list of names
 * is navigable at seven entries and is not at thirty (task 7.9). Rendering
 * the selected type's own `description` underneath is enough; this stays a
 * plain `<select>` rather than growing into a combobox. Generated types get
 * no visual distinction beyond that description — they are the same kind of
 * thing to an admin, and marking one "AI-generated" here would invite a
 * judgement the admin has no basis to make at this point.
 *
 * ## `preselectedType`
 *
 * Set by `RulesManager` when the admin chooses "Add this rule to the Space"
 * on a just-succeeded `RuleAuthoringPanel` job — the drop-in the task
 * describes. Applied with the same compare-during-render idiom `RulesManager`
 * uses for its own external resets (`rulesKey`) rather than an effect, since
 * this repo's eslint config forbids calling `setState` synchronously inside
 * one.
 *
 * ## Creation stays its own immediate action
 *
 * Unlike an existing row's `params`/`applies_to`, which are now drafted and
 * saved through the page-level Save, creating a rule keeps its own button and
 * fires immediately — it is a different action (there is nothing "stored" to
 * compare a draft against until it exists) and folding it into the
 * page-level Save would leave that button meaning two different things at
 * once.
 */
function AddRulePanel({
  space,
  ruleTypes,
  existingRules,
  onCreated,
  disabled,
  preselectedType,
}: {
  space: Space
  ruleTypes: RuleTypeRead[]
  existingRules: SpaceRuleRead[]
  onCreated: (rule: SpaceRuleRead) => void
  disabled: boolean
  preselectedType?: string | null
}) {
  const [selectedType, setSelectedType] = useState('')
  const [values, setValues] = useState<RuleParamValues>({})
  const [appliesDraft, setAppliesDraft] = useState<AppliesToDraft>({ mode: 'always' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const ruleType = ruleTypes.find((candidate) => candidate.rule_type === selectedType) ?? null

  function handleSelectType(next: string) {
    setSelectedType(next)
    const type = ruleTypes.find((candidate) => candidate.rule_type === next)
    setValues(type ? emptyRuleParamValues(type.params) : {})
    setError('')
  }

  const [seenPreselect, setSeenPreselect] = useState<string | null | undefined>(preselectedType)
  if (preselectedType && preselectedType !== seenPreselect) {
    setSeenPreselect(preselectedType)
    handleSelectType(preselectedType)
  }

  const alreadyHasInstance =
    ruleType !== null &&
    ruleType.is_single &&
    existingRules.some((existing) => existing.rule_type === ruleType.rule_type)

  const valid =
    ruleType !== null &&
    ruleParamsAreValid(ruleType.params, values) &&
    appliesToDraftIsValid(appliesDraft)

  async function handleCreate() {
    if (!ruleType) return
    setBusy(true)
    setError('')

    const result = await createSpaceRule(space.public_id, {
      rule_type: ruleType.rule_type,
      params: ruleParamValuesToWire(ruleType.params, values),
      applies_to: appliesToFromDraft(appliesDraft),
    })
    setBusy(false)

    if (result.outcome === 'ok') {
      onCreated(result.data)
      setSelectedType('')
      setValues({})
      setAppliesDraft({ mode: 'always' })
      return
    }

    setError(messageFor(result))
  }

  return (
    <section
      className="rounded-lg border border-slate-200 bg-white p-4"
      data-testid="add-rule-panel"
    >
      <h2 className="text-sm font-semibold text-slate-900">Add a rule</h2>

      <div className="mt-3">
        <label className="block text-xs text-slate-600" htmlFor="add-rule-type">
          Rule type
        </label>
        <select
          id="add-rule-type"
          className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
          data-testid="add-rule-type-select"
          value={selectedType}
          disabled={disabled || busy}
          onChange={(event) => handleSelectType(event.target.value)}
        >
          <option value="">Choose a rule type…</option>
          {ruleTypes.map((type) => (
            <option key={type.rule_type} value={type.rule_type}>
              {type.label}
            </option>
          ))}
        </select>
        {ruleType ? (
          <p className="mt-1 text-xs text-slate-600" data-testid="add-rule-type-description">
            {ruleType.description}
          </p>
        ) : null}
      </div>

      {ruleType ? (
        <div className="mt-3">
          <RuleParamsForm
            idPrefix="add-rule"
            params={ruleType.params}
            values={values}
            onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
            disabled={disabled || busy}
          />

          <div className="mt-3">
            <AppliesToEditor
              idPrefix="add-rule"
              draft={appliesDraft}
              onChange={setAppliesDraft}
              disabled={disabled || busy}
            />
          </div>

          {alreadyHasInstance ? (
            <p className="mt-2 text-xs text-amber-700" data-testid="add-rule-single-warning">
              This Space already has an instance of {ruleType.label}. A second one is unusual, not
              refused — you can still add it.
            </p>
          ) : null}

          <button
            type="button"
            className="mt-3 rounded bg-slate-800 px-3 py-1 text-sm text-white disabled:opacity-50"
            data-testid="add-rule-submit"
            disabled={disabled || busy || !valid}
            onClick={() => void handleCreate()}
          >
            {busy ? 'Adding…' : 'Add rule'}
          </button>
        </div>
      ) : null}

      {error ? (
        <p className="mt-2 text-sm text-red-700" data-testid="add-rule-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  )
}

/**
 * One configured rule instance: its params form, its `applies_to` editor,
 * pause, and delete.
 *
 * ## Presentational for `params`/`applies_to`
 *
 * This component no longer owns a draft of either field — `RulesManager`
 * does, one per rule id, and hands this row its current draft (`draft`) plus
 * `onParamChange` / `onAppliesChange` to report edits upward, along with
 * whether the draft is currently `changed` (dirty) and, if so, whether it is
 * `invalidBlockingSave`. That is what makes a single page-level Save/Cancel
 * possible: a per-row draft with no shared owner cannot be saved or
 * discarded as one page-level action.
 *
 * ## Pause and delete stay immediate, on purpose
 *
 * Both remain exactly as before this task: their own buttons, their own
 * request, fired the moment they are clicked, with no relation to the
 * draft/dirty machinery above. This is deliberate, not a leftover: pause is
 * one field with one obvious meaning, and delete is destructive and already
 * carries its own two-step confirm. Folding either into the page-level Save
 * would mean a "deleted" (or "paused") row sitting on screen pending a Save
 * the admin might still Cancel — worse in every direction than the row
 * simply changing state right away. This component therefore still owns its
 * own `confirmingDelete`, `busy` and `error` state, scoped to those two
 * actions only.
 */
function RuleRow({
  space,
  rule,
  ruleType,
  disabled,
  draft,
  changed,
  invalidBlockingSave,
  onParamChange,
  onAppliesChange,
  onUpdated,
  onDeleted,
}: {
  space: Space
  rule: SpaceRuleRead
  ruleType: RuleTypeRead | undefined
  disabled: boolean
  draft: RuleDraft
  changed: boolean
  invalidBlockingSave: boolean
  onParamChange: (name: string, value: string) => void
  onAppliesChange: (draft: AppliesToDraft) => void
  onUpdated: (rule: SpaceRuleRead) => void
  onDeleted: (ruleId: number) => void
}) {
  const brokenReason = ruleBrokenReason(ruleType ? [ruleType] : [], rule)

  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function handleToggleEnabled() {
    setBusy(true)
    setError('')

    const result = await updateSpaceRule(space.public_id, rule.id, { enabled: !rule.enabled })
    setBusy(false)

    if (result.outcome === 'ok') {
      onUpdated(result.data)
      return
    }

    setError(messageFor(result))
  }

  async function handleDelete() {
    setBusy(true)
    setError('')

    const result = await deleteSpaceRule(space.public_id, rule.id)
    setBusy(false)

    if (result.outcome === 'ok') {
      onDeleted(rule.id)
      return
    }

    setError(messageFor(result))
  }

  return (
    <li
      className={`py-4 ${changed ? 'border-l-4 border-blue-400 bg-blue-50/40 pl-3' : ''}`}
      data-testid={`rule-${rule.id}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <p className="text-sm font-medium text-slate-900" data-testid={`rule-${rule.id}-type`}>
            {ruleType?.label ?? rule.rule_type}
          </p>
          {!rule.enabled ? (
            <span className="text-xs text-amber-700" data-testid={`rule-${rule.id}-paused-badge`}>
              Paused
            </span>
          ) : null}
          {changed ? (
            <span
              className="text-xs font-medium text-blue-700"
              data-testid={`rule-${rule.id}-changed`}
            >
              Changed
            </span>
          ) : null}
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            className="rounded border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
            data-testid={`rule-${rule.id}-toggle-enabled`}
            disabled={disabled || busy}
            onClick={() => void handleToggleEnabled()}
          >
            {rule.enabled ? 'Pause' : 'Resume'}
          </button>

          {confirmingDelete ? (
            <span
              className="flex items-center gap-2"
              data-testid={`rule-${rule.id}-delete-confirm`}
            >
              <button
                type="button"
                className="rounded bg-red-700 px-2 py-1 text-sm text-white disabled:opacity-50"
                data-testid={`rule-${rule.id}-delete-confirm-yes`}
                disabled={busy}
                onClick={() => void handleDelete()}
              >
                {busy ? 'Deleting…' : 'Yes, delete'}
              </button>
              <button
                type="button"
                className="rounded border border-slate-300 px-2 py-1 text-sm"
                data-testid={`rule-${rule.id}-delete-cancel`}
                disabled={busy}
                onClick={() => setConfirmingDelete(false)}
              >
                Cancel
              </button>
            </span>
          ) : (
            <button
              type="button"
              className="rounded border border-red-300 px-2 py-1 text-sm text-red-700 disabled:opacity-50"
              data-testid={`rule-${rule.id}-delete-start`}
              disabled={disabled || busy}
              onClick={() => setConfirmingDelete(true)}
            >
              Delete
            </button>
          )}
        </div>
      </div>

      {brokenReason ? (
        <p
          className="mt-2 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-800"
          data-testid={`rule-${rule.id}-broken`}
          role="alert"
        >
          This rule is broken, and every booking against this Space is being refused because of it:{' '}
          {brokenReason}
        </p>
      ) : null}

      {invalidBlockingSave ? (
        <p
          className="mt-2 rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800"
          data-testid={`rule-${rule.id}-invalid`}
          role="alert"
        >
          This row&rsquo;s changes are invalid and are blocking Save — fix the highlighted field(s)
          below, or Cancel to discard them.
        </p>
      ) : null}

      {ruleType ? (
        <div className="mt-2">
          <RuleParamsForm
            idPrefix={`rule-${rule.id}`}
            params={ruleType.params}
            values={draft.paramValues}
            onChange={onParamChange}
            disabled={disabled || busy}
          />
        </div>
      ) : null}

      <div className="mt-3">
        <AppliesToEditor
          idPrefix={`rule-${rule.id}`}
          draft={draft.appliesDraft}
          onChange={onAppliesChange}
          disabled={disabled || busy}
        />
      </div>

      {error ? (
        <p className="mt-2 text-sm text-red-700" data-testid={`rule-${rule.id}-error`} role="alert">
          {error}
        </p>
      ) : null}
    </li>
  )
}
