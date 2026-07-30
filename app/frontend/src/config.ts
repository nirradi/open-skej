/**
 * The single source of truth for calendar configuration.
 *
 * Everything about how the grid is laid out derives from `CalendarConfig`.
 * Changing `slotMinutes` from 30 to 10 must require no other edit anywhere in
 * the codebase — if a component ever hardcodes a slot count, a row height per
 * half-hour, or an opening hour, that is a bug in the component, not
 * something to fix by adding a second config value here.
 *
 * ## Built per Space, not a compile-time constant
 *
 * `slotMinutes` / `openMinutes` / `closeMinutes` used to mirror
 * `AVAILABILITY_OPEN` / `AVAILABILITY_CLOSE`, hardcoded constants in
 * `app/backend/app/rules_stub.py`. Those constants are gone: task 4.13a moved
 * operating hours and slot interval onto the Space, where an admin edits them
 * in `SpaceSchedulePanel`. `buildCalendarConfig` is what turns a Space's own
 * `slot_minutes` / `opens_at` / `closes_at` into a `CalendarConfig` at
 * runtime — `ResourceCalendarPage` calls it once the Space is fetched and
 * passes the result to `CalendarGrid`. **The backend is still authoritative**:
 * it re-evaluates every booking against that same Space's rule canon and
 * returns a `rule_denied` response for anything outside it, regardless of
 * what this file computes. A config built from stale or wrong Space data
 * produces a grid that renders bookable slots the server will refuse — the
 * denial copy will still be correct and friendly, but the user was invited to
 * click something that could never work.
 *
 * `calendarConfig`, the module-level default, survives only as the fallback
 * `CalendarGrid` uses when no `config` prop is given (tests, mostly — a real
 * page always builds one from its Space). It models a Space whose hours are
 * unset: `opens_at` / `closes_at` null means the availability-hours rule is
 * not enforced (`.claude/rules/identity-and-access.md`), so the honest
 * default is the *whole day* bookable, not the old hardcoded 06:00–23:00
 * window — inventing a window here would be a rule this file has no
 * authority to make up.
 *
 * ## The grid always renders the full day
 *
 * `slotsPerDayFor` spans midnight to midnight regardless of `openMinutes` /
 * `closeMinutes` — those two only decide which rows come back from
 * `isSlotOutOfHours` as greyed. Clipping the day to `[openMinutes,
 * closeMinutes)` was the previous shape, and it silently broke: a Space
 * closing at 18:00 rendered a grid that simply stopped, and a booking made
 * before the hours were narrowed had no row left to sit on and vanished from
 * a calendar it was still on. Rendering the full day and greying the rest
 * means a booking is always somewhere on screen, whatever the Space's
 * current hours say.
 */

/** Minutes in an hour — named so the arithmetic below reads as intent, not magic. */
const MINUTES_PER_HOUR = 60

/** Minutes in a day. The grid always spans exactly this many. */
const MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR

export interface CalendarConfig {
  /**
   * Selection granularity of the grid, in minutes. Governs how finely a user can
   * pick a range — *not* how long a booking may be. Bookings are variable length;
   * the backend's max-duration rule is what bounds them.
   *
   * Must divide a day evenly, and must divide `openMinutes` / `closeMinutes`
   * when they are set (see `coherenceIssue`).
   */
  slotMinutes: number
  /**
   * Minutes since midnight, local wall-clock, that the Space opens — inclusive.
   * `null` means the Space enforces no opening bound: nothing before midnight
   * is greyed for it.
   */
  openMinutes: number | null
  /**
   * Minutes since midnight, local wall-clock, that the Space closes. A
   * booking may end exactly here. `null` means the Space enforces no closing
   * bound.
   */
  closeMinutes: number | null
}

export const calendarConfig: CalendarConfig = {
  slotMinutes: 30,
  openMinutes: null,
  closeMinutes: null,
}

/** How many slot rows the grid renders per day, for an arbitrary config. */
export function slotsPerDayFor(config: CalendarConfig = calendarConfig): number {
  return Math.floor(MINUTES_PER_DAY / config.slotMinutes)
}

/** How many slot rows the grid renders per day. */
export const slotsPerDay = slotsPerDayFor()

/**
 * Minutes from midnight to the start of slot `index` (0-based).
 *
 * The grid's row-to-time mapping lives here rather than in the component so that
 * a slot's identity is derived the same way everywhere it is computed. Slot 0
 * is always midnight — the full day is always rendered, whatever a Space's
 * own hours say (see the module docblock).
 */
export function slotStartMinutes(index: number, config: CalendarConfig = calendarConfig): number {
  return index * config.slotMinutes
}

/**
 * The local-time `Date` at which slot `index` starts on the given day.
 *
 * `day` contributes only its calendar date; its time component is discarded.
 * Constructed via the `Date` constructor rather than by adding milliseconds so
 * that a slot lands on the intended wall-clock time across a DST boundary.
 */
export function slotStart(day: Date, index: number, config: CalendarConfig = calendarConfig): Date {
  const minutes = slotStartMinutes(index, config)
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
export function formatSlotLabel(index: number, config: CalendarConfig = calendarConfig): string {
  const minutes = slotStartMinutes(index, config)
  const hh = String(Math.floor(minutes / MINUTES_PER_HOUR)).padStart(2, '0')
  const mm = String(minutes % MINUTES_PER_HOUR).padStart(2, '0')
  return `${hh}:${mm}`
}

/**
 * Whether slot `index` falls outside the Space's operating hours.
 *
 * A slot counts as out-of-hours unless it sits **entirely** within
 * `[openMinutes, closeMinutes)` — the same "never offer what the server will
 * refuse" reasoning as everywhere else in the grid: a slot straddling the
 * opening or closing instant contains a minute the backend's
 * `AvailabilityHoursRule` would deny, so it reads as blocked rather than
 * bookable. With both bounds `null` (a Space with no hours restriction),
 * nothing is ever out-of-hours.
 */
export function isSlotOutOfHours(index: number, config: CalendarConfig = calendarConfig): boolean {
  if (config.openMinutes === null && config.closeMinutes === null) return false
  const start = slotStartMinutes(index, config)
  const end = start + config.slotMinutes
  const open = config.openMinutes ?? 0
  const close = config.closeMinutes ?? MINUTES_PER_DAY
  return start < open || end > close
}

/**
 * What's wrong with `config`, or `null` if it can render a correct grid.
 *
 * Two distinct shapes of bad configuration are folded into one check, because
 * both leave the grid unable to draw a coherent set of rows:
 *
 * 1. **A `slotMinutes` that cannot tile the day, or that does not land
 *    `openMinutes` / `closeMinutes` on a slot boundary.** The direct
 *    descendant of the old boot-time check: a slot size that does not divide
 *    the day, or hours that fall mid-slot, would either truncate the last
 *    slot or grey half of one — silently wrong rather than incoherent, which
 *    is worse.
 * 2. **`closeMinutes` at or before `openMinutes`.** In the frontend's
 *    browser-local model (this task keeps the clock browser-local — task
 *    5.11 moves it to the Space's own zone) this is the closest observable
 *    shadow of `DEFERRED.md` item 17: a Space whose local hours resolve to a
 *    UTC window crossing a calendar day is unusable on the backend, and
 *    every booking against it is denied. This function does not attempt that
 *    resolution — it only refuses to draw a window that is inverted or empty
 *    in whatever clock it is given.
 *
 * Not a boot-time assertion (the old `assertConfigIsCoherent` threw and ran
 * once at import time, which was correct for a compile-time constant nobody
 * could mistype). A `CalendarConfig` is now built from data an admin typed
 * into `SpaceSchedulePanel`, and one bad Space must not white-screen every
 * calendar in the app — `buildCalendarConfig` calls this and the caller
 * degrades to a notice instead.
 */
export function coherenceIssue(config: CalendarConfig): string | null {
  if (config.slotMinutes <= 0) {
    return `Slot length must be positive, got ${config.slotMinutes} minutes.`
  }
  if (MINUTES_PER_DAY % config.slotMinutes !== 0) {
    return `Slot length (${config.slotMinutes} minutes) must divide a day evenly.`
  }
  if (config.openMinutes !== null && config.openMinutes % config.slotMinutes !== 0) {
    return `Opening time must land on a ${config.slotMinutes}-minute slot boundary.`
  }
  if (config.closeMinutes !== null && config.closeMinutes % config.slotMinutes !== 0) {
    return `Closing time must land on a ${config.slotMinutes}-minute slot boundary.`
  }
  if (
    config.openMinutes !== null &&
    config.closeMinutes !== null &&
    config.closeMinutes <= config.openMinutes
  ) {
    return 'Closing time must be after opening time.'
  }
  return null
}

/** `HH:MM:SS` (Python's `time`, the wire shape `Space.opens_at`/`closes_at` use) → minutes since midnight. */
function parseClockMinutes(value: string): number {
  const [hh, mm] = value.split(':').map(Number)
  return hh * MINUTES_PER_HOUR + mm
}

export type CalendarConfigResult =
  | { status: 'ok'; config: CalendarConfig }
  | { status: 'incoherent'; message: string }

/**
 * Builds a `CalendarConfig` from a Space's own schedule fields.
 *
 * `slot_minutes` null falls back to the shipped default granularity.
 * `opens_at` / `closes_at` null means "not enforced" and renders as no
 * restriction — never the old hardcoded 06:00–23:00 window; see the module
 * docblock for why that fallback would be inventing a rule this file has no
 * authority to make up.
 *
 * Never throws: see `coherenceIssue` for what "incoherent" covers and why a
 * misconfigured Space degrades to a result the caller renders as a notice.
 */
export function buildCalendarConfig(space: {
  slot_minutes: number | null
  opens_at: string | null
  closes_at: string | null
}): CalendarConfigResult {
  const config: CalendarConfig = {
    slotMinutes: space.slot_minutes ?? calendarConfig.slotMinutes,
    openMinutes: space.opens_at === null ? null : parseClockMinutes(space.opens_at),
    closeMinutes: space.closes_at === null ? null : parseClockMinutes(space.closes_at),
  }
  const issue = coherenceIssue(config)
  if (issue !== null) return { status: 'incoherent', message: issue }
  return { status: 'ok', config }
}
