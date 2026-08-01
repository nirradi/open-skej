/**
 * Tests for week arithmetic and the booking horizon.
 *
 * These are the rules that decide what the grid is allowed to offer, so they
 * are tested as functions rather than only through the rendered component —
 * the boundary cases (exactly on the horizon, exactly at the week edge) are
 * fiddly to reach by clicking and trivial to state here.
 */

import { describe, expect, it } from 'vitest'

import {
  addDays,
  BOOKING_HORIZON_DAYS,
  canGoToNextWeek,
  canGoToPreviousWeek,
  dateFromKey,
  dayBounds,
  DAYS_PER_WEEK,
  daysOfWeek,
  formatClockTime,
  horizonEnd,
  intervalsOverlap,
  isSlotBeyondHorizon,
  isSlotInPast,
  parseWeekStartParam,
  slotInterval,
  slotTestId,
  startOfDay,
  startOfWeek,
  toDateKey,
} from './week'
import { SYSTEM_TIME_ZONE } from '../timezone'

// Every existing test below passes `SYSTEM_TZ` where a `timeZone` is now
// required, so "today" and "the venue's clock" both mean whatever this suite
// already assumed they meant — the environment's own zone. The point of the
// fix is the *parameter*, not a hardcoded zone, and the "depends on the
// Space's zone, not the environment's" describe block further down is what
// actually exercises that.
const SYSTEM_TZ = SYSTEM_TIME_ZONE

/** A Wednesday, mid-afternoon. */
const NOW = new Date(2026, 6, 22, 14, 30)
/** The Monday of NOW's week. */
const THIS_WEEK = new Date(2026, 6, 20)

describe('startOfWeek', () => {
  it('returns the Monday at or before the date', () => {
    expect(startOfWeek(NOW).getTime()).toBe(THIS_WEEK.getTime())
  })

  it('is idempotent on a Monday', () => {
    expect(startOfWeek(THIS_WEEK).getTime()).toBe(THIS_WEEK.getTime())
  })

  it('walks back six days from a Sunday, not forward one', () => {
    // The off-by-one this guards is the classic `getDay() === 0` case, where a
    // naive modulo lands on the *next* Monday and silently shifts the week.
    const sunday = new Date(2026, 6, 26, 9, 0)
    expect(sunday.getDay()).toBe(0)
    expect(startOfWeek(sunday).getTime()).toBe(THIS_WEEK.getTime())
  })

  it('discards the time component', () => {
    expect(startOfWeek(NOW).getHours()).toBe(0)
  })
})

describe('daysOfWeek', () => {
  it('returns seven consecutive days beginning at the week start', () => {
    const days = daysOfWeek(THIS_WEEK)
    expect(days).toHaveLength(DAYS_PER_WEEK)
    expect(days.map(toDateKey)).toEqual([
      '2026-07-20',
      '2026-07-21',
      '2026-07-22',
      '2026-07-23',
      '2026-07-24',
      '2026-07-25',
      '2026-07-26',
    ])
  })
})

describe('the previous-week bound', () => {
  it('is closed on the current week', () => {
    expect(canGoToPreviousWeek(THIS_WEEK, NOW, SYSTEM_TZ)).toBe(false)
  })

  it('is open on any later week', () => {
    expect(canGoToPreviousWeek(addDays(THIS_WEEK, DAYS_PER_WEEK), NOW, SYSTEM_TZ)).toBe(true)
  })

  it('is closed on the current week even at one minute past midnight on its first day', () => {
    // Guards against comparing against `now` rather than the start of its week,
    // which would make the current week look navigable-away-from all Monday.
    const mondayMorning = new Date(2026, 6, 20, 0, 1)
    expect(canGoToPreviousWeek(THIS_WEEK, mondayMorning, SYSTEM_TZ)).toBe(false)
  })
})

describe('the next-week bound', () => {
  it('is open while the following week still contains bookable days', () => {
    expect(canGoToNextWeek(THIS_WEEK, NOW, SYSTEM_TZ)).toBe(true)
  })

  it('is closed on the week that holds the horizon', () => {
    const lastBookableDay = startOfDay(horizonEnd(NOW))
    const lastWeek = startOfWeek(lastBookableDay)
    expect(canGoToNextWeek(lastWeek, NOW, SYSTEM_TZ)).toBe(false)
  })

  it('is open on the week before the one that holds the horizon', () => {
    // The negative control for the case above: if `canGoToNextWeek` were simply
    // returning false for every late week, this would fail.
    const lastWeek = startOfWeek(startOfDay(horizonEnd(NOW)))
    expect(canGoToNextWeek(addDays(lastWeek, -DAYS_PER_WEEK), NOW, SYSTEM_TZ)).toBe(true)
  })

  it('opens the next week exactly when it contains the horizon day and not after', () => {
    const lastBookableDay = startOfDay(horizonEnd(NOW))
    const weekEndingOnHorizon = addDays(lastBookableDay, -DAYS_PER_WEEK)
    expect(canGoToNextWeek(weekEndingOnHorizon, NOW, SYSTEM_TZ)).toBe(true)
    expect(canGoToNextWeek(addDays(weekEndingOnHorizon, 1), NOW, SYSTEM_TZ)).toBe(false)
  })
})

describe("the current week depends on the Space's zone, not the environment's", () => {
  // A single real instant, chosen so its calendar date — and therefore which
  // week is "current" — differs by a full week between two IANA zones,
  // regardless of whatever zone this suite happens to run under: Sunday
  // 23:00 UTC, July 26 2026, is still Sunday the 26th (this week) in
  // Honolulu (UTC-10) but already Monday the 27th (next week) in Tokyo
  // (UTC+9).
  const sundayNightUtc = new Date('2026-07-26T23:00:00Z')
  const nextWeek = addDays(THIS_WEEK, DAYS_PER_WEEK) // Monday 2026-07-27

  it("reads 'today' in Honolulu as still inside this week", () => {
    expect(canGoToPreviousWeek(nextWeek, sundayNightUtc, 'Pacific/Honolulu')).toBe(true)
  })

  it("reads 'today' in Tokyo as already inside next week", () => {
    expect(canGoToPreviousWeek(nextWeek, sundayNightUtc, 'Asia/Tokyo')).toBe(false)
  })

  it('parseWeekStartParam accepts or rejects the same URL differently per zone', () => {
    const key = toDateKey(THIS_WEEK)
    // In Tokyo, THIS_WEEK is already the past — forward-only navigation
    // rejects it.
    expect(parseWeekStartParam(key, sundayNightUtc, 'Asia/Tokyo')).toBeNull()
    // In Honolulu, THIS_WEEK is still the current week — a valid target.
    expect(parseWeekStartParam(key, sundayNightUtc, 'Pacific/Honolulu')?.getTime()).toBe(
      THIS_WEEK.getTime(),
    )
  })

  // `canGoToNextWeek` compares against the *horizon*, not "today" — a
  // different boundary, so it needs its own instant: `horizonNow` is chosen
  // so `horizonEnd(horizonNow)` lands on that same Sunday-23:00-UTC instant,
  // which the two zones read as different calendar days (Sunday the 26th in
  // Honolulu, Monday the 27th in Tokyo) — and therefore different "last
  // reachable" weeks.
  const horizonNow = new Date(sundayNightUtc.getTime() - BOOKING_HORIZON_DAYS * 24 * 60 * 60 * 1000)

  it('closes next-week navigation where the horizon falls in Honolulu', () => {
    // The horizon day (Jul 26, Honolulu) is already inside THIS_WEEK, so
    // THIS_WEEK is the last reachable week.
    expect(canGoToNextWeek(THIS_WEEK, horizonNow, 'Pacific/Honolulu')).toBe(false)
  })

  it('keeps next-week navigation open where the horizon falls in Tokyo', () => {
    // The horizon day (Jul 27, Tokyo) falls in `nextWeek`, so it is still
    // reachable from THIS_WEEK.
    expect(canGoToNextWeek(THIS_WEEK, horizonNow, 'Asia/Tokyo')).toBe(true)
  })
})

describe('slot horizon predicates', () => {
  it('treats a slot that has already started as past', () => {
    expect(isSlotInPast(new Date(NOW.getTime() - 1), NOW)).toBe(true)
  })

  it('does not treat a slot starting exactly now as past', () => {
    expect(isSlotInPast(NOW, NOW)).toBe(false)
  })

  it('allows a slot exactly on the horizon and denies one a minute later', () => {
    // Mirrors the backend boundary test in 1.4b: inclusive at exactly
    // BOOKING_HORIZON_DAYS, denied at + 1 minute.
    const onHorizon = horizonEnd(NOW)
    expect(isSlotBeyondHorizon(onHorizon, NOW)).toBe(false)
    expect(isSlotBeyondHorizon(new Date(onHorizon.getTime() + 60_000), NOW)).toBe(true)
  })

  it('puts the horizon 60 days out', () => {
    expect(BOOKING_HORIZON_DAYS).toBe(60)
    const days = (horizonEnd(NOW).getTime() - NOW.getTime()) / (24 * 60 * 60 * 1000)
    expect(days).toBe(60)
  })
})

describe('slotInterval', () => {
  it('spans exactly one slot length', () => {
    // Slot 0 is always midnight now (the grid renders the full day); index 12
    // at 30-minute slots is 06:00.
    const config = { slotMinutes: 30, openMinutes: null, closeMinutes: null, timeZone: SYSTEM_TZ }
    const { start, end } = slotInterval(THIS_WEEK, 12, config)
    expect(start.getHours()).toBe(6)
    expect((end.getTime() - start.getTime()) / 60_000).toBe(30)
  })

  it('follows the configured granularity', () => {
    // Index 39 at 10-minute slots, from midnight, is 06:30.
    const { start, end } = slotInterval(THIS_WEEK, 39, {
      slotMinutes: 10,
      openMinutes: null,
      closeMinutes: null,
      timeZone: SYSTEM_TZ,
    })
    expect(start.getHours()).toBe(6)
    expect(start.getMinutes()).toBe(30)
    expect((end.getTime() - start.getTime()) / 60_000).toBe(10)
  })
})

describe('dayBounds', () => {
  it('spans midnight to midnight on the Space\'s own clock', () => {
    const config = { slotMinutes: 30, openMinutes: null, closeMinutes: null, timeZone: SYSTEM_TZ }
    const { start, end } = dayBounds(THIS_WEEK, config)
    expect(start.getTime()).toBe(slotInterval(THIS_WEEK, 0, config).start.getTime())
    expect(end.getTime()).toBe(slotInterval(addDays(THIS_WEEK, 1), 0, config).start.getTime())
  })

  it('is 23 real hours on the day the Space\'s zone springs forward', () => {
    // Europe/Berlin's clock jumps 02:00 to 03:00 on 2026-03-29 — the
    // calendar day is still "one day" on the Space's own clock, but the
    // real span between its midnight and the next is an hour short. A
    // fixed-offset day (`+ 24h`) would get this wrong; going through the
    // zone for both bounds does not.
    const config = { slotMinutes: 30, openMinutes: null, closeMinutes: null, timeZone: 'Europe/Berlin' }
    const { start, end } = dayBounds(new Date(2026, 2, 29), config)
    expect((end.getTime() - start.getTime()) / (60 * 60 * 1000)).toBe(23)
  })
})

describe('intervalsOverlap', () => {
  const at = (h: number, m = 0) => new Date(2026, 6, 20, h, m)

  it('is false for adjacent intervals', () => {
    // The half-open predicate the backend uses: a booking ending at 10:00
    // leaves the 10:00 slot free.
    expect(intervalsOverlap(at(9), at(10), at(10), at(11))).toBe(false)
  })

  it('is true for a partial overlap in either direction', () => {
    expect(intervalsOverlap(at(9), at(10, 30), at(10), at(11))).toBe(true)
    expect(intervalsOverlap(at(10), at(11), at(9), at(10, 30))).toBe(true)
  })

  it('is true for containment', () => {
    expect(intervalsOverlap(at(9, 30), at(9, 45), at(9), at(11))).toBe(true)
  })
})

describe('dateFromKey', () => {
  it('inverts toDateKey', () => {
    const day = new Date(2026, 6, 20)
    expect(toDateKey(dateFromKey(toDateKey(day)))).toBe(toDateKey(day))
    expect(dateFromKey('2026-07-20').getTime()).toBe(day.getTime())
  })
})

describe('parseWeekStartParam', () => {
  it('reads a week-start key as itself', () => {
    expect(parseWeekStartParam('2026-07-20', NOW, SYSTEM_TZ)?.getTime()).toBe(THIS_WEEK.getTime())
  })

  it('normalises a real date that is not a week start to the week containing it', () => {
    // Thursday of THIS_WEEK.
    expect(parseWeekStartParam('2026-07-23', NOW, SYSTEM_TZ)?.getTime()).toBe(THIS_WEEK.getTime())
  })

  it('falls back to null when there is no value at all', () => {
    expect(parseWeekStartParam(null, NOW, SYSTEM_TZ)).toBeNull()
  })

  it('falls back to null for a value that is not a date at all', () => {
    expect(parseWeekStartParam('banana', NOW, SYSTEM_TZ)).toBeNull()
  })

  it('falls back to null for an empty string', () => {
    expect(parseWeekStartParam('', NOW, SYSTEM_TZ)).toBeNull()
  })

  it('falls back to null for a date that rolls over rather than naming a real day', () => {
    // Date(2026, 1, 30) rolls over to March, which is not what the string says.
    expect(parseWeekStartParam('2026-02-30', NOW, SYSTEM_TZ)).toBeNull()
  })

  it('falls back to null for a week earlier than the current one', () => {
    expect(parseWeekStartParam('2026-07-13', NOW, SYSTEM_TZ)).toBeNull()
  })

  it('falls back to null for a date beyond the booking horizon', () => {
    const beyond = toDateKey(addDays(startOfWeek(horizonEnd(NOW)), DAYS_PER_WEEK))
    expect(parseWeekStartParam(beyond, NOW, SYSTEM_TZ)).toBeNull()
  })

  it('accepts the last week the horizon still reaches', () => {
    const lastWeek = startOfWeek(horizonEnd(NOW))
    expect(parseWeekStartParam(toDateKey(lastWeek), NOW, SYSTEM_TZ)?.getTime()).toBe(
      lastWeek.getTime(),
    )
  })

  it('accepts the current week itself', () => {
    expect(parseWeekStartParam(toDateKey(THIS_WEEK), NOW, SYSTEM_TZ)?.getTime()).toBe(
      THIS_WEEK.getTime(),
    )
  })
})

describe('formatClockTime', () => {
  it('reads the Space\'s own zone, not the environment\'s', () => {
    // 11:00Z is 13:00 in Berlin during CEST (see `config.test.ts`'s item 19
    // repro) and 01:00 the same day in Honolulu — two different labels for
    // one instant, each correct in its own zone.
    const instant = new Date('2026-08-03T11:00:00Z')
    expect(formatClockTime(instant, 'Europe/Berlin')).toBe('13:00')
    expect(formatClockTime(instant, 'Pacific/Honolulu')).toBe('01:00')
  })

  it('reads the identical wall-clock label on either side of a DST boundary', () => {
    // Mirrors the `slotStart` DST test in `config.test.ts`: 05:00Z is 07:00
    // in Berlin in July (CEST) and 06:00Z is 07:00 in Berlin in January
    // (CET) — the same label, resolved through two different real offsets.
    expect(formatClockTime(new Date('2026-07-20T05:00:00Z'), 'Europe/Berlin')).toBe('07:00')
    expect(formatClockTime(new Date('2026-01-20T06:00:00Z'), 'Europe/Berlin')).toBe('07:00')
  })
})

describe('slot test ids', () => {
  it('are derivable from a date and a slot index', () => {
    expect(slotTestId(new Date(2026, 6, 20, 18, 45), 12)).toBe('slot-2026-07-20-12')
  })

  it('zero-pad the month and day so ids sort and match predictably', () => {
    expect(slotTestId(new Date(2026, 0, 5), 0)).toBe('slot-2026-01-05-0')
  })
})
