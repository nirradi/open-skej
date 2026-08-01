import type { RuleParamRead } from '../api'

/**
 * A generic form for one rule type's declared parameter schema.
 *
 * ## The whole point: this file knows two `kind`s, and no rule type names
 *
 * `SpaceRulesPage` renders a form per configured rule instance from
 * `RuleTypeRead.params` alone — `rules/rules/registry.py`'s own description
 * of what a type's constructor needs. A type added later to the registry (by
 * a future task, or by Stream 7's generation loop) gets a working form with
 * zero changes here, because this component never branches on `param.name`
 * or `rule_type`: only on `param.kind`, of which there are exactly two
 * (`ParamKind` in the registry) — `"integer"` renders a number input,
 * `"local_time"` a time input. `SpaceRulesPage.test.tsx` proves this directly
 * by inventing a rule type the real registry does not have and asserting a
 * field renders for it.
 *
 * Values are held as strings throughout — the shape every HTML input already
 * produces — and converted to the wire's `number` / `"HH:MM:SS"` only at the
 * edges (`ruleParamValuesToWire`), the same input-is-a-string,
 * convert-at-the-boundary shape `SpaceSchedulePanel` already uses for its own
 * numeric and time fields.
 */
export type RuleParamValues = Record<string, string>

/** An empty draft for `params`, one blank string per declared parameter. */
export function emptyRuleParamValues(params: RuleParamRead[]): RuleParamValues {
  return Object.fromEntries(params.map((param) => [param.name, '']))
}

/**
 * A draft seeded from a stored `space_rules` row's raw `params`.
 *
 * A value missing, `null`, or of the wrong JS type for its declared `kind`
 * all collapse to `''` here rather than throwing — this is also how a
 * *broken* row (see `ruleValidation.ts`) becomes editable at all: the admin
 * needs a blank field to type a fix into, not a crash.
 */
export function ruleParamValuesFromStored(
  params: RuleParamRead[],
  stored: Record<string, unknown>,
): RuleParamValues {
  const values: RuleParamValues = {}
  for (const param of params) {
    const raw = stored[param.name]
    if (raw === undefined || raw === null) {
      values[param.name] = ''
    } else if (param.kind === 'local_time') {
      values[param.name] = typeof raw === 'string' ? raw.slice(0, 5) : ''
    } else {
      values[param.name] = String(raw)
    }
  }
  return values
}

/**
 * The draft, converted to the wire shape `SpaceRuleCreateInput.params` /
 * `SpaceRuleUpdateInput.params` expect: `number` for `"integer"`, `"HH:MM:SS"`
 * for `"local_time"`. A blank optional value is omitted from the object
 * entirely rather than sent as `null` — matching `SpaceRuleCreate`, which has
 * no way to mark one param of a `params` dict absent except leaving the key
 * out. Call `ruleParamsAreValid` first; this function does not itself refuse
 * a blank required value.
 */
export function ruleParamValuesToWire(
  params: RuleParamRead[],
  values: RuleParamValues,
): Record<string, unknown> {
  const wire: Record<string, unknown> = {}
  for (const param of params) {
    const value = values[param.name] ?? ''
    if (value === '') continue
    wire[param.name] = param.kind === 'local_time' ? `${value}:00` : Number(value)
  }
  return wire
}

/**
 * Whether a draft is submittable: every required parameter filled, every
 * integer parsing to a finite number at or above its declared `minimum`.
 * Gates the "Add rule" / "Save parameters" buttons — the server re-validates
 * regardless, this only spares a round trip for a mistake the form can see
 * coming.
 */
export function ruleParamsAreValid(params: RuleParamRead[], values: RuleParamValues): boolean {
  return params.every((param) => {
    const value = values[param.name] ?? ''
    if (value === '') return !param.required
    if (param.kind === 'integer') {
      const num = Number(value)
      if (!Number.isFinite(num)) return false
      if (param.minimum !== null && num < param.minimum) return false
    }
    return true
  })
}

/**
 * One field per declared parameter — the entire form. `idPrefix` namespaces
 * `data-testid`s and `<label htmlFor>` ids so the same component can render
 * more than once per page (the "add rule" picker and every configured row).
 */
export function RuleParamsForm({
  idPrefix,
  params,
  values,
  onChange,
  disabled,
}: {
  idPrefix: string
  params: RuleParamRead[]
  values: RuleParamValues
  onChange: (name: string, value: string) => void
  disabled?: boolean
}) {
  if (params.length === 0) {
    return (
      <p className="text-xs text-slate-500" data-testid={`${idPrefix}-no-params`}>
        This rule type takes no parameters.
      </p>
    )
  }

  return (
    <div className="flex flex-wrap items-end gap-3" data-testid={`${idPrefix}-params`}>
      {params.map((param) => {
        const inputId = `${idPrefix}-param-${param.name}`
        return (
          <div key={param.name}>
            <label className="block text-xs text-slate-600" htmlFor={inputId}>
              {param.label}
              {param.required ? ' *' : ' (optional)'}
              {param.unit ? ` (${param.unit})` : ''}
            </label>
            <input
              id={inputId}
              type={param.kind === 'local_time' ? 'time' : 'number'}
              min={param.kind === 'integer' && param.minimum !== null ? param.minimum : undefined}
              className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid={inputId}
              value={values[param.name] ?? ''}
              disabled={disabled}
              onChange={(event) => onChange(param.name, event.target.value)}
            />
          </div>
        )
      })}
    </div>
  )
}
