/**
 * Shared calendar-shape fixtures for the suites that render a grid.
 *
 * Test-only: nothing in the application imports this, and the bundler drops it.
 * It lives beside the components rather than under a `__fixtures__` directory so
 * that a drift between `DayProjectionRead` here and `DayProjectionRead` in
 * `api/types.ts` is a compile error in the same `tsc -b` that builds the app —
 * the identical posture `admin/fixtures.ts` takes for its own domain.
 *
 * **The projection is the server's answer, so a fixture is a hand-written
 * server answer** (`.claude/rules/calendar-shape.md`). Nothing here re-derives
 * what a shape document would offer; these builders produce the *wire* shape
 * `GET /spaces/{public_id}/calendar` serves, and a test that needs one of the
 * worked examples (the teacher's slots, the lab's cooldowns, the music room's
 * two blocks) writes its own entries out in full rather than asking a builder
 * to infer them. `rules/tests/test_shape.py` asserts the same three examples
 * against the real projection, and is what those hand-written fixtures are
 * checked against.
 */

import type { DayProjectionRead } from '../api'
import { MINUTES_PER_DAY } from '../config'
import { buildWeekProjection, type DayProjection, type WeekProjection } from './shape'
import { daysOfWeek, toDateKey } from './week'

/**
 * The durations an open fixture day offers at every start, unless a caller
 * says otherwise — a Space offering 30 through 120 minutes, the shape the
 * sandbox seed's own Space A holds. Several durations rather than one because
 * a day offering a single duration cannot exercise a *drag* at all: a drag
 * resolves to one of the durations offered at its own start, so a `[30]` day
 * can only ever produce 30 minutes.
 */
export const FIXTURE_DURATIONS = [30, 60, 90, 120]

export interface OpenDayOptions {
  /**
   * The spacing of offered starts. Keep it equal to the smallest offered
   * duration, which is what the real projection's grid steps by
   * (`.claude/rules/calendar-shape.md`, "the grid is chunked forward from each
   * operating block's own `start_time`").
   */
  stepMinutes?: number
  /** Durations offered at each start, filtered per start to those that fit inside the window. */
  durationsMins?: number[]
  /** The operating window, in minutes from local midnight. Defaults to the whole day. */
  openFromMinutes?: number
  openUntilMinutes?: number
}

/**
 * One open date, as the server would project a single operating block over it.
 *
 * Every start from `openFromMinutes` that leaves room for the step, carrying
 * every duration that still ends inside the window — so the last start of a
 * day offers only the durations that actually fit, exactly as the projection
 * does rather than offering a length that would run past closing.
 */
export function openDayRead(dateKey: string, options: OpenDayOptions = {}): DayProjectionRead {
  const {
    stepMinutes = 30,
    durationsMins = FIXTURE_DURATIONS,
    openFromMinutes = 0,
    openUntilMinutes = MINUTES_PER_DAY,
  } = options

  const offered: DayProjectionRead['offered_starts'] = []
  for (let start = openFromMinutes; start + stepMinutes <= openUntilMinutes; start += stepMinutes) {
    const fits = durationsMins.filter((duration) => start + duration <= openUntilMinutes)
    if (fits.length > 0) offered.push({ start_minutes: start, durations_mins: fits })
  }

  return {
    date: dateKey,
    operating_intervals: [
      {
        start_minutes: openFromMinutes,
        end_minutes: openUntilMinutes,
        allowed_durations_mins: durationsMins,
      },
    ],
    blackout_intervals: [],
    offered_starts: offered,
    bookable: offered.length > 0,
  }
}

/** A closed date — no operating time, nothing offered. What `bookable: false` looks like. */
export function closedDayRead(dateKey: string): DayProjectionRead {
  return {
    date: dateKey,
    operating_intervals: [],
    blackout_intervals: [],
    offered_starts: [],
    bookable: false,
  }
}

/** The seven dates of the week beginning `weekStart`, every one open on the same terms. */
export function openWeekRead(weekStart: Date, options: OpenDayOptions = {}): DayProjectionRead[] {
  return daysOfWeek(weekStart).map((day) => openDayRead(toDateKey(day), options))
}

/** One `DayProjection`, mapped through the same `buildWeekProjection` the page uses. */
export function dayProjection(read: DayProjectionRead, timeZone: string): DayProjection {
  return buildWeekProjection([read], timeZone).forDate(read.date)
}

/**
 * A `WeekProjection` that answers *every* date, not only a fixed seven.
 *
 * For the suites that page the grid across weeks — the horizon bounds, the
 * `?week=` round trip — where pinning the fixture to one week would render
 * every other week closed and leave those tests asserting against an empty
 * grid. A real page refetches per week and gets exactly this, one week at a
 * time.
 */
export function openEveryDay(timeZone: string, options: OpenDayOptions = {}): WeekProjection {
  return {
    timeZone,
    forDate: (dateKey) => dayProjection(openDayRead(dateKey, options), timeZone),
  }
}
