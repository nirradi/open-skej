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
} from '../api'
import {
  AppliesToEditor,
  appliesToDraftIsValid,
  appliesToFromDraft,
  draftFromAppliesTo,
  type AppliesToDraft,
} from './AppliesToEditor'
import { messageFor } from './messages'
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

type RegistryLoad = { kind: 'ok'; ruleTypes: RuleTypeRead[] } | { kind: 'error'; message: string } | null
type RulesLoad = { kind: 'ok'; rules: SpaceRuleRead[] } | { kind: 'error'; message: string } | null

/** Fetches the registry and the Space's configured rules, then renders the editor. */
function RulesManager({ space }: { space: Space }) {
  const [registryLoad, setRegistryLoad] = useState<RegistryLoad>(null)
  const [rulesLoad, setRulesLoad] = useState<RulesLoad>(null)

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

  const ruleTypes = registryLoad.ruleTypes
  const rules = rulesLoad.rules
  const archived = space.archived_at !== null

  function handleCreated(rule: SpaceRuleRead) {
    setRulesLoad((current) =>
      current?.kind === 'ok' ? { kind: 'ok', rules: [...current.rules, rule] } : current,
    )
  }

  function handleUpdated(rule: SpaceRuleRead) {
    setRulesLoad((current) =>
      current?.kind === 'ok'
        ? { kind: 'ok', rules: current.rules.map((existing) => (existing.id === rule.id ? rule : existing)) }
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

      <AddRulePanel
        space={space}
        ruleTypes={ruleTypes}
        existingRules={rules}
        onCreated={handleCreated}
        disabled={archived}
      />

      <section className="rounded-lg border border-slate-200 bg-white p-4" data-testid="rules-list">
        <h2 className="text-sm font-semibold text-slate-900">Configured rules</h2>

        {rules.length === 0 ? (
          <p className="mt-2 text-sm text-slate-600" data-testid="rules-empty">
            No rules configured yet. Every booking is admitted unless a rule below refuses it.
          </p>
        ) : (
          <ul className="mt-3 divide-y divide-slate-100">
            {sortRulesForDisplay(rules, ruleTypes).map((rule) => (
              <RuleRow
                key={rule.id}
                space={space}
                rule={rule}
                ruleTypes={ruleTypes}
                disabled={archived}
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
 * "Add a rule": pick a registered type, fill its params, submit.
 *
 * `applies_to` starts at "always" here — scoping a freshly created rule to a
 * weekday or a set of dates is an edit on the row afterward
 * (`RuleRow`'s own `AppliesToEditor`), not a second copy of that editor on
 * this form.
 */
function AddRulePanel({
  space,
  ruleTypes,
  existingRules,
  onCreated,
  disabled,
}: {
  space: Space
  ruleTypes: RuleTypeRead[]
  existingRules: SpaceRuleRead[]
  onCreated: (rule: SpaceRuleRead) => void
  disabled: boolean
}) {
  const [selectedType, setSelectedType] = useState('')
  const [values, setValues] = useState<RuleParamValues>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const ruleType = ruleTypes.find((candidate) => candidate.rule_type === selectedType) ?? null

  function handleSelectType(next: string) {
    setSelectedType(next)
    const type = ruleTypes.find((candidate) => candidate.rule_type === next)
    setValues(type ? emptyRuleParamValues(type.params) : {})
    setError('')
  }

  const alreadyHasInstance =
    ruleType !== null &&
    ruleType.is_single &&
    existingRules.some((existing) => existing.rule_type === ruleType.rule_type)

  const valid = ruleType !== null && ruleParamsAreValid(ruleType.params, values)

  async function handleCreate() {
    if (!ruleType) return
    setBusy(true)
    setError('')

    const result = await createSpaceRule(space.public_id, {
      rule_type: ruleType.rule_type,
      params: ruleParamValuesToWire(ruleType.params, values),
    })
    setBusy(false)

    if (result.outcome === 'ok') {
      onCreated(result.data)
      setSelectedType('')
      setValues({})
      return
    }

    setError(messageFor(result))
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4" data-testid="add-rule-panel">
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

/** One configured rule instance: its params form, its `applies_to` editor, pause, and delete. */
function RuleRow({
  space,
  rule,
  ruleTypes,
  disabled,
  onUpdated,
  onDeleted,
}: {
  space: Space
  rule: SpaceRuleRead
  ruleTypes: RuleTypeRead[]
  disabled: boolean
  onUpdated: (rule: SpaceRuleRead) => void
  onDeleted: (ruleId: number) => void
}) {
  const ruleType = findRuleTypeFor(ruleTypes, rule.rule_type)
  const brokenReason = ruleBrokenReason(ruleTypes, rule)

  const [paramValues, setParamValues] = useState<RuleParamValues>(
    ruleType ? ruleParamValuesFromStored(ruleType.params, rule.params) : {},
  )
  const [appliesDraft, setAppliesDraft] = useState<AppliesToDraft>(draftFromAppliesTo(rule.applies_to))
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // The row's own stored params/applies_to can change out from under a local
  // edit — another admin's write, or this component's own successful save
  // landing a fresh `updated_at` — and the drafts should reset to match, the
  // same "compare during render" idiom `SpaceSchedulePanel`'s `scheduleKey`
  // uses rather than an effect (the `set-state-in-effect` lint this repo
  // enforces).
  const rowKey = `${rule.id}:${JSON.stringify(rule.params)}:${JSON.stringify(rule.applies_to)}:${rule.enabled}`
  const [seenRowKey, setSeenRowKey] = useState(rowKey)
  if (seenRowKey !== rowKey) {
    setSeenRowKey(rowKey)
    setParamValues(ruleType ? ruleParamValuesFromStored(ruleType.params, rule.params) : {})
    setAppliesDraft(draftFromAppliesTo(rule.applies_to))
    setError('')
  }

  async function handleSaveParams() {
    if (!ruleType) return
    setBusy(true)
    setError('')

    const result = await updateSpaceRule(space.public_id, rule.id, {
      params: ruleParamValuesToWire(ruleType.params, paramValues),
    })
    setBusy(false)

    if (result.outcome === 'ok') {
      onUpdated(result.data)
      return
    }

    setError(messageFor(result))
  }

  async function handleSaveAppliesTo() {
    setBusy(true)
    setError('')

    const result = await updateSpaceRule(space.public_id, rule.id, {
      applies_to: appliesToFromDraft(appliesDraft),
    })
    setBusy(false)

    if (result.outcome === 'ok') {
      onUpdated(result.data)
      return
    }

    setError(messageFor(result))
  }

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

  const paramsValid = ruleType ? ruleParamsAreValid(ruleType.params, paramValues) : false
  const appliesValid = appliesToDraftIsValid(appliesDraft)

  return (
    <li className="py-4" data-testid={`rule-${rule.id}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium text-slate-900" data-testid={`rule-${rule.id}-type`}>
            {ruleType?.label ?? rule.rule_type}
          </p>
          {!rule.enabled ? (
            <span className="text-xs text-amber-700" data-testid={`rule-${rule.id}-paused-badge`}>
              Paused
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
            <span className="flex items-center gap-2" data-testid={`rule-${rule.id}-delete-confirm`}>
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

      {ruleType ? (
        <div className="mt-2">
          <RuleParamsForm
            idPrefix={`rule-${rule.id}`}
            params={ruleType.params}
            values={paramValues}
            onChange={(name, value) => setParamValues((current) => ({ ...current, [name]: value }))}
            disabled={disabled || busy}
          />
          <button
            type="button"
            className="mt-2 rounded border border-slate-300 px-2 py-1 text-xs disabled:opacity-50"
            data-testid={`rule-${rule.id}-params-save`}
            disabled={disabled || busy || !paramsValid}
            onClick={() => void handleSaveParams()}
          >
            Save parameters
          </button>
        </div>
      ) : null}

      <div className="mt-3">
        <AppliesToEditor
          idPrefix={`rule-${rule.id}`}
          draft={appliesDraft}
          onChange={setAppliesDraft}
          disabled={disabled || busy}
        />
        <button
          type="button"
          className="mt-2 rounded border border-slate-300 px-2 py-1 text-xs disabled:opacity-50"
          data-testid={`rule-${rule.id}-applies-save`}
          disabled={disabled || busy || !appliesValid}
          onClick={() => void handleSaveAppliesTo()}
        >
          Save scope
        </button>
      </div>

      {error ? (
        <p className="mt-2 text-sm text-red-700" data-testid={`rule-${rule.id}-error`} role="alert">
          {error}
        </p>
      ) : null}
    </li>
  )
}
