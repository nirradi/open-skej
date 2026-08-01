import type { RuleAppliesTo } from '../api'

/**
 * An in-progress edit of `SpaceRule.applies_to` — a draft shape the editor
 * below can hold mid-edit (an empty weekday selection, a half-typed date)
 * that `RuleAppliesTo` itself cannot represent, since the wire type only
 * describes the three shapes a *submitted* value may take.
 */
export type AppliesToDraft =
  | { mode: 'always' }
  | { mode: 'weekdays'; weekdays: number[] }
  | { mode: 'dates'; dates: string[] }

/** A draft seeded from a stored row's `applies_to` — `null` means "always". */
export function draftFromAppliesTo(appliesTo: RuleAppliesTo | null): AppliesToDraft {
  if (appliesTo === null) return { mode: 'always' }
  if ('weekdays' in appliesTo) return { mode: 'weekdays', weekdays: appliesTo.weekdays }
  return { mode: 'dates', dates: appliesTo.dates }
}

/**
 * The draft, converted to the wire shape — `null` for "always", or whichever
 * of the two legal objects `_validate_applies_to` accepts. An empty
 * weekday/date selection also collapses to `null` rather than sending an
 * empty list the server would reject as malformed; `appliesToDraftIsValid`
 * is what actually gates the save button on that case, this just keeps the
 * conversion total.
 */
export function appliesToFromDraft(draft: AppliesToDraft): RuleAppliesTo | null {
  if (draft.mode === 'always') return null
  if (draft.mode === 'weekdays') {
    return draft.weekdays.length > 0 ? { weekdays: [...draft.weekdays].sort((a, b) => a - b) } : null
  }
  const dates = draft.dates.filter((date) => date !== '')
  return dates.length > 0 ? { dates } : null
}

/** Whether a draft is submittable: a non-`"always"` mode needs at least one entry. */
export function appliesToDraftIsValid(draft: AppliesToDraft): boolean {
  if (draft.mode === 'weekdays') return draft.weekdays.length > 0
  if (draft.mode === 'dates') return draft.dates.some((date) => date !== '')
  return true
}

const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/**
 * *Always* / *these weekdays* / *these dates* — the whole of the `applies_to`
 * mechanism (decisions table). `applies_to` is a property of the instance the
 * caller writes directly onto the row, never a rule parameter, so this editor
 * is deliberately separate from `RuleParamsForm` even though both render into
 * the same row.
 */
export function AppliesToEditor({
  idPrefix,
  draft,
  onChange,
  disabled,
}: {
  idPrefix: string
  draft: AppliesToDraft
  onChange: (draft: AppliesToDraft) => void
  disabled?: boolean
}) {
  const radioName = `${idPrefix}-applies-mode`

  return (
    <div data-testid={`${idPrefix}-applies-to`}>
      <div className="flex flex-wrap gap-3 text-xs text-slate-600">
        <label className="flex items-center gap-1">
          <input
            type="radio"
            name={radioName}
            data-testid={`${idPrefix}-applies-mode-always`}
            checked={draft.mode === 'always'}
            disabled={disabled}
            onChange={() => onChange({ mode: 'always' })}
          />
          Always
        </label>
        <label className="flex items-center gap-1">
          <input
            type="radio"
            name={radioName}
            data-testid={`${idPrefix}-applies-mode-weekdays`}
            checked={draft.mode === 'weekdays'}
            disabled={disabled}
            onChange={() => onChange({ mode: 'weekdays', weekdays: [] })}
          />
          These weekdays
        </label>
        <label className="flex items-center gap-1">
          <input
            type="radio"
            name={radioName}
            data-testid={`${idPrefix}-applies-mode-dates`}
            checked={draft.mode === 'dates'}
            disabled={disabled}
            onChange={() => onChange({ mode: 'dates', dates: [''] })}
          />
          These dates
        </label>
      </div>

      {draft.mode === 'weekdays' ? (
        <div className="mt-2 flex flex-wrap gap-3">
          {WEEKDAY_LABELS.map((label, day) => (
            <label key={day} className="flex items-center gap-1 text-xs text-slate-600">
              <input
                type="checkbox"
                data-testid={`${idPrefix}-applies-weekday-${day}`}
                checked={draft.weekdays.includes(day)}
                disabled={disabled}
                onChange={(event) => {
                  const next = event.target.checked
                    ? [...draft.weekdays, day]
                    : draft.weekdays.filter((existing) => existing !== day)
                  onChange({ mode: 'weekdays', weekdays: next })
                }}
              />
              {label}
            </label>
          ))}
        </div>
      ) : null}

      {draft.mode === 'dates' ? (
        <div className="mt-2 flex flex-col gap-1">
          {draft.dates.map((date, index) => (
            <div key={index} className="flex items-center gap-2">
              <input
                type="date"
                data-testid={`${idPrefix}-applies-date-${index}`}
                className="rounded border border-slate-300 px-2 py-1 text-sm"
                value={date}
                disabled={disabled}
                onChange={(event) => {
                  const next = [...draft.dates]
                  next[index] = event.target.value
                  onChange({ mode: 'dates', dates: next })
                }}
              />
              <button
                type="button"
                data-testid={`${idPrefix}-applies-date-remove-${index}`}
                disabled={disabled}
                className="text-xs text-red-700"
                onClick={() => {
                  const next = draft.dates.filter((_, i) => i !== index)
                  onChange({ mode: 'dates', dates: next.length > 0 ? next : [''] })
                }}
              >
                Remove
              </button>
            </div>
          ))}
          <button
            type="button"
            data-testid={`${idPrefix}-applies-date-add`}
            disabled={disabled}
            className="w-fit text-xs text-slate-700 underline"
            onClick={() => onChange({ mode: 'dates', dates: [...draft.dates, ''] })}
          >
            Add another date
          </button>
        </div>
      ) : null}
    </div>
  )
}
