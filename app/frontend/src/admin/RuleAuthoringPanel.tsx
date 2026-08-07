import { useEffect, useRef, useState } from 'react'

import {
  createRuleDraft,
  getRuleDraft,
  listRuleDrafts,
  type RuleDraftRead,
  type RuleTypeRead,
  type Space,
} from '../api'
import { messageFor } from './messages'

/** Mirrors `_RULE_PROMPT_MAX` in `app/backend/app/identity/schemas.py`. */
const PROMPT_MAX_LENGTH = 500

/** The loop's own budget: one attempt plus up to three retries. */
const MAX_ATTEMPTS = 4

/** How often to poll while a job is queued or running, before the first minute. */
const POLL_INTERVAL_MS = 2_000

/** How often to poll after the first minute — the "slower after" half of the spec. */
const POLL_INTERVAL_SLOW_MS = 10_000

/** When the backoff switches from `POLL_INTERVAL_MS` to `POLL_INTERVAL_SLOW_MS`. */
const POLL_SLOWDOWN_AFTER_MS = 60_000

type State =
  | { kind: 'loading' }
  | { kind: 'hidden' }
  | { kind: 'blocked'; message: string }
  | { kind: 'idle' }
  | { kind: 'submitting' }
  | { kind: 'running'; job: RuleDraftRead }
  | { kind: 'succeeded'; job: RuleDraftRead }
  | { kind: 'failed'; job: RuleDraftRead }

/**
 * "Write a rule in English": a prompt, a submit, and a job that reports on
 * itself while it runs (task 7.9).
 *
 * ## A convenience, not a boundary
 *
 * Rendered only inside `RulesManager`'s admin branch, but — like every other
 * panel in this directory — it does not assume that gate held. On mount it
 * calls `listRuleDrafts` to resume an in-flight job, and that call is where
 * every access outcome is actually handled: `forbidden` (a second admin
 * demoted this caller between page load and this panel mounting) renders as
 * an error, and a bare `not_found` is read as "the backend does not have
 * `RULE_GENERATION_ENABLED` set" — see `listRuleDrafts`'s own docstring — so
 * the panel hides itself entirely rather than showing an error for a feature
 * that was never there.
 *
 * ## Polling, with backoff and a visibility pause
 *
 * While a job is `queued` or `running`, this polls `GET .../rule-drafts/{id}`
 * every two seconds for the first minute and every ten seconds after that —
 * generation is minutes of wall clock, and a poll cadence that never eases up
 * would cost nothing the admin notices but nothing anyone asked for either.
 * Polling stops while the tab is hidden (`document.hidden`) and an immediate
 * poll fires the moment it becomes visible again, rather than resuming
 * whatever cadence was in flight when it was hidden — a tab backgrounded for
 * an hour should not open to a stale status held over from before.
 *
 * ## What the running state shows, and what it deliberately does not
 *
 * `job.attempts` is the material for "what is actually happening" — a
 * spinner with no account of itself reads as a hang on a job that can take
 * minutes. What is never rendered is any attempt's own `failure` text: that
 * is pytest output and traceback excerpts written for the model to read, the
 * same category of thing `RULE_ERROR_MESSAGE` exists to keep out of the UI
 * (`.claude/rules/rule-engine.md`). A failed job says only that the rule
 * could not be written and that rephrasing more specifically usually helps —
 * the prompt stays in the box, editable and resubmittable.
 */
export function RuleAuthoringPanel({
  space,
  disabled,
  ruleTypes,
  onJobSucceeded,
  onAddToSpace,
}: {
  space: Space
  disabled: boolean
  /**
   * The registry `RulesManager` already fetched, so the succeeded state can
   * render the new type's label and description rather than its bare
   * `rule_type` slug. Freshly generated types are not in this list at first —
   * see `onJobSucceeded`.
   */
  ruleTypes: RuleTypeRead[]
  /**
   * Called the moment a job reaches `succeeded`, before the admin does
   * anything — the new type is not in `ruleTypes` yet (it did not exist when
   * `RulesManager` last fetched the registry), so both the label shown here
   * and the option `AddRulePanel` would need to offer it depend on a refetch
   * happening regardless of whether the admin ever clicks "Add".
   */
  onJobSucceeded: () => void
  /**
   * Called when the admin chooses "Add this rule to the Space", with the
   * `rule_type` string it produced — the drop-in to the existing "Add a
   * rule" flow the task describes. The page owns what that means
   * (preselecting it in `AddRulePanel`); this component only reports the
   * choice.
   */
  onAddToSpace: (ruleType: string) => void
}) {
  const [state, setState] = useState<State>({ kind: 'loading' })
  const [prompt, setPrompt] = useState('')
  const [submitError, setSubmitError] = useState('')

  // Kept in a ref rather than an effect dependency: `RulesManager` passes a
  // fresh closure on every render, and depending on it directly would tear
  // down and restart the polling effect below — resetting its backoff clock —
  // any time something unrelated re-renders the page. Updated from its own
  // effect, never assigned during render, per this repo's `react-hooks/refs`
  // lint rule.
  const onJobSucceededRef = useRef(onJobSucceeded)
  useEffect(() => {
    onJobSucceededRef.current = onJobSucceeded
  })

  // Resume-on-mount: does this Space already have a job in flight?
  useEffect(() => {
    let cancelled = false

    void listRuleDrafts(space.public_id).then((result) => {
      if (cancelled) return

      if (result.outcome === 'ok') {
        const inFlight = result.data.find(
          (job) => job.status === 'queued' || job.status === 'running',
        )
        setState(inFlight ? { kind: 'running', job: inFlight } : { kind: 'idle' })
        return
      }

      if (result.outcome === 'not_found') {
        setState({ kind: 'hidden' })
        return
      }

      setState({ kind: 'blocked', message: messageFor(result) })
    })

    return () => {
      cancelled = true
    }
  }, [space.public_id])

  // Poll while a job is in flight. Keyed on the job's id rather than on
  // `state` itself, so updating `state` with each poll's fresh `job` (to show
  // its growing `attempts`) does not itself retrigger this effect.
  const runningJobId = state.kind === 'running' ? state.job.id : null

  useEffect(() => {
    if (runningJobId === null) return

    let cancelled = false
    let timeoutId: ReturnType<typeof setTimeout> | null = null
    const startedAt = Date.now()

    function poll() {
      timeoutId = null
      void getRuleDraft(space.public_id, runningJobId!).then((result) => {
        if (cancelled) return

        if (result.outcome !== 'ok') {
          setState({ kind: 'blocked', message: messageFor(result) })
          return
        }

        const job = result.data
        if (job.status === 'succeeded') {
          setState({ kind: 'succeeded', job })
          onJobSucceededRef.current()
          return
        }
        if (job.status === 'failed') {
          setState({ kind: 'failed', job })
          return
        }

        setState({ kind: 'running', job })
        scheduleNext()
      })
    }

    function scheduleNext() {
      if (cancelled || document.hidden) return
      const elapsed = Date.now() - startedAt
      const delay = elapsed < POLL_SLOWDOWN_AFTER_MS ? POLL_INTERVAL_MS : POLL_INTERVAL_SLOW_MS
      timeoutId = setTimeout(poll, delay)
    }

    function handleVisibilityChange() {
      if (document.hidden) {
        if (timeoutId !== null) {
          clearTimeout(timeoutId)
          timeoutId = null
        }
        return
      }
      // Becoming visible again: poll now rather than waiting out whatever was
      // left of the interval that was running when the tab was hidden.
      if (timeoutId === null) poll()
    }

    document.addEventListener('visibilitychange', handleVisibilityChange)
    scheduleNext()

    return () => {
      cancelled = true
      if (timeoutId !== null) clearTimeout(timeoutId)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }, [runningJobId, space.public_id])

  async function handleSubmit() {
    setSubmitError('')
    setState({ kind: 'submitting' })

    const result = await createRuleDraft(space.public_id, prompt.trim())

    if (result.outcome === 'ok') {
      // `prompt` deliberately survives a successful submit rather than being
      // cleared here: if this job later fails, the failed state re-shows the
      // form with the value still in the box, exactly as the task asks —
      // "the prompt still in the box so it can be edited and resubmitted".
      // Clearing happens only when the admin explicitly starts over
      // (`handleWriteAnother`), never as a side effect of submitting.
      setState({ kind: 'running', job: result.data })
      return
    }

    setSubmitError(messageFor(result))
    setState({ kind: 'idle' })
  }

  function handleWriteAnother() {
    setPrompt('')
    setState({ kind: 'idle' })
  }

  if (state.kind === 'loading' || state.kind === 'hidden') {
    return null
  }

  return (
    <section
      className="rounded-lg border border-slate-200 bg-white p-4"
      data-testid="rule-authoring-panel"
    >
      <h2 className="text-sm font-semibold text-slate-900">Write a rule in English</h2>
      <p className="mt-1 text-xs text-slate-600">
        Describe a booking constraint in plain language — "no more than two bookings a week" — and a
        rule type will be written and tested for you. Adding it to this Space is a separate step,
        once it is ready.
      </p>

      {state.kind === 'blocked' ? (
        <p className="mt-3 text-sm text-red-700" data-testid="rule-authoring-blocked" role="alert">
          {state.message}
        </p>
      ) : null}

      {state.kind === 'running' ? <RunningStatus job={state.job} /> : null}

      {state.kind === 'succeeded' ? (
        <SucceededSummary
          job={state.job}
          ruleType={
            ruleTypes.find((candidate) => candidate.rule_type === state.job.generated_rule_type) ??
            null
          }
          onAddToSpace={() => {
            if (state.job.generated_rule_type) onAddToSpace(state.job.generated_rule_type)
          }}
          onWriteAnother={handleWriteAnother}
        />
      ) : null}

      {state.kind === 'idle' || state.kind === 'submitting' || state.kind === 'failed' ? (
        <div className="mt-3">
          {state.kind === 'failed' ? (
            <p
              className="mb-2 text-sm text-red-700"
              data-testid="rule-authoring-failed"
              role="alert"
            >
              That rule could not be written. Rephrasing the prompt more specifically usually helps.
            </p>
          ) : null}

          <label className="block text-xs text-slate-600" htmlFor="rule-authoring-prompt">
            What should this rule refuse?
          </label>
          <textarea
            id="rule-authoring-prompt"
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="rule-authoring-prompt"
            rows={2}
            maxLength={PROMPT_MAX_LENGTH}
            value={prompt}
            disabled={disabled || state.kind === 'submitting'}
            onChange={(event) => setPrompt(event.target.value)}
          />

          <button
            type="button"
            className="mt-2 rounded bg-slate-800 px-3 py-1 text-sm text-white disabled:opacity-50"
            data-testid="rule-authoring-submit"
            disabled={disabled || state.kind === 'submitting' || prompt.trim().length === 0}
            onClick={() => void handleSubmit()}
          >
            {state.kind === 'submitting' ? 'Submitting…' : 'Write this rule'}
          </button>

          {submitError ? (
            <p
              className="mt-2 text-sm text-red-700"
              data-testid="rule-authoring-error"
              role="alert"
            >
              {submitError}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

/** What a queued or running job's attempts say about how far it has got. */
function RunningStatus({ job }: { job: RuleDraftRead }) {
  const attemptNumber = Math.min(job.attempts.length + 1, MAX_ATTEMPTS)
  const label =
    job.status === 'queued'
      ? 'Queued — waiting to start.'
      : `Writing the rule and testing it — attempt ${attemptNumber} of ${MAX_ATTEMPTS}.`

  return (
    <p className="mt-3 text-sm text-slate-600" data-testid="rule-authoring-status" role="status">
      {label}
    </p>
  )
}

/**
 * The succeeded state: what got built, an offer to add it, and its source
 * behind a disclosure.
 *
 * `ruleType` is looked up from `RulesManager`'s own registry, which
 * `onJobSucceeded` refetches the instant a job succeeds — so this renders the
 * generated type's real label and LLM-authored description, the same as
 * `AddRulePanel` shows for every other type, rather than its bare
 * `rule_type` slug. `null` for the brief window before that refetch resolves,
 * where the slug is the only thing there is to show.
 */
function SucceededSummary({
  job,
  ruleType,
  onAddToSpace,
  onWriteAnother,
}: {
  job: RuleDraftRead
  ruleType: RuleTypeRead | null
  onAddToSpace: () => void
  onWriteAnother: () => void
}) {
  return (
    <div
      className="mt-3 rounded border border-emerald-200 bg-emerald-50 p-3"
      data-testid="rule-authoring-succeeded"
    >
      <p
        className="text-sm font-medium text-emerald-900"
        data-testid="rule-authoring-generated-type"
      >
        {ruleType?.label ?? job.generated_rule_type}
      </p>
      {ruleType ? (
        <p
          className="mt-1 text-sm text-emerald-800"
          data-testid="rule-authoring-generated-description"
        >
          {ruleType.description}
        </p>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          className="rounded bg-slate-800 px-3 py-1 text-sm text-white"
          data-testid="rule-authoring-add-to-space"
          onClick={onAddToSpace}
        >
          Add this rule to the Space
        </button>
        <button
          type="button"
          className="rounded border border-slate-300 px-3 py-1 text-sm"
          data-testid="rule-authoring-write-another"
          onClick={onWriteAnother}
        >
          Write another rule
        </button>
      </div>

      {job.human_code ? (
        <details className="mt-3" data-testid="rule-authoring-code-disclosure">
          <summary
            className="cursor-pointer text-xs text-slate-600"
            data-testid="rule-authoring-code-toggle"
          >
            Show the code
          </summary>
          <pre
            className="mt-2 overflow-x-auto rounded bg-slate-900 p-2 text-xs text-slate-100"
            data-testid="rule-authoring-code"
          >
            {job.human_code}
          </pre>
        </details>
      ) : null}
    </div>
  )
}
