# `GET /spaces/{public_id}/resources/{resource_id}/bookable` — a real response

Captured against the sandbox seed's Space B ("Sandbox Space B (Sydney)"), which
`app/backend/app/sandbox_seed.py` configures with a real `availability_hours` row
(09:00–21:00 local), a `session_length` row (30 minutes), a `max_duration` row (90 minutes) and a
`max_bookings_per_week` row (3) — see `app.routers.bookable`'s own module docstring for what each
piece of the response is built from and why.

Brought up per `POC.md`; authenticated as `sandbox|owner` via `POST /sandbox/token`. Real command:

```
curl -s "http://127.0.0.1:8123/spaces/$SPACE_B/resources/4/bookable?from=2026-08-17&to=2026-08-24" \
  -H "Authorization: Bearer $TOKEN"
```

Response, trimmed to the first day in full and every other day's `reasons` only (`starts` is 48
entries long per day and is mechanical once the first day's shape is clear):

```json
{
  "slotMinutes": 30,
  "days": [
    {
      "date": "2026-08-17",
      "firstSlotMinutes": 0,
      "starts": [
        [0, 0], [0, 0], [0, 0], [0, 0], [0, 0],
        [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0],
        [0, 0], [0, 0], [0, 0], [0, 0], [0, 0],
        [1, 3], [1, 3], [1, 3], [1, 3], [1, 3], [1, 3], [1, 3], [1, 3],
        [1, 3], [1, 3], [1, 3], [1, 3], [1, 3], [1, 3], [1, 3], [1, 3],
        [1, 3], [1, 3], [1, 3], [1, 3], [1, 3], [1, 3], [1, 3],
        [1, 2], [1, 1],
        [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0]
      ],
      "reasons": [
        {"fromSlot": 0, "toSlot": 5, "code": "deny_ea5ef02f",
         "text": "That time has already passed, so it can't be booked. Please pick a time in the future."},
        {"fromSlot": 5, "toSlot": 18, "code": "deny_72ea9789",
         "text": "That's before we open, so this booking starts too early. Please check this Space's opening hours on the calendar and pick a later time."},
        {"fromSlot": 42, "toSlot": 48, "code": "deny_45823cc8",
         "text": "That's after we close, so this booking runs too late. Please check this Space's opening hours on the calendar and pick an earlier time."}
      ]
    },
    {"date": "2026-08-18", "reasons": [
      {"fromSlot": 0, "toSlot": 18, "code": "deny_72ea9789", "text": "before we open"},
      {"fromSlot": 42, "toSlot": 48, "code": "deny_45823cc8", "text": "after we close"}
    ]},
    {"date": "2026-08-19", "reasons": "identical shape to 2026-08-18"},
    {"date": "2026-08-20", "reasons": "identical shape to 2026-08-18"},
    {"date": "2026-08-21", "reasons": "identical shape to 2026-08-18"},
    {"date": "2026-08-22", "reasons": "identical shape to 2026-08-18"},
    {"date": "2026-08-23", "reasons": "identical shape to 2026-08-18"}
  ]
}
```

What this shows, mapped back to the Space's real rows:

* Slots 18–39 (09:00–20:00 local) are bookable at `[1, 3]` — 1 to 3 slots of 30 minutes, i.e. 30 to
  90 minutes, the exact ceiling `max_duration_minutes: 90` sets. Slots 40 and 41 (20:00 and 20:30)
  shrink to `[1, 2]` and `[1, 1]` — not `max_duration` any more, but the 21:00 close cutting the
  *contiguous run* short before the duration cap ever would. This is `SlotProjection.max_slots`
  behaving exactly as documented: the top of the run starting at `min_slots`, whichever wall — the
  cap or closing time — is hit first for that particular start.
* Slots 0–17 and 42–47 are denied with `deny_72ea9789` / `deny_45823cc8` — `availability_hours`
  09:00–21:00, reported once per contiguous run rather than per slot.
* 2026-08-17's own slots 0–4 additionally deny with `deny_ea5ef02f` — `NotInThePastRule`, because
  this window's early morning had already passed relative to the captured `now`. This is the one
  day-specific line in the whole response, and it is exactly the kind of thing a database-free
  benchmark can never produce — it depends on the wall clock at request time, not on any
  configuration row.

## Two further live checks, not shown above (see the PR/commit history for the exact commands)

* **The overlap rule.** After `POST`ing a real 10:00–11:00 booking on 2026-08-17 (Resource 4), the
  same request re-projected slots 20–21 (10:00–11:00) as `[0, 0]` with
  `"This time overlaps a booking that already exists on this Resource. Pick a different slot."` —
  `_ExistingBookingRule` in `app.routers.bookable`, not a `space_rules` row (module docstring, "Real
  bookability, not just real rules").
* **The weekly cap, live.** After three confirmed bookings landed in the same local week, every
  remaining candidate for the rest of that week denied with `"You can make at most 3 sessions a
  week, ..."` — `max_bookings_per_week`, which reads this member's own history
  (`app.rules_stub._load_space_history`, loaded once for the window by the endpoint, not once per
  candidate) — proving the endpoint is genuinely per-member, not a Space-wide answer cached across
  callers.

The database was reset (`python -m app.sandbox_seed`, idempotent) after these two checks so the
worktree is left in its plain seeded state, not mid-demo.
