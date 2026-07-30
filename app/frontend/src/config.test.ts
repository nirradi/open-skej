/**
 * Tests for the calendar configuration module.
 *
 * The central claim this file defends is the one in `config.ts`'s own docblock:
 * changing `slotMinutes` must require no other edit anywhere. That is a promise
 * about the *derived* helpers, so most of these tests drive them with an
 * explicit config rather than the module default — a regression that hardcodes
 * a 30-minute assumption somewhere in the arithmetic then fails here rather
 * than surfacing as a subtly wrong grid in task 1.6.
 *
 * The grid always spans the full day now (task 5.9): slot 0 is midnight
 * regardless of a Space's own hours, and `openMinutes` / `closeMinutes` only
 * decide what `isSlotOutOfHours` greys.
 */

import { describe, expect, it } from 'vitest'

import {
  buildCalendarConfig,
  calendarConfig,
  coherenceIssue,
  formatSlotLabel,
  isSlotOutOfHours,
  slotStart,
  slotStartMinutes,
  slotsPerDay,
  slotsPerDayFor,
  type CalendarConfig,
} from './config'

/** No hours restriction, 30-minute slots — the shipped default. */
const DEFAULT: CalendarConfig = { slotMinutes: 30, openMinutes: null, closeMinutes: null }

/** The same slot size, with hours matching the grid's old hardcoded window. */
const NINE_TO_FIVE: CalendarConfig = { slotMinutes: 30, openMinutes: 9 * 60, closeMinutes: 17 * 60 }

/** The same day, at a finer granularity — the documented future change. */
const TEN_MINUTE: CalendarConfig = { ...DEFAULT, slotMinutes: 10 }

describe('the shipped defaults', () => {
  it('are 30-minute slots with no hours restriction', () => {
    expect(calendarConfig).toEqual(DEFAULT)
  })

  it('are coherent', () => {
    expect(coherenceIssue(calendarConfig)).toBeNull()
  })

  it('render 48 slots per day', () => {
    // 24 hours / 30 min. Stated as a literal rather than recomputed, so an
    // arithmetic regression cannot agree with itself and pass.
    expect(slotsPerDay).toBe(48)
  })
})

describe('slot arithmetic at 30 minutes', () => {
  it('starts the first slot at midnight', () => {
    expect(formatSlotLabel(0, DEFAULT)).toBe('00:00')
  })

  it('ends the last slot one slot before the next midnight', () => {
    expect(formatSlotLabel(slotsPerDayFor(DEFAULT) - 1, DEFAULT)).toBe('23:30')
  })

  it('advances by the slot size', () => {
    expect(formatSlotLabel(1, DEFAULT)).toBe('00:30')
    expect(formatSlotLabel(2, DEFAULT)).toBe('01:00')
  })

  it('counts minutes from midnight', () => {
    expect(slotStartMinutes(12, DEFAULT)).toBe(6 * 60)
  })
})

describe('the config pivot: 30 to 10 minutes changes nothing but the config', () => {
  // This is the requirement in the plan's verification section, enforced here
  // rather than by a manual check, so it cannot quietly regress.

  it('triples the slot count', () => {
    expect(slotsPerDayFor(TEN_MINUTE)).toBe(144)
  })

  it('still starts at midnight', () => {
    expect(formatSlotLabel(0, TEN_MINUTE)).toBe('00:00')
  })

  it('advances in 10-minute steps', () => {
    expect(formatSlotLabel(1, TEN_MINUTE)).toBe('00:10')
    expect(formatSlotLabel(6, TEN_MINUTE)).toBe('01:00')
  })

  it('ends the last slot at 23:50, one slot before midnight', () => {
    expect(formatSlotLabel(slotsPerDayFor(TEN_MINUTE) - 1, TEN_MINUTE)).toBe('23:50')
  })

  it('lands the final slot flush against midnight', () => {
    // The real invariant behind "the grid spans the full day": the last
    // slot's start plus one slot length must equal 24:00 exactly, at any
    // granularity.
    for (const config of [DEFAULT, TEN_MINUTE, { ...DEFAULT, slotMinutes: 15 }]) {
      const lastStart = slotStartMinutes(slotsPerDayFor(config) - 1, config)
      expect(lastStart + config.slotMinutes).toBe(24 * 60)
    }
  })
})

describe('slotStart', () => {
  it('places a slot on the given calendar date at local wall-clock time', () => {
    const slot = slotStart(new Date(2026, 6, 20, 17, 45), 12, DEFAULT)
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

describe('isSlotOutOfHours', () => {
  it('marks nothing out of hours when neither bound is set', () => {
    expect(isSlotOutOfHours(0, DEFAULT)).toBe(false)
    expect(isSlotOutOfHours(47, DEFAULT)).toBe(false)
  })

  it('greys everything before opening', () => {
    // 09:00 is slot 18 at 30-minute granularity.
    expect(isSlotOutOfHours(17, NINE_TO_FIVE)).toBe(true)
    expect(isSlotOutOfHours(18, NINE_TO_FIVE)).toBe(false)
  })

  it('greys everything from closing onward', () => {
    // 17:00 is slot 34; the slot starting there is already closed, matching
    // "a booking may end exactly at closing" — nothing may start there.
    expect(isSlotOutOfHours(33, NINE_TO_FIVE)).toBe(false)
    expect(isSlotOutOfHours(34, NINE_TO_FIVE)).toBe(true)
  })

  it('greys a slot that only partially overlaps the open window', () => {
    // Opens at 09:15: the 09:00-09:30 slot contains a minute the backend
    // would refuse, so the whole slot reads as out-of-hours — the grid must
    // never offer what the server will refuse.
    const config = { slotMinutes: 30, openMinutes: 9 * 60 + 15, closeMinutes: 17 * 60 }
    expect(isSlotOutOfHours(18, config)).toBe(true)
    expect(isSlotOutOfHours(19, config)).toBe(false)
  })
})

describe('coherenceIssue', () => {
  it('accepts the shipped default and a bounded window that aligns to slots', () => {
    expect(coherenceIssue(DEFAULT)).toBeNull()
    expect(coherenceIssue(NINE_TO_FIVE)).toBeNull()
  })

  it('rejects a non-positive slot size', () => {
    expect(coherenceIssue({ ...DEFAULT, slotMinutes: 0 })).toMatch(/must be positive/)
    expect(coherenceIssue({ ...DEFAULT, slotMinutes: -30 })).toMatch(/must be positive/)
  })

  it('rejects a slot size that does not divide a day evenly', () => {
    // 1440 minutes in a day; 1440 % 13 is 10, so the day cannot tile in
    // 13-minute slots without a truncated remainder.
    expect(coherenceIssue({ ...DEFAULT, slotMinutes: 13 })).toMatch(/must divide a day evenly/)
  })

  it.each([30, 10, 15, 20, 60, 45])('accepts %i-minute slots, which divide a day evenly', (slotMinutes) => {
    expect(coherenceIssue({ ...DEFAULT, slotMinutes })).toBeNull()
  })

  it('rejects an opening time that does not land on a slot boundary', () => {
    expect(
      coherenceIssue({ slotMinutes: 30, openMinutes: 9 * 60 + 15, closeMinutes: 17 * 60 }),
    ).toMatch(/Opening time must land/)
  })

  it('rejects a closing time that does not land on a slot boundary', () => {
    expect(
      coherenceIssue({ slotMinutes: 30, openMinutes: 9 * 60, closeMinutes: 17 * 60 + 15 }),
    ).toMatch(/Closing time must land/)
  })

  it('rejects a closing time at or before the opening time', () => {
    // DEFERRED.md item 17, seen from this frontend's browser-local model: a
    // window that cannot resolve to a real span at all.
    expect(
      coherenceIssue({ slotMinutes: 30, openMinutes: 9 * 60, closeMinutes: 9 * 60 }),
    ).toMatch(/Closing time must be after/)
    expect(
      coherenceIssue({ slotMinutes: 30, openMinutes: 9 * 60, closeMinutes: 8 * 60 }),
    ).toMatch(/Closing time must be after/)
  })
})

describe('buildCalendarConfig', () => {
  it('builds a config from a Space with hours set', () => {
    const result = buildCalendarConfig({
      slot_minutes: 30,
      opens_at: '09:00:00',
      closes_at: '17:00:00',
    })
    expect(result).toEqual({
      status: 'ok',
      config: { slotMinutes: 30, openMinutes: 9 * 60, closeMinutes: 17 * 60 },
    })
  })

  it('renders the whole day bookable for a Space with hours unset — never the old default window', () => {
    const result = buildCalendarConfig({ slot_minutes: 30, opens_at: null, closes_at: null })
    expect(result).toEqual({
      status: 'ok',
      config: { slotMinutes: 30, openMinutes: null, closeMinutes: null },
    })
  })

  it('falls back to the shipped slot size when slot_minutes is unset', () => {
    const result = buildCalendarConfig({
      slot_minutes: null,
      opens_at: '09:00:00',
      closes_at: '17:00:00',
    })
    expect(result.status).toBe('ok')
    expect(result.status === 'ok' && result.config.slotMinutes).toBe(calendarConfig.slotMinutes)
  })

  it('degrades to a notice, never a throw, for a slot size that cannot tile the day', () => {
    const result = buildCalendarConfig({ slot_minutes: 13, opens_at: null, closes_at: null })
    expect(result.status).toBe('incoherent')
    expect(result.status === 'incoherent' && result.message).toMatch(/must divide a day evenly/)
  })

  it('degrades to a notice for hours that cannot resolve to a real window', () => {
    // The DEFERRED.md item 17 shape: an admin-configured Space whose hours
    // are unusable. 5.9 does not repair this (that is engine work, out of
    // scope) — it must not white-screen on it either.
    const result = buildCalendarConfig({
      slot_minutes: 30,
      opens_at: '21:00:00',
      closes_at: '09:00:00',
    })
    expect(result.status).toBe('incoherent')
    expect(result.status === 'incoherent' && result.message).toMatch(/Closing time must be after/)
  })
})
