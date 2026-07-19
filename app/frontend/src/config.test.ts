/**
 * Tests for the calendar configuration module.
 *
 * The central claim this file defends is the one in `config.ts`'s own docblock:
 * changing `slotMinutes` must require no other edit anywhere. That is a promise
 * about the *derived* helpers, so most of these tests drive them with an
 * explicit config rather than the module default — a regression that hardcodes
 * a 30-minute assumption somewhere in the arithmetic then fails here rather
 * than surfacing as a subtly wrong grid in task 1.6.
 */

import { describe, expect, it } from 'vitest'

import {
  assertConfigIsCoherent,
  availabilityMinutesFor,
  calendarConfig,
  formatSlotLabel,
  slotStart,
  slotStartMinutes,
  slotsPerDay,
  slotsPerDayFor,
  type CalendarConfig,
} from './config'

/** The shipped defaults, restated so a change to them fails loudly here. */
const DEFAULT: CalendarConfig = { slotMinutes: 30, openHour: 6, closeHour: 23 }

/** The same day, at a finer granularity — the documented future change. */
const TEN_MINUTE: CalendarConfig = { ...DEFAULT, slotMinutes: 10 }

describe('the shipped defaults', () => {
  it('are 30-minute slots from 06:00 to 23:00', () => {
    expect(calendarConfig).toEqual(DEFAULT)
  })

  it('are coherent', () => {
    expect(() => assertConfigIsCoherent()).not.toThrow()
  })

  it('derives 34 slots per day', () => {
    // 17 hours / 30 min. Stated as a literal rather than recomputed, so an
    // arithmetic regression cannot agree with itself and pass.
    expect(slotsPerDay).toBe(34)
  })
})

describe('slot arithmetic at 30 minutes', () => {
  it('starts the first slot at the opening hour', () => {
    expect(formatSlotLabel(0, DEFAULT)).toBe('06:00')
  })

  it('ends the last slot one slot before closing', () => {
    expect(formatSlotLabel(slotsPerDayFor(DEFAULT) - 1, DEFAULT)).toBe('22:30')
  })

  it('advances by the slot size', () => {
    expect(formatSlotLabel(1, DEFAULT)).toBe('06:30')
    expect(formatSlotLabel(2, DEFAULT)).toBe('07:00')
  })

  it('counts minutes from midnight, not from opening', () => {
    expect(slotStartMinutes(0, DEFAULT)).toBe(6 * 60)
  })
})

describe('the config pivot: 30 to 10 minutes changes nothing but the config', () => {
  // This is the requirement in the plan's verification section, enforced here
  // rather than by a manual check, so it cannot quietly regress.

  it('triples the slot count', () => {
    expect(slotsPerDayFor(TEN_MINUTE)).toBe(102)
  })

  it('still opens at 06:00', () => {
    expect(formatSlotLabel(0, TEN_MINUTE)).toBe('06:00')
  })

  it('advances in 10-minute steps', () => {
    expect(formatSlotLabel(1, TEN_MINUTE)).toBe('06:10')
    expect(formatSlotLabel(6, TEN_MINUTE)).toBe('07:00')
  })

  it('ends the last slot at 22:50, one slot before closing', () => {
    expect(formatSlotLabel(slotsPerDayFor(TEN_MINUTE) - 1, TEN_MINUTE)).toBe('22:50')
  })

  it('covers exactly the same window as the 30-minute config', () => {
    expect(availabilityMinutesFor(TEN_MINUTE)).toBe(availabilityMinutesFor(DEFAULT))
  })

  it('lands the final slot flush against closing time', () => {
    // The real invariant behind "the grid stops at closeHour": the last slot's
    // start plus one slot length must equal closing exactly, at any granularity.
    for (const config of [DEFAULT, TEN_MINUTE, { ...DEFAULT, slotMinutes: 15 }]) {
      const lastStart = slotStartMinutes(slotsPerDayFor(config) - 1, config)
      expect(lastStart + config.slotMinutes).toBe(config.closeHour * 60)
    }
  })
})

describe('slotStart', () => {
  it('places a slot on the given calendar date at local wall-clock time', () => {
    const slot = slotStart(new Date(2026, 6, 20, 17, 45), 0, DEFAULT)
    expect(slot.getFullYear()).toBe(2026)
    expect(slot.getMonth()).toBe(6)
    expect(slot.getDate()).toBe(20)
    expect(slot.getHours()).toBe(6)
    expect(slot.getMinutes()).toBe(0)
  })

  it('discards the time component of the day it is given', () => {
    const morning = slotStart(new Date(2026, 6, 20, 1, 0), 3, DEFAULT)
    const evening = slotStart(new Date(2026, 6, 20, 23, 59), 3, DEFAULT)
    expect(morning.getTime()).toBe(evening.getTime())
  })

  it('zeroes seconds and milliseconds so slot identity compares cleanly', () => {
    const slot = slotStart(new Date(2026, 6, 20, 9, 30, 44, 512), 5, DEFAULT)
    expect(slot.getSeconds()).toBe(0)
    expect(slot.getMilliseconds()).toBe(0)
  })
})

describe('assertConfigIsCoherent', () => {
  it('rejects a slot size that does not divide the window evenly', () => {
    // 17 hours is 1020 minutes; 1020 % 45 is 30, so the last slot would be
    // truncated and the grid would silently stop short of closing time.
    expect(() => assertConfigIsCoherent({ ...DEFAULT, slotMinutes: 45 })).toThrow(
      /must divide the 1020-minute availability window evenly/,
    )
  })

  it.each([30, 10, 15, 20, 60])('accepts %i-minute slots, which divide evenly', (slotMinutes) => {
    expect(() => assertConfigIsCoherent({ ...DEFAULT, slotMinutes })).not.toThrow()
  })

  it('rejects a non-positive slot size', () => {
    expect(() => assertConfigIsCoherent({ ...DEFAULT, slotMinutes: 0 })).toThrow(/must be positive/)
    expect(() => assertConfigIsCoherent({ ...DEFAULT, slotMinutes: -30 })).toThrow(
      /must be positive/,
    )
  })

  it('rejects a closing hour at or before the opening hour', () => {
    expect(() => assertConfigIsCoherent({ ...DEFAULT, closeHour: 6 })).toThrow(/must be after/)
    expect(() => assertConfigIsCoherent({ ...DEFAULT, closeHour: 5 })).toThrow(/must be after/)
  })
})
