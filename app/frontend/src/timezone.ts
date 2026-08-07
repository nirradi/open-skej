/**
 * The one seam where a Space's wall-clock time, in its own IANA zone, meets a
 * real instant.
 *
 * Every other module that needs a Space's `timezone` — `config.ts`'s
 * `slotStart`, `calendar/week.ts`'s day and week arithmetic, `booking/
 * summary.ts`'s confirm-panel copy — converts through the two functions here
 * rather than reading or writing zoned wall-clock time itself. `Intl.
 * DateTimeFormat` with an explicit `timeZone` is what does the resolving,
 * always asked fresh for the specific instant in question — never a fixed
 * offset computed once and reused, which is right for the day it was read
 * and silently wrong the next time the zone's DST rule flips. This mirrors
 * `app/backend/app/rules_stub.py`'s `_local_midnight_utc` reasoning on the
 * backend, applied to the one place the frontend needs the same conversion:
 * turning what the grid shows into the instant a click actually submits.
 */

/** Wall-clock parts read back from an instant, in an explicit zone. */
export interface ZonedParts {
  year: number
  /** 0-based, matching `Date`'s own month convention. */
  month: number
  day: number
  hour: number
  minute: number
}

/**
 * The system's own IANA zone — Node's or the browser's, whichever this code
 * is running under.
 *
 * Used only as a bootstrapping default for the brief window before a Space's
 * real `timezone` is known (`calendarConfig`'s module-level fallback,
 * `ResourceCalendarPage` before its header fetch resolves) and to compute the
 * "your zone" hint the grid shows the viewer. It is never a per-user
 * preference and never substitutes for a Space's own `timezone` once that is
 * known — see `ops/DEFERRED.md` item 19 for why a per-viewer zone is
 * rejected outright.
 */
export const SYSTEM_TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone

const partsFormatCache = new Map<string, Intl.DateTimeFormat>()

function partsFormatFor(timeZone: string): Intl.DateTimeFormat {
  let format = partsFormatCache.get(timeZone)
  if (!format) {
    format = new Intl.DateTimeFormat('en-US', {
      timeZone,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
    partsFormatCache.set(timeZone, format)
  }
  return format
}

/** The wall-clock date and time `instant` reads as in `timeZone`. */
export function zonedParts(instant: Date, timeZone: string): ZonedParts {
  const parts = partsFormatFor(timeZone).formatToParts(instant)
  const value = (type: string) => Number(parts.find((part) => part.type === type)?.value ?? NaN)
  return {
    year: value('year'),
    month: value('month') - 1,
    day: value('day'),
    // `hourCycle: 'h23'` should never emit "24", but normalising here costs
    // nothing and means no caller has to know the difference if some
    // environment's ICU data does.
    hour: value('hour') % 24,
    minute: value('minute'),
  }
}

/** `timeZone`'s offset from UTC, in milliseconds, at the instant `instantMs`. */
function offsetAt(instantMs: number, timeZone: string): number {
  const { year, month, day, hour, minute } = zonedParts(new Date(instantMs), timeZone)
  const asUtc = Date.UTC(year, month, day, hour, minute, 0, 0)
  return asUtc - instantMs
}

/**
 * The instant at which `timeZone`'s wall clock reads `year`-`month`-`day`
 * `hour`:`minute`.
 *
 * Not offset arithmetic in the sense that bit the backend once (see
 * `rules_stub.py`'s `_local_midnight_utc`): `Date.UTC` below supplies only a first guess, which
 * is then corrected by asking `Intl.DateTimeFormat` what `timeZone`'s actual
 * offset is *at that guess*, and corrected again at the resulting instant —
 * the second pass is what keeps a guess landing on a DST transition from
 * resolving an hour off, because the offset right after the transition can
 * differ from the offset the first guess assumed. Two passes converge for
 * every real IANA zone: none changes its offset twice inside one
 * offset-sized window.
 */
export function zonedTimeToInstant(
  year: number,
  month: number,
  day: number,
  hour: number,
  minute: number,
  timeZone: string,
): Date {
  const guess = Date.UTC(year, month, day, hour, minute, 0, 0)
  const firstOffset = offsetAt(guess, timeZone)
  const secondOffset = offsetAt(guess - firstOffset, timeZone)
  return new Date(guess - secondOffset)
}

/**
 * The calendar date `instant` falls on in `timeZone`, as a local-midnight
 * `Date` — the same "calendar date carrier" shape `calendar/week.ts` already
 * builds with its own local `Date` constructor and reads back with its own
 * local getters. Only the zone used to *read* the date differs; once built,
 * this carrier is plain calendar arithmetic exactly like any other `day` in
 * this codebase, and every function in `week.ts` that takes a `day` accepts
 * it unchanged.
 */
export function zonedCalendarDate(instant: Date, timeZone: string): Date {
  const { year, month, day } = zonedParts(instant, timeZone)
  return new Date(year, month, day)
}
