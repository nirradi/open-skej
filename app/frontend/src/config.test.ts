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
  buildWeekSchedule,
  calendarConfig,
  calendarConfigForDay,
  formatSlotLabel,
  isSlotOutOfHours,
  slotStart,
  slotStartMinutes,
  slotsPerDay,
  slotsPerDayFor,
  uniformWeekSchedule,
  type CalendarConfig,
} from './config'
import { SYSTEM_TIME_ZONE, zonedParts } from './timezone'

// Every fixture below that does not test cross-zone behaviour explicitly
// pins `timeZone` to the environment's own zone, so a bare `.getHours()` /
// `.getMinutes()` read on a `slotStart` result still means what it always
// meant here: this suite runs correctly under any system zone, not just one
// CI happens to pin.
const SYSTEM_TZ = SYSTEM_TIME_ZONE

/** No hours restriction, 30-minute slots — the shipped default. */
const DEFAULT: CalendarConfig = {
  slotMinutes: 30,
  openMinutes: null,
  closeMinutes: null,
  timeZone: SYSTEM_TZ,
  minDurationMinutes: null,
}

/** The same slot size, with hours matching the grid's old hardcoded window. */
const NINE_TO_FIVE: CalendarConfig = {
  slotMinutes: 30,
  openMinutes: 9 * 60,
  closeMinutes: 17 * 60,
  timeZone: SYSTEM_TZ,
  minDurationMinutes: null,
}

/** The same day, at a finer granularity — the documented future change. */
const TEN_MINUTE: CalendarConfig = { ...DEFAULT, slotMinutes: 10 }

describe('the shipped defaults', () => {
  it('are 30-minute slots with no hours restriction', () => {
    expect(calendarConfig).toEqual(DEFAULT)
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
  // `DEFAULT.timeZone` is pinned to the environment's own zone, so a plain
  // local `.getHours()` / `.getMinutes()` read on the result means what it
  // says here — the cross-zone claim (a Space's own zone, not the
  // environment's) gets its own describe block below.
  it('places a slot on the given calendar date at the Space\'s wall-clock time', () => {
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

describe('slotStart resolves through the Space\'s own timezone, not the viewer\'s', () => {
  // The DEFERRED.md item 19 repro: a Space at 13:00 in `Europe/Berlin`
  // (UTC+2 in July) must resolve to 11:00Z regardless of what zone this
  // suite happens to run in — never a fixed offset computed once.
  it('resolves the exact item 19 repro: Berlin 13:00 in July is 11:00Z', () => {
    const berlin: CalendarConfig = { ...DEFAULT, timeZone: 'Europe/Berlin' }
    const slot = slotStart(new Date(2026, 7, 3), 26 /* 13:00 at 30-minute slots */, berlin)
    expect(slot.toISOString()).toBe('2026-08-03T11:00:00.000Z')
  })

  // "Both directions" — a viewer's own zone ahead of the venue's, and one
  // behind it. Neither changes what gets booked (the venue's zone is what
  // `slotStart` resolves through); reading the *same* resulting instant back
  // in each viewer zone is what proves the two are honestly different clocks
  // on one real instant, not two different bookings.
  it('reads the same instant differently for a viewer ahead of the venue', () => {
    const berlin: CalendarConfig = { ...DEFAULT, timeZone: 'Europe/Berlin' }
    const slot = slotStart(new Date(2026, 7, 3), 26, berlin) // 13:00 Berlin
    expect(zonedParts(slot, 'Europe/Berlin')).toMatchObject({ hour: 13, minute: 0 })
    // Asia/Jerusalem is UTC+3 in August, an hour ahead of Berlin's UTC+2.
    expect(zonedParts(slot, 'Asia/Jerusalem')).toMatchObject({ hour: 14, minute: 0 })
  })

  it('reads the same instant differently for a viewer behind the venue', () => {
    const berlin: CalendarConfig = { ...DEFAULT, timeZone: 'Europe/Berlin' }
    const slot = slotStart(new Date(2026, 7, 3), 26, berlin) // 13:00 Berlin, 11:00Z
    // Pacific/Honolulu is UTC-10 year-round.
    expect(zonedParts(slot, 'Pacific/Honolulu')).toMatchObject({
      year: 2026,
      month: 7,
      day: 3,
      hour: 1,
      minute: 0,
    })
  })

  it('resolves a venue behind UTC correctly too — not just ahead', () => {
    const honolulu: CalendarConfig = { ...DEFAULT, timeZone: 'Pacific/Honolulu' }
    const slot = slotStart(new Date(2026, 7, 3), 16 /* 08:00 */, honolulu)
    expect(slot.toISOString()).toBe('2026-08-03T18:00:00.000Z')
  })

  it('resolves the identical wall-clock label to different instants across a DST boundary', () => {
    // Mirrors `rules_stub.py`'s own docstring example: 07:00
    // Europe/Berlin is 05:00Z in July (CEST, UTC+2) and 06:00Z in January
    // (CET, UTC+1) — the same config, only the date changes.
    const berlin: CalendarConfig = { ...DEFAULT, timeZone: 'Europe/Berlin' }
    const july = slotStart(new Date(2026, 6, 20), 14 /* 07:00 */, berlin)
    const january = slotStart(new Date(2026, 0, 20), 14, berlin)
    expect(july.toISOString()).toBe('2026-07-20T05:00:00.000Z')
    expect(january.toISOString()).toBe('2026-01-20T06:00:00.000Z')
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
    const config = { ...DEFAULT, openMinutes: 9 * 60 + 15, closeMinutes: 17 * 60 }
    expect(isSlotOutOfHours(18, config)).toBe(true)
    expect(isSlotOutOfHours(19, config)).toBe(false)
  })
})

describe('minDurationMinutes parsing and defaults', () => {
  it('is null on the shipped default config', () => {
    expect(calendarConfig.minDurationMinutes).toBeNull()
  })

  it('parses a resolved minimum duration from the wire shape', () => {
    const schedule = buildWeekSchedule(
      [
        {
          date: '2026-07-20',
          slot_minutes: 30,
          opens_at: null,
          closes_at: null,
          coherence_issue: null,
          min_duration_minutes: 60,
        },
      ],
      SYSTEM_TZ,
    )
    expect(schedule.forDate('2026-07-20').minDurationMinutes).toBe(60)
    expect(calendarConfigForDay(schedule, '2026-07-20').minDurationMinutes).toBe(60)
  })

  it('defaults an unenforced minimum duration to null, not an invented floor', () => {
    const schedule = buildWeekSchedule(
      [
        {
          date: '2026-07-20',
          slot_minutes: 30,
          opens_at: null,
          closes_at: null,
          coherence_issue: null,
          min_duration_minutes: null,
        },
      ],
      SYSTEM_TZ,
    )
    expect(schedule.forDate('2026-07-20').minDurationMinutes).toBeNull()
  })

  it('resolves a date this WeekSchedule was never built for to null, the shipped default', () => {
    const schedule = buildWeekSchedule([], SYSTEM_TZ)
    expect(schedule.forDate('2026-07-20').minDurationMinutes).toBeNull()
  })

  it('uniformWeekSchedule always resolves to no minimum, regardless of the config it is built from', () => {
    const withMinimum: CalendarConfig = { ...DEFAULT, minDurationMinutes: 60 }
    const schedule = uniformWeekSchedule(withMinimum)
    expect(schedule.forDate('2026-07-20').minDurationMinutes).toBeNull()
  })
})

