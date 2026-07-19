/**
 * Human-readable descriptions of the interval a user is about to book.
 *
 * Kept as pure functions over `Date`s, in the same spirit as `calendar/week.ts`:
 * the confirm panel asks what a range *says*, it does not work it out inline.
 *
 * Bookings are variable length (see `stream-1-plan.md`), so the duration is not
 * derivable from the slot size and has to be stated explicitly — "08:00 – 09:30"
 * alone leaves the user counting, which is exactly the arithmetic a confirmation
 * step exists to remove.
 */

// Imported from the leaf module rather than the `../calendar` barrel. The barrel
// re-exports `CalendarGrid`, which reaches `api/client.ts` and its
// `import.meta.env` — so a barrel import drags a Vite-only dependency into
// anything that touches this file, including the Playwright suite, which runs
// in plain Node.
import { formatClockTime } from '../calendar/week'
import type { SelectedInterval } from '../calendar/CalendarGrid'

const MS_PER_MINUTE = 60 * 1000
const MINUTES_PER_HOUR = 60

const dayFormat = new Intl.DateTimeFormat(undefined, {
  weekday: 'long',
  month: 'short',
  day: 'numeric',
  year: 'numeric',
})

/** What the confirm panel shows about a pending booking. */
export interface IntervalSummary {
  /** The calendar day, e.g. `Friday, Jul 24, 2026`. */
  day: string
  /** Wall-clock start, e.g. `08:00`. */
  start: string
  /** Wall-clock end, e.g. `09:30`. */
  end: string
  /** Length in words, e.g. `1 hour 30 minutes`. */
  duration: string
}

/** `n` with a unit, pluralised. */
function plural(n: number, unit: string): string {
  return `${n} ${unit}${n === 1 ? '' : 's'}`
}

/**
 * Formats a span of minutes as friendly copy.
 *
 * Whole hours drop the minute part entirely — "2 hours", not "2 hours 0
 * minutes" — because the zero reads as a truncation error rather than as
 * precision.
 */
export function formatDuration(minutes: number): string {
  const whole = Math.max(0, Math.round(minutes))
  const hours = Math.floor(whole / MINUTES_PER_HOUR)
  const rest = whole % MINUTES_PER_HOUR

  if (hours === 0) return plural(rest, 'minute')
  if (rest === 0) return plural(hours, 'hour')
  return `${plural(hours, 'hour')} ${plural(rest, 'minute')}`
}

/** Describes `interval` in the browser's local timezone. */
export function summariseInterval(interval: SelectedInterval): IntervalSummary {
  const minutes = (interval.end.getTime() - interval.start.getTime()) / MS_PER_MINUTE
  return {
    day: dayFormat.format(interval.start),
    start: formatClockTime(interval.start),
    end: formatClockTime(interval.end),
    duration: formatDuration(minutes),
  }
}
