/**
 * The grid's pixel configuration and display formatting.
 *
 * **What the calendar is drawn FROM is the server's projection**
 * (`calendar/shape.ts`, task 10.3's `GET /spaces/{public_id}/calendar`), and
 * this module has no opinion about what is bookable. What lives here is only
 * the pixel arithmetic: how many pixels per minute, how to format a time label.
 * The grid's substrate — its operating intervals, blackouts, and offered starts
 * — is described by the shape document the server projected, never re-derived
 * here.
 *
 * ## Every clock in the grid is the Space's own
 *
 * Every time label and every instant a selection resolves to is in the Space's
 * own `timeZone`, never the viewer's. The conversion happens through
 * `timezone.ts`'s `zonedTimeToInstant`, which asks `Intl.DateTimeFormat` for
 * the zone's actual offset at that specific date rather than assuming one, so
 * the same wall-clock time resolves to a different UTC instant in July than in
 * January, correctly. Every member looking at a Space sees the identical grid,
 * in the identical clock, regardless of where they are; a viewer whose own zone
 * differs from the Space's sees that only as a secondary hint elsewhere in the
 * UI, never as a second version of the grid. A per-viewer clock was considered
 * and rejected — it would let two members read different times for the same
 * slot, and it makes the operating window wrap midnight for anyone far enough
 * from the venue.
 */

/** Minutes in an hour — named so the arithmetic below reads as intent, not magic. */
export const MINUTES_PER_HOUR = 60

/** Minutes in a day. The grid always spans it. */
export const MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR

/**
 * Height of a single slot row, in pixels.
 *
 * Per *minute*, via `pxPerMinuteFor` — the day's total pixel height is
 * `MINUTES_PER_DAY * pxPerMinute`. At a finer duration (smaller step) the day
 * is taller, which is the honest consequence of asking for more resolution.
 */
export const SLOT_ROW_HEIGHT_PX = 28

/**
 * The fallback step for a week offering nothing at all.
 *
 * Used by `finestDurationMinutes` (`calendar/shape.ts`) when no duration is
 * offered anywhere in the visible week — the pixel scale has to come from
 * somewhere, and this is what it was before task 10.4. A week with a real
 * shape renders at the finest duration that shape actually offers, not at this
 * default.
 */
export const DEFAULT_STEP_MINUTES = 60

/**
 * Pixels per minute, for the grid rendered at `finestDurationMinutes`.
 *
 * A week whose finest duration is 30 therefore renders exactly the height it
 * renders today. A finer week is taller — the honest consequence of asking for
 * more resolution, the same reasoning the original `SLOT_ROW_HEIGHT_PX`
 * docblock already gives.
 */
export function pxPerMinuteFor(finestDurationMinutes: number): number {
  return SLOT_ROW_HEIGHT_PX / finestDurationMinutes
}

/**
 * Formats local `minutes` (minutes since midnight) as `HH:MM`.
 *
 * Used by the hour axis and by every slot's `aria-label`. Forced to 24-hour
 * rather than left to the locale, so that every time in the UI reads the same
 * way.
 */
export function formatMinutesLabel(minutes: number): string {
  const hh = String(Math.floor(minutes / MINUTES_PER_HOUR)).padStart(2, '0')
  const mm = String(minutes % MINUTES_PER_HOUR).padStart(2, '0')
  return `${hh}:${mm}`
}
