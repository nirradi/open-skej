/**
 * Tests for the shape projection's domain types and the pure queries over
 * them. `buildWeekProjection` is a mapping from the wire shape to the
 * client's own vocabulary; the interesting claim to defend is the fail-closed
 * one — a date the server never sent projects as closed, never as
 * permissive.
 */

import { describe, expect, it } from 'vitest'

import {
  buildWeekProjection,
  closedDay,
  durationsAt,
  finestDurationMinutes,
  nextStartAfter,
  smallestDurationAt,
  type DayProjection,
} from './shape'

const TEACHER_DAY = {
  date: '2026-08-18',
  operating_intervals: [
    { start_minutes: 18 * 60, end_minutes: 20 * 60, allowed_durations_mins: [20] },
  ],
  blackout_intervals: [{ start_minutes: 19 * 60 + 30, end_minutes: 19 * 60 + 40, reason: 'Break' }],
  offered_starts: [
    { start_minutes: 18 * 60, durations_mins: [20] },
    { start_minutes: 18 * 60 + 20, durations_mins: [20] },
    { start_minutes: 18 * 60 + 40, durations_mins: [20] },
    { start_minutes: 19 * 60 + 40, durations_mins: [20] },
  ],
  bookable: true,
}

describe('buildWeekProjection', () => {
  it('maps every field from the wire shape to the client vocabulary', () => {
    const week = buildWeekProjection([TEACHER_DAY], 'Europe/Berlin')
    expect(week.timeZone).toBe('Europe/Berlin')

    const day = week.forDate('2026-08-18')
    expect(day.dateKey).toBe('2026-08-18')
    expect(day.bookable).toBe(true)
    expect(day.operatingIntervals).toEqual([
      { startMinutes: 18 * 60, endMinutes: 20 * 60, allowedDurationsMins: [20] },
    ])
    expect(day.blackoutIntervals).toEqual([
      { startMinutes: 19 * 60 + 30, endMinutes: 19 * 60 + 40, reason: 'Break' },
    ])
    expect(day.offeredStarts).toEqual([
      { startMinutes: 18 * 60, durationsMins: [20] },
      { startMinutes: 18 * 60 + 20, durationsMins: [20] },
      { startMinutes: 18 * 60 + 40, durationsMins: [20] },
      { startMinutes: 19 * 60 + 40, durationsMins: [20] },
    ])
  })

  it('falls back to a closed day for a date the server never sent — fail closed, not permissive', () => {
    const week = buildWeekProjection([TEACHER_DAY], 'Europe/Berlin')
    const missing = week.forDate('2026-08-19')
    expect(missing).toEqual(closedDay('2026-08-19'))
    expect(missing.bookable).toBe(false)
    expect(missing.offeredStarts).toEqual([])
    expect(missing.operatingIntervals).toEqual([])
    expect(missing.blackoutIntervals).toEqual([])
  })

  it('projects an empty entry list to closed for every date asked about', () => {
    const week = buildWeekProjection([], 'Europe/Berlin')
    expect(week.forDate('2026-08-18')).toEqual(closedDay('2026-08-18'))
  })
})

describe('closedDay', () => {
  it('carries the given dateKey and nothing bookable', () => {
    expect(closedDay('2026-01-01')).toEqual({
      dateKey: '2026-01-01',
      operatingIntervals: [],
      blackoutIntervals: [],
      offeredStarts: [],
      bookable: false,
    })
  })
})

describe('finestDurationMinutes', () => {
  it('is the smallest duration offered anywhere across the given dates', () => {
    const week = buildWeekProjection(
      [
        TEACHER_DAY,
        {
          date: '2026-08-19',
          operating_intervals: [],
          blackout_intervals: [],
          offered_starts: [{ start_minutes: 8 * 60, durations_mins: [60, 120] }],
          bookable: true,
        },
      ],
      'Europe/Berlin',
    )
    expect(finestDurationMinutes(week, ['2026-08-18', '2026-08-19'])).toBe(20)
  })

  it('ignores a date not asked about', () => {
    const week = buildWeekProjection(
      [
        TEACHER_DAY,
        {
          date: '2026-08-19',
          operating_intervals: [],
          blackout_intervals: [],
          offered_starts: [{ start_minutes: 8 * 60, durations_mins: [5] }],
          bookable: true,
        },
      ],
      'Europe/Berlin',
    )
    expect(finestDurationMinutes(week, ['2026-08-18'])).toBe(20)
  })

  it('defaults to 60 for a week offering nothing at all', () => {
    const week = buildWeekProjection([], 'Europe/Berlin')
    expect(finestDurationMinutes(week, ['2026-08-18', '2026-08-19'])).toBe(60)
  })

  it('honours an explicit default step', () => {
    const week = buildWeekProjection([], 'Europe/Berlin')
    expect(finestDurationMinutes(week, ['2026-08-18'], 15)).toBe(15)
  })
})

describe('durationsAt / smallestDurationAt', () => {
  const day: DayProjection = buildWeekProjection([TEACHER_DAY], 'Europe/Berlin').forDate('2026-08-18')

  it('returns the durations offered at an offered start', () => {
    expect(durationsAt(day, 18 * 60)).toEqual([20])
  })

  it('returns an empty array for a start that is not offered', () => {
    expect(durationsAt(day, 19 * 60 + 20)).toEqual([])
  })

  it('returns the smallest duration at an offered start', () => {
    const musicRoomEvening: DayProjection = buildWeekProjection(
      [
        {
          date: '2026-08-18',
          operating_intervals: [],
          blackout_intervals: [],
          offered_starts: [{ start_minutes: 18 * 60, durations_mins: [120, 60] }],
          bookable: true,
        },
      ],
      'Europe/Berlin',
    ).forDate('2026-08-18')
    expect(smallestDurationAt(musicRoomEvening, 18 * 60)).toBe(60)
  })

  it('returns null for a start that is not offered at all', () => {
    expect(smallestDurationAt(day, 19 * 60 + 20)).toBeNull()
  })
})

describe('nextStartAfter', () => {
  const day: DayProjection = buildWeekProjection([TEACHER_DAY], 'Europe/Berlin').forDate('2026-08-18')

  it('returns the next offered start strictly after the given minute', () => {
    expect(nextStartAfter(day, 18 * 60)).toBe(18 * 60 + 20)
    expect(nextStartAfter(day, 18 * 60 + 40)).toBe(19 * 60 + 40)
  })

  it('returns null after the last offered start', () => {
    expect(nextStartAfter(day, 19 * 60 + 40)).toBeNull()
  })

  it('returns null for a day offering nothing', () => {
    expect(nextStartAfter(closedDay('2026-08-18'), 0)).toBeNull()
  })
})
