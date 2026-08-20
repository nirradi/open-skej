/**
 * `/s/{public_id}/shape` — the Space's structural-calendar studio.
 *
 * This is intentionally a second authoring surface beside `SpaceRulesPage`,
 * not a routed chat that tries to decide whether English describes structure
 * or policy. A shape says what the venue offers, so the shared `CalendarGrid`
 * can preview it without bookings; rules stay on their existing page and say
 * who may take that offered time.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'

import {
  createShapeConversation,
  createShapeConversationTurn,
  discardCalendarShapeDraft,
  getOpenShapeConversation,
  getShapeConversation,
  getSpace,
  getSpaceCalendar,
  publishCalendarShape,
  type DayProjectionRead,
  type ShapeConversationRead,
  type ShapeMessageRead,
  type Space,
} from '../api'
import { addDays, buildWeekProjection, CalendarGrid, DAYS_PER_WEEK, startOfWeek } from '../calendar'
import { SYSTEM_TIME_ZONE, zonedCalendarDate } from '../timezone'
import { messageFor } from './messages'
import {
  clearStoredShapeConversation,
  readStoredShapeConversation,
  storeShapeConversation,
} from './shapeConversationStore'

type SpaceLoad = { kind: 'ok'; space: Space } | { kind: 'error'; message: string } | null
type CalendarLoad =
  | { kind: 'ok'; key: string; data: DayProjectionRead[] }
  | { kind: 'error'; key: string; message: string }
  | null

const CALENDAR_ERROR = "We couldn't load this calendar preview."

function isAdmin(space: Space): boolean {
  return space.my_role === 'owner' || space.my_role === 'admin'
}

/** Compare the stored shape document, retaining array order but normalising object key order. */
function sameShapeDocument(left: Record<string, unknown>, right: Record<string, unknown>): boolean {
  const canonical = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(canonical)
    if (value !== null && typeof value === 'object') {
      return Object.fromEntries(
        Object.entries(value as Record<string, unknown>)
          .sort(([leftKey], [rightKey]) => leftKey.localeCompare(rightKey))
          .map(([key, entry]) => [key, canonical(entry)]),
      )
    }
    return value
  }
  return JSON.stringify(canonical(left)) === JSON.stringify(canonical(right))
}

/** The latest assistant turn is the server-authoritative status of the current draft. */
function questionFromTranscript(messages: ShapeMessageRead[]): string | null {
  return [...messages].reverse().find((message) => message.role === 'assistant')?.question ?? null
}

export function SpaceShapePage() {
  const { publicId } = useParams<{ publicId: string }>()

  if (!publicId) {
    return (
      <Shell>
        <p className="text-sm text-red-700" role="alert" data-testid="space-shape-invalid">
          That link doesn&rsquo;t work.
        </p>
      </Shell>
    )
  }

  return <SpaceShapePageInner publicId={publicId} />
}

function SpaceShapePageInner({ publicId }: { publicId: string }) {
  const [spaceLoad, setSpaceLoad] = useState<SpaceLoad>(null)
  const [conversation, setConversation] = useState<ShapeConversationRead | null>(null)
  const [conversationLoading, setConversationLoading] = useState(false)
  const [question, setQuestion] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const [turnBusy, setTurnBusy] = useState(false)
  const [publishBusy, setPublishBusy] = useState(false)
  const [discardBusy, setDiscardBusy] = useState(false)
  const [discardConfirming, setDiscardConfirming] = useState(false)
  const [allowUnbookable, setAllowUnbookable] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [frozenPreview, setFrozenPreview] = useState<ReturnType<typeof buildWeekProjection> | null>(
    null,
  )
  const [now] = useState(() => new Date())
  const [calendarRevision, setCalendarRevision] = useState(0)

  const space = spaceLoad?.kind === 'ok' ? spaceLoad.space : null
  const timeZone = space?.timezone ?? SYSTEM_TIME_ZONE
  const currentWeek = startOfWeek(zonedCalendarDate(now, timeZone))
  const [weekState, setWeekState] = useState(() => ({ timeZone, weekStart: currentWeek }))
  if (weekState.timeZone !== timeZone) {
    // `weekStart` is a calendar date in the Space's clock. Until `getSpace`
    // resolves there is no such clock; when it does, re-anchor the initial
    // week through the actual zone rather than treating the browser's date as
    // a portable value.
    setWeekState({ timeZone, weekStart: currentWeek })
  }
  const weekStart = weekState.weekStart
  const calendarKey = `${weekStart.getTime()}:${timeZone}:${calendarRevision}`
  const draftId = conversation?.draft?.id ?? null
  const hasDraft = draftId !== null
  // `upsert_draft` keeps the same row id between ordinary turns. Its updated
  // timestamp is therefore the revision marker that must key a fresh preview.
  const draftRevision = conversation?.draft?.created_at ?? 'none'
  const draftCalendarKey = `${calendarKey}:${draftId ?? 'none'}:${draftRevision}`

  const [liveCalendar, setLiveCalendar] = useState<CalendarLoad>(null)
  const [draftCalendar, setDraftCalendar] = useState<CalendarLoad>(null)

  const startConversation = useCallback(async () => {
    setConversationLoading(true)
    setActionError(null)
    setSuccess(null)
    const result = await createShapeConversation(publicId)
    setConversationLoading(false)
    if (result.outcome === 'ok') {
      setConversation(result.data)
      setQuestion(null)
      storeShapeConversation(publicId, result.data.id)
      return
    }
    setActionError(messageFor(result))
  }, [publicId])

  useEffect(() => {
    let cancelled = false

    void (async () => {
      const spaceResult = await getSpace(publicId)
      if (cancelled) return
      if (spaceResult.outcome !== 'ok') {
        setSpaceLoad({ kind: 'error', message: messageFor(spaceResult) })
        return
      }
      setSpaceLoad({ kind: 'ok', space: spaceResult.data })
      if (!isAdmin(spaceResult.data) || spaceResult.data.archived_at !== null) return

      const adoptOpenConversation = (next: ShapeConversationRead) => {
        const recoveredQuestion = questionFromTranscript(next.messages)
        setConversation(next)
        setQuestion(recoveredQuestion)
        storeShapeConversation(publicId, next.id)
      }
      const discoverOrCreate = async () => {
        setConversationLoading(true)
        const currentResult = await getOpenShapeConversation(publicId)
        if (cancelled) return
        setConversationLoading(false)
        if (currentResult.outcome !== 'ok') {
          setActionError(messageFor(currentResult))
          return
        }
        if (currentResult.data !== null) {
          adoptOpenConversation(currentResult.data)
          return
        }
        await startConversation()
      }

      const stored = readStoredShapeConversation(publicId)
      if (stored === null) {
        await discoverOrCreate()
        return
      }

      setConversationLoading(true)
      const conversationResult = await getShapeConversation(publicId, stored.id)
      if (cancelled) return
      setConversationLoading(false)
      if (conversationResult.outcome === 'ok' && conversationResult.data.status === 'open') {
        adoptOpenConversation(conversationResult.data)
        return
      }

      // The id route is Space-scoped, so a pointer from another Space, a
      // closed conversation, and a deleted local entry are all stale. Ask the
      // recovery endpoint before POSTing so a fresh browser never races an
      // existing conversation just because localStorage was unavailable.
      clearStoredShapeConversation(publicId)
      if (conversationResult.outcome === 'ok' || conversationResult.outcome === 'not_found') {
        await discoverOrCreate()
        return
      }
      setActionError(messageFor(conversationResult))
    })()

    return () => {
      cancelled = true
    }
  }, [publicId, startConversation])

  useEffect(() => {
    if (space === null) return
    let cancelled = false
    void getSpaceCalendar(publicId, weekStart, addDays(weekStart, DAYS_PER_WEEK - 1)).then(
      (result) => {
        if (cancelled) return
        if (result.outcome === 'ok') {
          setLiveCalendar({ kind: 'ok', key: calendarKey, data: result.data })
          return
        }
        setLiveCalendar({
          kind: 'error',
          key: calendarKey,
          message: result.outcome === 'invalid_request' ? CALENDAR_ERROR : result.message,
        })
      },
    )
    return () => {
      cancelled = true
    }
    // `calendarKey` carries both values read by the request. Keeping it value-keyed avoids
    // refetching for an equal calendar-date carrier constructed during a render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [publicId, calendarKey, space])

  useEffect(() => {
    if (space === null || draftId === null) return
    let cancelled = false
    void getSpaceCalendar(publicId, weekStart, addDays(weekStart, DAYS_PER_WEEK - 1), {
      draft: true,
    }).then((result) => {
      if (cancelled) return
      if (result.outcome === 'ok') {
        setDraftCalendar({ kind: 'ok', key: draftCalendarKey, data: result.data })
        return
      }
      setDraftCalendar({
        kind: 'error',
        key: draftCalendarKey,
        message: result.outcome === 'invalid_request' ? CALENDAR_ERROR : result.message,
      })
    })
    return () => {
      cancelled = true
    }
    // See the matching live-calendar effect above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [publicId, draftCalendarKey, draftId, space])

  const liveWeek = useMemo(
    () =>
      liveCalendar?.kind === 'ok' && liveCalendar.key === calendarKey
        ? buildWeekProjection(liveCalendar.data, timeZone)
        : null,
    [liveCalendar, calendarKey, timeZone],
  )
  const draftWeek = useMemo(
    () =>
      hasDraft && draftCalendar?.kind === 'ok' && draftCalendar.key === draftCalendarKey
        ? buildWeekProjection(draftCalendar.data, timeZone)
        : null,
    [draftCalendar, draftCalendarKey, hasDraft, timeZone],
  )
  // A reloaded clarification has no prior rendered week to freeze. Keep the
  // usable live calendar visible instead of presenting the known-unbookable
  // saved draft as a useful preview.
  const previewWeek =
    frozenPreview ?? (question !== null ? liveWeek : (draftWeek ?? liveWeek)) ?? undefined
  const draftMatchesLive =
    conversation !== null &&
    conversation.draft !== null &&
    sameShapeDocument(conversation.live.document, conversation.draft.document)

  async function submitTurn() {
    const trimmed = message.trim()
    if (!conversation || !trimmed || turnBusy) return

    setTurnBusy(true)
    setActionError(null)
    setSuccess(null)
    const result = await createShapeConversationTurn(publicId, conversation.id, trimmed)
    setTurnBusy(false)
    if (result.outcome !== 'ok') {
      if (result.outcome === 'not_found') {
        clearStoredShapeConversation(publicId)
        setConversation(null)
      }
      setActionError(messageFor(result))
      return
    }

    const timestamp = new Date().toISOString()
    const lastOrdinal = conversation.messages.at(-1)?.ordinal ?? 0
    const userMessage: ShapeMessageRead = {
      ordinal: lastOrdinal + 1,
      role: 'user',
      content: trimmed,
      question: null,
      resulting_shape_version_id: null,
      created_at: timestamp,
    }
    const assistantMessage: ShapeMessageRead = {
      ordinal: lastOrdinal + 2,
      role: 'assistant',
      content: result.data.summary,
      question: result.data.question,
      resulting_shape_version_id: result.data.draft.id,
      created_at: timestamp,
    }
    setConversation({
      ...conversation,
      messages: [...conversation.messages, userMessage, assistantMessage],
      draft: result.data.draft,
    })
    setMessage('')
    setQuestion(result.data.question)
    storeShapeConversation(publicId, conversation.id)
    if (result.data.question !== null) {
      // Keep the last useful preview on screen. The candidate remains saved as
      // a draft for the next turn, but the agent has explicitly said it offers
      // no booking, so replacing the grid with it would hide the feedback loop.
      setFrozenPreview(previewWeek ?? null)
    } else {
      setFrozenPreview(null)
    }
  }

  async function publish() {
    if (!hasDraft || publishBusy) return
    setPublishBusy(true)
    setActionError(null)
    const result = await publishCalendarShape(publicId, { allowUnbookable })
    setPublishBusy(false)
    if (result.outcome !== 'ok') {
      if (result.outcome === 'not_found') clearStoredShapeConversation(publicId)
      setActionError(messageFor(result))
      return
    }
    clearStoredShapeConversation(publicId)
    setConversation(null)
    setQuestion(null)
    setFrozenPreview(null)
    setCalendarRevision((revision) => revision + 1)
    setSuccess('Published. This is now what members can book.')
  }

  async function discard() {
    if (discardBusy) return
    setDiscardBusy(true)
    setActionError(null)
    const result = await discardCalendarShapeDraft(publicId)
    setDiscardBusy(false)
    if (result.outcome !== 'ok') {
      if (result.outcome === 'not_found') clearStoredShapeConversation(publicId)
      setActionError(messageFor(result))
      return
    }
    clearStoredShapeConversation(publicId)
    setConversation(null)
    setQuestion(null)
    setFrozenPreview(null)
    setCalendarRevision((revision) => revision + 1)
    setDiscardConfirming(false)
    setSuccess('Discarded the draft and closed this conversation.')
  }

  if (spaceLoad === null) {
    return (
      <Shell>
        <p className="text-sm text-slate-600" role="status" data-testid="space-shape-loading">
          Loading…
        </p>
      </Shell>
    )
  }
  if (spaceLoad.kind === 'error') {
    return (
      <Shell>
        <p className="text-sm text-red-700" role="alert" data-testid="space-shape-error">
          {spaceLoad.message}
        </p>
      </Shell>
    )
  }
  const resolvedSpace = spaceLoad.space
  if (!isAdmin(resolvedSpace)) {
    return (
      <Shell space={resolvedSpace}>
        <section
          className="rounded-lg border border-slate-200 bg-white p-4"
          data-testid="shape-member-notice"
        >
          <h2 className="text-sm font-semibold text-slate-900">{resolvedSpace.name}</h2>
          <p className="mt-2 text-sm text-slate-600">
            You are a member of this Space. Only its admins can change its calendar shape.
          </p>
        </section>
      </Shell>
    )
  }

  const controlsDisabled = resolvedSpace.archived_at !== null
  return (
    <Shell space={resolvedSpace}>
      {resolvedSpace.archived_at !== null ? (
        <p
          className="mb-4 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
          data-testid="shape-archived"
          role="status"
        >
          This Space is archived. Its calendar shape can no longer be changed.
        </p>
      ) : null}
      <p className="mb-6 text-sm text-slate-600">
        Need to change who may book or how much they may take?{' '}
        <Link className="underline" to={`/s/${resolvedSpace.public_id}/rules`}>
          Manage rules instead.
        </Link>
      </p>

      <div className="grid gap-6 xl:grid-cols-2">
        <section
          className="rounded-lg border border-slate-200 bg-white p-4"
          data-testid="shape-chat"
        >
          <h2 className="text-lg font-semibold text-slate-900">Describe the calendar</h2>
          <p className="mt-1 text-sm text-slate-600">
            Tell the assistant when the venue opens, closes, or takes breaks. Each reply changes a
            private draft until you publish it.
          </p>

          {actionError ? (
            <p className="mt-3 text-sm text-red-700" data-testid="shape-action-error" role="alert">
              {actionError}
            </p>
          ) : null}
          {success ? (
            <p className="mt-3 text-sm text-emerald-700" data-testid="shape-success" role="status">
              {success}
            </p>
          ) : null}

          <div className="mt-4 max-h-96 space-y-3 overflow-y-auto" data-testid="shape-transcript">
            {conversation?.messages.map((entry) => (
              <article
                key={entry.ordinal}
                className={`rounded p-3 text-sm ${entry.role === 'user' ? 'bg-slate-100' : 'bg-indigo-50'}`}
                data-testid={`shape-message-${entry.role}-${entry.ordinal}`}
              >
                <p className="mb-1 text-xs font-medium text-slate-500">
                  {entry.role === 'user' ? 'You' : 'Shape assistant'}
                </p>
                <p>{entry.content}</p>
              </article>
            ))}
            {conversationLoading ? (
              <p className="text-sm text-slate-500" data-testid="shape-conversation-loading">
                Opening conversation…
              </p>
            ) : null}
          </div>

          {question ? (
            <aside
              className="mt-4 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
              data-testid="shape-question"
            >
              <p className="font-medium">The assistant needs a clarification</p>
              <p className="mt-1">{question}</p>
            </aside>
          ) : null}

          {conversation === null ? (
            <button
              type="button"
              className="mt-4 rounded bg-slate-800 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
              data-testid="shape-start-conversation"
              disabled={controlsDisabled || conversationLoading}
              onClick={() => void startConversation()}
            >
              Start a new conversation
            </button>
          ) : (
            <form
              className="mt-4 space-y-2"
              onSubmit={(event) => {
                event.preventDefault()
                void submitTurn()
              }}
            >
              <label className="block text-sm font-medium text-slate-700" htmlFor="shape-message">
                Your instruction
              </label>
              <textarea
                id="shape-message"
                className="min-h-24 w-full rounded border border-slate-300 p-2 text-sm"
                data-testid="shape-message-input"
                value={message}
                disabled={controlsDisabled || turnBusy}
                onChange={(event) => setMessage(event.target.value)}
                placeholder="For example: open at 4pm and close at 9pm weekdays."
              />
              <button
                type="submit"
                className="rounded bg-slate-800 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
                data-testid="shape-send"
                disabled={controlsDisabled || turnBusy || message.trim().length === 0}
              >
                {turnBusy ? 'Updating…' : 'Send'}
              </button>
            </form>
          )}
        </section>

        <section
          className="min-w-0 rounded-lg border border-slate-200 bg-white p-4"
          data-testid="shape-preview"
        >
          <div className="mb-3 flex items-start justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold text-slate-900">Calendar preview</h2>
              <p className="mt-1 text-sm text-slate-600">
                {hasDraft && frozenPreview === null && question === null
                  ? 'Previewing the private draft.'
                  : 'Previewing the live calendar.'}
              </p>
            </div>
            <span className="rounded bg-slate-100 px-2 py-1 text-xs text-slate-600">
              No bookings shown
            </span>
          </div>
          {liveCalendar?.kind === 'error' && liveCalendar.key === calendarKey ? (
            <p className="mb-3 text-sm text-red-700" role="alert">
              {liveCalendar.message}
            </p>
          ) : null}
          {hasDraft && draftCalendar?.kind === 'error' && draftCalendar.key === draftCalendarKey ? (
            <p className="mb-3 text-sm text-red-700" role="alert">
              {draftCalendar.message}
            </p>
          ) : null}
          <CalendarGrid
            publicId={publicId}
            showBookings={false}
            now={now}
            weekStart={weekStart}
            week={previewWeek}
            onWeekChange={(nextWeekStart) => setWeekState({ timeZone, weekStart: nextWeekStart })}
          />
        </section>
      </div>

      <section
        className="mt-6 flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-white p-4"
        data-testid="shape-actions"
      >
        <div className="mr-auto">
          <h2 className="text-sm font-semibold text-slate-900">Publish this calendar</h2>
          <p className="mt-1 text-sm text-slate-600">
            Publishing makes this what members can book.
          </p>
        </div>
        {question ? (
          <label className="text-sm text-slate-700">
            <input
              className="mr-2"
              type="checkbox"
              checked={allowUnbookable}
              disabled={controlsDisabled}
              onChange={(event) => setAllowUnbookable(event.target.checked)}
            />
            I intentionally want no bookable time
          </label>
        ) : null}
        {discardConfirming ? (
          <span
            className="flex items-center gap-2 text-sm text-slate-700"
            data-testid="shape-discard-confirm"
          >
            Discard this draft and conversation?
            <button
              type="button"
              className="rounded border border-red-300 px-2 py-1 text-red-700"
              data-testid="shape-discard-confirm-yes"
              disabled={discardBusy}
              onClick={() => void discard()}
            >
              {discardBusy ? 'Discarding…' : 'Discard'}
            </button>
            <button
              type="button"
              className="rounded border border-slate-300 px-2 py-1"
              data-testid="shape-discard-confirm-no"
              disabled={discardBusy}
              onClick={() => setDiscardConfirming(false)}
            >
              Keep it
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="rounded border border-red-300 px-3 py-2 text-sm font-medium text-red-700 disabled:opacity-50"
            data-testid="shape-discard-start"
            disabled={controlsDisabled || !hasDraft}
            onClick={() => setDiscardConfirming(true)}
          >
            Discard
          </button>
        )}
        <button
          type="button"
          className="rounded bg-indigo-700 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          data-testid="shape-publish"
          disabled={
            controlsDisabled ||
            !hasDraft ||
            draftMatchesLive ||
            publishBusy ||
            (question !== null && !allowUnbookable)
          }
          onClick={() => void publish()}
        >
          {publishBusy ? 'Publishing…' : 'Publish'}
        </button>
      </section>
    </Shell>
  )
}

function Shell({ space, children }: { space?: Space; children: ReactNode }) {
  return (
    <main className="min-h-screen bg-slate-50 p-8 text-slate-800">
      <div className="mx-auto max-w-7xl" data-testid="space-shape-page">
        <Link
          to={space ? `/s/${space.public_id}` : '/admin'}
          className="text-xs text-slate-500 hover:underline"
          data-testid="shape-back-link"
        >
          ← Back to {space ? space.name : 'admin'}
        </Link>
        <h1 className="mt-2 text-2xl font-semibold text-slate-900">
          Calendar shape{space ? ` — ${space.name}` : ''}
        </h1>
        {children}
      </div>
    </main>
  )
}
