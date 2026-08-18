/**
 * Tests for what is left of the calendar configuration module: pixel scale
 * and time-label formatting. Everything else this file used to cover —
 * `CalendarConfig`, slot arithmetic, `isSlotOutOfHours`, the anchored grid —
 * moved with the substrate itself: the grid is drawn from the shape
 * projection now (`calendar/shape.test.ts`), not from a slot-size config.
 */

import { describe, expect, it } from 'vitest'

import { formatMinutesLabel, MINUTES_PER_DAY, MINUTES_PER_HOUR, pxPerMinuteFor } from './config'

describe('MINUTES_PER_HOUR / MINUTES_PER_DAY', () => {
  it('are the plain constants the rest of the grid multiplies by', () => {
    expect(MINUTES_PER_HOUR).toBe(60)
    expect(MINUTES_PER_DAY).toBe(24 * 60)
  })
})

describe('pxPerMinuteFor', () => {
  it('renders a 30-minute week at exactly the pre-shape row height', () => {
    // SLOT_ROW_HEIGHT_PX (28) / 30 — the shipped default before task 10.4,
    // preserved so a Space at the ordinary 30-minute step renders unchanged.
    expect(pxPerMinuteFor(30)).toBeCloseTo(28 / 30)
  })

  it('renders a finer week taller — more resolution costs more height', () => {
    expect(pxPerMinuteFor(10)).toBeGreaterThan(pxPerMinuteFor(30))
    expect(pxPerMinuteFor(10)).toBeCloseTo(28 / 10)
  })

  it('renders a coarser week shorter', () => {
    expect(pxPerMinuteFor(60)).toBeLessThan(pxPerMinuteFor(30))
    expect(pxPerMinuteFor(60)).toBeCloseTo(28 / 60)
  })
})

describe('formatMinutesLabel', () => {
  it('formats midnight', () => {
    expect(formatMinutesLabel(0)).toBe('00:00')
  })

  it('formats an ordinary time', () => {
    expect(formatMinutesLabel(9 * 60 + 15)).toBe('09:15')
  })

  it('pads single-digit hours and minutes', () => {
    expect(formatMinutesLabel(65)).toBe('01:05')
  })

  it('formats a time past local midnight without wrapping — the caller decides whether to draw it', () => {
    // 1500 minutes is 25:00 — a venue open past midnight (the shape schema
    // permits this; `CalendarGrid` is what declines to render it). This
    // function itself has no opinion and simply renders the number handed to
    // it.
    expect(formatMinutesLabel(25 * 60)).toBe('25:00')
  })
})
