/**
 * Test 9 — the Space rules page (task 6.8): creating a rule, scoping it to a
 * weekday, pausing it, and deleting it, driven end to end through the real
 * backend rather than a mock.
 *
 * `max_duration` is the rule type exercised here — one required integer
 * parameter (`rules/rules/registry.py`), the simplest real member of the
 * registry, so this spec is about the page's write path rather than any one
 * rule's own logic. `SpaceRulesPage.test.tsx` is where the genericity claim
 * ("this page never branches on rule type") is proven, against a rule type
 * invented for that test; this spec instead proves the page's writes really
 * reach the server and really persist.
 *
 * ## Space A already has a `max_duration` row of its own
 *
 * It is backfilled from `SPACE_A_MAX_DURATION_MINUTES` (task 6.6), so this
 * spec's own row is a *second* instance, not the Space's only one —
 * `max_duration` is not `is_single`, and two scoped instances (one always-on,
 * one narrowed to a weekday) is exactly the registry's own intended use of
 * `applies_to`. Both rows render the identical label ("Maximum duration"), so
 * this spec cannot pick its own row out by text alone: it records every
 * `max_duration` row's id before creating one, and the id that appears
 * afterward and was not in that set is the row this spec owns. Every
 * subsequent step scopes to that one row, and the spec deletes it as its
 * last action so Space A's configuration is unchanged for whatever runs
 * after this file.
 *
 * ## Task 8.9 added two more `describe` blocks below
 *
 * Neither is about this page — both book directly against the API and
 * assert on the rule engine's own verdict — but they live in this file
 * rather than a new one because a fresh Playwright file costs a whole setup
 * for one assertion and this one already has Space A's discovery machinery
 * in scope. See each block's own docstring.
 */

import type { Page } from '@playwright/test'

import {
  BACKEND_URL,
  createBookingViaApi,
  discoverSpaceAResource,
  expect,
  listAllBookings,
  localInstantDaysFromNow,
  mintSandboxToken,
  SANDBOX_ADMIN_SUB,
  signInAsSandbox,
  test,
} from './fixtures'

/** Every rendered `max_duration` row's `data-testid` (`rule-{id}`), in DOM order. */
async function maxDurationRowTestIds(page: Page): Promise<string[]> {
  const rows = page.getByTestId(/^rule-\d+$/).filter({ hasText: 'Maximum duration' })
  return rows.evaluateAll((elements) =>
    elements.map((element) => element.getAttribute('data-testid') ?? ''),
  )
}

test.describe('the Space rules page', () => {
  test('creates a rule, scopes it to a weekday, pauses it, and deletes it', async ({
    page,
    api,
  }) => {
    const { publicId } = await discoverSpaceAResource(api)

    // The admin identity (`ADMIN_AUTH0_SUB` in `app.sandbox_seed`) is admin of
    // Space A — enough to manage its rules, and deliberately not the owner,
    // since managing rules is an admin+ action and this proves it does not
    // require ownership.
    await signInAsSandbox(page, SANDBOX_ADMIN_SUB)
    await page.goto(`/s/${publicId}/rules`)

    await expect(page.getByTestId('add-rule-panel')).toBeVisible()
    await expect(page.getByTestId('rules-list')).toBeVisible()
    const idsBeforeCreate = new Set(await maxDurationRowTestIds(page))

    // Create: pick the type, fill its one parameter, submit.
    await page.getByTestId('add-rule-type-select').selectOption('max_duration')
    await page.getByTestId('add-rule-param-max_duration_minutes').fill('90')
    await page.getByTestId('add-rule-submit').click()

    // The new row's id is whichever `max_duration` testid appeared that was
    // not there before — see the module docstring for why text alone cannot
    // tell this spec's row apart from Space A's pre-existing one.
    await expect
      .poll(async () => maxDurationRowTestIds(page))
      .toHaveLength(idsBeforeCreate.size + 1)
    const idsAfterCreate = await maxDurationRowTestIds(page)
    const newRowTestId = idsAfterCreate.find((id) => !idsBeforeCreate.has(id))
    expect(newRowTestId, 'the newly created row was not found').toBeTruthy()

    const ruleRow = page.getByTestId(newRowTestId!)
    await expect(ruleRow).toBeVisible()
    await expect(ruleRow.locator('[data-testid$="-paused-badge"]')).toHaveCount(0)

    // Scope to Wednesday (index 2 of the Mon-first `WEEKDAY_LABELS` in
    // `AppliesToEditor.tsx`), then save through the page-level Save
    // (`rules-save`) — task 9.7 replaced the row's own two save buttons with
    // one save for the whole page, rendered in `rules-list`'s own header
    // rather than inside any one row, so it is looked up unscoped.
    await ruleRow.locator('[data-testid$="-applies-mode-weekdays"]').check()
    await ruleRow.locator('[data-testid$="-applies-weekday-2"]').check()
    await expect(ruleRow.getByTestId(`${newRowTestId}-changed`)).toBeVisible()
    await page.getByTestId('rules-save').click()
    await expect(ruleRow.getByTestId(`${newRowTestId}-changed`)).toHaveCount(0)

    // A reload proves the scope actually reached the server rather than only
    // ever having lived in this component's own state.
    await page.reload()
    const ruleRowAfterReload = page.getByTestId(newRowTestId!)
    await expect(
      ruleRowAfterReload.locator('[data-testid$="-applies-mode-weekdays"]'),
    ).toBeChecked()
    await expect(ruleRowAfterReload.locator('[data-testid$="-applies-weekday-2"]')).toBeChecked()

    // Pause it.
    await ruleRowAfterReload.locator('[data-testid$="-toggle-enabled"]').click()
    await expect(ruleRowAfterReload.locator('[data-testid$="-paused-badge"]')).toBeVisible()

    // Delete it, through the confirm step — the same two-click shape
    // `ArchiveSpacePanel` and `MembersPanel` already use elsewhere in this
    // dashboard.
    await ruleRowAfterReload.locator('[data-testid$="-delete-start"]').click()
    await ruleRowAfterReload.locator('[data-testid$="-delete-confirm-yes"]').click()
    await expect(ruleRowAfterReload).toHaveCount(0)

    // A reload proves the delete persisted too, not just the local list —
    // and that Space A's own pre-existing row is untouched. `rules-list`
    // renders empty (no rows at all) for the instant between navigation and
    // the fetch resolving, and that instant would also vacuously satisfy
    // "the deleted row is gone" and "no unexpected row survived" — so both
    // checks below poll rather than read the DOM once, the same reason
    // `idsAfterCreate` above polls for the created row to appear.
    await page.reload()
    await expect.poll(async () => maxDurationRowTestIds(page)).toEqual([...idsBeforeCreate])
    expect(await page.getByTestId(newRowTestId!).count()).toBe(0)
  })
})

/**
 * Task 8.9 — the cross-Resource guard the testing note in
 * `ops/pending/bugs/max-duration-cannon.md` asked for: "a space with two
 * resources needs to be verified in tests - a user who has a max 2 hour
 * consecutive can't use 2 different courts to circumvent".
 *
 * **This confirms an invariant; it does not close a gap.** `HistoryContext`
 * is filtered to the **user**, never to one Resource
 * (`rules/rules/interfaces.py`), and `rules_stub.py`'s router loads history
 * across every Resource in the Space — so a member could never have dodged
 * a cross-booking cap by switching courts, before this stream or after it.
 * A green tick here is not a bug found and fixed; it is a regression guard
 * on two separate pieces of code — the engine's contract and the router's
 * own query — continuing to agree with each other. Nothing today fails if
 * that query narrows to one Resource; this is the test that would catch it.
 *
 * Space A already carries two Resources (`RESOURCE_A1_NAME` / `_A2_NAME`)
 * and, as of this task, an unscoped `max_consecutive_duration` row at
 * `SPACE_A_MAX_CONSECUTIVE_MINUTES` (2 hours, `app/backend/app/
 * sandbox_seed.py`) — deliberately the same bound `SPACE_A_MAX_DURATION_
 * MINUTES` already uses, so a booking that would trip this rule was already
 * going to trip `max_duration` if it were a single request, and no other
 * spec in this suite books two abutting or overlapping slots on Space A
 * whose combined run exceeds two hours.
 *
 * Every booking below goes straight through the API, never the calendar
 * grid: `dragAcrossSlots` has a documented intermittent failure
 * (`ops/deferred/pointer-drag-e2e-flakiness.md`, three specs hit it this
 * stream alone) and nothing about this test's subject — a history query
 * spanning Resources — needs a rendered grid or a real pointer at all.
 */
test.describe('the cross-Resource consecutive-duration guard', () => {
  test('max_consecutive_duration cannot be dodged by switching courts', async ({ api }) => {
    const { publicId, resourceIds, headers } = await discoverSpaceAResource(api)
    expect(
      resourceIds.length,
      'Space A needs at least two Resources for this guard to mean anything',
    ).toBeGreaterThanOrEqual(2)
    const [court1, court2] = resourceIds

    // Prior bookings stay inside `rules.history_window`; upper is always at least now + 7 days.
    const DAYS_AHEAD = 2
    const start1 = localInstantDaysFromNow(DAYS_AHEAD, 17, 0)
    const end1 = localInstantDaysFromNow(DAYS_AHEAD, 18, 0)
    const end2 = localInstantDaysFromNow(DAYS_AHEAD, 19, 0)
    const end3 = localInstantDaysFromNow(DAYS_AHEAD, 20, 0)

    // 17:00-18:00 on Court 1 — the opening booking of the run.
    await createBookingViaApi(api, start1, end1, court1)

    // 18:00-19:00 on Court 2 — a *different* court, but the same contiguous
    // run: the exact circumvention the testing note worried about, and it
    // is admitted, because the run is resolved from this user's history
    // across the whole Space, not from one Resource's own bookings.
    await createBookingViaApi(api, end1, end2, court2)

    // 19:00-20:00 back on Court 1 completes three hours of contiguous play
    // against a two-hour cap.
    const response = await api.post(
      `${BACKEND_URL}/spaces/${publicId}/resources/${court1}/bookings`,
      { headers, data: { start_at: end2.toISOString(), end_at: end3.toISOString() } },
    )
    expect(response.status(), await response.text()).toBe(422)
    const body = (await response.json()) as { error: string; message: string }
    expect(body.error).toBe('rule_denied')
    // Not a full-string match: `rule-engine.md` records that
    // `03-sad-path.spec.ts`'s full-string match on `max_duration`'s own copy
    // makes rewording that message a breaking change in another package,
    // and this test does not create a second such coupling. "consecutive
    // play" is `MaxConsecutiveDurationRule`'s own wording
    // (`rules/rules/canon.py`) and is what tells this denial apart from
    // `max_duration`'s unrelated copy — proving it is the run-aware rule
    // that refused, not a coincidence of the request's own length.
    expect(body.message).toContain('consecutive play')

    // Exactly the two admitted bookings were written; the third never was.
    expect(await listAllBookings(api)).toHaveLength(2)
  })
})

/**
 * Task 8.9's second case — the counterpart to task 8.6's own behaviour
 * change, which shipped with no end-to-end coverage until now: a member
 * holding two abutting bookings against a weekly cap of two can still book
 * a second session, because the abutting pair is one session, not two rows.
 *
 * A naive `existing + 1 > max_bookings` count would refuse the third
 * booking below — two existing rows plus a new one is three, over a cap of
 * two — which is exactly the miscount task 8.6 fixed
 * (`rules/rules/frequency.py`'s `_sessions`): the merged-session count for
 * the week is *two* (the abutting pair, and the new booking), so the third
 * is admitted, and a fourth, unrelated session is what actually trips the
 * cap — asserted here too, so a pass above cannot be mistaken for the cap
 * silently not applying at all.
 *
 * `max_bookings_per_week` is not part of Space A's seeded canon —
 * `sandbox_seed.py`'s own module docstring explains why: a frequency cap
 * would eventually deny a booking some other spec in this suite expects to
 * succeed. So this test adds its own scoped instance as the admin and
 * removes it in a `finally`, the same discipline `10-rule-authoring.spec.ts`
 * follows for the generated row it adds to this same Space. Booked via the
 * API for the identical reason the guard above is: nothing here is about
 * the calendar grid, and setup has no reason to go through the pointer.
 */
test.describe('the weekly cap counts sessions, not rows', () => {
  test('an abutting pair counts once against the cap', async ({ api }) => {
    const { publicId, resourceIds, headers: memberHeaders } = await discoverSpaceAResource(api)
    const [court1] = resourceIds

    const adminToken = await mintSandboxToken(api, SANDBOX_ADMIN_SUB)
    const adminHeaders = { Authorization: `Bearer ${adminToken}` }

    const createResponse = await api.post(`${BACKEND_URL}/spaces/${publicId}/rules`, {
      headers: adminHeaders,
      data: { rule_type: 'max_bookings_per_week', params: { max_bookings: 2 } },
    })
    expect(createResponse.status(), await createResponse.text()).toBe(201)
    const { id: ruleId } = (await createResponse.json()) as { id: number }

    try {
      // **Every booking sits on ONE day, two days out, and that is the whole
      // point of the anchor.** A counting rule only ever sees the history the
      // adapter was allowed to load, and `rules.interfaces.history_window`
      // bounds that at `max(next_month_start, now + 7 days)`. An anchor a
      // month ahead puts every booking below *past* that bound, so the rule
      // counts an empty history, the request becomes session one, and the
      // booking this test expects to be refused is correctly allowed — which
      // is what the first version of this test hit. `upper >= now + 7 days`
      // holds by construction whatever the date, so anything inside a week of
      // now is always in-window; "fewer days" alone is not enough, because
      // `upper` is a `max` of two moving quantities and an anchor that clears
      // it in mid-month can fall outside it near a month boundary.
      //
      // One day also means one local week without arithmetic, so
      // `MaxBookingsPerWeekRule`'s Monday-Sunday window (`WEEK_STARTS_ON` in
      // `rules_stub.py`) needs no alignment here at all.
      const DAYS_AHEAD = 2

      // Two abutting bookings compose one two-hour session — exactly at
      // `max_consecutive_duration`'s own cap, which is inclusive, so the pair
      // is allowed and it is the *cap on sessions* being tested, not that one.
      await createBookingViaApi(
        api,
        localInstantDaysFromNow(DAYS_AHEAD, 9, 0),
        localInstantDaysFromNow(DAYS_AHEAD, 10, 0),
        court1,
      )
      await createBookingViaApi(
        api,
        localInstantDaysFromNow(DAYS_AHEAD, 10, 0),
        localInstantDaysFromNow(DAYS_AHEAD, 11, 0),
        court1,
      )
      // Session two: a real gap, so nothing merges it with the pair above.
      // Space A's calendar shape is open every day, all day (`sandbox_seed.py`),
      // so an afternoon hour is as bookable as a morning one.
      await createBookingViaApi(
        api,
        localInstantDaysFromNow(DAYS_AHEAD, 14, 0),
        localInstantDaysFromNow(DAYS_AHEAD, 15, 0),
        court1,
      )
      expect(await listAllBookings(api)).toHaveLength(3)

      // Three rows so far but only two sessions, so the cap of two is met and
      // not yet broken. A third session is what trips it — and under the old
      // row-counting behaviour the *second* of the abutting pair would already
      // have been refused, which is the regression this test guards.
      const fourthStart = localInstantDaysFromNow(DAYS_AHEAD, 17, 0)
      const fourthEnd = localInstantDaysFromNow(DAYS_AHEAD, 18, 0)
      const deniedResponse = await api.post(
        `${BACKEND_URL}/spaces/${publicId}/resources/${court1}/bookings`,
        {
          headers: memberHeaders,
          data: { start_at: fourthStart.toISOString(), end_at: fourthEnd.toISOString() },
        },
      )
      expect(deniedResponse.status(), await deniedResponse.text()).toBe(422)
      const deniedBody = (await deniedResponse.json()) as { error: string; message: string }
      expect(deniedBody.error).toBe('rule_denied')
      // Not a full-string match, for the identical reason the guard above
      // gives — "sessions" is `_format_sessions`'s own word
      // (`rules/rules/frequency.py`) and distinguishes this denial from
      // every other rule's copy without pinning the whole sentence.
      expect(deniedBody.message).toContain('sessions')
    } finally {
      // Space A's configuration must be unchanged for whatever runs after
      // this file — the same discipline the rest of this spec follows for
      // the row it creates and deletes above.
      const deleteResponse = await api.delete(
        `${BACKEND_URL}/spaces/${publicId}/rules/${ruleId}`,
        { headers: adminHeaders },
      )
      expect(
        deleteResponse.ok(),
        `DELETE .../rules/${ruleId} failed: ${deleteResponse.status()}`,
      ).toBeTruthy()
    }
  })
})
