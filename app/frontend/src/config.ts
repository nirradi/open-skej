/**
 * The single source of truth for calendar configuration.
 *
 * Everything about how the grid is laid out derives from `CalendarConfig`.
 * Changing `sessionMinutes` from 30 to 10 must require no other edit anywhere in
 * the codebase — if a component ever hardcodes a slot count, a row height per
 * half-hour, or an opening hour, that is a bug in the component, not
 * something to fix by adding a second config value here.
 *
 * ## Resolved per date by the server, not a compile-time constant
 *
 * `sessionMinutes` / `openMinutes` / `closeMinutes` used to mirror
 * `AVAILABILITY_OPEN` / `AVAILABILITY_CLOSE`, hardcoded constants in
 * `app/backend/app/rules_stub.py`. Those constants are gone, and so is the
 * later shape that replaced them — one `CalendarConfig` built from a Space's
 * own schedule fields — because a Space no longer has one session length or one
 * operating window good for a whole week: a rule instance's `applies_to` can
 * narrow it to particular weekdays or dates. `buildWeekSchedule` turns `GET
 * /spaces/{public_id}/schedule`'s **already resolved** per-date answer into a
 * `WeekSchedule`, and `calendarConfigForDay` narrows that to the single-date
 * `CalendarConfig` the functions here take. This module never decides which
 * rules govern a date — the server does, and a second implementation of that
 * in TypeScript is the invariant in `.claude/rules/rule-engine.md` broken
 * quietly. **The backend is still authoritative** for the booking itself too:
 * it re-evaluates every booking against that Space's rule canon and returns a
 * `rule_denied` response for anything outside it, regardless of what this file
 * computes. A grid drawn from a stale schedule renders bookable slots the
 * server will refuse — the denial copy will still be correct and friendly, but
 * the user was invited to click something that could never work.
 *
 * `calendarConfig`, the module-level default, survives only as the fallback
 * `CalendarGrid` uses when no `schedule` prop is given (tests, mostly — a real
 * page always fetches one). It models a Space whose hours are unset:
 * `opens_at` / `closes_at` null means the availability-hours rule is not
 * enforced (`.claude/rules/identity-and-access.md`), so the honest default is
 * the *whole day* bookable, not the old hardcoded 06:00–23:00 window —
 * inventing a window here would be a rule this file has no authority to make
 * up. Its `timeZone` is `SYSTEM_TIME_ZONE` — the closest thing to a Space when
 * there is no Space at all, and never presented as a Space's real zone once
 * one is loaded (see `timezone.ts`).
 *
 * ## The grid always renders the full day
 *
 * `slotsPerDayFor` spans the whole day regardless of `openMinutes` /
 * `closeMinutes` — those two only decide which rows come back from
 * `isSlotOutOfHours` as greyed. Clipping the day to `[openMinutes,
 * closeMinutes)` was the previous shape, and it silently broke: a Space
 * closing at 18:00 rendered a grid that simply stopped, and a booking made
 * before the hours were narrowed had no row left to sit on and vanished from
 * a calendar it was still on. Rendering the full day and greying the rest
 * means a booking is always somewhere on screen, whatever the Space's
 * current hours say.
 *
 * ## Every clock in the grid is the Space's own
 *
 * `openMinutes` / `closeMinutes` are minutes since midnight on the Space's
 * own wall clock, and `slotStart` resolves a slot's day-plus-minutes to a
 * real instant through the Space's own `timeZone` — never the viewer's. The
 * conversion happens through `timezone.ts`'s `zonedTimeToInstant`, which asks
 * `Intl.DateTimeFormat` for the zone's actual offset at that specific date
 * rather than assuming one, so the same 13:00 resolves to a different UTC
 * instant in July than in January, correctly. Every member looking at a Space
 * sees the identical grid, in the identical clock, regardless of where they
 * are; a viewer whose own zone differs from the Space's sees that only as a
 * secondary hint elsewhere in the UI, never as a second version of the grid.
 * A per-viewer clock was considered and rejected — it would let two members
 * read different times for the same slot, and it makes the operating window
 * wrap midnight for anyone far enough from the venue.
 *
 * ## The grid is anchored on the opening time, and one click is one session
 *
 * `sessionMinutes` and `anchorMinutes` together are the whole grid: sessions
 * run from the venue's own opening time, in steps of the session length, which
 * is what the server's `session_length` rule enforces on both bounds of every
 * booking. One click is therefore exactly one session — there is no separate
 * floor to widen it past, because a booking that starts and ends on this grid
 * cannot be shorter than one session in the first place.
 *
 * `anchorMinutes` is the date's own resolved opening time, and the grid it
 * defines extends in **both** directions from it so the full day still renders:
 * the first row of the day sits at `anchorMinutes % sessionMinutes`
 * (`gridOffsetMinutes`), which is `0` whenever the opening time already falls
 * on a whole number of sessions from midnight — the ordinary case, where this
 * is exactly the midnight-based grid it always was. A venue opening at 09:15
 * with hour-long sessions is the case this exists for: its rows land at 00:15,
 * 01:15, … 09:15, so the first session of the day starts when the venue opens
 * rather than being reported as a misconfiguration.
 */

import { SYSTEM_TIME_ZONE, zonedTimeToInstant } from './timezone'

/** Minutes in an hour — named so the arithmetic below reads as intent, not magic. */
const MINUTES_PER_HOUR = 60

/** Minutes in a day. The grid always spans it. */
const MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR

export interface CalendarConfig {
  /**
   * The length of one session, in minutes — the grid's step, and exactly how
   * much one click books. Bookings may still be longer, by dragging across
   * several sessions; they may never be shorter, and they may never begin or
   * end between two grid lines.
   *
   * Must divide a day evenly. A date whose resolved rules cannot be laid out
   * coherently is reported by the server as that date's own
   * `DaySchedule.coherenceIssue`.
   */
  sessionMinutes: number
  /**
   * Minutes since midnight on the Space's own wall clock that it opens —
   * inclusive. `null` means the Space enforces no opening bound: nothing
   * before midnight is greyed for it.
   */
  openMinutes: number | null
  /**
   * Minutes since midnight on the Space's own wall clock that it closes. A
   * booking may end exactly here. `null` means the Space enforces no closing
   * bound.
   */
  closeMinutes: number | null
  /**
   * The Space's own IANA zone (`Europe/Berlin`, never a fixed offset) — what
   * `openMinutes` / `closeMinutes` are wall-clock times *in*, and what
   * `slotStart` resolves every rendered slot's real instant through. See the
   * module docblock's "every clock in the grid is the Space's own".
   */
  timeZone: string
  /**
   * Minutes since midnight that this date's session grid is anchored on — the
   * date's own resolved opening time, or `0` when no `availability_hours` row
   * governs it. Resolved by the server and carried through verbatim; this
   * module never derives it from `openMinutes`, because which rows govern a
   * date is the server's question alone (see the module docblock).
   */
  anchorMinutes: number
}

export const calendarConfig: CalendarConfig = {
  sessionMinutes: 30,
  openMinutes: null,
  closeMinutes: null,
  timeZone: SYSTEM_TIME_ZONE,
  anchorMinutes: 0,
}

/**
 * Minutes from midnight to the grid's first line of the day.
 *
 * The grid is anchored on `anchorMinutes` and steps by `sessionMinutes`, so the
 * earliest line at or after midnight is the anchor reduced modulo the session
 * length. This is `0` for every opening time that already falls on a whole
 * number of sessions from midnight, which is what makes the anchored grid a
 * strict generalisation of the midnight-based one rather than a change to it.
 */
export function gridOffsetMinutes(config: CalendarConfig = calendarConfig): number {
  return config.anchorMinutes % config.sessionMinutes
}

/**
 * How many whole sessions the grid renders for a day, for an arbitrary config.
 *
 * Counted from `gridOffsetMinutes` rather than from midnight: with a non-zero
 * offset the day holds one fewer whole session, and the minutes below the first
 * line are left unrendered rather than drawn as a short row nobody could book.
 * A booking sitting in them is still displayed — every booking block is
 * positioned from its own start minute, never from a row index.
 */
export function slotsPerDayFor(config: CalendarConfig = calendarConfig): number {
  return Math.floor((MINUTES_PER_DAY - gridOffsetMinutes(config)) / config.sessionMinutes)
}

/** How many session rows the grid renders per day. */
export const slotsPerDay = slotsPerDayFor()

/**
 * Minutes from midnight to the start of session `index` (0-based).
 *
 * The grid's row-to-time mapping lives here rather than in the component so that
 * a slot's identity is derived the same way everywhere it is computed. Session 0
 * is the first grid line at or after midnight — the full day is always
 * rendered, whatever a Space's own hours say (see the module docblock).
 */
export function slotStartMinutes(index: number, config: CalendarConfig = calendarConfig): number {
  return gridOffsetMinutes(config) + index * config.sessionMinutes
}

/**
 * The real instant at which session `index` starts on the given day, resolved
 * through the Space's own `config.timeZone`.
 *
 * `day` contributes only its calendar date; its time component is discarded.
 * The date-plus-minutes pair is resolved to an instant by `zonedTimeToInstant`
 * (`timezone.ts`) rather than by adding milliseconds to a UTC guess, so a slot
 * lands on the intended wall-clock time across a DST boundary — the same
 * per-date resolution `rules_stub.py`'s `_local_midnight_utc` uses on the backend, and the
 * reason a Space's 13:00 in July and its 13:00 in January can be (and, near a
 * DST transition, are) different real instants for the identical wall-clock
 * label.
 */
export function slotStart(day: Date, index: number, config: CalendarConfig = calendarConfig): Date {
  const minutes = slotStartMinutes(index, config)
  return zonedTimeToInstant(
    day.getFullYear(),
    day.getMonth(),
    day.getDate(),
    Math.floor(minutes / MINUTES_PER_HOUR),
    minutes % MINUTES_PER_HOUR,
    config.timeZone,
  )
}

/**
 * Midnight on `day`, on the Space's own clock.
 *
 * Resolved the same way `slotStart` resolves a session's own instant, through
 * `zonedTimeToInstant` and the Space's `timeZone`, so a day's bounds and its
 * sessions can never disagree about which instant midnight was. It takes its
 * own route rather than going through `slotStart(day, 0, config)`, which was
 * correct only while every grid started at midnight: an anchored grid's first
 * line sits at `gridOffsetMinutes`, and a day column still spans the real day.
 */
export function dayStartInstant(day: Date, config: CalendarConfig = calendarConfig): Date {
  return zonedTimeToInstant(day.getFullYear(), day.getMonth(), day.getDate(), 0, 0, config.timeZone)
}

/** Formats a session index as `HH:MM` for axis labels. */
export function formatSlotLabel(index: number, config: CalendarConfig = calendarConfig): string {
  const minutes = slotStartMinutes(index, config)
  const hh = String(Math.floor(minutes / MINUTES_PER_HOUR)).padStart(2, '0')
  const mm = String(minutes % MINUTES_PER_HOUR).padStart(2, '0')
  return `${hh}:${mm}`
}

/**
 * Whether the session starting at `index` falls outside the Space's operating
 * hours.
 *
 * One session is one click, so the check is over exactly `[start, start +
 * sessionMinutes)`: a session whose own row is inside the window but whose end
 * runs past closing contains a minute the backend's `AvailabilityHoursRule`
 * would deny, so it reads as blocked rather than bookable — the same "never
 * offer what the server will refuse" reasoning as everywhere else in the grid.
 * With both bounds `null` (a Space with no hours restriction), nothing is ever
 * out-of-hours.
 */
export function isSlotOutOfHours(index: number, config: CalendarConfig = calendarConfig): boolean {
  if (config.openMinutes === null && config.closeMinutes === null) return false
  const start = slotStartMinutes(index, config)
  const end = start + config.sessionMinutes
  const open = config.openMinutes ?? 0
  const close = config.closeMinutes ?? MINUTES_PER_DAY
  return start < open || end > close
}

/** `HH:MM:SS` (Python's `time`, the wire shape the schedule endpoint's bounds use) → minutes since midnight. */
function parseClockMinutes(value: string): number {
  const [hh, mm] = value.split(':').map(Number)
  return hh * MINUTES_PER_HOUR + mm
}

// --- The resolved per-day schedule ------------------------------------------
//
// A single `CalendarConfig` for a whole Space cannot express "Tuesdays are
// different" now that a rule's `applies_to` can narrow it to particular
// weekdays or dates. What the grid lays itself out from is instead a per-date
// resolution read from `GET /spaces/{public_id}/schedule`
// (`app.rules_stub.resolve_day_schedule`, the backend's own flat-AND
// resolution over the Space's `space_rules` rows), never re-derived here.

/**
 * One date's resolved session length, grid anchor and operating window, in
 * minutes since midnight — the per-day counterpart to `CalendarConfig`'s single
 * global values, built from one entry of `GET /spaces/{public_id}/schedule`'s
 * response (`DayScheduleRead` in `api/types.ts`).
 *
 * `openMinutes` / `closeMinutes` `null` means the Space enforces no bound on
 * this date, exactly like `CalendarConfig`'s fields. `sessionMinutes` falls
 * back to the shipped default (`calendarConfig.sessionMinutes`) when the Space
 * configures no session rule for the date at all, and `anchorMinutes` falls
 * back to `0` under the same condition — a grid with nothing to anchor it runs
 * from midnight, which is what it always did.
 *
 * `coherenceIssue` is carried straight through from the server's own
 * `DayScheduleRead.coherence_issue` and is never computed here: whether a
 * date's resolved rules can be laid out at all is a question about which rules
 * govern that date, and this module must never re-derive rule semantics
 * (`.claude/rules/rule-engine.md`).
 */
export interface DaySchedule {
  sessionMinutes: number
  openMinutes: number | null
  closeMinutes: number | null
  coherenceIssue: string | null
  anchorMinutes: number
}

/**
 * A week's resolved layout: what `slotStart` etc. get called with. Reduces
 * to one `DaySchedule` per date, sharing one `timeZone` — `timezone` is the
 * one genuinely per-Space column left (`.claude/rules/identity-and-access.md`),
 * so it is never per-day the way hours and session length now are.
 *
 * `forDate` rather than a plain `Record` keyed by date: a `CalendarGrid`
 * asking about a date this `WeekSchedule` was never built for (nothing
 * fetched yet, or a date outside the requested range) needs an honest answer
 * rather than a lookup a caller has to null-check everywhere, and the
 * fallback — the shipped default, matching `uniformWeekSchedule` below and
 * `calendarConfig`'s own module default — is exactly what a `CalendarConfig`
 * built with `?? calendarConfig` already resolved to before this task.
 */
export interface WeekSchedule {
  timeZone: string
  forDate: (dateKey: string) => DaySchedule
}

/** The shipped default, in `DaySchedule` shape — no hours restriction, the default session length. */
const DEFAULT_DAY_SCHEDULE: DaySchedule = {
  sessionMinutes: calendarConfig.sessionMinutes,
  openMinutes: calendarConfig.openMinutes,
  closeMinutes: calendarConfig.closeMinutes,
  coherenceIssue: null,
  anchorMinutes: calendarConfig.anchorMinutes,
}

/** One `DayScheduleRead` (the wire shape) parsed into a `DaySchedule`. */
function parseDaySchedule(entry: {
  session_minutes: number | null
  opens_at: string | null
  closes_at: string | null
  coherence_issue: string | null
  anchor_minutes: number | null
}): DaySchedule {
  return {
    sessionMinutes: entry.session_minutes ?? calendarConfig.sessionMinutes,
    openMinutes: entry.opens_at === null ? null : parseClockMinutes(entry.opens_at),
    closeMinutes: entry.closes_at === null ? null : parseClockMinutes(entry.closes_at),
    coherenceIssue: entry.coherence_issue,
    anchorMinutes: entry.anchor_minutes ?? calendarConfig.anchorMinutes,
  }
}

/**
 * Builds a `WeekSchedule` from `GET /spaces/{public_id}/schedule`'s response.
 *
 * `entries[i].date` (`YYYY-MM-DD`) is used verbatim as the lookup key — the
 * identical shape `toDateKey` (`calendar/week.ts`) produces for every other
 * per-day computation, so `CalendarGrid` addresses a day's resolved schedule
 * with the same key it already uses for everything else about that day.
 */
export function buildWeekSchedule(
  entries: readonly {
    date: string
    session_minutes: number | null
    opens_at: string | null
    closes_at: string | null
    coherence_issue: string | null
    anchor_minutes: number | null
  }[],
  timeZone: string,
): WeekSchedule {
  const byDate = new Map<string, DaySchedule>()
  for (const entry of entries) {
    byDate.set(entry.date, parseDaySchedule(entry))
  }
  return { timeZone, forDate: (dateKey) => byDate.get(dateKey) ?? DEFAULT_DAY_SCHEDULE }
}

/**
 * A `WeekSchedule` that resolves every date to the identical `DaySchedule` —
 * the pre-6.9 single-`CalendarConfig`-for-the-whole-week shape, expressed in
 * the new per-day interface. Two callers: `CalendarGrid` itself, as its
 * fallback when no real `WeekSchedule` prop is supplied (matching the old
 * `config ?? calendarConfig` default), and this module's own test suite,
 * whose fixtures still think in one `CalendarConfig` for a whole week.
 */
export function uniformWeekSchedule(config: CalendarConfig): WeekSchedule {
  const day: DaySchedule = {
    sessionMinutes: config.sessionMinutes,
    openMinutes: config.openMinutes,
    closeMinutes: config.closeMinutes,
    coherenceIssue: null,
    anchorMinutes: config.anchorMinutes,
  }
  return { timeZone: config.timeZone, forDate: () => day }
}

/** `schedule`'s resolved `CalendarConfig` for one date — what feeds `slotStart` / `dayBounds` / `isSlotOutOfHours` / `slotInterval`, each still single-config functions that `CalendarGrid` now calls once per day rather than once per week. */
export function calendarConfigForDay(schedule: WeekSchedule, dateKey: string): CalendarConfig {
  const day = schedule.forDate(dateKey)
  return {
    sessionMinutes: day.sessionMinutes,
    openMinutes: day.openMinutes,
    closeMinutes: day.closeMinutes,
    timeZone: schedule.timeZone,
    anchorMinutes: day.anchorMinutes,
  }
}

/**
 * The smallest `sessionMinutes` across `dateKeys`' own resolved schedule — the
 * shared row-axis granularity a heterogeneous week's grid renders at (see
 * `CalendarGrid`'s module docblock). Every day's own grid lines land on a
 * *subset* of the axis rows only when every configured `sessionMinutes` in the
 * week is a multiple of this value **and** every day's anchor agrees with the
 * axis's own; when they do not, the axis is still the finest of them, and the
 * mismatch is a readability finding rather than a case this function papers
 * over. Reconciling a shared axis with per-day anchors is deferred with the
 * rest of the week-axis work (`ops/pending/bugs/grid-from-hours-and-min-duration.md`,
 * decision 7).
 */
export function finestSessionMinutes(schedule: WeekSchedule, dateKeys: readonly string[]): number {
  let finest = Infinity
  for (const key of dateKeys) {
    finest = Math.min(finest, schedule.forDate(key).sessionMinutes)
  }
  return Number.isFinite(finest) ? finest : calendarConfig.sessionMinutes
}
