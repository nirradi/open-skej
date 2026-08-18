/**
 * Tests for selection arithmetic over the shape projection's own offered
 * starts.
 *
 * The claims worth defending: a click takes the smallest duration offered at
 * a start; a drag takes the largest duration that both fits under the drag
 * head and clears obstructions, direction-agnostic exactly like the old
 * index-based `rangeBetween`; and a start not in `offered_starts` yields
 * `null` rather than inventing a selection the projection never offered.
 */

import { describe, expect, it } from 'vitest'

import { buildWeekProjection } from './shape'
import { clickSelection, dragSelection, isStartInSelection } from './selection'

/** Every candidate span free — the plain case. */
const allFree = () => true

/** Teacher example: 18:00-20:00, 20-minute slots, a 19:30-19:40 "Break". */
const TEACHER_DAY = buildWeekProjection(
  [
    {
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
    },
  ],
  'Europe/Berlin',
).forDate('2026-08-18')

/** Music room example: 08:00-14:00 [60], 14:00-22:00 [60, 120]. */
const MUSIC_ROOM_DAY = buildWeekProjection(
  [
    {
      date: '2026-08-18',
      operating_intervals: [
        { start_minutes: 8 * 60, end_minutes: 14 * 60, allowed_durations_mins: [60] },
        { start_minutes: 14 * 60, end_minutes: 22 * 60, allowed_durations_mins: [60, 120] },
      ],
      blackout_intervals: [],
      offered_starts: [
        ...Array.from({ length: 6 }, (_, i) => ({
          start_minutes: (8 + i) * 60,
          durations_mins: [60],
        })),
        ...Array.from({ length: 8 }, (_, i) => ({
          start_minutes: (14 + i) * 60,
          durations_mins: [60, 120],
        })),
      ],
      bookable: true,
    },
  ],
  'Europe/Berlin',
).forDate('2026-08-18')

describe('clickSelection', () => {
  it('takes the smallest duration offered at the start', () => {
    expect(clickSelection(MUSIC_ROOM_DAY, 18 * 60)).toEqual({
      dateKey: '2026-08-18',
      startMinutes: 18 * 60,
      durationMinutes: 60,
    })
  })

  it('returns null for a start not in offered_starts', () => {
    // 19:20 — the teacher's grid never offers this start, since 19:20+20
    // would straddle the 19:30 blackout.
    expect(clickSelection(TEACHER_DAY, 19 * 60 + 20)).toBeNull()
  })
})

describe('dragSelection', () => {
  it('stretches to the largest duration offered that fits under the drag head', () => {
    // Anchor 18:00, head 19:00 in the evening block — 120 fits exactly
    // (18:00-20:00), matching the head's own 19:00-20:00 end.
    const selection = dragSelection(MUSIC_ROOM_DAY, 18 * 60, 19 * 60, allFree)
    expect(selection).toEqual({ dateKey: '2026-08-18', startMinutes: 18 * 60, durationMinutes: 120 })
  })

  it('never exceeds 60 in the morning block, since only 60 is ever offered there', () => {
    const selection = dragSelection(MUSIC_ROOM_DAY, 8 * 60, 11 * 60, allFree)
    expect(selection).toEqual({ dateKey: '2026-08-18', startMinutes: 8 * 60, durationMinutes: 60 })
  })

  it('is direction-agnostic: dragging backwards from the anchor produces the same span', () => {
    const forward = dragSelection(MUSIC_ROOM_DAY, 18 * 60, 19 * 60, allFree)
    const backward = dragSelection(MUSIC_ROOM_DAY, 19 * 60, 18 * 60, allFree)
    expect(backward).toEqual(forward)
  })

  it('falls back to the click selection when no wider duration clears an obstruction', () => {
    // Everything from 19:00 onward is obstructed, so only the anchor's own
    // 60-minute click unit (18:00-19:00) survives.
    const isFree = (_start: number, end: number) => end <= 19 * 60
    const selection = dragSelection(MUSIC_ROOM_DAY, 18 * 60, 20 * 60, isFree)
    expect(selection).toEqual({ dateKey: '2026-08-18', startMinutes: 18 * 60, durationMinutes: 60 })
  })

  it('cannot be built across a blackout: the grid never offers a straddling start at all', () => {
    // Dragging from 18:40 toward 19:40 in the teacher's example: nothing
    // wider than the anchor's own 20-minute click unit is offered, because
    // 19:20 (which would need to exist to widen through) was never offered.
    const selection = dragSelection(TEACHER_DAY, 18 * 60 + 40, 19 * 60 + 40, allFree)
    expect(selection).toEqual({
      dateKey: '2026-08-18',
      startMinutes: 18 * 60 + 40,
      durationMinutes: 20,
    })
  })

  it('returns null when the earlier start is not offered at all', () => {
    expect(dragSelection(TEACHER_DAY, 19 * 60 + 20, 19 * 60 + 40, allFree)).toBeNull()
  })
})

describe('isStartInSelection', () => {
  const selection = { dateKey: '2026-08-18', startMinutes: 18 * 60, durationMinutes: 120 }

  it('includes the start itself and every start within the span', () => {
    expect(isStartInSelection(selection, '2026-08-18', 18 * 60)).toBe(true)
    expect(isStartInSelection(selection, '2026-08-18', 19 * 60)).toBe(true)
  })

  it('excludes the start exactly at the end of the span', () => {
    expect(isStartInSelection(selection, '2026-08-18', 20 * 60)).toBe(false)
  })

  it('excludes a start before the span', () => {
    expect(isStartInSelection(selection, '2026-08-18', 17 * 60)).toBe(false)
  })

  it('excludes the same minute on another day', () => {
    expect(isStartInSelection(selection, '2026-08-19', 19 * 60)).toBe(false)
  })

  it('is false when nothing is selected', () => {
    expect(isStartInSelection(null, '2026-08-18', 19 * 60)).toBe(false)
  })
})
