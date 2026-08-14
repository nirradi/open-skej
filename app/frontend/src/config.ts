/**
 * The single source of truth for calendar configuration.
 *
 * Everything about how the grid is laid out derives from `CalendarConfig`.
 * Changing `slotMinutes` from 30 to 10 must require no other edit anywhere in
 * the codebase — if a component ever hardcodes a slot count, a row height per
 * half-hour, or an opening hour, that is a bug in the component, not
 * something to fix by adding a second config value here.
 *
 * ## Resolved per date by the server, not a compile-time constant
 *
 * `slotMinutes` / `openMinutes` / `closeMinutes` used to mirror
 * `AVAILABILITY_OPEN` / `AVAILABILITY_CLOSE`, hardcoded constants in
 * `app/backend/app/rules_stub.py`. Those constants are gone, and so is the
 * later shape that replaced them — one `CalendarConfig` built from a Space's
 * own schedule fields — because a Space no longer has one slot size or one
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
 * `slotsPerDayFor` spans midnight to midnight regardless of `openMinutes` /
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
 * ## The click unit honours the minimum duration
 *
 * `DaySchedule.minDurationMinutes` — carried through `CalendarConfig` by
 * `calendarConfigForDay` — is a date's own resolved `min_duration` floor, and
 * it is independent of `slotMinutes`: nothing forces a Space's minimum
 * duration to be a multiple of its slot size. The click unit is the smallest
 * whole number of slots that reaches the date's minimum duration
 * (`slotInterval` in `calendar/week.ts`), so the grid cannot offer a booking
 * the floor would refuse.
 */

import { SYSTEM_TIME_ZONE, zonedTimeToInstant } from './timezone'

/** Minutes in an hour — named so the arithmetic below reads as intent, not magic. */
const MINUTES_PER_HOUR = 60

/** Minutes in a day. The grid always spans exactly this many. */
const MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR

export interface CalendarConfig {
  /**
   * Selection granularity of the grid, in minutes. Governs how finely a user can
   * pick a range — *not* how long a booking may be. Bookings are variable length;
   * the backend's max-duration rule is what bounds them.
   *
   * Must divide a day evenly, and must divide `openMinutes` / `closeMinutes`
   * when they are set — a date whose resolved rules do not satisfy that is
   * reported by the server as that date's own `DaySchedule.coherenceIssue`.
   */
  slotMinutes: number
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
   * The date's own resolved `min_duration` floor, in minutes — `null` means
   * no such rule governs this date. Read only by `slotInterval`
   * (`calendar/week.ts`) to decide how many slots one click consumes; never
   * how the grid itself is drawn (see the module docblock's "the click unit
   * honours the minimum duration").
   */
  minDurationMinutes: number | null
}

export const calendarConfig: CalendarConfig = {
  slotMinutes: 30,
  openMinutes: null,
  closeMinutes: null,
  timeZone: SYSTEM_TIME_ZONE,
  minDurationMinutes: null,
}

/** How many slot rows the grid renders per day, for an arbitrary config. */
export function slotsPerDayFor(config: CalendarConfig = calendarConfig): number {
  return Math.floor(MINUTES_PER_DAY / config.slotMinutes)
}

/** How many slot rows the grid renders per day. */
export const slotsPerDay = slotsPerDayFor()

/**
 * Minutes from midnight to the start of slot `index` (0-based).
 *
 * The grid's row-to-time mapping lives here rather than in the component so that
 * a slot's identity is derived the same way everywhere it is computed. Slot 0
 * is always midnight — the full day is always rendered, whatever a Space's
 * own hours say (see the module docblock).
 */
export function slotStartMinutes(index: number, config: CalendarConfig = calendarConfig): number {
  return index * config.slotMinutes
}

/**
 * The real instant at which slot `index` starts on the given day, resolved
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

/** Formats a slot index as `HH:MM` for axis labels. */
export function formatSlotLabel(index: number, config: CalendarConfig = calendarConfig): string {
  const minutes = slotStartMinutes(index, config)
  const hh = String(Math.floor(minutes / MINUTES_PER_HOUR)).padStart(2, '0')
  const mm = String(minutes % MINUTES_PER_HOUR).padStart(2, '0')
  return `${hh}:${mm}`
}

/**
 * How many slots one click starting at `config`'s slot size consumes: the
 * smallest whole number of slots whose combined length reaches
 * `config.minDurationMinutes`, or exactly one when no minimum governs the
 * date (see the module docblock's "the click unit honours the minimum
 * duration"). A pure function of `minDurationMinutes` and `slotMinutes` —
 * exported so `isSlotOutOfHours` here and `slotInterval`
 * (`calendar/week.ts`) share one definition of what one click means, rather
 * than each rounding it up on its own and risking the two drifting apart.
 */
export function slotsPerClick(config: CalendarConfig = calendarConfig): number {
  return config.minDurationMinutes === null
    ? 1
    : Math.ceil(config.minDurationMinutes / config.slotMinutes)
}

/**
 * Whether a click starting at slot `index` falls outside the Space's
 * operating hours.
 *
 * A click counts as out-of-hours unless the **whole click unit** —
 * `slotsPerClick(config)` slots, not just the one row `index` names — sits
 * entirely within `[openMinutes, closeMinutes)`. This is the same "never
 * offer what the server will refuse" reasoning as everywhere else in the
 * grid, applied to the minimum duration's own widening: a click whose row is
 * itself inside the window but whose resolved end runs past closing
 * contains a minute the backend's `AvailabilityHoursRule` would deny just as
 * surely as a single slot straddling the boundary does, so it reads as
 * blocked rather than bookable. With no minimum configured,
 * `slotsPerClick` is 1 and this is exactly the single-slot check it always
 * was. With both bounds `null` (a Space with no hours restriction), nothing
 * is ever out-of-hours.
 */
export function isSlotOutOfHours(index: number, config: CalendarConfig = calendarConfig): boolean {
  if (config.openMinutes === null && config.closeMinutes === null) return false
  const start = slotStartMinutes(index, config)
  const end = start + slotsPerClick(config) * config.slotMinutes
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
 * One date's resolved slot size and operating window, in minutes since
 * midnight — the per-day counterpart to `CalendarConfig`'s single global
 * values, built from one entry of `GET /spaces/{public_id}/schedule`'s
 * response (`DayScheduleRead` in `api/types.ts`).
 *
 * `openMinutes` / `closeMinutes` `null` means the Space enforces no bound on
 * this date, exactly like `CalendarConfig`'s fields. `slotMinutes` falls
 * back to the shipped default (`calendarConfig.slotMinutes`) when the Space
 * configures no slot rule for the date at all.
 *
 * `coherenceIssue` is carried straight through from the server's own
 * `DayScheduleRead.coherence_issue` and is never computed here: whether a
 * date's resolved hours land on its resolved slot grid is a question about
 * which rules govern that date, and this module must never re-derive rule
 * semantics (`.claude/rules/rule-engine.md`).
 *
 * `minDurationMinutes` is this date's own resolved `min_duration` floor,
 * `null` meaning no such rule governs it — the per-day counterpart to
 * `CalendarConfig.minDurationMinutes`, and read the same way, only by
 * `slotInterval`.
 */
export interface DaySchedule {
  slotMinutes: number
  openMinutes: number | null
  closeMinutes: number | null
  coherenceIssue: string | null
  minDurationMinutes: number | null
}

/**
 * A week's resolved layout: what `slotStart` etc. get called with. Reduces
 * to one `DaySchedule` per date, sharing one `timeZone` — `timezone` is the
 * one genuinely per-Space column left (`.claude/rules/identity-and-access.md`),
 * so it is never per-day the way hours and slot size now are.
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

/** The shipped default, in `DaySchedule` shape — no hours restriction, the default slot size. */
const DEFAULT_DAY_SCHEDULE: DaySchedule = {
  slotMinutes: calendarConfig.slotMinutes,
  openMinutes: calendarConfig.openMinutes,
  closeMinutes: calendarConfig.closeMinutes,
  coherenceIssue: null,
  minDurationMinutes: calendarConfig.minDurationMinutes,
}

/** One `DayScheduleRead` (the wire shape) parsed into a `DaySchedule`. */
function parseDaySchedule(entry: {
  slot_minutes: number | null
  opens_at: string | null
  closes_at: string | null
  coherence_issue: string | null
  min_duration_minutes: number | null
}): DaySchedule {
  return {
    slotMinutes: entry.slot_minutes ?? calendarConfig.slotMinutes,
    openMinutes: entry.opens_at === null ? null : parseClockMinutes(entry.opens_at),
    closeMinutes: entry.closes_at === null ? null : parseClockMinutes(entry.closes_at),
    coherenceIssue: entry.coherence_issue,
    minDurationMinutes: entry.min_duration_minutes,
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
    slot_minutes: number | null
    opens_at: string | null
    closes_at: string | null
    coherence_issue: string | null
    min_duration_minutes: number | null
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
 *
 * `minDurationMinutes` is always `null` here rather than carried through from
 * `config`: every caller of this function is a uniform, no-rules fallback
 * (the shipped default, or a test fixture built before this field existed),
 * never a real per-date resolution — a real minimum duration only ever
 * reaches a `DaySchedule` through `buildWeekSchedule`'s wire parsing.
 */
export function uniformWeekSchedule(config: CalendarConfig): WeekSchedule {
  const day: DaySchedule = {
    slotMinutes: config.slotMinutes,
    openMinutes: config.openMinutes,
    closeMinutes: config.closeMinutes,
    coherenceIssue: null,
    minDurationMinutes: null,
  }
  return { timeZone: config.timeZone, forDate: () => day }
}

/** `schedule`'s resolved `CalendarConfig` for one date — what feeds `slotStart` / `dayBounds` / `isSlotOutOfHours` / `slotInterval`, each still single-config functions that `CalendarGrid` now calls once per day rather than once per week. */
export function calendarConfigForDay(schedule: WeekSchedule, dateKey: string): CalendarConfig {
  const day = schedule.forDate(dateKey)
  return {
    slotMinutes: day.slotMinutes,
    openMinutes: day.openMinutes,
    closeMinutes: day.closeMinutes,
    timeZone: schedule.timeZone,
    minDurationMinutes: day.minDurationMinutes,
  }
}

/**
 * The smallest `slotMinutes` across `dateKeys`' own resolved schedule — the
 * shared row-axis granularity a heterogeneous week's grid renders at (see
 * `CalendarGrid`'s module docblock). Every day's own grid lines land on a
 * *subset* of the axis rows only when every configured `slotMinutes` in the
 * week is a multiple of this value; when it is not (a 20-minute day beside a
 * 30-minute one), the axis is still the finest of the two, per the plan, and
 * the mismatch is a readability finding recorded in the PR rather than a
 * case this function papers over.
 */
export function finestSlotMinutes(schedule: WeekSchedule, dateKeys: readonly string[]): number {
  let finest = Infinity
  for (const key of dateKeys) {
    finest = Math.min(finest, schedule.forDate(key).slotMinutes)
  }
  return Number.isFinite(finest) ? finest : calendarConfig.slotMinutes
}
