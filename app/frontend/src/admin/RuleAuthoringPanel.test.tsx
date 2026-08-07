// @vitest-environment jsdom
/**
 * Tests for `RuleAuthoringPanel` (task 7.9).
 *
 * Polling is real `setTimeout`, driven by fake timers
 * (`vi.useFakeTimers({ shouldAdvanceTime: true })`, the same convention
 * `App.test.tsx` uses) so a job's backoff can be advanced deterministically
 * rather than the suite actually waiting seconds per test.
 */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createRuleDraft, getRuleDraft, listRuleDrafts } from '../api'
import { makeRuleDraft, makeRuleType, makeSpace, ok } from './fixtures'
import { RuleAuthoringPanel } from './RuleAuthoringPanel'

vi.mock('../api', () => ({
  createRuleDraft: vi.fn(),
  listRuleDrafts: vi.fn(),
  getRuleDraft: vi.fn(),
}))

const SPACE = makeSpace({ public_id: 'sp_7f3a9c' })

function renderPanel(overrides: Partial<Parameters<typeof RuleAuthoringPanel>[0]> = {}) {
  const onJobSucceeded = vi.fn()
  const onAddToSpace = vi.fn()
  const utils = render(
    <RuleAuthoringPanel
      space={SPACE}
      disabled={false}
      ruleTypes={[]}
      onJobSucceeded={onJobSucceeded}
      onAddToSpace={onAddToSpace}
      {...overrides}
    />,
  )
  return { ...utils, onJobSucceeded, onAddToSpace }
}

/** Flushes both the pending timer and the promise chain it triggers. */
async function advancePoll(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('RuleAuthoringPanel — hidden when generation is disabled', () => {
  it('renders nothing on a bare 404 from listRuleDrafts', async () => {
    vi.mocked(listRuleDrafts).mockResolvedValue({
      outcome: 'not_found',
      message: "We couldn't find that.",
    })

    const { container } = renderPanel()

    await act(async () => {})
    expect(container.firstChild).toBeNull()
  })
})

describe('RuleAuthoringPanel — blocked', () => {
  it('shows an error rather than assuming the route guard held, on forbidden', async () => {
    vi.mocked(listRuleDrafts).mockResolvedValue({
      outcome: 'forbidden',
      message: "You don't have permission to do that.",
    })

    renderPanel()

    const blocked = await screen.findByTestId('rule-authoring-blocked')
    expect(blocked.textContent).toBe("You don't have permission to do that.")
  })
})

describe('RuleAuthoringPanel — submit, running, succeeded', () => {
  it('walks a job from submit through to succeeded, polling with backoff', async () => {
    vi.mocked(listRuleDrafts).mockResolvedValue(ok([]))
    const queued = makeRuleDraft({ id: 1, status: 'queued' })
    vi.mocked(createRuleDraft).mockResolvedValue(ok(queued))

    const ruleType = makeRuleType({
      rule_type: 'no_long_bookings',
      label: 'No long bookings',
      description: 'Refuses a booking longer than one hour.',
    })
    const { onJobSucceeded, onAddToSpace } = renderPanel({ ruleTypes: [ruleType] })

    const prompt = await screen.findByTestId('rule-authoring-prompt')
    fireEvent.change(prompt, { target: { value: 'no booking may be longer than one hour' } })
    fireEvent.click(screen.getByTestId('rule-authoring-submit'))

    expect(await screen.findByTestId('rule-authoring-status')).toBeTruthy()
    expect(vi.mocked(createRuleDraft)).toHaveBeenCalledWith(
      'sp_7f3a9c',
      'no booking may be longer than one hour',
    )

    // First poll, at the fast (two-second) cadence: still running, one
    // attempt recorded.
    const running = makeRuleDraft({
      id: 1,
      status: 'running',
      attempts: [{ number: 1, outcome: null, failure: null }],
    })
    vi.mocked(getRuleDraft).mockResolvedValueOnce(ok(running))
    await advancePoll(2_000)
    expect(screen.getByTestId('rule-authoring-status').textContent).toBe(
      'Writing the rule and testing it — attempt 2 of 4.',
    )

    // Second poll: succeeded, with the generated type and its source.
    const succeeded = makeRuleDraft({
      id: 1,
      status: 'succeeded',
      attempts: [{ number: 1, outcome: 'passed', failure: null }],
      generated_rule_type: 'no_long_bookings',
      human_code: 'class NoLongBookings(BaseRule):\n    pass\n',
    })
    vi.mocked(getRuleDraft).mockResolvedValueOnce(ok(succeeded))
    await advancePoll(2_000)

    expect(await screen.findByTestId('rule-authoring-succeeded')).toBeTruthy()
    expect(screen.getByTestId('rule-authoring-generated-type').textContent).toBe('No long bookings')
    expect(screen.getByTestId('rule-authoring-generated-description').textContent).toBe(
      'Refuses a booking longer than one hour.',
    )
    expect(onJobSucceeded).toHaveBeenCalledTimes(1)

    // The source sits behind a disclosure, not rendered open by default.
    const disclosure = screen.getByTestId('rule-authoring-code-disclosure') as HTMLDetailsElement
    expect(disclosure.open).toBe(false)
    expect(screen.getByTestId('rule-authoring-code').textContent).toContain('NoLongBookings')

    // "Add this rule to the Space" reports the rule_type upward and nothing else.
    fireEvent.click(screen.getByTestId('rule-authoring-add-to-space'))
    expect(onAddToSpace).toHaveBeenCalledWith('no_long_bookings')
  })
})

describe('RuleAuthoringPanel — failed', () => {
  it('shows generic copy, never attempt failure text, and keeps the prompt editable', async () => {
    vi.mocked(listRuleDrafts).mockResolvedValue(ok([]))
    const queued = makeRuleDraft({ id: 2, status: 'queued', prompt: 'an odd request' })
    vi.mocked(createRuleDraft).mockResolvedValue(ok(queued))

    renderPanel()

    const prompt = await screen.findByTestId('rule-authoring-prompt')
    fireEvent.change(prompt, { target: { value: 'an odd request' } })
    fireEvent.click(screen.getByTestId('rule-authoring-submit'))
    await screen.findByTestId('rule-authoring-status')

    const failed = makeRuleDraft({
      id: 2,
      status: 'failed',
      prompt: 'an odd request',
      error: 'Generation failed after 4 attempt(s): tests_failed.\n\nTraceback pytest output here',
      attempts: [{ number: 1, outcome: 'tests_failed', failure: 'Traceback pytest output here' }],
    })
    vi.mocked(getRuleDraft).mockResolvedValueOnce(ok(failed))
    await advancePoll(2_000)

    const failedNotice = await screen.findByTestId('rule-authoring-failed')
    expect(failedNotice.textContent).not.toContain('Traceback')
    expect(failedNotice.textContent).not.toContain('pytest')

    // The prompt is still in the box, editable and resubmittable.
    const promptAfterFailure = screen.getByTestId('rule-authoring-prompt') as HTMLTextAreaElement
    expect(promptAfterFailure.value).toBe('an odd request')
    expect((screen.getByTestId('rule-authoring-submit') as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('RuleAuthoringPanel — resume on mount', () => {
  it('goes straight to running for a job already in flight', async () => {
    const inFlight = makeRuleDraft({
      id: 9,
      status: 'running',
      attempts: [{ number: 1, outcome: null, failure: null }],
    })
    vi.mocked(listRuleDrafts).mockResolvedValue(ok([inFlight]))
    vi.mocked(getRuleDraft).mockResolvedValue(ok(inFlight))

    renderPanel()

    expect(await screen.findByTestId('rule-authoring-status')).toBeTruthy()
    expect(screen.queryByTestId('rule-authoring-prompt')).toBeNull()
  })

  it('starts idle, with the prompt box shown, when nothing is in flight', async () => {
    vi.mocked(listRuleDrafts).mockResolvedValue(ok([]))

    renderPanel()

    expect(await screen.findByTestId('rule-authoring-prompt')).toBeTruthy()
    expect(screen.queryByTestId('rule-authoring-status')).toBeNull()
  })
})

/** Flips `document.hidden` and fires the event the panel actually listens for. */
function setDocumentHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('RuleAuthoringPanel — pauses while the tab is hidden', () => {
  afterEach(() => setDocumentHidden(false))

  it('stops polling while hidden and polls immediately on return', async () => {
    const inFlight = makeRuleDraft({ id: 9, status: 'running', attempts: [] })
    vi.mocked(listRuleDrafts).mockResolvedValue(ok([inFlight]))
    vi.mocked(getRuleDraft).mockResolvedValue(ok(inFlight))

    renderPanel()
    await screen.findByTestId('rule-authoring-status')

    setDocumentHidden(true)
    // Long past the two-second cadence — if the pause did not take, this
    // would already have polled at least once.
    await advancePoll(20_000)
    expect(vi.mocked(getRuleDraft)).not.toHaveBeenCalled()

    setDocumentHidden(false)
    await act(async () => {
      await Promise.resolve()
    })
    expect(vi.mocked(getRuleDraft)).toHaveBeenCalledTimes(1)
  })
})
