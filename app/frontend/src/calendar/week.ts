/**
 * Week arithmetic, the booking horizon, and slot identity.
 *
 * Kept separate from the component so the rules that decide what a user may
 * click are plain functions over `Date`s, testable without a DOM. The component
 * asks these questions; it does not answer them itself.
 *
 * Everything about slot *layout* still comes from `config.ts` — nothing here
 * knows how many slots a day has or when the day opens.
 *
 * ## Keep in sync with the backend
 *
 * `BOOKING_HORIZON_DAYS` mirrors the constant of the same name in
 * `app/backend/app/rules_stub.py`, and the past/horizon predicates below mirror
 * its two horizon rules. **The backend is authoritative** — it re-evaluates
 * every booking and returns `rule_denied` regardless of what the grid allowed.
 * The point of duplicating the bound here is the converse: the grid must never
 * *offer* something the server will refuse.
 *
 * ## Calendar dates are zone-agnostic; instants are not
 *
 * A `day` passed around this module — `weekStart`, an entry of `daysOfWeek`,
 * the argument to `toDateKey` — is a **calendar date carrier**: a `Date` built
 * from and read back through the local `Date` constructor and local getters
 * purely to hold a `(year, month, day)` triple. Two carriers are "the same
 * day" or "one day apart" by that triple alone, and every function that only
 * moves or compares carriers (`startOfDay`, `addDays`, `startOfWeek`,
 * `daysOfWeek`, `toDateKey`, `dateFromKey`) needs no zone at all, because a
 * calendar date is not tied to one — arithmetic over the triple gives the
 * same answer no matter which zone the environment happens to be running in.
 *
 * A carrier stops being zone-agnostic the moment it needs to become a real
 * instant — a slot's start, a day's opening and closing bound — and that
 * conversion happens through the Space's own `timeZone`, via `config.ts`'s
 * `slotStart` and this module's `dayBounds`, both of which go through
 * `timezone.ts`'s `zonedTimeToInstant`. The other place a zone enters is the
 * reverse direction: turning `now` — a real instant — into "today"'s carrier,
 * which `canGoToPreviousWeek`, `canGoToNextWeek` and `parseWeekStartParam` do
 * through `timezone.ts`'s `zonedCalendarDate` and an explicit `timeZone`
 * parameter, so that "this week" means the Space's today, not the viewer's.
 */

import { calendarConfig, slotStart, type CalendarConfig } from '../config'
import { zonedCalendarDate, zonedParts } from '../timezone'

/** Days in a rendered week. Not configurable — a week view shows a week. */
export const DAYS_PER_WEEK = 7

/**
 * The weekday a rendered week starts on, 0 = Sunday through 6 = Saturday.
 *
 * Deliberately *not* in `config.ts`: that module documents itself as mirroring
 * the backend's availability constants, and week start has no backend
 * counterpart — it is presentation only. Stream 3's `CalendarContext` will own
 * it once rules need to reason about "this week".
 */
export const WEEK_STARTS_ON = 1

/**
 * How many days ahead a booking may start.
 *
 * Mirrors `BOOKING_HORIZON_DAYS` in `app/backend/app/rules_stub.py`.
 */
export const BOOKING_HORIZON_DAYS = 60

/** Midnight local time on the calendar date of `date`. */
export function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate())
}

/**
 * `date` shifted by `days` calendar days, at midnight local time.
 *
 * Built through the `Date` constructor rather than by adding milliseconds so a
 * DST transition inside the range does not drift the result into the previous
 * or next day.
 */
export function addDays(date: Date, days: number): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days)
}

/** Midnight local time on the `WEEK_STARTS_ON` day at or before `date`. */
export function startOfWeek(date: Date, weekStartsOn: number = WEEK_STARTS_ON): Date {
  const offset = (date.getDay() - weekStartsOn + DAYS_PER_WEEK) % DAYS_PER_WEEK
  return addDays(date, -offset)
}

/** The seven days rendered for the week beginning at `weekStart`. */
export function daysOfWeek(weekStart: Date): Date[] {
  return Array.from({ length: DAYS_PER_WEEK }, (_, i) => addDays(weekStart, i))
}

/**
 * The last instant a booking may start: `BOOKING_HORIZON_DAYS` after `now`.
 *
 * The bound is inclusive, matching the backend, which denies only what starts
 * *strictly* beyond it.
 */
export function horizonEnd(now: Date): Date {
  return new Date(now.getTime() + BOOKING_HORIZON_DAYS * 24 * 60 * 60 * 1000)
}

/**
 * Whether the week before `weekStart` may be navigated to.
 *
 * False on the current week: earlier weeks contain nothing bookable, and per
 * the plan, viewing past bookings is out of scope this phase. "Current" means
 * the week containing `now`'s calendar date **in `timeZone`** — the Space's
 * own zone, per the module docblock, so a viewer whose local date has already
 * turned over (or has not yet) still sees the same current week the Space
 * itself would name.
 */
export function canGoToPreviousWeek(weekStart: Date, now: Date, timeZone: string): boolean {
  return weekStart.getTime() > startOfWeek(zonedCalendarDate(now, timeZone)).getTime()
}

/**
 * Whether the week after `weekStart` may be navigated to.
 *
 * True only while that next week still contains at least one bookable day, so
 * paging stops on the week holding the horizon rather than one past it. The
 * horizon instant itself (`horizonEnd`) is a fixed duration after `now` and
 * needs no zone; which calendar day it falls on — the boundary actually
 * compared against — does, for the same reason `canGoToPreviousWeek` reads
 * "today" in `timeZone` rather than the environment's own zone.
 */
export function canGoToNextWeek(weekStart: Date, now: Date, timeZone: string): boolean {
  return (
    addDays(weekStart, DAYS_PER_WEEK).getTime() <=
    zonedCalendarDate(horizonEnd(now), timeZone).getTime()
  )
}

/** Why a slot cannot be selected, or `null` when it can. */
export type SlotBlockedReason = 'past' | 'beyond-horizon' | 'booked' | 'unavailable' | 'out-of-hours'

/**
 * Whether a slot has already started.
 *
 * A slot in progress counts as past — the backend denies any booking whose
 * `start_at` precedes `now`, so offering it would invite a denial.
 */
export function isSlotInPast(slotStartsAt: Date, now: Date): boolean {
  return slotStartsAt.getTime() < now.getTime()
}

/** Whether a slot starts beyond the booking horizon. */
export function isSlotBeyondHorizon(slotStartsAt: Date, now: Date): boolean {
  return slotStartsAt.getTime() > horizonEnd(now).getTime()
}

/** `YYYY-MM-DD` in local time — the date half of a slot's stable identity. */
export function toDateKey(day: Date): string {
  const yyyy = String(day.getFullYear()).padStart(4, '0')
  const mm = String(day.getMonth() + 1).padStart(2, '0')
  const dd = String(day.getDate()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd}`
}

/**
 * The `data-testid` of one slot cell.
 *
 * Deterministic and derivable from a date plus a slot index, so task 1.9's
 * Playwright suite can address "the 09:00 slot next Tuesday" by computing the
 * id rather than by scraping the DOM for a label. Exported so the E2E suite and
 * the component cannot drift apart.
 */
export function slotTestId(day: Date, index: number): string {
  return `slot-${toDateKey(day)}-${index}`
}

/** The `data-testid` of a rendered booking block. */
export function bookingTestId(bookingId: number): string {
  return `booking-${bookingId}`
}

/**
 * The inverse of `toDateKey`: a local-time `Date` at midnight on the day the
 * key names.
 *
 * Kept beside `toDateKey` so the two representations of a date cannot drift
 * apart — `parseWeekStartParam` below and `app/e2e/tests/fixtures.ts` both
 * need exactly this shape.
 */
export function dateFromKey(key: string): Date {
  const [year, month, day] = key.split('-').map(Number)
  return new Date(year, month - 1, day)
}

/**
 * Reads the `?week=` query value into the week-start it names, or `null` if
 * it should be treated as absent.
 *
 * `null` covers everything a hand-typed or stale URL can do wrong: not a date
 * at all, a date `Date` silently rolls over (`2026-02-30`), a real date
 * earlier than the current week (navigation is forward-only —
 * `canGoToPreviousWeek`), or one beyond the booking horizon. A real date that
 * is not itself a week start is *not* one of these — it is normalised to the
 * week containing it, the friendly reading of a hand-edited link. The caller
 * falls back to the current week on `null`; this never throws and never
 * signals which of the above happened, because a mistyped URL is not an
 * error a visitor can act on.
 *
 * `timeZone` is the Space's own zone, used only to read `now`'s calendar date
 * for the `earliest` / `latest` bounds below — the same "today means the
 * Space's today" rule `canGoToPreviousWeek` / `canGoToNextWeek` follow. A
 * literal `value` from the URL names a calendar date directly and needs no
 * zone to parse.
 */
export function parseWeekStartParam(value: string | null, now: Date, timeZone: string): Date | null {
  if (value === null || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null

  const parsed = dateFromKey(value)
  const [year, month, day] = value.split('-').map(Number)
  if (parsed.getFullYear() !== year || parsed.getMonth() !== month - 1 || parsed.getDate() !== day) {
    return null
  }

  const weekStart = startOfWeek(parsed)
  const earliest = startOfWeek(zonedCalendarDate(now, timeZone))
  const latest = startOfWeek(zonedCalendarDate(horizonEnd(now), timeZone))
  if (weekStart.getTime() < earliest.getTime() || weekStart.getTime() > latest.getTime()) {
    return null
  }

  return weekStart
}

/**
 * A wall-clock time as `HH:MM`, in an explicit `timeZone`.
 *
 * Forced to 24-hour rather than left to the locale, so that every time in the
 * UI reads the same way. `formatSlotLabel` renders the time axis as `HH:MM`
 * unconditionally; a locale-dependent formatter alongside it produced a grid
 * whose axis said `12:00` while the booking sitting on that row said
 * `12:00 PM`, which looks like two different times at a glance.
 *
 * Every caller in the grid itself passes the Space's own `timeZone`, per the
 * module docblock — a booking block reads the same clock the slot it sits on
 * does. `timeZone` is a required parameter rather than defaulted so a new
 * call site cannot silently fall back to the environment's own zone the way
 * this function used to.
 */
export function formatClockTime(value: Date, timeZone: string): string {
  const { hour, minute } = zonedParts(value, timeZone)
  const hh = String(hour).padStart(2, '0')
  const mm = String(minute).padStart(2, '0')
  return `${hh}:${mm}`
}

/**
 * The half-open interval `[start, end)` a click starting at slot `index`
 * would submit.
 *
 * Spans exactly one slot when `config.minDurationMinutes` is `null` (no
 * floor governs the date) or already fits in one — otherwise `end` extends
 * across `slotsPerClick` slots, the smallest whole number that reaches the
 * minimum. Whole slots, not the raw minute count: a `slot_alignment` row may
 * also govern the date, and the booking's end has to land on its grid too,
 * so rounding up to the nearest slot boundary is what keeps a click from
 * trading one denial for another (`config.ts`'s "the click unit honours the
 * minimum duration").
 */
export function slotInterval(
  day: Date,
  index: number,
  config: CalendarConfig = calendarConfig,
): { start: Date; end: Date } {
  const start = slotStart(day, index, config)
  const slotsPerClick =
    config.minDurationMinutes === null ? 1 : Math.ceil(config.minDurationMinutes / config.slotMinutes)
  return { start, end: new Date(start.getTime() + slotsPerClick * config.slotMinutes * 60 * 1000) }
}

/**
 * The real instants at which the calendar date `day` begins and ends, on the
 * Space's own clock — midnight to midnight in `config.timeZone`.
 *
 * This is what a day *column* actually spans as an interval, and it is what
 * booking-grouping and pixel-positioning in `CalendarGrid` compare against —
 * both need the Space's midnight, not the environment's. Built from
 * `slotStart` at index 0 (always midnight, see `config.ts`) rather than a
 * fresh conversion, so a day's bounds and its first slot can never disagree
 * about what midnight resolved to.
 */
export function dayBounds(day: Date, config: CalendarConfig = calendarConfig): { start: Date; end: Date } {
  return { start: slotStart(day, 0, config), end: slotStart(addDays(day, 1), 0, config) }
}

/**
 * The half-open overlap test, matching the backend's predicate exactly:
 * `existing.start < new.end AND new.start < existing.end`.
 *
 * Adjacency is therefore not an overlap — a booking ending at 10:00 leaves the
 * 10:00 slot free.
 */
export function intervalsOverlap(aStart: Date, aEnd: Date, bStart: Date, bEnd: Date): boolean {
  return aStart.getTime() < bEnd.getTime() && bStart.getTime() < aEnd.getTime()
}
