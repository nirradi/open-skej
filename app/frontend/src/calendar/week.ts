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
 */

import { calendarConfig, slotStart, type CalendarConfig } from '../config'

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
 * the plan, viewing past bookings is out of scope this phase.
 */
export function canGoToPreviousWeek(weekStart: Date, now: Date): boolean {
  return weekStart.getTime() > startOfWeek(now).getTime()
}

/**
 * Whether the week after `weekStart` may be navigated to.
 *
 * True only while that next week still contains at least one bookable day, so
 * paging stops on the week holding the horizon rather than one past it.
 */
export function canGoToNextWeek(weekStart: Date, now: Date): boolean {
  return addDays(weekStart, DAYS_PER_WEEK).getTime() <= startOfDay(horizonEnd(now)).getTime()
}

/** Why a slot cannot be selected, or `null` when it can. */
export type SlotBlockedReason = 'past' | 'beyond-horizon' | 'booked' | 'unavailable'

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
 */
export function parseWeekStartParam(value: string | null, now: Date): Date | null {
  if (value === null || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null

  const parsed = dateFromKey(value)
  const [year, month, day] = value.split('-').map(Number)
  if (parsed.getFullYear() !== year || parsed.getMonth() !== month - 1 || parsed.getDate() !== day) {
    return null
  }

  const weekStart = startOfWeek(parsed)
  const earliest = startOfWeek(now)
  const latest = startOfWeek(horizonEnd(now))
  if (weekStart.getTime() < earliest.getTime() || weekStart.getTime() > latest.getTime()) {
    return null
  }

  return weekStart
}

/**
 * A wall-clock time as `HH:MM`, in the browser's local timezone.
 *
 * Forced to 24-hour rather than left to the locale, so that every time in the
 * UI reads the same way. `formatSlotLabel` renders the time axis as `HH:MM`
 * unconditionally; a locale-dependent formatter alongside it produced a grid
 * whose axis said `12:00` while the booking sitting on that row said
 * `12:00 PM`, which looks like two different times at a glance.
 */
export function formatClockTime(value: Date): string {
  const hh = String(value.getHours()).padStart(2, '0')
  const mm = String(value.getMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

/** The half-open interval `[start, end)` a slot occupies. */
export function slotInterval(
  day: Date,
  index: number,
  config: CalendarConfig = calendarConfig,
): { start: Date; end: Date } {
  const start = slotStart(day, index, config)
  return { start, end: new Date(start.getTime() + config.slotMinutes * 60 * 1000) }
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
