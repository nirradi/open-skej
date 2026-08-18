/**
 * The calendar shape domain types and queries over them.
 *
 * **The projection is what the grid draws; it is never re-derived here.** The
 * server resolves a date's operating intervals, blackouts and offered starts
 * through the shape (task 10.3, `GET /spaces/{public_id}/calendar`), and the
 * grid renders exactly what it received. A date the server did not project is
 * a date this client knows nothing about — see `closedDay` below for what
 * replaces the old permissive default.
 */

import { DEFAULT_STEP_MINUTES } from '../config'

/** One grid-aligned local minute a booking may begin at, and every duration offered there. */
export interface OfferedStart {
  startMinutes: number
  durationsMins: number[]
}

/** One merged region of operating time in force on a date. */
export interface OperatingInterval {
  startMinutes: number
  endMinutes: number
  allowedDurationsMins: number[]
}

/** One blackout window in force on a date, with its own reason. */
export interface BlackoutInterval {
  startMinutes: number
  endMinutes: number
  reason: string
}

/**
 * Everything the grid needs to draw one local date, resolved and ready to render.
 *
 * `offeredStarts` is the authoritative table `selection.ts`'s resolution is
 * answered from — a start/duration pair is selectable exactly when it appears
 * here. `bookable` is `offeredStarts.length > 0`, named separately because "is
 * there anything to draw at all" is the first question a calendar view asks
 * about a date, before it asks what.
 */
export interface DayProjection {
  dateKey: string
  operatingIntervals: OperatingInterval[]
  blackoutIntervals: BlackoutInterval[]
  offeredStarts: OfferedStart[]
  bookable: boolean
}

/**
 * A week's projection: the resolution for a range of dates plus a shared zone.
 *
 * `forDate` falls back to a CLOSED day (`bookable: false`, nothing offered),
 * never a permissive one. This is the direct inversion of `config.ts`'s old
 * `DEFAULT_DAY_SCHEDULE`, which fell back to "the whole day, default slot
 * size", and the inversion is the point: **fail closed**. A date the server
 * did not project is a date this client knows nothing about, and offering it
 * would be the grid inventing availability — precisely the defect this stream
 * exists to close. That is why there is no fallback that looks like the old
 * default: the new default is to offer nothing, which is the only honest
 * answer when the grid has no shape.
 */
export interface WeekProjection {
  timeZone: string
  forDate: (dateKey: string) => DayProjection
}

/**
 * Builds a `WeekProjection` from `GET /spaces/{public_id}/calendar`'s response.
 *
 * `entries[i].date` (an ISO date string) is used verbatim as the lookup key —
 * the identical shape `toDateKey` (`calendar/week.ts`) produces for every other
 * per-day computation, so `CalendarGrid` addresses a day's projection with the
 * same key it already uses for everything else about that day.
 */
export function buildWeekProjection(
  entries: readonly {
    date: string
    operating_intervals: readonly {
      start_minutes: number
      end_minutes: number
      allowed_durations_mins: readonly number[]
    }[]
    blackout_intervals: readonly {
      start_minutes: number
      end_minutes: number
      reason: string
    }[]
    offered_starts: readonly {
      start_minutes: number
      durations_mins: readonly number[]
    }[]
    bookable: boolean
  }[],
  timeZone: string,
): WeekProjection {
  const byDate = new Map<string, DayProjection>()
  for (const entry of entries) {
    byDate.set(entry.date, {
      dateKey: entry.date,
      operatingIntervals: entry.operating_intervals.map((interval) => ({
        startMinutes: interval.start_minutes,
        endMinutes: interval.end_minutes,
        allowedDurationsMins: [...interval.allowed_durations_mins],
      })),
      blackoutIntervals: entry.blackout_intervals.map((interval) => ({
        startMinutes: interval.start_minutes,
        endMinutes: interval.end_minutes,
        reason: interval.reason,
      })),
      offeredStarts: entry.offered_starts.map((offered) => ({
        startMinutes: offered.start_minutes,
        durationsMins: [...offered.durations_mins],
      })),
      bookable: entry.bookable,
    })
  }
  return { timeZone, forDate: (dateKey) => byDate.get(dateKey) ?? closedDay(dateKey) }
}

/**
 * A closed day — nothing bookable, nothing operating, used as the fallback when
 * a date was never projected by the server.
 *
 * This is what replaces `config.ts`'s old permissive `DEFAULT_DAY_SCHEDULE`. A
 * date the server did not send a projection for is a date this client has no
 * shape information about, and the only honest answer in that state is to offer
 * nothing. The old default — "the whole day bookable" — was the grid inventing
 * availability the server never asserted, which is exactly the failure mode this
 * stream closes.
 */
export function closedDay(dateKey: string): DayProjection {
  return {
    dateKey,
    operatingIntervals: [],
    blackoutIntervals: [],
    offeredStarts: [],
    bookable: false,
  }
}

/**
 * The smallest duration offered anywhere in the visible week — the shared
 * pixel-scale granularity a heterogeneous week's grid renders at.
 *
 * Defaults to `config.ts`'s `DEFAULT_STEP_MINUTES` for a week with
 * nothing offered at all — the fallback pixel scale when there is no shape to
 * measure. Every day's own offered starts land on a subset of the axis rows
 * only when every duration offered anywhere in the week is a multiple of this
 * value; when they are not, the axis is still the finest of them, and the
 * mismatch is a readability finding rather than a case this function papers
 * over.
 */
export function finestDurationMinutes(
  week: WeekProjection,
  dateKeys: readonly string[],
  defaultStepMinutes: number = DEFAULT_STEP_MINUTES,
): number {
  let finest = Infinity
  for (const key of dateKeys) {
    const day = week.forDate(key)
    for (const offered of day.offeredStarts) {
      for (const duration of offered.durationsMins) {
        finest = Math.min(finest, duration)
      }
    }
  }
  return Number.isFinite(finest) ? finest : defaultStepMinutes
}

/**
 * The durations offered at `startMinutes` on the given date, or an empty array
 * if that start is not offered.
 */
export function durationsAt(day: DayProjection, startMinutes: number): readonly number[] {
  const offered = day.offeredStarts.find((s) => s.startMinutes === startMinutes)
  return offered?.durationsMins ?? []
}

/**
 * The smallest duration offered at `startMinutes`, or `null` if that start is
 * not offered at all.
 */
export function smallestDurationAt(day: DayProjection, startMinutes: number): number | null {
  const durations = durationsAt(day, startMinutes)
  return durations.length > 0 ? Math.min(...durations) : null
}

/**
 * The next offered start strictly after `startMinutes`, or `null` if there is
 * none.
 */
export function nextStartAfter(day: DayProjection, startMinutes: number): number | null {
  for (const offered of day.offeredStarts) {
    if (offered.startMinutes > startMinutes) {
      return offered.startMinutes
    }
  }
  return null
}
