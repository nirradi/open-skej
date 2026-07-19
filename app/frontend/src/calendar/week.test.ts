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
  DAYS_PER_WEEK,
  daysOfWeek,
  horizonEnd,
  intervalsOverlap,
  isSlotBeyondHorizon,
  isSlotInPast,
  slotInterval,
  slotTestId,
  startOfDay,
  startOfWeek,
  toDateKey,
} from './week'

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
    expect(canGoToPreviousWeek(THIS_WEEK, NOW)).toBe(false)
  })

  it('is open on any later week', () => {
    expect(canGoToPreviousWeek(addDays(THIS_WEEK, DAYS_PER_WEEK), NOW)).toBe(true)
  })

  it('is closed on the current week even at one minute past midnight on its first day', () => {
    // Guards against comparing against `now` rather than the start of its week,
    // which would make the current week look navigable-away-from all Monday.
    const mondayMorning = new Date(2026, 6, 20, 0, 1)
    expect(canGoToPreviousWeek(THIS_WEEK, mondayMorning)).toBe(false)
  })
})

describe('the next-week bound', () => {
  it('is open while the following week still contains bookable days', () => {
    expect(canGoToNextWeek(THIS_WEEK, NOW)).toBe(true)
  })

  it('is closed on the week that holds the horizon', () => {
    const lastBookableDay = startOfDay(horizonEnd(NOW))
    const lastWeek = startOfWeek(lastBookableDay)
    expect(canGoToNextWeek(lastWeek, NOW)).toBe(false)
  })

  it('is open on the week before the one that holds the horizon', () => {
    // The negative control for the case above: if `canGoToNextWeek` were simply
    // returning false for every late week, this would fail.
    const lastWeek = startOfWeek(startOfDay(horizonEnd(NOW)))
    expect(canGoToNextWeek(addDays(lastWeek, -DAYS_PER_WEEK), NOW)).toBe(true)
  })

  it('opens the next week exactly when it contains the horizon day and not after', () => {
    const lastBookableDay = startOfDay(horizonEnd(NOW))
    const weekEndingOnHorizon = addDays(lastBookableDay, -DAYS_PER_WEEK)
    expect(canGoToNextWeek(weekEndingOnHorizon, NOW)).toBe(true)
    expect(canGoToNextWeek(addDays(weekEndingOnHorizon, 1), NOW)).toBe(false)
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
    const config = { slotMinutes: 30, openHour: 6, closeHour: 23 }
    const { start, end } = slotInterval(THIS_WEEK, 0, config)
    expect(start.getHours()).toBe(6)
    expect((end.getTime() - start.getTime()) / 60_000).toBe(30)
  })

  it('follows the configured granularity', () => {
    const { start, end } = slotInterval(THIS_WEEK, 3, {
      slotMinutes: 10,
      openHour: 6,
      closeHour: 23,
    })
    expect(start.getHours()).toBe(6)
    expect(start.getMinutes()).toBe(30)
    expect((end.getTime() - start.getTime()) / 60_000).toBe(10)
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

describe('slot test ids', () => {
  it('are derivable from a date and a slot index', () => {
    expect(slotTestId(new Date(2026, 6, 20, 18, 45), 12)).toBe('slot-2026-07-20-12')
  })

  it('zero-pad the month and day so ids sort and match predictably', () => {
    expect(slotTestId(new Date(2026, 0, 5), 0)).toBe('slot-2026-01-05-0')
  })
})
