/**
 * Drag-selection range arithmetic.
 *
 * A selection is an anchor (where the pointer went down) and a head (the slot
 * it is currently over), both within one day. Deriving the range from that pair
 * rather than accumulating slots as they are entered is what makes dragging
 * *backwards* work for free: the range is simply the span between the two, in
 * whichever order they happen to sit.
 */

/** A contiguous run of slot indices, inclusive at both ends. */
export interface SlotRange {
  /** Index of the first slot in the run. */
  start: number
  /** Index of the last slot in the run. */
  end: number
}

/** An in-progress or completed selection, pinned to one day. */
export interface Selection extends SlotRange {
  /** `toDateKey` of the day the selection lives on. */
  dateKey: string
}

/**
 * The clamped range between `anchor` and `head`.
 *
 * Walks outward from the anchor toward the head and stops *before* the first
 * slot `isSelectable` rejects, so a drag across a booked or past slot selects
 * the contiguous run up to the obstruction instead of swallowing it. Returns
 * `null` when the anchor itself is not selectable.
 *
 * Direction-agnostic by construction: `head < anchor` walks down, `head >
 * anchor` walks up, and both produce a range in ascending order.
 */
export function rangeBetween(
  anchor: number,
  head: number,
  isSelectable: (index: number) => boolean,
): SlotRange | null {
  if (!isSelectable(anchor)) return null

  const step = head >= anchor ? 1 : -1
  let last = anchor
  for (let i = anchor + step; step > 0 ? i <= head : i >= head; i += step) {
    if (!isSelectable(i)) break
    last = i
  }

  return { start: Math.min(anchor, last), end: Math.max(anchor, last) }
}

/** Whether `index` on `dateKey` falls inside `selection`. */
export function isInSelection(
  selection: Selection | null,
  dateKey: string,
  index: number,
): boolean {
  if (selection === null || selection.dateKey !== dateKey) return false
  return index >= selection.start && index <= selection.end
}

/** How many slots a range covers. */
export function rangeLength(range: SlotRange): number {
  return range.end - range.start + 1
}
