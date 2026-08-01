import { describe, expect, it } from 'vitest'

import { formatDuration, summariseInterval } from './summary'
import { SYSTEM_TIME_ZONE } from '../timezone'

describe('formatDuration', () => {
  it('drops the minute part for whole hours', () => {
    // "2 hours 0 minutes" reads as a truncation bug rather than as precision.
    expect(formatDuration(120)).toBe('2 hours')
    expect(formatDuration(60)).toBe('1 hour')
  })

  it('states minutes alone under an hour', () => {
    expect(formatDuration(30)).toBe('30 minutes')
    expect(formatDuration(1)).toBe('1 minute')
  })

  it('combines both when there is a remainder', () => {
    expect(formatDuration(90)).toBe('1 hour 30 minutes')
    expect(formatDuration(145)).toBe('2 hours 25 minutes')
  })

  it('singularises correctly', () => {
    expect(formatDuration(61)).toBe('1 hour 1 minute')
  })
})

describe('summariseInterval', () => {
  it('describes a variable-length interval in the environment\'s own zone', () => {
    const summary = summariseInterval(
      {
        start: new Date(2026, 6, 24, 8, 0),
        end: new Date(2026, 6, 24, 9, 30),
      },
      SYSTEM_TIME_ZONE,
    )

    expect(summary.start).toBe('08:00')
    expect(summary.end).toBe('09:30')
    expect(summary.duration).toBe('1 hour 30 minutes')
    expect(summary.day).toContain('2026')
  })

  it('reports the duration of a single 30-minute slot', () => {
    const summary = summariseInterval(
      {
        start: new Date(2026, 6, 24, 8, 0),
        end: new Date(2026, 6, 24, 8, 30),
      },
      SYSTEM_TIME_ZONE,
    )
    expect(summary.duration).toBe('30 minutes')
  })

  it('describes the interval in an explicit zone, not the instant\'s own construction zone', () => {
    // Same real instant as the item 19 repro (11:00Z = 13:00 Berlin), read
    // through a Space's own `timeZone` rather than whatever zone the `Date`
    // happened to be built in.
    const summary = summariseInterval(
      {
        start: new Date('2026-08-03T11:00:00Z'),
        end: new Date('2026-08-03T12:00:00Z'),
      },
      'Europe/Berlin',
    )
    expect(summary.start).toBe('13:00')
    expect(summary.end).toBe('14:00')
  })
})
