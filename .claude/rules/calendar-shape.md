# Calendar Shape

> A shape says what the venue offers. A rule says who may take it, and how much of it.

That line (`ops/plans/stream-10/OVERVIEW.md`) is the boundary this whole domain exists to draw.
Opening hours, breaks, session lengths, holidays and seasonal patterns are *shape* — structure a
calendar can be drawn from without knowing who is asking. Frequency caps, horizons, per-day totals
and consecutive-play limits stay *rules*, evaluated by `.claude/rules/rule-engine.md`'s engine. When
something could be answered either way, it belongs on the side that can be **drawn**.

**Lives in:** `rules/shape/`, a third package distributed alongside `rules` and `generation`
(`rules/pyproject.toml`'s `packages` list). The distribution declares no **third-party** runtime
dependency — its `dependencies = []` remains accurate — because the backend installs it editable and
every added package is a cost the booking API pays forever. The document, validator, and projection
core is standard-library-only (`dataclasses` and `datetime`) and holds no ORM, HTTP, or model call.
The shape agent is the one deliberate authoring boundary in this package: it imports the internal
`generation.llm` `LLMClient` seam (and its `LLMCallError`) to call a model, then returns to the same
validator. Every consumer uses the one projection rather than recomputing it.

This document owns the schema, the projection's contract, the anchoring and truncation rules, the
agent's response contract, and the fail-closed statement. Later tasks in Stream 10 add the storage,
the gate's position relative to the engine, and the benchmark — each updates this file as it lands.

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
   open past midnight. An inversion (`end_time` earlier than `start_time`) is a value a typo can
   produce and a meaning nobody can read, whereas `26:00` says exactly one thing; a window is
   therefore capped at `start_time + 24h` and always starts on the day it is configured for, so what
   a bare pair of clock times could only express as an ambiguous inversion is a value nothing can
   typo into existing at all. This is the identical choice the retired `availability_hours` rule
   type made when it moved onto minutes ("Two rule types this document replaced", below), carried
   across rather than re-argued. A window crossing local midnight is *represented* by this schema
   but the grid (task 10.4) does not *draw* one — `ops/done/stream-7/passed-midnight.md` records the
   same limit for the pre-shape calendar, and this stream does not lift it.
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
`min(allowed_durations_mins)`. **Anchoring on the opening time rather than on midnight is a
decision, not an implementation detail**, and it is the one the retired `session_length` rule type
established ("Two rule types this document replaced", below): a venue's sessions begin when the
venue opens, so a grid anchored anywhere else describes a schedule nobody asked for — a block
opening at 09:15 against a midnight-anchored hourly grid has no start at 09:15 at all. It is
resolved per **block** here rather than per Space, which is strictly better than what it replaces:
two blocks opening at different times of day each get their own correct anchor with no scoping
mechanism to reconcile. Each block's grid is entirely independent: two overlapping blocks
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
current shape is its one `live` row; a chat turn writes or replaces its one `draft` row;
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

## The conversation API and its provenance

`space_shape_conversations` is the durable, Space-scoped transcript owner: `open` and `closed`
are its only states, and `uq_space_shape_conversations_open` is a partial unique index over `open`
rows, so a Space has one in-flight conversation even when two browser tabs race to start one.
`space_shape_messages` records user and assistant messages in a unique, ordered ordinal sequence;
the assistant summary and nullable clarification question point at the draft version its turn
produced. The question is durable actionable state, not text the browser derives from the summary:
on recovery it determines whether an unbookable draft falls back to the live preview and requires
explicit publish acknowledgement. `space_shape_exchanges` writes
each prompt as `pending` **before dispatch**, then records its completion or transport failure as
`completed` / `failed`; a malformed completion that triggers the agent's one validation retry is
therefore retained along with a call that never answered. It references the existing sha256-keyed
`prompt_versions` row, with the `shape` `PromptAgent` label, rather than storing a second copy of
the system prompt or adding a tracing service for tenant-authored text.

All conversation routes are admin+ through `require_space_role`: `POST
/spaces/{public_id}/shape-conversations` opens a conversation from the current live document;
`GET .../shape-conversations/current` returns that Space's open conversation or `null` for safe
browser recovery; `GET .../shape-conversations/{id}` returns its transcript, current draft, and the
authoritative current live `ShapeVersionRead`; and `POST
.../shape-conversations/{id}/turns` stores the admin message, makes the bounded model call, and
returns its summary, question and new draft synchronously. This differs deliberately from
`rule_drafts.py`: generated rules require a multi-minute adversarial/sandbox job and polling,
while a shape turn is one completion plus at most one schema correction. An unreachable or timed-out
model produces a plain renderable error and is not retried. `SHAPE_CONVERSATION_TIMEOUT_SECONDS`
(30 seconds by default) is applied to the selected transport, so the underlying socket or subprocess
is bounded rather than only the HTTP wrapper waiting for it.

The recorder writes a pending exchange before dispatch and completes it afterwards, before draft
persistence. Strict recorder hooks refuse to dispatch if this product-database provenance cannot be
written, so a model call that would be impossible to account for never occurs. A process that dies
mid-turn still leaves the exact prompt; a transport failure leaves its failed row. Before a model
response becomes a draft, the service locks and re-reads the Space and conversation, then compares
the draft it read before dispatch with the current one. An archive or discard race cannot publish a
new draft, and an overlapping turn receives 409 rather than silently overwriting a later draft.
Publish, discard and conversation-open mutations take the same Space lock, so none can commit from
a stale pre-archive ORM object. The resulting draft and assistant transcript message commit in that
same locked finalization transaction; publish or discard cannot slip between them and leave a
message pointing at a deleted working copy. Every transcript append takes the conversation lock
before assigning its ordinal, so a concurrent turn cannot collide with that final assistant append
or write into a conversation that closed while its request was being authorized. Conversation ids
are always loaded in one query scoped to `space_id`, so a foreign id and an absent id receive the
same 404.

A discarded draft is the domain's deliberate deletion exception. Its assistant messages remain as
the transcript, but their `resulting_shape_version_id` foreign key becomes null (`ON DELETE SET
NULL`): the message still records what was said, while it must not make a working copy impossible
to discard.

`POST /spaces/{public_id}/calendar-shape/publish` publishes the one draft and closes its open
conversation in the same transaction. It refuses `NoDraftToPublishError`, an archived Space, and a
shape with no offered time unless the caller explicitly sends `allow_unbookable: true`; closing a
venue is valid, silently publishing an accidental all-closed shape is not. `POST
/spaces/{public_id}/calendar-shape/draft` discards the working copy and closes the conversation.
Neither route examines existing bookings; that warning remains the deferred concern attached to the
single publish moment, not a hidden fourth publish policy.

## The shape agent

`shape.agent.generate_shape(conversation, client, model)` turns a conversation into a
`ShapeAgentResult(document, summary, question)`. It owns neither storage nor HTTP. `document` is
always a complete, typed `Shape`, obtained only by running the JSON response's `document` field
through `validate_shape`; a valid empty shape remains a document rather than being represented as
`None`. The strict envelope also carries a non-empty one- or two-sentence, member-neutral `summary`
and a nullable `question`. The summary makes a committed reading visible in words as well as in the
preview — “open at 4” means 04:00 unless a later turn corrects it.

**A turn returns a whole document, never a patch.** The document is small, while a patch protocol
would need merge and deletion semantics before a preview could be a function of its last turn. A
complete response makes the preview exactly that function and leaves the next caller no partial
state to reconstruct.

**Exactly one candidate correction is allowed.** JSON parsing, envelope validation, and
`InvalidShapeError` are candidate failures: the agent sends one retry containing both the exact
validator/error message and the original completion verbatim. A second candidate failure raises.
`LLMCallError` propagates immediately, since another prompt cannot repair an unreachable model, bad
credential, or timeout. This reuses `generation.llm.LLMClient`, `LLMCallError`, and
`RecordingClient`'s exchange seam, not the generation loop or its AST validation, sandbox,
adversarial tests, bytecode hoist, manifest, or parameter contract; those exist because generated
rules are executable Python, and a shape is data.

**Routine ambiguity commits; an unbookable shape asks.** The agent validates candidate bookability
through the same projection the grid and gate use. When no date has an offered start — including an
empty/all-closed document, a block too short for its smallest duration, or a blackout that closes
the only viable date — `question` is required and the future caller treats it as do-not-publish.
For a bookable document `question` is null. The document remains non-optional in both cases so the
admin can see and refine the exact reading rather than starting another conversation from nothing.

`shape.stub.StubShapeLLMClient` is CI's model client. It sits at the same `LLMClient` seam, is
deterministic, and makes no network or subprocess call. It recognises only `open at <time>` and
`from <time> to <time>` in the prompt so typing a recognisable time moves the returned operating
block and exercises the chat-to-preview wiring; it is a test double, not a natural-language parser.

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
by `MAX_SCHEDULE_DAYS` — two calendar months, comfortably more than the single week the grid ever
asks for in one request, while still bounding the work one request can cause.

**Two roles sit on this one route.** Member+ reads the live shape; `draft=true` requires admin+,
checked inline against the caller's resolved role rather than inferred from the query string — a
draft is an admin's half-finished thought and must not be visible to members. The wire shape mirrors
`rules.shape.projection.DayProjection` field for field, in the package's own vocabulary — **minutes
from local midnight throughout, never a `HH:MM:SS` wall-clock string**, because converting from
minutes to a wall clock and back is two chances to disagree with the gate that enforces the
identical table.

This endpoint is what the calendar grid reads, and the only thing it reads. There is no second
schedule endpoint beside it: `GET /spaces/{public_id}/schedule` was retired with the two rule types
it resolved (below).

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

## Two rule types this document replaced

**`availability_hours` and `session_length` were retired on 2026-08-18**, along with `GET
/spaces/{public_id}/schedule` and the `resolve_day_schedule` that served it. They were the two canon
rule types (`.claude/rules/rule-engine.md`) that expressed a venue's opening window and the grid its
bookings sat on, and the shape expresses both — as one document a calendar can be **drawn** from
rather than two predicates that can only answer yes or no about a booking already proposed. Leaving
them registered would have left an admin two places to configure hours that can disagree, which is
the state this stream exists to end (OVERVIEW decision 2). This section exists so that a plan file
or a comment from Stream 6 or 8 naming either type is one hop from finding out where it went.

Retirement here means **gone**, not deprecated: both types left `rules.REGISTRY`, both classes left
`rules.canon`, `AvailabilityHoursRule` left `DEFAULT_CANON` (which is now three rules), the two
write-boundary validators left `app.identity.service`, and a migration deleted every `space_rules`
row of either type. Nothing was derived from those rows on the way out — there is no production data
to derive a shape from (OVERVIEW decision 3), so a derivation would have been written for zero rows
and tested against fixtures invented to exercise it. `create_space` seeds no rule row at all now; a
fresh venue is bookable because of its live `DEFAULT_SHAPE` row.

`max_duration` deliberately **stayed** a rule. A shape's `allowed_durations_mins` says which lengths
are *offered*; `max_duration` says how long *this member* may book, which a later stream will want
to vary by who is asking. Two different statements, and only one of them is drawable.

Two arguments those types established are load-bearing and live on here rather than being lost with
them: the grid is anchored on the opening time rather than on local midnight ("The projection",
above), and the run's gap tolerance is one bookable length rather than exact abutment (below).

### The run's gap tolerance is re-sourced from the projection

`rules_stub._resolve_run` merges a user's adjoining bookings into one run, and it closes a gap
smaller than a **tolerance** rather than requiring exact abutment. That tolerance used to be the
date's own resolved `session_length`. It is now the smallest duration this shape offers on that
date — `app.rules_stub._gap_tolerance`, the minimum `allowed_durations_mins` across
`project_day(shape, on_date)`'s operating intervals, and zero on a date with no operating block.

The original argument survives the move intact, which is why the move is a re-sourcing rather than a
rewrite: **any gap a legal booking could actually occupy is at least one bookable length long**, so
a gap shorter than that is dead space nobody could ever construct a booking to fill, and merging
across it is what stops such a gap fracturing every run-based rule for free. The shape's allowed
durations are now the complete statement of what a legal booking's length may be, so they are
exactly the right source. `merge_adjoining_spans` joins on `gap < tolerance` **strictly**, so where
one uniform grid governs a date the tolerance changes nothing — every gap there is a whole multiple
of the step — and it earns its place on a shape offering several durations, or one whose blocks keep
separate anchors.

**This is the one consequence of retirement that is invisible from the code being deleted.** Left
un-re-sourced, the tolerance becomes zero everywhere, the sweep stops merging across any
non-abutting gap, and `max_consecutive_duration` and all three counting rules quietly loosen —
nothing raises and no test that existed at the time fails. `SpaceRuleConfig.shape` therefore
defaults to `DEFAULT_SHAPE`, the document a fresh Space actually holds, never to `None`: an absent
shape resolves to a tolerance of zero, and zero is the permissive direction.

## The shape studio

`/s/{public_id}/shape` is the admin+ authoring surface for a Space's calendar shape. It keeps a
durable conversation id as a browser-local pointer scoped to that Space, but recovers through
`GET /spaces/{public_id}/shape-conversations/current` before creating one, so cleared browser
storage cannot strand the Space's one open conversation. The product database, not browser storage,
remains the source of truth for the transcript and working document. A missing or closed pointer is
cleared rather than allowed to address a later conversation.

The studio places the synchronous chat beside the shared `CalendarGrid` with `showBookings=false`.
It obtains `GET /spaces/{public_id}/calendar?draft=true` whenever a draft exists and displays that
projection after a bookable turn; otherwise it uses the live projection, so the preview is exactly
the grid members use without inventing a dummy Resource or a second renderer. The preview refetches
when the draft's `created_at` changes because ordinary turns update one draft row in place. An
assistant summary and nullable question are transcript state; an unbookable candidate's question
remains visible and the preview stays on its last useful calendar until the next bookable turn. On
reload, the latest assistant question is read from that transcript, so when a frozen preview is
unavailable the studio falls back to live rather than presenting the unbookable draft as useful.

Publish compares the draft document exactly with the authoritative live document returned alongside
the conversation, rather than comparing a displayed week: projections can coincide for one range
while shape documents differ in a later season. Publish makes the draft the members' live calendar,
and discard confirms before closing the conversation and deleting its working copy.

The rules page remains separate and links here, while this page links back to rules. The chats are
not merged or intent-routed: shapes describe what the venue offers, and rules describe who may take
it and how much.

## Benchmarking the shape agent

`rules/shape_benchmark.py` is a hand-invoked, checkpointed benchmark over the shape agent. It stays
at the top level of `rules/`, beside `benchmark.py` and outside the distributed packages, so a live
model harness never becomes part of the booking API's import surface. Its `RecordingClient` records
the calls the agent actually made, including a candidate-correction call, without teaching the shape
agent or projection about benchmarking. The report records its client, model, applied temperature,
and seed only when the selected client actually receives one; Google AI Studio receives a temperature
but no reliable seed, so its report records `seed: null` rather than a value it did not apply.

**The benchmark asserts on the projection, never on JSON.** `GoldenShapeExample` holds a single-turn
prompt and pure `OffersExactly`, `OffersProjectionExactly`, `OffersNothing`, or `Permits`
expectations. Each calls `project_day` or `permits`, the same answer the grid renders and the booking
gate enforces. A teacher break represented by one blackout or by two operating blocks with a hole is
therefore equally correct when it offers the same calendar; comparing documents would instead measure
an arbitrary encoding and push the authoring prompt toward it. These assertions are free of network
calls and LLM judges, so they are also unit-tested against hand-written shapes.

The fixed thin set is deliberately ordered and contains only the core, static vocabulary:

| Prompt | Projection expectation |
|---|---|
| Teacher | 20-minute slots from 18:00–20:00, with the 19:30–19:40 break removing the straddling 19:20 start. |
| Lab equipment | One long 30-minute grid, with cooldowns at 10:00, 13:00, and 15:00 removing their overlapping starts. |
| Music room | A morning one-hour block and an evening one- or two-hour block; a two-hour offer is reachable only in the evening. |

The seasonal, relative-date, past-effective-date-refusal, and multi-turn preservation cases remain
the deferred tough benchmark. They need a reference-date and conversation contract; the thin set
does not claim to validate either one. Appending, editing, or reordering the fixed cases changes the
baseline and is a new benchmark decision, not routine prompt editing.

An invalid candidate gets the shape agent's one correction attempt and reports `gave_up` if it still
cannot produce a valid envelope. A valid document that offers the wrong calendar remains a completed
measurement (`verified` with `succeeded: false`), while an `LLMCallError` is a non-run: it aborts the
remaining examples for that model as `skipped` and exits non-zero. `--checkpoint` writes after every
example and refuses a client, seed, temperature, or golden-set fingerprint mismatch on resume. The
fingerprint is a sha256 identity of the exact ordered prompts and expectation data, so cached
examples never survive a changed assertion or reordered baseline. Model ids are not part of that
identity: a matching checkpoint can add another model because each model has its own completed rows.

| Client | Model | Temperature | Seed | Thin-set result | Usage and latency | Shape benchmark default |
|---|---|---:|---|---|---|---|
| Google AI Studio | `gemini-3.1-flash-lite` | 0.0 | unset | 3/3 succeeded; every example validated and met its projection expectations on its first call, with no correction retry. | 3 calls; 5,028 input tokens; 891 output tokens; cost and model duration unavailable; 4,165 ms total wall time. | Selected for the thin static benchmark. |

`gemini-3.1-flash-lite` is the evidence-based default for this thin static benchmark. The result
does not validate the deferred tough cases or establish a default for relative-date interpretation
or multi-turn preservation.
