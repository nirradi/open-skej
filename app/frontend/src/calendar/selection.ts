/**
 * Selection arithmetic over the shape projection's own offered starts.
 *
 * A selection used to be a contiguous run of indices into a *uniform* slot
 * grid, so a click-and-drag was index arithmetic (`rangeBetween`) and its
 * length fell out of the two endpoints (`rangeLength`). That grid does not
 * exist any more. A day's `offered_starts` (`calendar/shape.ts`) is a table
 * of independent local minutes, each with its own set of offered durations —
 * two operating blocks with different anchors (the music-room example: a
 * 60-minute morning grid and a {60, 120}-minute evening one) put two starts
 * closer together than either one's own shortest booking, so there is no
 * single "index spacing" a range could walk. A selection is therefore
 * resolved directly against `offered_starts`, never against a position in an
 * array: **one offered start plus one duration offered at that start**, and
 * nothing else is constructable.
 */

import { durationsAt, smallestDurationAt, type DayProjection } from './shape'

/** One offered start, paired with one duration offered there — nothing else is buildable. */
export interface Selection {
  /** `toDateKey` of the day the selection lives on. */
  dateKey: string
  startMinutes: number
  durationMinutes: number
}

/**
 * The selection a plain click on `startMinutes` proposes: the **smallest**
 * duration offered there, or `null` if that start is not offered at all.
 *
 * The smallest duration is the honest "one unit" a single click can offer:
 * one click proposes one booking of the shortest length this venue sells at
 * that time, and a start offering several durations is widened by dragging,
 * never by guessing which of them the click meant.
 */
export function clickSelection(day: DayProjection, startMinutes: number): Selection | null {
  const duration = smallestDurationAt(day, startMinutes)
  if (duration === null) return null
  return { dateKey: day.dateKey, startMinutes, durationMinutes: duration }
}

/**
 * The selection a drag between `anchorStartMinutes` and `headStartMinutes`
 * proposes.
 *
 * Direction-agnostic: the earlier of the two starts is always where the
 * resulting selection begins, so dragging backwards from the anchor works
 * exactly as dragging forwards does, matching the old `rangeBetween`.
 *
 * The candidate duration is the **largest** one offered at the earlier start
 * that both (a) ends no later than the drag head's own end — `headStartMinutes`
 * plus the smallest duration offered *there* — and (b) is free of obstruction
 * across its whole span, per the caller's `isFree` predicate (bookings, past,
 * horizon). Falls back to `clickSelection` at the earlier start when no wider
 * duration clears both checks, so a drag can never propose *less* than a plain
 * click at the same start would.
 */
export function dragSelection(
  day: DayProjection,
  anchorStartMinutes: number,
  headStartMinutes: number,
  isFree: (startMinutes: number, endMinutes: number) => boolean,
): Selection | null {
  const earlier = Math.min(anchorStartMinutes, headStartMinutes)
  const later = Math.max(anchorStartMinutes, headStartMinutes)

  const headDuration = smallestDurationAt(day, later)
  if (headDuration === null) return clickSelection(day, earlier)
  const headEnd = later + headDuration

  const durations = durationsAt(day, earlier)
  let best: number | null = null
  for (const duration of durations) {
    const end = earlier + duration
    if (end > headEnd) continue
    if (!isFree(earlier, end)) continue
    if (best === null || duration > best) best = duration
  }

  if (best === null) return clickSelection(day, earlier)
  return { dateKey: day.dateKey, startMinutes: earlier, durationMinutes: best }
}

/** Whether `startMinutes` on `dateKey` falls inside `selection`'s span. */
export function isStartInSelection(
  selection: Selection | null,
  dateKey: string,
  startMinutes: number,
): boolean {
  if (selection === null || selection.dateKey !== dateKey) return false
  return (
    startMinutes >= selection.startMinutes &&
    startMinutes < selection.startMinutes + selection.durationMinutes
  )
}
