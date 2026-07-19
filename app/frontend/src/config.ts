/**
 * The single source of truth for calendar configuration.
 *
 * Everything about how the grid is laid out derives from the three raw values in
 * `calendarConfig`. Changing `slotMinutes` from 30 to 10 must require no other
 * edit anywhere in the codebase — if a component ever hardcodes a slot count, a
 * row height per half-hour, or an opening hour, that is a bug in the component,
 * not something to fix by adding a second config value here.
 *
 * ## Keep in sync with the backend
 *
 * `openHour` and `closeHour` mirror `AVAILABILITY_OPEN` / `AVAILABILITY_CLOSE`
 * in `app/backend/app/rules_stub.py`. **The backend is authoritative**: it
 * re-evaluates every booking against its own values and returns a `rule_denied`
 * response for anything outside them, regardless of what this file says. Widening
 * the window here without widening it there produces a grid that renders bookable
 * slots the server will refuse — the denial copy will still be correct and
 * friendly, but the user was invited to click something that could never work.
 *
 * Stream 3 replaces the stub with per-Space rules served from the API, at which
 * point these become defaults fetched at runtime rather than compile-time
 * constants.
 */

/** Minutes in an hour — named so the arithmetic below reads as intent, not magic. */
const MINUTES_PER_HOUR = 60

export interface CalendarConfig {
  /**
   * Selection granularity of the grid, in minutes. Governs how finely a user can
   * pick a range — *not* how long a booking may be. Bookings are variable length;
   * the backend's max-duration rule is what bounds them.
   *
   * Must divide the availability window evenly (see `assertConfigIsCoherent`).
   */
  slotMinutes: number
  /** First bookable hour of the day, local wall-clock, inclusive. */
  openHour: number
  /** Last bookable hour, local wall-clock. A booking may end exactly at this hour. */
  closeHour: number
}

export const calendarConfig: CalendarConfig = {
  slotMinutes: 30,
  openHour: 6,
  closeHour: 23,
}

/** Length of the bookable day in minutes. */
export const availabilityMinutes =
  (calendarConfig.closeHour - calendarConfig.openHour) * MINUTES_PER_HOUR

/** How many slot rows the grid renders per day. */
export const slotsPerDay = Math.floor(availabilityMinutes / calendarConfig.slotMinutes)

/**
 * Minutes from midnight to the start of slot `index` (0-based).
 *
 * The grid's row-to-time mapping lives here rather than in the component so that
 * a slot's identity is derived the same way everywhere it is computed.
 */
export function slotStartMinutes(index: number): number {
  return calendarConfig.openHour * MINUTES_PER_HOUR + index * calendarConfig.slotMinutes
}

/**
 * The local-time `Date` at which slot `index` starts on the given day.
 *
 * `day` contributes only its calendar date; its time component is discarded.
 * Constructed via the `Date` constructor rather than by adding milliseconds so
 * that a slot lands on the intended wall-clock time across a DST boundary.
 */
export function slotStart(day: Date, index: number): Date {
  const minutes = slotStartMinutes(index)
  return new Date(
    day.getFullYear(),
    day.getMonth(),
    day.getDate(),
    Math.floor(minutes / MINUTES_PER_HOUR),
    minutes % MINUTES_PER_HOUR,
    0,
    0,
  )
}

/** Formats a slot index as `HH:MM` for axis labels. */
export function formatSlotLabel(index: number): string {
  const minutes = slotStartMinutes(index)
  const hh = String(Math.floor(minutes / MINUTES_PER_HOUR)).padStart(2, '0')
  const mm = String(minutes % MINUTES_PER_HOUR).padStart(2, '0')
  return `${hh}:${mm}`
}

/**
 * Fails loudly on a configuration that cannot render a correct grid.
 *
 * A `slotMinutes` that does not divide the window evenly (e.g. 45 across 17
 * hours) would silently truncate the last partial slot, so the grid would stop
 * short of `closeHour` and quietly hide bookable time. Better to refuse to boot.
 */
export function assertConfigIsCoherent(config: CalendarConfig = calendarConfig): void {
  if (config.slotMinutes <= 0) {
    throw new Error(`slotMinutes must be positive, got ${config.slotMinutes}`)
  }
  if (config.closeHour <= config.openHour) {
    throw new Error(`closeHour (${config.closeHour}) must be after openHour (${config.openHour})`)
  }
  const windowMinutes = (config.closeHour - config.openHour) * MINUTES_PER_HOUR
  if (windowMinutes % config.slotMinutes !== 0) {
    throw new Error(
      `slotMinutes (${config.slotMinutes}) must divide the ${windowMinutes}-minute ` +
        `availability window evenly`,
    )
  }
}
