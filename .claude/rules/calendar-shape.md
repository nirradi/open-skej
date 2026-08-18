# Calendar Shape

> A shape says what the venue offers. A rule says who may take it, and how much of it.

That line (`ops/plans/stream-10/OVERVIEW.md`) is the boundary this whole domain exists to draw.
Opening hours, breaks, session lengths, holidays and seasonal patterns are *shape* — structure a
calendar can be drawn from without knowing who is asking. Frequency caps, horizons, per-day totals
and consecutive-play limits stay *rules*, evaluated by `.claude/rules/rule-engine.md`'s engine. When
something could be answered either way, it belongs on the side that can be **drawn**.

**Lives in:** `rules/shape/`, a third package distributed alongside `rules` and `generation`
(`rules/pyproject.toml`'s `packages` list). It declares no runtime dependency — standard library
only, `dataclasses` and `datetime` — for the identical reason the other two packages don't: the
backend installs this distribution editable, so anything added here is a cost the booking API pays
forever. It holds no ORM, no HTTP, and calls no model; every later task in this stream (the table,
the gate, the calendar, the agent, the benchmark) is a *consumer* of what this package computes, not
a place that recomputes it.

This document owns the schema, the projection's contract, the anchoring and truncation rules, and
the fail-closed statement. Later tasks in Stream 10 add the storage, the gate's position relative to
the engine, the authoring agent and the benchmark — each updates this file as it lands.

## Why not inside `rules/rules/` or the backend

| Candidate home | Rejected because |
|---|---|
| Inside `rules/rules/` | That package is the engine contract and the canon — a `BaseRule`, a `Context`, a `RuleResult`. A shape does not answer `evaluate`, and putting it there makes "is this a rule?" unanswerable from the import path alone. |
| `app/backend/app/schedule/` | The shape benchmark has to import the projection to assert on it, and the benchmark lives in `rules/`, which cannot import the backend. |
| A new top-level repo directory | `rules/pyproject.toml` already distributes two packages and the backend already installs `rules` editable; a third package is one line in that list. |

## The schema

A shape document is JSON with three top-level fields:

```jsonc
{
  "version": 1,
  "operating_blocks": [
    {
      "days": ["MON", "TUE", "WED", "THU", "FRI"],   // required, non-empty, no duplicates
      "start_time": "18:00",                          // required, local wall clock, "HH:MM"
      "end_time": "20:00",                             // required, > start_time (see below)
      "allowed_durations_mins": [20],                  // required, non-empty, ascending, unique
      "effective_from": "2026-01-01",                  // optional, local date, inclusive
      "effective_to": "2026-05-31"                     // optional, local date, inclusive
    }
  ],
  "blackout_windows": [
    {
      "start_time": "19:30",
      "end_time": "19:40",
      "reason": "Break",                               // required, non-empty, member-facing
      "days": ["MON"],                                 // optional; omitted means every day
      "date": "2026-04-15",                             // optional; a single local date
      "effective_from": null,
      "effective_to": null
    }
  ]
}
```

`validate_shape(document) -> Shape` (`rules/shape/validate.py`) is the only supported way to obtain
a `Shape` from untrusted JSON. It raises `InvalidShapeError` naming the first failing field, in
document order, with a message of the form `operating_blocks[1].allowed_durations_mins: must not be
empty` — never a paraphrase. That message is retried verbatim against the model in task 10.6,
exactly as `rules.safety.UnsafeRuleError`'s message is fed back to the generation loop, so it names
the construct and where it is rather than gesturing at "your shape has an error".

Five decisions in that schema, each settled once here rather than re-litigated by a later reader:

1. **`version` is on the document from the first commit.** It costs one integer now and is the only
   thing that makes a schema change in a later stream something other than a guess about which rows
   are which.
2. **`end_time` may exceed `24:00`** — `"26:00"` is 02:00 the next local day, representing a venue
   open past midnight. This is the identical choice `AvailabilityHoursRule` made moving onto
   `closes_at_minutes` above 1440 (`.claude/rules/rule-engine.md`), for the identical reason: an
   inversion (`end_time` earlier than `start_time`) is a value a typo can produce and a meaning
   nobody can read, whereas `26:00` says exactly one thing. It is capped at `start_time + 24h`. A
   window crossing local midnight is *represented* by this schema but the grid (task 10.4) does not
   *draw* one — `ops/done/stream-7/passed-midnight.md` and `resolve_day_schedule`'s own docstring
   record the same limit for the pre-shape calendar, and this stream does not lift it.
3. **A blackout may carry `days` or `date`, never both**, and neither means every day. `date` is the
   one-off ("closed this Friday"); `days` is the recurring break ("closed Mondays"). A blackout
   naming neither applies every day within its own `effective_from`/`effective_to` range — exactly
   what "closed for the whole season" means without also spelling out which weekdays that covers.
4. **`effective_from`/`effective_to` are inclusive on both ends**, unlike every half-open interval
   in the engine. They are calendar *dates* a human typed into a chat, not instants — "closed June 1
   to August 31" excluding the 31st is the bug every scheduling product ships once, and a half-open
   reading here would reproduce it silently.
5. **Overlapping operating blocks are legal and are unioned, never rejected.** Two blocks are how
   "1 hour in the morning, 1 or 2 hours in the evening" is expressed at all, and an admin refining a
   shape by chat produces touching and overlapping blocks constantly. Where two blocks overlap, the
   allowed durations in the merged region are the **union** of the overlapping blocks' own lists — a
   permissive union is correct because each block is an independent *grant* of time, not a
   constraint being ANDed. This is the one place this schema does not follow the engine's flat-AND
   convention, and it is deliberate: an operating block grants, a rule denies. Two blocks that merely
   *touch* — share a boundary, like `08:00-14:00` and `14:00-22:00` — are **not** merged; each keeps
   its own anchor and grid (see below), and merging a shared boundary would make the second block's
   wider duration list reachable one tick before it actually opens.

An **empty** document (`operating_blocks: []`, `blackout_windows: []`) is valid, not an error — it
is what a Space told "closed until further notice" holds, and every date of it projects as
unbookable rather than the validator refusing it.

**Every clock value is local wall-clock minutes, and every date is a local calendar date.** A shape
carries no timezone anywhere in it (OVERVIEW decision 7) — the identical split
`.claude/rules/rule-engine.md` already draws for the engine ("the adapter is the only thing in the
system that knows a timezone"). A `start_time` of `540` means 09:00 in whatever zone the Space is
eventually resolved against; nothing in this package ever asks which one that is. The Space's own
zone converts at the boundary, per date, the same discipline `CLAUDE.md`'s cross-cutting invariants
state for every other local question in this codebase.

## The projection

`rules/shape/projection.py` holds two functions, the second built on the first.

**`project_day(shape, on_date) -> DayProjection`** resolves everything a calendar needs to draw one
local date: the merged operating intervals in force (each carrying the union of its source blocks'
allowed durations), the blackout intervals in force with their reasons, the exact table of
grid-aligned starts a booking may begin at with the durations offered there (`offered_starts`), and
whether the date is bookable at all (`bookable`, `bool(offered_starts)`). It never raises for any
valid `Shape` and any local `date` — a date no block or blackout applies to simply projects to an
unbookable day, which is the correct answer, not a failure.

**`permits(shape, on_date, start_minutes, end_minutes) -> ShapeVerdict`** is the enforcement
question: does `[start_minutes, end_minutes)` fit. It is answered by looking the request up in
`project_day`'s own `offered_starts` table — the identical table a calendar and a chat preview would
render from — so the gate can never grant something the grid never drew, or refuse something the
grid offered. `ShapeVerdict.allowed` is `True` exactly when the pair appears in that table; when
`False`, `reason` is member-facing copy diagnosing, in order: no operating block covers the span,
then a blackout overlap (naming the blackout's own `reason`), then a duration this Space does not
offer at that start, then a start that does not land on any covering block's own grid.

**The grid is chunked forward from each operating block's own `start_time`, never from local
midnight**, and the step is that block's own smallest declared duration,
`min(allowed_durations_mins)`. This reproduces the anchoring decision `SessionLengthRule` already
made ("the anchor is the date's own resolved opening time") — per block here rather than per Space,
since a shape's blocks can each open at a different time of day — and is why retiring that rule type
in task 10.5 loses no behaviour. Each block's grid is entirely independent: two overlapping blocks
each keep their own anchor and step, and a start reachable from *either* grid is offered — decision
5's union expressed at the level of an actual candidate start, not only as a description of the
merged structural interval. Every declared duration is checked independently for fit at each such
start; a 120-minute booking offered at 15:00 in a block stepped by 60 does not need its own
multiple-of-120 grid from the same anchor.

**A blackout truncates, never shifts.** A candidate `[start, start + duration)` that overlaps any
blackout window in force is simply not offered; the grid keeps stepping and the next candidate it
produces is offered normally if nothing else rules it out. There is no "resume right after the
blackout ends" special case and none is needed — the grid already puts a candidate exactly there. A
residual left over after a straddling slot is refused (the 10 minutes of a 20-minute break after a
20-minute slot is excluded) is never offered on its own, because it was never a candidate: only
durations a block itself declares are ever checked.

## Fail closed

The projection is what the booking gate (task 10.3) consults before the rule engine runs, so its
failure modes are booking failure modes. A shape that does not validate, a date the projection
raises on, a duration list that resolves to empty — every one of those is **no booking**. There is
no path through this package that answers "I could not tell" as "yes". `.claude/rules/rule-
engine.md`'s own statement of this ("Fail closed — non-negotiable") is the register every message in
this package is written to match, including never printing an absolute clock time, calendar date, or
timezone name in denial copy: a minute count has no zone to be wrong in, but a rendered `"18:00"`
silently claims one it does not have.

`permits` never raises for an ordinary refusal — that is a `ShapeVerdict` with `allowed=False`. It
raises `InvalidBookingRequestError` only for a genuine caller mistake: an inverted or non-positive
interval, a `datetime` handed where a local `date` was asked for. This is the identical split
`rules.controller` draws between a denial and `ContextMismatchError`: a denial is user-facing copy,
and presenting a caller bug as a polite refusal would hide the bug instead of surfacing it.

## Storage: one shape per Space, versioned

`space_calendar_shapes` (`app.identity.models.SpaceCalendarShape`) holds one row per **version** of
a Space's shape, never one row per Space. `status` is `draft` | `live` | `superseded`. A Space's
current shape is its one `live` row; a chat turn (task 10.8) writes or replaces its one `draft` row;
publishing turns a `draft` into `live` and the row it replaces into `superseded` — nothing is ever
deleted, so the full sequence of a Space's `live` rows, in `created_at` order, is the whole "what
changed and when" answer with no second table for it.

**Two partial unique indexes carry the "at most one live, at most one draft per Space" constraint**,
`uq_space_calendar_shapes_live` and `uq_space_calendar_shapes_draft`, each unique on `space_id`
filtered to their own status — the identical shape and reasoning
`uq_rule_generation_jobs_in_flight` already gives (`.claude/rules/identity-and-access.md`): two
concurrent writers (an admin's chat tab open in two windows) can both pass a read-then-write check
in the service layer and only one can pass a database index, so the index is the actual enforcement
and the service layer never re-derives it as a check of its own.

**Publishing moves two rows in one transaction, never one.** The current `live` row becomes
`superseded` and the `draft` row becomes `live` with `published_at` / `published_by_user_id` set,
flushed in that order — the old `live` row must already be `superseded` before the draft flips to
`live`, or the flip collides with `uq_space_calendar_shapes_live` inside its own transaction, since
that index is an ordinary (non-deferrable) one checked per statement. `publish_draft` refuses with
`NoDraftToPublishError` when the Space holds no draft, rather than treating the call as a silent
no-op that would tell an admin "published" while the live shape never moved.

**A Space with no live shape row is not a reachable state.** `create_space` writes a live
`DEFAULT_SHAPE` row in the same transaction as the rest of Space creation, so the availability gate
(task 10.3) never has to carry a permissive branch for "no shape configured yet" — decision 1
above is exactly this stated for the schema. `DEFAULT_SHAPE` (`rules/shape/types.py`) is open every
day 00:00–24:00, `allowed_durations_mins: [60]`, no blackouts — what a Space with neither
`availability_hours` nor `session_length` already rendered as, so adopting it changes no existing
test's meaning. Every Space that predates this table was backfilled the identical document by this
table's own migration, as a `live` row with `created_by_user_id`/`published_by_user_id` left `NULL`
— a system backfill has no acting user, and both columns are nullable on this table for exactly that
reason. No shape is derived from a Space's prior `availability_hours` / `session_length` rows; there
is no production data to derive one from (OVERVIEW decision 3).

`app.identity.service` reads and writes this table beside `space_rule_config`, the module's other
per-Space configuration assembly: `live_shape` returns the validated `rules.shape.Shape`, re-running
`validate_shape` on every read rather than trusting that the stored document validated once at write
time — the identical "re-validate at every load" discipline `app.rule_catalog` applies to a stored
generated rule's source, and for the same reason: a document written before a schema change is a
document nobody re-checked. `draft_shape`, `upsert_draft`, `publish_draft` and `discard_draft` round
out the write side; every one refuses on an archived Space through the existing
`SpaceArchivedError` path, and none of them re-checks the caller's role — authorization is the
router's job, matching how every other write in this module already splits the two.

## The availability gate, and the endpoint the calendar reads

The shape is enforced in `create_resource_booking`
(`app/backend/app/routers/resource_bookings.py`) at a fixed position relative to the Space's own
archived check and the rule engine: `archived? -> shape? -> rules -> driver`. Structure is checked
before policy — when a booking breaks both, *"we close at 20:00"* is more actionable than *"you
have had three bookings this week"*, the same fail-fast arbitration the engine's own rule
priorities already perform one layer down (`rule-engine.md`'s canon ordering). The gate returns the
existing 422 `BookingDenied` with `verdict.reason` as its message — no new status code, no new
response model, so no client learns a second branch to render for what is, from a member's side,
one situation.

**An unreadable shape refuses; it is never skipped.** `service.live_shape` re-validates the stored
document on every read and raises `shape.InvalidShapeError` for one that no longer parses. The
gate catches exactly that exception, logs the real cause, and returns the engine's own generic
`RULE_ERROR_MESSAGE` — never the validator's message, which names an internal field path rather
than anything a member booking a court should read. This is the identical rule `app.rule_catalog`
already states for a generated rule row that will not hoist: an unavailable check is a check that
refuses, never one silently bypassed.

**The gate never runs for a cancellation.** Cancelling a booking that has fallen outside a newly
published shape must still work — a member trapped with an uncancellable booking is the direct
cost of getting this wrong, and it is the kind of defect that only shows up after a publish.
`cancel_resource_booking` does not consult the shape at all, matching how it already skips the rule
engine for the same reason (rules gate acquiring a slot, not releasing one).

**The local conversion happens once, at this boundary, and nowhere else.** The shape holds local
wall-clock minutes; a booking request holds a UTC instant. The gate resolves the booking's local
date and its start/end as minutes from that date's local midnight — reusing
`app.rules_stub`'s own `_local_date` and `_local_day_bounds` rather than a second implementation of
"this venue's local day" — and calls `shape.permits` with them. A booking spanning local midnight
resolves against the date it **starts** on, matching every window convention in the engine, and its
`end_minutes` may legitimately exceed 1440, which is exactly what an `end_time` past `24:00` in the
shape exists to meet.

`GET /spaces/{public_id}/calendar?from={date}&to={date}[&draft=true]` is the read half of the same
boundary: it serves one `DayProjection` per local date in `[from, to]` (**inclusive** on both
ends, matching the shape schema's own `effective_from`/`effective_to` convention rather than the
engine's half-open one — this is a calendar range a human typed, not an instant), calling
`shape.project_day` directly rather than leaving the client to derive it — the same rule
`identity-and-access.md` already states for `anchor_minutes` ("the client never derives it, because
which rows govern a date is the server's question"), carried into the shape. The range is bounded
by the same `MAX_SCHEDULE_DAYS` constant `GET /spaces/{public_id}/schedule` already uses, rather
than a second number.

**Two roles sit on this one route.** Member+ reads the live shape; `draft=true` requires admin+,
checked inline against the caller's resolved role rather than inferred from the query string — a
draft is an admin's half-finished thought and must not be visible to members. The wire shape mirrors
`rules.shape.projection.DayProjection` field for field, in the package's own vocabulary — minutes
from local midnight throughout, never `DayScheduleRead`'s `HH:MM:SS` wall-clock strings, because
converting from minutes to a wall clock and back is two chances to disagree with the gate that
enforces the identical table.

This endpoint replaces `GET /spaces/{public_id}/schedule` in role but does not remove it; both
serve the calendar grid until task 10.4 moves the frontend across and 10.5 retires the schedule
endpoint along with the two rule types it resolved.

## What the grid draws, and what it deliberately does not know

`CalendarGrid` (`app/frontend/src/calendar/CalendarGrid.tsx`) draws one week from
`GET /spaces/{public_id}/calendar` and from nothing else. A day column is a **minute canvas** — one
box 1440 minutes tall — with four layers positioned into it: closed time as the canvas's own
background, the operating intervals painted over it, the blackout intervals greyed over those
**with their own `reason` rendered**, and one button per `offered_starts` entry. Existing bookings
are drawn last, unchanged.

**Closed is the default state of a minute; being open is the positive statement.** The grid used to
render all 24 hours as bookable and grey whatever fell outside an `availability_hours` row, so a
Space whose hours came from anywhere else was drawn wide open — the reported defect this stream
exists to close. Painting the operating region instead means the grid can only ever offer what the
projection offered, and a date the server did not send projects as **closed**: `WeekProjection`'s
own lookup (`app/frontend/src/calendar/shape.ts`) falls back to a closed day, the deliberate
inversion of the permissive fallback the retired per-date schedule carried. Fail closed reaches the
client too.

**A selection is one offered start plus one duration offered there** — a lookup into the same
`offered_starts` table the gate answers `permits` from, never arithmetic over a uniform slot grid.
That is what makes a length this Space does not offer *unconstructable* rather than merely refused
after submission, which is the structural half of
`ops/pending/bugs/calendar-does-not-reflect-the-rule-set.md`'s symptom 2. A click takes the
smallest duration offered at that start; a drag takes the largest one that ends no later than the
drag head and clears every booking in the way, so dragging past an existing booking shortens the
selection instead of spanning it.

**The grid knows nothing about policy rules, and that is the design rather than a residue.** A
Space configuring a 120-minute `max_duration` beside a shape offering 180 still lets a member build
a selection the server refuses. The shape says what the venue offers; a rule says who may take it
and how much of it (`ops/plans/stream-10/OVERVIEW.md`, decision 2), and only one of those two is
drawable. Projecting the whole rule set onto the calendar is the repair this stream rejected
outright: a slot-guesser can hide a bookable slot as easily as offer an unbookable one, and the
first is the worse failure of the two.

**A start button is as tall as its own click unit, capped at the next offered start.** Two blocks
with different anchors keep two independent grids, so two starts can sit closer together than
either one's shortest booking; capping the height is what keeps every offered start clickable
instead of letting one paint over its neighbour.

**A window past local midnight is clamped, never drawn.** An `end_time` above `24:00` is
representable (decision 2 above) and the grid does not render the wrap: regions are clipped at 1440
minutes and a start at or past it is not drawn at all. `ops/done/stream-7/passed-midnight.md`
records the identical limit for the pre-shape calendar, and this stream does not lift it.

**`showBookings` is the seam the chat preview reuses this component through.** With it off the grid
issues no booking request and draws no booking layer, so the component a member books on is the one
a draft is previewed with — one grid, not a second implementation that could disagree with it.

## What later tasks in this stream add here

* **10.5** — the retirement of `availability_hours` and `session_length`, and where the run's gap
  tolerance is re-sourced from once `resolve_day_schedule` is gone.
* **10.6** — the shape agent: one prompt, one validated document, one retry against
  `InvalidShapeError`'s own message.
* **10.7** — the shape benchmark, asserted on the projection rather than the JSON.
* **10.8–10.10** — the conversation API, the shape studio, and the E2E guard.
