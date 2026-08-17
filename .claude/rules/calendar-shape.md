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

## What later tasks in this stream add here

* **10.2** — the table storing a Space's draft and live shape, and the default every Space is seeded
  with.
* **10.3** — the gate's exact position relative to `evaluate_request` in `create_resource_booking`,
  and the projection endpoint the calendar reads.
* **10.4** — how the grid draws blocks, blackouts and allowed durations from the projection.
* **10.5** — the retirement of `availability_hours` and `session_length`, and where the run's gap
  tolerance is re-sourced from once `resolve_day_schedule` is gone.
* **10.6** — the shape agent: one prompt, one validated document, one retry against
  `InvalidShapeError`'s own message.
* **10.7** — the shape benchmark, asserted on the projection rather than the JSON.
* **10.8–10.10** — the conversation API, the shape studio, and the E2E guard.
