/**
 * Tests for drag-selection range arithmetic.
 *
 * The two claims worth defending: dragging backwards works exactly as well as
 * dragging forwards, and a drag stops at an obstruction rather than selecting
 * through it.
 */

import { describe, expect, it } from 'vitest'

import { isInSelection, rangeBetween, rangeLength } from './selection'

/** Every slot selectable — the plain case. */
const allFree = () => true

describe('rangeBetween', () => {
  it('selects a single slot when the head is the anchor', () => {
    expect(rangeBetween(4, 4, allFree)).toEqual({ start: 4, end: 4 })
  })

  it('selects downward', () => {
    expect(rangeBetween(4, 8, allFree)).toEqual({ start: 4, end: 8 })
  })

  it('selects upward', () => {
    // Dragging backwards is the case a "collect slots as they are entered"
    // implementation gets wrong; the range is normalised to ascending order.
    expect(rangeBetween(8, 4, allFree)).toEqual({ start: 4, end: 8 })
  })

  it('produces the same range in either direction', () => {
    expect(rangeBetween(2, 9, allFree)).toEqual(rangeBetween(9, 2, allFree))
  })

  it('returns null when the anchor itself is not selectable', () => {
    expect(rangeBetween(3, 6, () => false)).toBeNull()
  })

  it('stops before a blocked slot when dragging down', () => {
    const blockedAt7 = (i: number) => i !== 7
    expect(rangeBetween(4, 10, blockedAt7)).toEqual({ start: 4, end: 6 })
  })

  it('stops before a blocked slot when dragging up', () => {
    const blockedAt7 = (i: number) => i !== 7
    expect(rangeBetween(10, 4, blockedAt7)).toEqual({ start: 8, end: 10 })
  })

  it('does not reach past a blocked slot even when the far side is free', () => {
    // The negative control for the two above: without the early break the
    // range would run to 10, silently spanning a booked slot.
    const blockedAt7 = (i: number) => i !== 7
    const range = rangeBetween(4, 10, blockedAt7)
    expect(range).not.toBeNull()
    expect(range!.end).toBeLessThan(7)
  })

  it('collapses to the anchor when the very next slot is blocked', () => {
    expect(rangeBetween(4, 10, (i) => i === 4)).toEqual({ start: 4, end: 4 })
  })
})

describe('isInSelection', () => {
  const selection = { dateKey: '2026-07-20', start: 3, end: 5 }

  it('includes both endpoints', () => {
    expect(isInSelection(selection, '2026-07-20', 3)).toBe(true)
    expect(isInSelection(selection, '2026-07-20', 5)).toBe(true)
  })

  it('excludes slots outside the range', () => {
    expect(isInSelection(selection, '2026-07-20', 2)).toBe(false)
    expect(isInSelection(selection, '2026-07-20', 6)).toBe(false)
  })

  it('excludes the same indices on another day', () => {
    expect(isInSelection(selection, '2026-07-21', 4)).toBe(false)
  })

  it('is false when nothing is selected', () => {
    expect(isInSelection(null, '2026-07-20', 4)).toBe(false)
  })
})

describe('rangeLength', () => {
  it('counts inclusively', () => {
    expect(rangeLength({ start: 3, end: 3 })).toBe(1)
    expect(rangeLength({ start: 3, end: 5 })).toBe(3)
  })
})
