---
description: The rule engine — contract, execution model, safety, and AI rule generation.
glob: "rules/**/*"
---

# Rule Engine

The isolated Python environment that decides whether a booking is permitted.

**Lives in:** `rules/`, entirely — the contract, the controller, the safety validator, the sandbox,
and the generation loop. Strictly backend execution logic; it holds no HTTP, no ORM, and no UI.

`rules/rules/interfaces.py` is the authoritative contract and `controller.py` the authoritative
execution model.

## Fail closed — non-negotiable

Any failure to *positively establish* that a booking is permitted results in **no booking**. Refusing
wrongly is visible and recoverable; allowing wrongly double-books a shared resource and is discovered
by two people standing in the same place. "Couldn't decide" resolves to **no**.

Three paths, all fail closed:

1. **A rule raises** → contained by the controller, converted to a denial carrying generic copy
   (`RULE_ERROR_MESSAGE`). The real exception goes to the log, never to `fail_reason`. A bug in one
   rule must never 500 the booking endpoint nor leak a traceback into text the UI renders verbatim.
2. **A rule returns a non-conforming response** (anything that is not a `RuleResult`) → same
   containment. This is a live risk for AI-generated rules, not a theoretical one.
3. **Malformed input** — a context that does not describe its request → raises
   `ContextMismatchError`. Still fail-closed on the outcome, but it *raises* rather than denying,
   because a denial is user-facing copy and would present a caller bug as a normal refusal. A context
   holding another user's bookings would silently count them toward this user's limits; answering
   "denied" would hide that. Fail closed on the outcome, loud on the cause.

**When writing or generating a rule:** never catch your own exception and return a pass, and never
return anything but a `RuleResult`. The controller contains both, but a rule that swallows errors
into a pass defeats containment *silently* — it looks like a working rule that simply never denies.

## The contract

* **`UserContext`** — `user_id` **only**. Role and tier are deliberately absent: roles belong to
  Identity & Access and no rule branches on either. A test asserts their absence. Add them when a
  rule genuinely needs them.
* **`CalendarContext`** — `week_starts_on` (a `Weekday` enum) and `now`. **No timezone field**, and
  there is never going to be one — a zone here would be readable by every rule in the canon,
  generated ones above all, and a rule that converts for itself is a rule whose correctness depends
  on it having picked the zone the caller meant. `week_starts_on` stays because it is a calendar
  *convention*, not a zone: it names which day a week begins on and nothing about where the venue is.
* **`LocalFrame`** — `day_start`/`day_end`, `week_start`/`week_end`, `month_start`/`month_end` (UTC
  instants bounding the booking's **local** day, week and calendar month), `weekday` (int, 0 =
  Monday), and `start_minutes`/`end_minutes` (minutes from local midnight to the request's own
  bounds). **It carries no timezone and no offset either**, and that is what makes the absence above
  survivable: the frame does not reintroduce a zone, it removes the *need* for one, by pre-answering
  every local question a rule could ask as a UTC instant or a plain integer. Without it, only the
  types the adapter holds a bespoke case for can express anything local and a type nobody hand-wrote
  can express nothing — so a generated "no more than 3 hours a day" would count against the **UTC**
  day and be wrong for every venue that is not on UTC. Each pair is half-open and is rejected if
  inverted: these are two absolute instants with no wrap to describe, so an inversion is the caller
  bug it looks like — the same reasoning the counting rules' window already gives, and (since
  `AvailabilityHoursRule` moved onto plain minutes) nothing in this contract holds an invertible pair
  at all any more. `end_minutes` **may exceed 1440 and is
  never capped**: that is a booking running past local midnight, and representing it is the point.
  Every bound is resolved from local midnight and nothing else, so a day, a week and a month all
  begin when the *venue's* day begins; and because a local day is 23 or 25 hours across a DST
  transition, `day_end` is the local midnight of the next date rather than `day_start + 24h`.
* **`BookingRequest`** / **`BookingRecord`** — `user_id`, `resource_id`, `start_at`, `end_at`.
* **`HistoryContext`** — `bookings`, the caller's pre-filtered, pre-capped list. **Everything in it
  counts.** It is filtered to the requesting **user**, never to one resource: a caller may legitimately
  draw it from several resources at once — the backend counts a frequency cap across every Resource in
  a Space, not per court — and the controller's cross-check enforces only that the user matches, not
  the resource. `BookingRecord` has no status field and the engine never inspects one: filtering
  belongs to the layer that owns the schema, so a future `deleted` or no-show flag cannot silently
  obsolete every rule that forgot to check it.
* **`RunContext`** — `start_at`, `end_at`, `booking_count` (a plain `int`, at least 1, the request
  itself included) and `duration` (a property, `end_at - start_at`). The contiguous, cross-Resource
  span of this user's own bookings the request sits in: by default, two consecutive bookings are
  treated as one session rather than two independent ones
  (`ops/pending/bugs/max-duration-cannon.md`, "Resolution: the run"), and a rule that wants that
  reading reads `context.run` instead of `request`; a rule that does not is unaffected by its
  existence. This is deliberately **not** built by rewriting `request` into the merged span and
  handing the engine that instead — the obvious shortcut breaks four things: `NotInThePastRule`
  denies a merged start that predates `now` for a booking genuinely extending one already in
  progress; `LocalFrame` resolves against the wrong date when a merged start crosses local midnight;
  `BookingHorizonRule` only ever loosens, since a merged start can look no further out than the real
  one; and the counting rules double-count a session unless history is rewritten too. Carrying the
  run as its own field on `Context` means every rule that does not explicitly read it keeps judging
  the request it was actually asked to judge.
* **`Context`** — aggregates `user` / `calendar` / `local` / `run` / `history` and enforces the
  history-window invariant. **`local` and `run` are both required and have no default**: the only
  frame a default could name is the UTC one, and a rule reading that is silently wrong for every
  venue off UTC — the precise bug the frame exists to remove, reintroduced as a convenience; the only
  run a default could name is "the request alone, count 1", which is right for a request with no
  neighbours and silently **permissive** for one the adapter forgot to resolve, since a run-aware cap
  would then see a one-hour run where the user holds six — the direction this codebase's fail-closed
  discipline does not accept. Every caller resolves a real frame and a real run or does not get a
  context. `local` was the first time the aggregate's promise below — that a field can be added
  without touching `evaluate`'s signature — was spent; `run` is the second.
* **`RuleResult`** — `passed` (bool), `fail_reason` (`str | None`, friendly copy shown verbatim in
  the UI). Named `passed` because `pass` is a keyword.
* **`BaseRule`** — abstract, requiring `evaluate(self, request, context) -> RuleResult`. The
  aggregate `Context` replaces four positional parameters so a new kind of context can be added
  without breaking the signature of every rule ever written. Adding a parameter to `evaluate` is a
  breaking change across the whole canon; adding a field to `Context` is not.

**UTC everywhere.** Every datetime crossing this boundary is timezone-aware with a **zero** offset;
naive datetimes and non-zero offsets are both rejected at construction. Rules read `.hour` to enforce
opening windows, so a `+02:00` value would yield a *local* hour and silently mis-enforce them.
Callers `.astimezone(timezone.utc)` at the boundary.

**History is bounded** to the current calendar month or a rolling week. A rule may not reach past it.

**The adapter is the only thing in the system that knows a timezone.** No type in this contract
carries one and none will. Every local question — the venue's day, week, month, weekday and the time
of day a booking starts — is answered by the caller before a rule runs and handed over on
`LocalFrame` as an absolute instant or a plain number. A rule reads the answer; it never converts.

## Controller

`evaluate_request()` is the single entry point the backend calls for **policy**, but it is no longer
the first thing a booking meets. `create_resource_booking` now runs a structural availability gate
ahead of it (`shape.permits`, task 10.3, `.claude/rules/calendar-shape.md`): archived check, then
shape, then this controller, then the driver. The engine is still the only thing that judges *who*
may take a slot and *how much* of it — the shape only says what the venue offers at all — so nothing
below this heading changes; a booking simply no longer reaches `evaluate_request` unless the shape
has already offered the slot it asks for.

In order: cross-check the request against the context (`Context` cannot do this itself — the
request is not in scope when a context is built), run the canon in order **fail-fast** (the first
denial wins and nothing after it runs), and contain a buggy rule.

**The cross-check covers the local frame as well as the history's user.** `local.day_start <=
request.start_at < local.day_end`, or `ContextMismatchError` — same reasoning as the user check: a
frame resolved for the wrong date describes a different stretch of the calendar than the booking
sits in, so a rule counting "bookings in this local day" would quietly count another day's, and
answering "denied" would present that adapter bug as an ordinary refusal. Fail closed on the
outcome, loud on the cause. **`start_at` only** — `end_at` may legitimately fall past the local day,
which is exactly what an `end_minutes` above 1440 means.

**The cross-check covers the run too.** `run.start_at <= request.start_at and request.end_at <=
run.end_at`, or `ContextMismatchError` — a run that does not contain its own request describes a
different stretch of the calendar than the booking sits in, so a rule reading `run.duration` or
`run.booking_count` would silently judge someone else's session. Both bounds are checked because a
run may legitimately be wider than the request on either side (the request extending a run that
already reaches past it) or both (the request landing inside a run two hops away on either side).

## Backend integration

`rules` is installed into the backend as an editable sibling package (`-e ../../rules`), and
`rules/pyproject.toml` distributes **both `rules` and `generation`**. Neither carries a runtime
dependency, so the backend's dependency set does not move.

**A generated rule now runs inside the booking process, and what replaces the guarantee that gave up
is stated here rather than assumed.** The app once imported `rules` alone, which made "nothing
generated is imported by the app" a fact about the packaging; that is not a fact about anything any
more, and it is withdrawn rather than softened. The properties carrying the weight instead are all
enforced on every load, not once at authoring time:

* **The AST validator runs at write time *and* at every load.** `validate_source` re-parses the
  stored `human_code` each time a row is hoisted (`app/backend/app/rule_catalog.py`), and a row whose
  source no longer validates is not hoisted whatever its stored blob says.
* **The parameter contract runs at every load too**, and for a different reason: the rows already in
  the table were written before it was enforced anywhere. `hoist` refuses a row whose rule treats a
  constructor parameter as a `timedelta` — see "Every parameter arrives as an integer" below.
* **The adversarial suite passed in the sandbox before the row existed.** Nothing reaches the table
  that did not survive the generation loop's own test run.
* **Execution happens in a restricted namespace**, never the real builtins — see below.
* **The controller contains every call**, exactly as it does for a hand-written rule. A generated
  rule that raises is a denial, not a 500.

The one property genuinely surrendered is that the *executed* artifact is provably the *validated*
one: `source_sha256` binds `human_code` to itself, but nothing proves `executable_bytecode` was
compiled from that source. That gap is why the namespace's `__import__` is a guarded one enforcing
the import allowlist at runtime rather than the real builtin — it is the only check that binds the
bytecode, rather than the source standing in for it, to the allowlist.

**Hoisting is the load path, and it fails closed by denying rather than by skipping.**
`app.rule_catalog` turns one `generated_rule_types` row back into a constructible `RuleType`: it
re-validates the source, checks `sha256(human_code)` against `source_sha256` and `bytecode_magic`
against the running `importlib.util.MAGIC_NUMBER` (the magic, not `python_version` — `marshal`'s
format tracks the magic number, while the version string is what a human reads in a report),
`marshal.loads` the blob, executes it, and requires **exactly one** `BaseRule` subclass to come out.
A row failing any step is logged with its id and reason and left out of the catalog — it never
raises past `reload`, because one bad row must not stop the others loading nor stop the process
booting. A `space_rules` row naming a type that did not hoist then denies with `RULE_ERROR_MESSAGE`
through the path an unregistered `rule_type` already took. That is the fail-closed outcome and needs
no new code: **an unavailable rule is a rule that refuses, never one that is skipped**, since
skipping it would silently drop a constraint a Space had configured.

**The namespace a hoisted rule executes in binds `SAFE_BUILTINS`, the eight engine free names, and
the two pieces of execution machinery a module cannot run without.** `SAFE_BUILTINS` is a list of
names a rule may *write*; a compiled module also calls builtins its source never mentions — every
`class` statement calls `__build_class__` and every `import` calls `__import__`. Withholding those
two hardens nothing and makes *every* generated rule unhoistable, since a rule is a class by
definition and the Generator's prompt requires it to import anything outside the eight free names.
`__import__` is the guarded one above; `__build_class__` is handed over as-is, because building a
class is what the source was validated as doing. The free names come from `harness.ENGINE_NAMES`
rather than a second list, for the same reason that tuple exists at all. This namespace is
deliberately **stricter than the sandbox**, which runs a candidate under the real builtins: the
sandbox bounds what execution can *cost*, this bounds what it can *reach*, so a rule that passes
there and raises `NameError` here fails closed — the direction it is safe to be wrong in.

**The catalog is the backend's, and `rules.REGISTRY` is never mutated.** `REGISTRY` is a
module-level constant in a package the backend *installs*; writing into it would invert that and
make the engine's behaviour depend on which HTTP requests a process had served. `app.rule_catalog`
reads it and adds its own separate map, which is the same information with the arrow pointing the
right way. `rules_stub` resolves a `rule_type` through a `lookup` callable carried on
`SpaceRuleConfig` rather than importing the catalog, because that module stays ORM-free and the
catalog is nothing but ORM; the callable defaults to `REGISTRY.get`, and only
`identity.service.space_rule_config` passes `catalog.lookup`.

**The catalog reloads at startup and once on a miss, and the miss reload is throttled on the
attempt.** Multi-worker uvicorn gives each process its own catalog, so a type hoisted in worker A is
invisible to worker B until it reloads; reloading on every lookup would cost a query on the hot path,
while reloading on a miss costs one only where the request was about to deny anyway. The cooldown
stamp is taken **before** the attempt rather than after a success, because stamping on success alone
leaves the throttle unarmed for the one failure it most needs to cover — a database that is down
makes every miss open a fresh connection, so the moment Postgres can least absorb load is the moment
the guard would stop working. A genuinely unknown id still denies; self-healing never substitutes
for failing closed.

`app/backend/app/rules_stub.py` is the adapter, and the whole of it. It holds no rule logic; it
translates between the HTTP boundary and `evaluate_request`, and six translations live there and
nowhere else. **It converts every datetime to UTC** (`.astimezone(timezone.utc)`) before building
engine types — the engine rejects a non-zero offset outright, so a booking a client sends as
`+02:00` must be converted, and is then judged on its UTC wall clock. **It supplies the allow-path
message**: `RuleResult(passed=True)` carries no copy, but the API shows friendly text on success. **It
assembles the canon per Space** from that Space's own `space_rules` rows rather than running
`DEFAULT_CANON` — a rule type the Space holds no matching row for is simply not in the canon.
**It passes Space-wide history**
only when the Space's canon includes a counting rule: the router loads the user's bookings across
every Resource in the Space, capped to `history_window`, and passes them in; with no counting rule
configured, history stays empty and no query runs. **It resolves the counting windows** in the
Space's own zone — the local day, week and local calendar month containing the booking, converted
to UTC instants and handed to `MaxBookingsPerDayRule` / `MaxBookingsPerWeekRule` /
`MaxBookingsPerMonthRule`, which derive none of them, and the local day alone to
`MaxDurationPerDayRule` (task 8.7), which needs no tolerance since it never merges (see
`frequency.py`'s own section below). **It resolves the `LocalFrame`** every context carries —
`_build_local_frame`, the general form of the per-type resolution above. Those five types get
bespoke resolved parameters only because they were written before the frame existed; a type nobody
hand-wrote has no such case and can express a local day through the frame or not at all. Every
bound in it comes from
`_local_midnight_utc` and nothing else, and `_local_day_bounds` takes the *next date's* local
midnight rather than adding 24 hours, for the reason `_local_week_bounds` already gives one line
down. `start_minutes` / `end_minutes` are the elapsed minutes from that local midnight, derived from
the instants rather than from a wall clock, which is what keeps them right on a 23- or 25-hour day;
the end rounds up, since rounding it down would report a booking as finishing earlier than it does
and is the permissive direction. `DEFAULT_CANON` is no longer what the API runs — it remains the *reference* canon the
generation loop is measured against and the source of the default values a Space that overrides
nothing would use.

**It resolves the run** every context carries — `_resolve_run`, task 8.4. The request plus every
history entry (already Space-wide and user-filtered, so cross-Resource falls out of the input rather
than needing to be built) are swept once, sorted by `start_at`, merging while the gap to the next
span is zero, negative (an overlap, reachable across two Resources), or smaller than a **gap
tolerance**; `_resolve_run` picks the merged span the request itself fell into back out of that
sweep. The merge is **transitive** — 17-18 and 18-19 held, a request for 19-20, is one 17-20 run —
so a user can be denied by a booking two hops away, and that is intended. When history is empty the
only span in the sweep is the request's own, so the run is the request alone with
`booking_count=1`; that is the correct answer, not a degraded one. The sweep itself is
`rules.spans.merge_adjoining_spans` (task 8.6 moved it out of this module and into the engine
package), because `_resolve_run` is no longer its only caller: `MaxBookingsPerDayRule` /
`MaxBookingsPerWeekRule` / `MaxBookingsPerMonthRule` (below) call it themselves too, on `request`
and `context.history.bookings` together, at evaluate time. `MaxDurationPerDayRule` (task 8.7) is
the one exception — it reads the same raw `context.history.bookings` but never calls
`merge_adjoining_spans`, so it takes no tolerance at all (see `frequency.py`'s own section below).

**The gap tolerance is that date's own resolved session length, zero when no `session_length` row
governs the date — not exact abutment, even though the run's own design note (`max-duration-cannon.md`,
decision 3) chose exact abutment first.** That decision rests on every booking landing on a grid,
which is what makes a sub-session gap unconstructable; `session_length` is a per-Space row, not a
property of the engine, so a Space configuring none has arbitrary start times, and a gap smaller than
one session — 17:00-18:00 and 18:05-19:05, with a 5-minute gap — is dead space nobody could ever
construct a booking to fill. Left as exact abutment, that gap would fracture every run-based rule for
free. A tolerance equal to the session length closes it without branching on whether a grid exists:
any gap a legal booking could actually occupy is at least that long, so a gap shorter than it is
indistinguishable from no gap at all, and a Space with no `session_length` row gets `tolerance == 0`
— exact abutment, unchanged.

**Where a grid does exist the tolerance changes nothing, and that is worth knowing before anyone
"simplifies" it away.** Every booking on the grid starts and ends on it, so every gap between two of
them is a whole multiple of the session length, and `merge_adjoining_spans` joins on `gap <
tolerance` strictly — so a gap of exactly one session does not merge, and the only gap that does is
zero. A tolerance of one session and a tolerance of zero are therefore behaviourally identical for a
Space with a grid. The tolerance earns its place on the Space that has no `session_length` row at
all, which is the case it was written for.

The resolution is read from
`resolve_day_schedule(config, on_date).session_minutes` rather than re-derived here, including
its combining rule (the LCM of every matching row's own length) — a second
implementation of "what session length governs this date" would be exactly the drift this document
keeps warning about. That same field is also reported over the wire by `GET
/spaces/{public_id}/schedule`, along with a per-date coherence issue — a resolved session length
longer than the resolved operating window means nothing on that date is bookable at all. **Nothing
renders that endpoint any more**: the calendar grid draws a Space's own shape projection instead
(`.claude/rules/calendar-shape.md`), so what is left of `resolve_day_schedule` is the gap tolerance
this paragraph is about.

`rules_stub.py` also holds one thing that is not a fifth translation onto `evaluate_request` — it
never calls it. `resolve_day_schedule`, called by `GET /spaces/{public_id}/schedule` and by the run's
own gap tolerance above, reports what a booking on a given date *would* be judged against — the
slot size and operating window, resolved from that Space's own `space_rules` rows — for display, not
judgment. It reuses `row_applies`, the identical `applies_to` matching `_build_canon` uses, so
"which rows govern this date" cannot drift between the two call paths, but it never touches
`REGISTRY`, `RuleType.build`, or `evaluate_request` itself, and it resolves in the Space's own local
wall clock rather than converting to UTC — there is no instant to judge here, only a calendar date to
describe. It lives in this module because this is already the one place that reads `space_rules` and
already resolves `applies_to` against a date, not because reporting a schedule is part of the
adapter's job of judging a booking.

`ContextMismatchError` is deliberately not caught at this boundary: the adapter builds both the
request and the context from one booking, so a mismatch is an adapter bug and must reach the error
tracker, not be served as a polite refusal.

## The canon

`canon.py` holds six hand-written request-local rules: `NotInThePastRule()`,
`BookingHorizonRule(days)`, `MaxDurationRule(max_duration)`,
`MaxConsecutiveDurationRule(max_duration)`, `SessionLengthRule(session_minutes, anchor_minutes)`,
`AvailabilityHoursRule(opens_at_minutes, closes_at_minutes)`. They are written by hand rather than
generated — they are the reference the generation loop is measured against, and the worked example
of the rule shape.

**Parameters live on the instance, never as module constants.** A Space allowing 45-minute bookings
and one allowing two hours are the same rule with different arguments, so per-Space configuration is
a change to how the canon is built rather than a change to any rule. `DEFAULT_CANON` is the reference
assembly of four of these six at their default values — `SessionLengthRule` and
`MaxConsecutiveDurationRule` are the two missing, for different reasons.
`SessionLengthRule`'s `anchor_minutes` is the date's own resolved opening time, so a literal baked
into a module-level constant at import time would be correct for the hours it was written against and
silently wrong the day an admin changed them — the same cached-value mistake `CLAUDE.md` warns
against elsewhere. It is therefore never part of `DEFAULT_CANON`, the same way the frequency rules in
`frequency.py` are registered and importable but kept out of it: an adapter resolves the anchor per
booking date and builds the rule fresh. `MaxConsecutiveDurationRule` is
excluded for the reason every rule type added since Stream 6 has stayed out of it: `DEFAULT_CANON`
is frozen at the four types it already asserts against, and adding a fifth would change what those
assertions cover without anyone asking them to. The canon the API actually runs is built per Space
(see Backend integration), where a type with no matching row is absent from the canon entirely and
`SessionLengthRule` is constructed fresh, with a freshly resolved anchor, for every booking.
`NotInThePastRule` is the only one always present — you can never book the past, whatever a Space
configures.

**There is no rule that bounds a booking's length from below on its own.** A floor is what
`SessionLengthRule` already produces: a booking that starts and ends on the grid cannot be shorter
than one session, so a separate minimum-duration type would be a second way to say the same thing and
a second row an admin has to keep consistent with the first. A rule that needs to judge a *run* of
adjoining bookings rather than one booking reads `context.run.duration`; that is a different question
with a different remedy, and `MaxConsecutiveDurationRule` is the type that asks it.

**`MaxConsecutiveDurationRule` denies when `context.run.duration` exceeds `max_duration`** —
the contiguous, cross-Resource span of back-to-back bookings the request joins (`RunContext`, "It
resolves the run" below), never `request.duration`. This is the rule that closes
`ops/pending/bugs/max-duration-cannon.md`: a Space configuring "max 2 hours" meaning *one session*
was already served by `MaxDurationRule`, which reads the request's own span and has no way to see
anything either side of it; a member booking 17:00-18:00 and then, separately, 18:00-19:00 passed
that rule twice and walked away with four hours of court time under a rule meant to cap two. **A
rule type declares which span it judges** — `request.duration` is one booking, `context.run.duration`
is the contiguous session it sits in — and that is the whole axis this rule opts into: a Space
configures `max_duration`, `max_consecutive_duration`, both, or neither, and configuring the new one
never silently changes what the old one already enforces, since a run always contains its own
request and an inclusive bound on the run can never pass what the identical bound on the request
alone would already have denied. The bound is inclusive, the same convention every duration rule in
this canon shares. Priority 32, between `max_duration` (30) and `session_length` (35): a booking
that breaks both duration rules at once is more usefully told to shorten itself — fixable by editing
only this request — than to stop abutting a neighbour, which is fixable only by touching a booking
that already exists, so `max_duration` keeps first refusal. **`reads_history=True`, though
`evaluate` never names `context.history`** — see "It resolves the run" and the registry paragraph
below for why that is still the correct flag, not an inconsistency. Its denial copy states two
durations, the configured cap and what the run would come to, and is worded around *consecutive
play* rather than the booking's own length precisely because a one-hour request that gets denied for
joining an existing run must never read as "your one-hour booking is too long" — that would be false,
and this codebase's denial copy is contract (`rules/tests/test_denial_copy.py`, "Denial copy is
contract" below).

**`SessionLengthRule` denies a booking whose start or end is not on the grid** defined by
`session_minutes` and `anchor_minutes` — both bounds are checked, so an aligned start with an
off-grid end is denied on the end, and because both must land on the grid a booking is always a whole
number of sessions and can never be shorter than one. It reads `context.local.start_minutes` /
`end_minutes` against two plain integers and holds no datetime at all, exactly as
`AvailabilityHoursRule` does; Python's `%` is non-negative for a negative left operand, so a start
earlier than the anchor lands on the same extended grid with no special case.

**The anchor is the date's own resolved opening time, and coupling the two rules is the point.** The
adapter resolves it from that date's own `availability_hours` rows, falling back to local midnight
when none governs the date. This is a deliberate coupling of two rule instances rather than an
accident of implementation: a venue's sessions begin when the venue opens, so a grid anchored
anywhere else describes a schedule nobody asked for — an opening time of 09:15 against a
midnight-anchored hourly grid has no session starting at 09:15 at all. The `applies_to` objection
that a midnight anchor would sidestep is answered by resolving per date rather than per Space: both
values are already resolved for each date by `resolve_day_schedule`, which is the one implementation
of "which rows govern this date", so two rows scoped to different days each get their own correct
anchor.

This closes a real gap rather than tightening a theoretical one: the grid was, until a rule enforced
it, the one piece of a Space's configuration nothing on the server read — the calendar UI
declined to *offer* an off-grid slot, but the API accepted one anyway, which is exactly the split
this document warns about elsewhere (the grid is advisory, the engine is the only boundary that
counts).

**A rule type is registered, not just implemented.** `rules/rules/registry.py` gives each of the
ten classes above a runtime identity separate from being importable Python. A registered type
declares: a **stable string id** (`not_in_the_past`, `max_duration`,
`max_consecutive_duration`, `session_length`, `availability_hours`, …) that a future
`space_rules.rule_type` column stores — never the Python class
name, since renaming the class must not silently orphan every row that named it; a **label** and a
**description** — the description is prose for an admin choosing between rule types, "what it
refuses", never a restatement of the code; an **ordered parameter schema**, one `RuleParam` per
constructor argument (name, kind, label, unit, required, a minimum), rich enough to render an admin
form field and to validate a request body's params against — one schema for both jobs, because two
independently written ones would drift, and the drift would show up as a form whose own submission
gets refused; a declared **priority**; **`reads_history`**, true for the three counting rules
(`max_bookings_per_day` / `_week` / `_month`), `max_duration_per_day`, and
`max_consecutive_duration`, so a caller can skip the Space-wide history query when nothing
configured would read it. The flag means "this rule type's **verdict** depends on history", not "its
`evaluate` names `context.history`" — `max_consecutive_duration` is the type that pulls the two
apart, since its own `evaluate` reads only `context.run`, and it is `True` anyway because the run
itself is resolved from history before the rule ever runs ("It resolves the run" below); a Space
configuring that rule and nothing else that reads history must still make the router run the
Space-wide history query, or the run the rule receives is always the request alone and it silently
never denies — the exact silently-permissive failure this codebase refuses. A *generated* type's own
derivation of the flag (`generation/manifest.py`, `_mentions_history`) checks for `context.run` in
the source as well as `context.history`, for the identical reason: a generated rule that reads only
the run never spells "history" anywhere in its own text, and checking for that word alone would
under-report `True` for it exactly as it would have for a hand-written `max_consecutive_duration`.
That check can only ever raise the model's own declared claim, never lower it (the manifest call's
own "AI generation loop" paragraph below has the mechanics).
**`needs_local_resolution`**, true for `session_length` and the four history-reading
rules from `frequency.py` — the five whose constructor needs values resolved against the Space's
own zone and the booking's own date rather than the raw stored params, which is what keeps every
local-to-UTC conversion at the adapter boundary instead of inviting a rule type to convert for
itself. `availability_hours` is deliberately **not** one of them any more: it stores
minutes from the venue's own local midnight directly and reads them off `context.local` inside its
own `evaluate`, so its `build` function needs nothing resolved for it at all — the same local
question every other type still needs an adapter to answer for it, this one answers for itself once
handed a `LocalFrame`. **`is_single`**, advisory only and never a uniqueness constraint, since the
engine's flat AND makes two instances of one type coherent — they AND to the stricter, which is true
for every type whether or not it is flagged. For a type like `max_bookings_per_week` (or its daily
and monthly siblings, or `max_duration_per_day`) a second instance is almost certainly not what an
admin meant, so it is `is_single`; `availability_hours`,
`max_duration`, `max_consecutive_duration` and `session_length` are deliberately
**not**, because scoping each
instance to a different day or date set via `applies_to` — "Mon/Wed/Fri 10–15" and "Tue/Thu 8–12" as
two `availability_hours` rows, or a finer grid on weekday evenings than on a Sunday morning — is the
intended way to use them, not a mistake to warn about. And a **build function** from validated params
(plus, for a type with `needs_local_resolution`, a second mapping of already-resolved values) to a
constructed instance of the class it names. `session_minutes` must divide 1440 so a day holds a whole
number of sessions; the schema has no field to express that bound, so it is stated twice — at the write
boundary, where an admin gets a 422 naming the constraint (`identity-and-access.md`), and inside
`SessionLengthRule`'s own constructor, which keeps it true for a row written by any other means.

**A generated type's label, description and parameter schema are authored by the model, not the
admin's own prompt, and the parameter schema is derived rather than trusted.** The prompt that asked
for the rule is retained as provenance (`identity-and-access.md`) but is never shown to an admin
choosing a rule type — it is the request, not the description of what was built. Once the generation
loop reports `PASSED`, one further model call (`generation.manifest.generate_manifest`, "the
manifest call") is made against the exact verified source, never a candidate still under retry, and
answers three things: a short `label`, a `description` written for someone who will never read the
Python, and a `params` list declaring each constructor argument's `kind` (one of `ParamKind`'s
members and nothing else — a kind the model invents is a field nobody wrote a widget for), `label`,
`unit`, `required` and `minimum`. **The parameter names are then cross-checked against
`inspect.signature` of the class the verified source actually defines** — same set, no extras, no
omissions apart from `self`. This is the load-bearing check: a manifest that disagrees with
`__init__` would let an admin form submit a body that raises `TypeError` building the rule, which the
adapter turns into a denial, which reads to that admin as every booking refused for no visible
reason. A mismatch — `ManifestRejectedError` — fails the job outright rather than triggering a retry:
the rule source already survived its own adversarial suite, and regenerating it to fix a description
would throw away the artifact that passed. **`reads_history` is corrected against the source, never
trusted from the model's claim alone** — `true` stands only if the source also mentions
`context.history`, because the damaging direction is a false positive that would run a counting rule
against a history nobody queried, silently permissive in exactly the way this codebase never accepts.

**Rule order comes from a type's declared priority, never from row order, insertion order, or an
admin's own arrangement.** An assembled canon sorts by priority, then by row id for two instances of
the same type. Priorities are spaced in multiples of ten rather than assigned consecutively (`32`
and `35` are the deliberate exceptions, `max_consecutive_duration`'s own and `session_length`'s —
see below), so a later type can be inserted between two existing ones without renumbering the rest
of the registry.

**The order a canon assembled this way runs in reproduces `(NotInThePast, BookingHorizon,
MaxDuration, MaxConsecutiveDuration, SessionLength, AvailabilityHours,
MaxDurationPerDay, MaxBookingsPerDay, MaxBookingsPerWeek, MaxBookingsPerMonth)` because that is what
each type's declared priority sorts to, and it arbitrates user-facing copy.** The controller is
fail-fast, so the first rule to deny decides the single message shown when a request breaks several
rules at once. The date rules run first because they reject a booking on *when* it is, which no
shortening or shifting within the day can fix; telling someone to trim a three-hour booking that
sits 90 days out sends them to fix the one thing that is not the problem. Duration (both flavours),
session length and availability hours are all remedies the user can apply to an otherwise
bookable date and time — shorten it, stop abutting a neighbour, line it up with the
grid, pick another time — so they follow, in that order. `max_consecutive_duration` sits **after**
`max_duration` at priority 32, because the two can break on the same request and shortening it is a
remedy `max_duration`'s copy can name that `max_consecutive_duration`'s cannot, so the rule naming
the fixable-by-editing-this-request problem gets first refusal over the one whose fix means touching
a booking that already exists. `session_length` sits at 35, beside the duration rules rather than
with the date rules above it or the counting rules below, because "line up with the grid" is exactly
as fixable-within-the-date as "shorten it" or "pick another time" is — and 32 and 35 both landed
between existing tens without moving `max_duration` off 30 or `availability_hours` off 40, which is
what the spacing discipline is for. `session_length` runs **before** `availability_hours`, so a
booking that is both off-grid and outside opening hours is told about the grid first; both messages
are actionable and the ordering is inherited rather than argued for. Past and horizon are mutually
exclusive and never arbitrate against each other. **The four counting and total rules come last**
because no shorter, earlier or later booking clears a frequency cap or a daily total on its own the
way it clears every rule above — moving *when* the request is never fixes *how much* history there
already is. Among the four, `max_duration_per_day` (42) and `max_bookings_per_day` (45) precede the
week (50) and month (60) caps: of the three windows a user could break a cap in at once, the
narrowest is the most useful thing to be told about first. And between the two day-scoped rules,
`max_duration_per_day` outranks `max_bookings_per_day` for the same reason `max_duration` outranks
`max_consecutive_duration` above — its own denial copy can still offer "shorten it", a fix within
this request, where `max_bookings_per_day`'s cannot: a count over the cap has no shorter form, only
another day entirely.

**Denial copy is contract, not wording.** `app/e2e/tests/03-sad-path.spec.ts` asserts the
max-duration message as a full-string match and reproduces the singular/plural and `" and "` join of
the engine's duration formatting. Rewording a canon message is a breaking change to a test in another
package.

**No absolute time, date or zone name ever appears in copy the engine produces.** Not a clock time,
not a calendar date, not "UTC" — whatever the rule, hand-written or generated. The engine has no
timezone, so every datetime it holds is UTC, and a time it prints is UTC wearing no label: a Berlin
member refused at 19:00 local was being told the club "closes at 17:00", which is neither the time
they typed nor the time on the door, and nothing in the message told them which clock it was in.
`AvailabilityHoursRule` therefore names *which* bound the booking missed in words — before we open,
after we close — and points at the calendar for the hours themselves, which is where the venue's own
zone is known and the only place they can be rendered honestly. A **duration is not an absolute
time**: "at most 2 hours long" says the same thing everywhere and stays legal, and the check that
enforces this has to tell the two apart or it will be loosened until it catches nothing.

**The constraint is enforced twice, because neither enforcement covers the other's code.**
`rules/tests/test_denial_copy.py` drives every rule in `DEFAULT_CANON` and every type `REGISTRY` can
build into a denial and greps the copy — and it asserts its own membership against both collections,
so a type added later cannot slip past unchecked. It says nothing about generated rules, which are in
neither collection, so the Generator's system prompt states the constraint as a hard constraint of
its own. Fixing the one rule that named an hour without teaching the Generator would have bought one
venue and reintroduced the defect on every rule authored after it.

**Availability hours are `LocalFrame` minutes, not clock times.** `opens_at_minutes` and
`closes_at_minutes` are plain integers — minutes from the venue's own local midnight — compared
against `context.local.start_minutes` / `end_minutes`. There is no UTC clock time in this rule at
all, and nothing left for the adapter to resolve per booking date: a `space_rules` row's two params
are read straight into the rule at construction (`needs_local_resolution=False`, "The canon" below).

**`closes_at_minutes` may exceed 1440, and that is how a window past local midnight is represented**
— 18:00 to 02:00 is `opens_at_minutes=1080, closes_at_minutes=1560`, plainly, with no inversion to
read a meaning into. The constructor enforces `0 <= opens_at_minutes < 1440` and `opens_at_minutes <
closes_at_minutes <= opens_at_minutes + 1440`: a window is at most 24 hours long and always starts on
the day it is configured for, so what a bare pair of clock times could only express as an ambiguous
inversion is now a value nothing can typo into existing at all. `evaluate` still has to pick between
at most two candidate occurrences of the recurring window — today's own, or the tail of yesterday's,
shifted back a full day (`closes_at_minutes - 1440`) — but that choice is between two non-overlapping
integer ranges now, not a reconstruction from a bare `time` with its date stripped off.

Because the range is enforced by the rule's own constructor, **the identical range is enforced again
at the write boundary** (`identity-and-access.md`): a submission failing it is refused with a 422
naming the problem rather than accepted and only discovered as `RULE_ERROR_MESSAGE` denying every
booking the next time someone books — the same reasoning `session_length`'s own write-boundary check
already gives for mirroring its rule's constructor bound. That write-boundary check is also the one
place a venue open past its own local midnight is admitted deliberately, rather than arriving by
accident through a typo — the same role it always played, now stated as a range rather than an
ordering.

`frequency.py` holds four rules, the only ones whose verdict depends on anything beyond the
request: three that **count** — `MaxBookingsPerDayRule(n, window_start, window_end, tolerance)`,
`MaxBookingsPerWeekRule(n, window_start, window_end, tolerance)` and
`MaxBookingsPerMonthRule(n, window_start, window_end, tolerance)`, built and behaving identically
bar the window each is handed (task 8.7 added the day rule beside the pre-existing week and
month ones) — and one that **sums**, `MaxDurationPerDayRule(max_duration, window_start,
window_end)`, a *total* rather than a count and the one rule in this module that does not merge
history into runs at all. They are **exported but deliberately absent from `DEFAULT_CANON`**, the
reference canon of four of `canon.py`'s six hand-written rules (`SessionLengthRule` and
`MaxConsecutiveDurationRule` are the other two missing, each excluded for its own, different reason — see
"The canon" above). The API does not run `DEFAULT_CANON`; it assembles a per-Space canon that
*includes* one of these four when a Space holds the matching `space_rules` row. Keeping them out
of the reference canon is what lets the end-to-end suite assert against a seeded Space configured
to those four rules' values without a history-reading rule silently changing the outcome.

**None of the four derives its own window — every one is handed it.** The window is a half-open
`[window_start, window_end)` pair of UTC instants passed at construction, and the rule does nothing
but read into it — count into it for the three counting rules, sum into it for the duration total.
Deriving it here is what made a local week wrong: a Sydney booking at 00:30 on Monday local is
13:30 **Sunday** in UTC, so a window snapped to UTC midnight put it in the previous week and a cap
of one admitted a second booking in the same Sydney week — the identical miscount a day window
would make for `max_bookings_per_day` / `max_duration_per_day` if either derived its own. A local
day, week or month has no fixed UTC representation and the engine has no timezone to find one
with, so the layer that does resolves it. This is the same boundary split availability hours
already follow, and `CalendarContext` still carries **no timezone field** for the same reason it
never did — a zone there would invite every rule, generated ones included, to convert for itself.

An inverted pair is a **caller bug** here and is rejected at construction, for all four rules. A
window here is two absolute instants, not a recurring daily one, so — like `AvailabilityHoursRule`
since it moved onto plain minutes — it has no wrap to describe at all.

**A booking is counted (or summed) against the window it starts in, and the window is anchored on
the request rather than on `now`.** A request three weeks out is judged against that week's
bookings, not this week's; anchoring on `now` would refuse next month's first booking because of
this month's traffic. That property now lives in the caller that computes the bounds —
`rules_stub._build_canon` resolves every window from the booking's own **local** date. Windows are
half-open, so a booking straddling a boundary counts once, against the side it begins on.

**A local week is seven local days, not `start + 7 days`, and a local day is the next date's local
midnight, not `start + 24h`.** Across a DST transition a week is 167 or 169 hours and a day is 23
or 25, and adding a fixed `timedelta` to the start would put the closing boundary an hour inside
the neighbouring window — the same class of error as the UTC snapping this replaced, one line
further down (`app.rules_stub._local_day_bounds` / `_local_week_bounds`, and their shared
docstring reasoning). The adapter resolves both ends of every window from local midnight
independently. `week_starts_on` stays a calendar convention on `CalendarContext`, read by the
adapter when it resolves the week; no rule reads it any more.

**The three counting rules bound runs, not rows, and each merges the request into them itself.**
Task 8.6's plan named a preferred mechanism first: have the adapter fold the request into the
merged runs ahead of time and hand these rules a `Context.history` that already reflects them, so
the `+1` would simply disappear. That mechanism does not survive contact with an invariant one
layer up: `Context.__post_init__` (`interfaces.py`) requires every entry in
`context.history.bookings` to fall inside `history_window(context.calendar.now)` — the "evaluation
costs at most one calendar month of history" promise. A request is not bound by that promise at
all (only by `BookingHorizonRule`, itself often configured weeks or months wide), so folding it
into `Context.history` raises on construction for any request beyond the window — denying an
ordinary future booking outright, on any Space running one of these three rules, not merely an
edge case. That conflict was not visible from the plan and is the reason the fallback mechanism it
also named is what shipped instead: `Context.history` stays exactly what it always was, raw rows,
and `MaxBookingsPerDayRule` / `MaxBookingsPerWeekRule` / `MaxBookingsPerMonthRule` merge `request`
with `context.history.bookings` themselves, inside `evaluate`, via
`rules.spans.merge_adjoining_spans` (task 8.4's sweep, shared rather than duplicated) and a
`tolerance` resolved by the adapter exactly as `context.run`'s is. Because the merge happens on a
live parameter rather than a `Context` field, no invariant governs how far in the future `request`
may sit.

Each counting rule then compares the merged count directly, with no `+1`: two bookings the same
user holds back to back are one session against the cap, not two
(`ops/pending/bugs/max-duration-cannon.md`, "Counting runs, not rows"), so `existing + 1 >
max_bookings` would double-count the very session a request completes. Since the request is always
one of the spans the sweep merges, a request that opens a new session raises the count by one; one
that only extends a run the user already holds raises it by zero, since extending a run never moves
where it starts. **A run counts on the side it begins**, matching every other window in this
contract: extending a run that started last week adds nothing to this week's count — consistent,
and mildly surprising the first time someone meets it, but the alternative is counting one session
twice.

**`MaxDurationPerDayRule` is the exception: it sums `request.duration` plus every history entry
starting in its window, and never calls `merge_adjoining_spans` at all.** This is not an oversight
— a later reader who "fixes" it to merge like its three siblings would introduce a silent
under-count. A total is the same number however the day's bookings are grouped, so merging would
ordinarily change nothing: two one-hour bookings held back to back sum to two hours whether counted
as one merged two-hour run or as two separate one-hour entries. But a user holding **two Resources
at overlapping times** breaks that equivalence — 17:00-18:00 on one court and 17:30-18:30 on
another overlap by half an hour, so `merge_adjoining_spans` (which treats an overlap, a negative
gap, exactly like an abutment) collapses them into one 17:00-18:30 run, 90 minutes, when the member
has actually booked two hours of court time across the two courts. Summing the raw entries instead
gives the correct two hours; merging first would silently report 90 minutes and let a cap that
should have caught this pass it. So this rule reads the raw `context.history.bookings` it needs
and sums it directly — the one place in this stream runs are deliberately not used. It also takes
no `tolerance`: nothing here merges, so there is no gap for one to close, and `_resolve_for_row`
resolves only the window for it, never a tolerance.

This merge mechanism is scoped to exactly the three counting rule types, and deliberately so: no
other rule in the canon reads `context.history.bookings` and merges it today
(`MaxConsecutiveDurationRule` reads only `context.run`, and `MaxDurationPerDayRule` reads the raw
entries directly per above), and a *generated* rule that counts raw history keeps counting raw
rows unless it is separately taught to merge — which is out of scope here and belongs with task
8.8's work teaching the Generator about the run.

Because the window follows the request, a request beyond the history window still merges (or sums)
— with its own history entries, if any are near enough in time or within the window — into a run
of its own (or a total of its own), and passes unless that alone is over the cap. That is the
documented bound of the engine's promise — evaluation costs at most one calendar month of history —
not a gap in these rules.

## Safe execution

Two halves, neither sufficient alone: `safety.py` validates candidate source statically before
anything writes or runs it, and `sandbox.py` bounds what execution can cost. The static pass
cannot cap CPU or memory; the sandbox cannot tell a rule that reads the filesystem from one that
reads a booking.

`validate_source(src) -> None` raises `UnsafeRuleError` and returns nothing else — there is no "safe
enough" verdict to inspect and no boolean a caller can forget to check. `rules/rules/safety.py` is
the authoritative list of what it rejects; the load-bearing choices are:

* **Imports are an allowlist** — `datetime`, `zoneinfo`, `math` — not a denylist of `os` and `sys`.
  A denylist is a standing guess about which module is dangerous, and it is wrong the first time
  someone reaches for `subprocess`, `socket`, or `importlib`.
* **Every `__`-prefixed attribute is refused**, not a curated set of dunders.
  `().__class__.__base__.__subclasses__()` reaches the whole loaded object graph from a literal, and
  no allowlist of module names constrains it.
* **Unparseable source raises `UnsafeRuleError`, never `SyntaxError`.** One exception type means a
  caller handling "this candidate is unacceptable" cannot let an unparseable one through by catching
  only the type it expected. Fail closed.

**A generated rule cannot import `BaseRule`.** `rules` is not on the import allowlist, and widening
it would readmit the whole package as a capability. `BaseRule`, `RuleResult` and the context types
are free names in generated source, bound by the namespace that loads it.

`sandbox.py` runs a candidate — and the tests written against it — in a subprocess under four
bounds: a wall-clock timeout, an `RLIMIT_AS` memory cap, a curated environment inheriting nothing
from the parent, and a fresh temp directory as cwd that is deleted with the run. The child gets its
own process session and the timeout kills the whole session, so something the candidate spawned
cannot outlive the run meant to bound it.

It returns a `SandboxResult` and never raises for candidate misbehaviour; a caller mistake — an
unusable filename, a non-positive timeout — does raise, the same split the controller draws between
a denial and `ContextMismatchError`. **`SandboxResult.passed` is true for exactly one outcome of
four**, so "we never found out" cannot be read as "it works". A timeout and a crash are not
successes, and a pytest run that collected nothing is a crash rather than a failure: reporting it as
a failure would invite a caller to read "the rule is wrong" where the truth is "the tests are
missing". An unverifiable candidate does not advance to the canon.

**Linux is the reference platform for the memory cap.** Linux honours `RLIMIT_AS` for the child's
whole address space; macOS accepts the same call and does not reliably enforce it. The Linux
behaviour is what is implemented — no weaker mechanism is substituted to make the platforms agree —
and where the cap cannot be imposed, the timeout remains as the bound that always holds.
`MEMORY_CAP_ENFORCED` reports which.

## AI generation loop

* **Generator** — takes a natural-language prompt ("users can only book twice a rolling week") and
  emits a Python class inheriting `BaseRule`, relying only on `HistoryContext`, `LocalFrame` and
  standard `datetime` math, with **parameterized** variables so the rule is reusable.

`rules/generation/` is a **sibling package of `rules`**, not part of it, and both are distributed.
The layout no longer buys the app any isolation from generated code — a generated rule is hoisted
into the booking process and run there, and `app.rule_catalog` reaches `harness.ENGINE_NAMES` from
this package to build the namespace it runs in. What the separation still says is narrower and
still worth saying: `rules` is the contract and the canon the API evaluates every booking through,
while `generation` is the authoring machinery a developer or a generation job drives. "Backend
integration" above states what replaces the isolation.

**The model is called through an `LLMClient` seam** — one method, `complete(system, prompt, model)` —
not an SDK directly. Three implementations ship. `ClaudeCliClient` shells out to
`claude -p --output-format json` and so needs no API key, only an authenticated CLI: an acceptable
dependency for a developer tool whose output is a file a human reviews, and one the booking API never
carries. `OllamaClient` calls a model served by a local Ollama daemon over its HTTP API, so it needs
neither a key nor a cloud account. `GoogleAIStudioClient` calls a hosted Gemini model over AI
Studio's REST API, and is the only one of the three that is both measurable and backed by a frontier
model.

**The CLI cannot serve the benchmark, which is why the seam exists.** A call whose real prompt is 10
input / 40 output tokens is billed for ~11.5k tokens of harness preamble at $0.015–0.023, and
`--system-prompt` with `--exclude-dynamic-system-prompt-sections` does not strip it — the overhead
stays and the cost *rises*, by losing the cache hit. Token, latency and cost figures measured through
this client describe the harness, not the prompt. `OllamaClient` is what the benchmark is given
instead: it reports the counts for the prompt actually sent and nothing else, and a local model costs
no money to call as often as a benchmark wants to.

**`OllamaClient` talks `/api/chat`, never `/api/generate`, and always with `stream: false`.** Chat
carries the system prompt as its own message; `/api/generate` would need it folded into the user
turn, and this package's long constraint-dense system prompt is precisely the variable under test, so
folding it in would benchmark a prompt nobody ships. Streaming is Ollama's default and answers with
newline-delimited JSON, one object per token — parsing the first yields an empty completion that is
rejected several layers later as bad rule source, blaming the model for a transport choice. It is
built on `urllib.request` and adds **no dependency**: `rules` is installed into the backend as an
editable sibling, so a package pulled in to serve a developer tool is a cost the booking API pays
forever. `cost_usd` stays `None` rather than `0.0` — a local model's price is not zero dollars, it is
a number this backend has no way to know, and the metadata fields are optional exactly so a backend
can say so. A model that is not pulled comes back as Ollama's own 404 and is surfaced as an
`LLMCallError` naming the `ollama pull` that fixes it, never as a generic HTTP failure — the same
lesson as `is_error` below, that a transport failure passed on as a completion is blamed on the model.

**`GoogleAIStudioClient` posts to `{base_url}/v1beta/models/{model}:generateContent` and carries
its key in the `x-goog-api-key` header, never a query string.** A URL is what every proxy and
exception handler in the path logs, and this one is a credential — keeping it out of the URL is what
lets the client's own failure messages name the URL freely. The key is read from
`GOOGLE_STUDIO_API_KEY` in the environment, falling back to a `KEY=value` line in `rules/.env`, which
is gitignored and stays that way: a hosted model needs a credential, and the one place it must never
reach is the repository. Environment wins over the file, so a shell export beats a stale value left
sitting in it. Reading it costs no dependency — a ten-line parser rather than `python-dotenv`, for
the same reason the transport is `urllib.request`: `rules` declares zero runtime dependencies and the
backend installs it editable, so anything added here is a cost the booking API pays forever. The
system prompt goes in `systemInstruction` and is never folded into the user turn, the identical
reasoning `OllamaClient` gives for `/api/chat` over `/api/generate`.

**AI Studio accepts a `seed` in `generationConfig` and does not honour it, so the client does not
send one.** Unknown keys there are rejected with a 400, so acceptance would ordinarily mean
something — but two live calls with an identical seed and temperature returned different
completions. `temperature` *is* applied and is sent. A benchmark run against this client therefore
records its temperature and records `seed` as unset, which is `BenchmarkReport`'s existing rule that
two reports agreeing on a sampling parameter neither applied are worse than two that say nothing
about it. Establishing this took live calls rather than a reading of the docs, and that is the point:
a client that sent a seed on the strength of the field existing would make every run it reported look
reproducible.

**What it can report and what it cannot.** `input_tokens` is `promptTokenCount`. `output_tokens` is
`candidatesTokenCount` **plus** `thoughtsTokenCount` where a thinking model reports one — a measured
call answered with 6 candidate tokens against 651 thinking tokens, so counting only the visible
answer would understate what the prompt cost by two orders of magnitude, and comparing that number
across models is the whole reason it is collected. The split stays recoverable in `LLMResponse.raw`.
`cost_usd` stays `None`, as it does for Ollama: a hardcoded price table goes stale silently and is
then reported as fact. `duration_ms` stays `None` because the API does not report one, and a number
invented here would be indistinguishable from a measured one.

**Every failure raises `LLMCallError` naming its cause, and three of them are not the obvious
status.** An invalid key comes back as **400 with `API_KEY_INVALID`**, not 401 — a 400 of that shape
names the key and `GOOGLE_STUDIO_API_KEY`, and any other 400 stays generic, because blaming the key
for a malformed body sends a developer to the wrong place entirely. A **429** names rate limiting
explicitly, so a five-example run that dies halfway through the free tier's per-minute limit is read
as one rate limit rather than five model failures; the client deliberately does **not** retry, since
the generation loop never retries an `LLMCallError` and a silent retry here would hide the limit
instead of reporting it. A **404** names the model id and the `GET /v1beta/models` that lists what the
key can reach. A blocked prompt, an empty candidate list, and a candidate that finished on `SAFETY`
or `MAX_TOKENS` with no text all raise rather than returning an empty completion — `MAX_TOKENS`
especially, since a rule is a few dozen lines and hitting the cap is a configuration fact, not a
model that answered badly.

**A failed CLI call is identified by `is_error` and the exit code, never by `subtype`.** A run that
404s on an unknown model id exits 1 and reports `is_error: true` while still reporting
`subtype: "success"`; reading the subtype would pass a human-readable error string on as if it were
rule source, to be rejected later for a syntax error and blamed on the model.

**Generated source is validated inside `generate_rule`**, after the markdown fence is stripped, so no
caller can hold unvalidated candidate source. A rejection raises `RuleRejectedError` carrying the
validator's message verbatim — it names the construct and its line, which is exactly what the retry
loop hands back, and paraphrasing would cost the model the detail that lets it fix the candidate.

### Every parameter arrives as an integer, and a duration arrives as minutes

**`space_rules.params` is JSONB, so a rule parameter can only ever be a JSON scalar.** `ParamKind`
has two members and both are `int` underneath; the write-boundary validator rejects anything whose
`type()` is not `int`. A duration is therefore an integer count of **minutes**, and a time of day an
integer count of minutes from local midnight. **Converting that into the type the rule's logic wants
is the rule's own job, in `__init__`** — `app.rule_catalog`'s `build` hands params over verbatim and
performs no unit-aware coercion, unlike the canon's hand-written build functions in
`rules/rules/registry.py`, which convert for the class.

That asymmetry is what made this the first real bug found against a generated rule: the contract was
written down in exactly one docstring, the system prompt's reference classes took `timedelta`
arguments, and a model copying the house style produced a rule whose `__init__` raised `TypeError`
the moment a venue added it — which, through the fail-closed path above, refused **every booking in
that Space** until an admin removed the rule.

Three changes hold it now, and they are deliberately not one:

* **The system prompt teaches it** (constraint 3), and its three worked examples convert in
  `__init__` rather than demonstrating the trap. A test asserts the prompt's own examples pass the
  check its candidates are held to — otherwise a model following the house style would be rejected
  for following it, and would spend its whole retry budget doing so.
* **`generation.param_contract` enforces it inside `generate_rule`**, so a rejection is *retryable*
  and the finding goes back to the model verbatim. It is a static AST pass — no execution, no model
  call — reporting a parameter compared against a `timedelta`, read for `.total_seconds()`/`.days`/
  `.seconds`, or defaulted to `timedelta(...)`. It tracks `self.x` aliases assigned in `__init__`
  **and duration-valued locals**, because the second half of the reported defect was a rule
  totalling time into `total` and comparing the parameter against that. It is conservative by
  design: a name it cannot resolve to one kind is dropped, since a false positive costs a retry the
  model did not need. `RuleContractError` subclasses `RuleRejectedError` so the loop retries it like
  any rejection while still naming which gate refused.
* **`hoist` checks it again at load**, which is the only gate a row written before any of this
  existed still passes through. This does not restore service for such a Space — an unavailable rule
  still refuses, by design — but the fault is now named once, against the row that has it, instead
  of surfacing as a bare `TypeError` the next time somebody books. No migration is attempted: the
  repair is regenerating the rule, which is an admin's decision about their own venue.

**Why not at the manifest call, where `_cross_check_params` already inspects the signature.** That
check compares parameter *names* and a rejection there is terminal by design — the artifact has
already survived its adversarial suite, and regenerating it to fix a *description* would throw that
away. A parameter-unit mistake is in the artifact itself and is precisely what one correction turn
fixes, so it belongs at the retryable gate.

**The system prompt states every constraint the validator enforces**, because enforcement without
instruction means every candidate fails and the retry budget is spent rediscovering a rule that could
have been stated once. Two of them are counter-intuitive and were observed failing against a live
model: `super().__init__()` is rejected by the dunder-attribute ban, so a generated `__init__` must
not call it; and **only the engine types are free names** — a rule naming `timedelta` must still
import it, or it passes the validator (a syntax check) and dies with `NameError` on load, including
from a default argument.
* **Tester (adversary)** — takes a candidate and the original description and writes a `pytest`
  module against it: positive cases, the bound asserted on both sides, window edges pinned to the
  instant, a case whose local frame disagrees with the UTC clock, and a **fail-closed probe** — a
  rule fed input it cannot evaluate must deny or raise, never pass.
* **The loop** — generate → test → run in the sandbox → feed the failure back to the Generator,
  **at most 3 retries**. Only `SandboxOutcome.PASSED` advances a candidate; a timeout and a crash are
  not successes. On success the candidate and its tests are written to `rules/generated/` for human
  review, and nothing there is ever imported.

**A generated rule cannot run in the sandbox unaided, and the loop is what closes that gap.**
`BaseRule` is a free name, the sandbox binds nothing and grants no `PYTHONPATH`, so a candidate run
as-is dies at class definition with a `NameError` — surfacing as a *crash* rather than a test
failure. Unaddressed, every rule exhausts the retry budget and the loop reports an orderly give-up
having never evaluated anything. `interfaces.py` depends only on the standard library, so
`generation/harness.py` ships it into the sandbox directory as `engine.py` and prepends a prelude
importing from it.

**The prelude binds exactly the free names the Generator's prompt promises**, and no more. It is that
promise in executable form; binding one extra would admit a candidate here that fails wherever it is
really loaded. There are **eight**: `BaseRule`, `BookingRecord`, `BookingRequest`, `Context`,
`LocalFrame`, `RuleResult`, `RunContext` and `Weekday`. `harness.ENGINE_NAMES` and the prompt's own count are
asserted equal by a test, because the two are one statement written in two languages and a name added
to only one of them fails silently — a candidate binds in the sandbox on the strength of a name the
model was never told it could use, passes there, and dies wherever it is really loaded.

**The Generator is told where local answers come from, and told there is no zone to convert with.**
This reverses the prompt's earlier claim that *there are no DST cases here*, which held only while no
rule could express a local anything. It is now false and is deleted rather than softened: a model
that still believes it reaches for `day_start + timedelta(hours=24)`, which is wrong on exactly the
two days a year a local day is not 24 hours long and correct every other time anyone looks. The constraint names
`context.local` and its six fields as the answer to every local question — the venue's day, week,
month, weekday, and the time of day a booking starts — and says plainly that `start_at.hour` and
`start_at.weekday()` are UTC and almost never what a rule means. The Style section carries a second
worked rule reading `context.local.start_minutes`, because that section does most of the teaching and
a facility shown only in a constraint list gets used less than one that is demonstrated.

**The Generator is told which span a rule judges, the same lesson arriving for a second facet.**
`request.duration` is the one booking a rule was handed; `context.run.duration` is the contiguous,
cross-Resource session the request already sits inside, with the request folded in
(`RunContext`, "It resolves the run" above). A constraint phrased "in a row", "back to back", or
"consecutively", or one that caps a total a member could trivially split into two separate bookings,
wants the run rather than the request, and the choice is the rule author's, never a default — the
same ambiguity `MaxConsecutiveDurationRule` closes for the hand-written canon
(`ops/pending/bugs/max-duration-cannon.md`) reproduces itself in every generated duration rule until
the prompt says so. A real generated peak-hours rule did exactly that
(`ops/pending/bugs/generated-rules-underparameterized.md`): 17:00–18:00 plus 18:00–19:00 read as two
hours of peak play under a rule whose whole purpose was to cap peak at one. The Style section carries
a third worked rule reading `context.run.duration`, for the identical reason the local-frame one does
— a field named only in a constraint list gets used less than one demonstrated.

**The Tester is told to pin a case where the local frame and the UTC clock disagree.** A rule reading
`start_at.hour` instead of `context.local.start_minutes` passes every test written for a venue on UTC
and is wrong for every other venue, so a suite that never separates the two cannot catch the mistake
generated rules are most likely to make. The prompt gives a worked `Australia/Sydney` frame — 23:00
UTC Monday is 09:00 Tuesday local — so the model has correct numbers rather than arithmetic to get
wrong. It also carries one complete `Context` fixture for the suite to copy, and that is not a
convenience: an example showing a `LocalFrame` with the surrounding construction left implied is one
a model completes from memory, and it completes it without `week_starts_on` — measured on a live run,
which is the same defect recorded below arriving through a new door.

**The Tester is told to pin a run-versus-request case, on whichever side applies to the candidate.**
A suite that never builds a `RunContext` wider than the request cannot tell a rule reading
`context.run` apart from one reading `request.duration` — both pass identically otherwise. So a
candidate that reads `context.run` must be tested against a run that starts before the request, wide
enough that the run alone breaks the bound while the request on its own would not; a candidate that
does not must be tested against a wide run around a request that is well within bounds by itself, and
must still pass. Without both halves a generated rule reaching for the wrong span is
indistinguishable from one reaching for the right one.

**`validate_source` runs on the generated source alone, and the prelude is prepended afterwards.**
The assembled module imports `engine`, which is not on the import allowlist, so validating it instead
rejects every candidate. The ordering reads like an untidiness and is load-bearing.

**The Tester's output is not put through `validate_source`.** The validator states what a *rule* may
do, because a rule is code the booking API runs in-process; a test suite legitimately needs
`import pytest`, `pytest.raises` and `parametrize`, none of which a rule may ever have. The sandbox
is the boundary for test code — the half of safe execution built for code whose shape cannot be
predicted — and the only checks made are that the module parses and defines a test.

**The suite is rewritten for every candidate.** It imports the rule class by name and constructs it
with that class's parameters, so a suite carried over from a previous attempt fails against a renamed
class and reports it as the rule being wrong.

**A model id belongs to the client that can serve it, so every client declares its own
`default_model` and there is no package-wide default.** `LLMClient` carries the attribute as part
of the protocol, `model=None` means "this client's default" everywhere below it, and
`run_generation_loop` resolves it once against the client it was handed so the model it records is
a real id rather than a null. The agents — Generator, Tester, manifest — name no model at all, which
is the point: none of them knows which backend it is calling.

That is stated as an invariant because its absence was a live defect. One package-wide
`DEFAULT_MODEL` of `claude-opus-4-8` was passed on to whichever client was configured, so a backend
running `RULE_GENERATION_CLIENT=google` with no model set asked Google for an Anthropic model id and
failed **every** generation job on a 404 — an error whose text reads as a bad API key and is not one.
The per-client mapping that would have prevented it existed only inside `benchmark.py`, which sits
outside both distributed packages, so the backend could not import it and no test compared the two.
A default that only one caller can reach is not a default.

**Model: `claude-opus-4-8` for the CLI client, `gemini-3.1-flash-lite` for the Google one,
`qwen2.5:1.5b` for Ollama — settled by the benchmark, not by assumption, where the benchmark can
reach them.**

> Do **not** use `claude-3-haiku-20240307` — retired 2026-04-19, now returns 404. Its live successor
> is `claude-haiku-4-5` ($1/$5 per MTok).

Opus remains the CLI client's default deliberately: a subtly wrong rule silently mis-enforces real
bookings, and every Tester retry costs a full generate-plus-test cycle, so the cheaper model is not
obviously cheaper end to end. What `benchmark.py` can compare is bounded by which clients exist:
`OllamaClient` and `GoogleAIStudioClient` serve it, `ClaudeCliClient` cannot (the harness preamble
above), and no Anthropic-API-backed client ships — so the numbers below describe a local model or a
hosted Gemini one, and Opus itself is still unmeasured.

**A hosted Gemini model holds the contract, and the cheapest tier holds it best.** Against all five
golden examples with three retries each, at `--temperature 0` (AI Studio honours no seed, so a run is
not bit-reproducible and the report records `seed` as unset):

| Model | Result | Cost of the run |
|---|---|---|
| `gemini-3.1-flash-lite` | **5/5, every example on the first attempt** | 10 calls, 16k in / 7.5k out, 26s |
| `gemini-3.5-flash-lite` | **5/5**, two examples needing retries | 18 calls, 34k in / 20k out, 156s |

`gemini-3.1-flash-lite` is therefore `GoogleAIStudioClient.default_model`, defined beside that client
in `generation/llm.py` and re-exported by `benchmark.py` rather than the other way round. Newer is
not better here, and the flagship tier is not what settled it — see the quota paragraph below.

**Every failure the first run recorded was the Tester's, not the rule's, and that is the finding.**
Before the prompt fix this run produced, the same two models scored 3/5 and 1/3 — and not one of
those failures was a rule whose logic was wrong. Every single one was `TypeError:
CalendarContext.__init__() missing 1 required positional argument: 'week_starts_on'`, raised on the
first line of the Tester's own module, so the whole suite failed before the candidate was called
once and the attempt was recorded as `TESTS_FAILED`, which reads as "the rule is wrong". A second
instance of the same defect had the Tester building a fail-closed probe from a `BookingRequest` with
`start_at > end_at`, which the engine type rejects at construction — again the test dying on its own
fixture. **The Tester's system prompt now states that every constructor argument it lists is required
and has no default, and that a fail-closed probe must be input the engine types will actually
build** — unusable *to the rule*, never malformed to the engine. Both models went to 5/5 on the
re-run, which is how this was settled: by measuring the change, not by re-reading the prompt.

The lesson generalises past those two sentences. A shape failure in the *Tester* is indistinguishable
in the report from a logic failure in the *rule*, and it is the more likely of the two: the Tester's
module is loaded as ordinary Python where nothing is a free name, while a rule module is loaded into
a namespace prepared for it. Read `last_failure` before concluding anything about a model from a
success count.

**No local model tested holds the contract.** `qwen2.5:1.5b`, `qwen2.5:7b` and `llama3.1:8b`, run
against all five golden examples with three retries each (fifteen runs, `--seed 0 --temperature 0`),
gave up on every one — `0/5` for every model. That measurement predates the Tester prompt fix above,
so its `TESTS_FAILED` counts are an upper bound on those models' real logic failures rather than a
clean reading; the shape failures are unaffected and stand as recorded:

* `qwen2.5:1.5b` mostly never reaches a working candidate at all — `RULE_REJECTED` on an unsafe
  comprehension, and `TESTS_REJECTED` from Tester output that does not parse as Python (an unclosed
  paren, a test module defining no test function). One example crashed outright: the Tester pasted
  the rule's own class definition into the test file instead of importing it from `candidate_rule`,
  producing a bare `NameError: name 'BaseRule' is not defined` — the free-name promise this package's
  prelude keeps for a *rule* module does not extend to a test module that redefines the rule inline.
* `qwen2.5:7b` and `llama3.1:8b` clear validation far more often — most attempts reach a real
  `pytest` run against a loaded candidate — and lose there, with `TESTS_FAILED` carrying genuine
  assertion failures. Some share of those is now known to be the `week_starts_on` defect rather than
  the rule's logic, and a re-run would say which.

**A free AI Studio key cannot measure the flagship models, and that is a property of the key, not of
the models.** The `gemini-3.x-pro` tier reports a free-tier input token quota of **0** and is not
reachable at all. The `gemini-3.x-flash` tier reports **20 requests per day per model**, while a
five-example run costs 10 calls at best and 40 at worst — so a run that retries anywhere exhausts the
day's quota mid-run and records the rest as `CALL_ERROR` and `SKIPPED`. The `-flash-lite` tier is
what a run can actually finish on, which is why both measured models come from it. A 503 naming
"high demand" is routine there too and clears within minutes. None of this is worked around inside
the client: `--checkpoint` and a hand-paced re-invocation are the mechanism, exactly as this
document already says, and a client that retried a quota for you would hide the one fact worth
knowing.

**A thinking-heavy model can outlast the client's own default wall clock, and being cut off is not a
result about the model.** `gemini-3-flash-preview` spent all 120 seconds of
`GoogleAIStudioClient`'s default on a single generation and was recorded as an unreachable backend.
`--timeout` therefore applies to whichever client `--client` selects rather than to ollama alone, and
defaults to `None` meaning "leave that client's own default alone" — the two clients' defaults differ
deliberately and neither is the other's.

If a future benchmark run against a different model changes any of this, rewrite these paragraphs and
flip the default — the instruction to settle it with evidence rather than assumption still holds.

### The loop also runs in the backend, as a job

`app.rule_generation.run_generation_job` drives the same `run_generation_loop` from inside the
booking process, with `output_dir=None` — the artifact directory is a developer-review affordance,
and this path's artifact is a database row. `identity-and-access.md` owns the tables, the API and the
admin gate; what belongs here is what the runner is allowed to do with a result.

**Only `AttemptOutcome.PASSED` writes a `generated_rule_types` row.** A timeout is not a success and
a crash is not a success. The loop's own docstring says this about `generated/`; here the consequence
is a row in a catalog that judges real bookings, so it is the second place it has to be true. Any
other outcome fails the job with its attempt history and writes nothing. After a successful write the
runner calls `catalog.reload`, so the type is live in that process without a restart — the other
workers pick it up through the miss-triggered reload described under "Backend integration".

`reads_history` is derived from the validated source by a **deliberately over-inclusive** check. The
two errors are not symmetric: a false positive costs one history query the rule ignores, while a
false negative hands the rule an empty history and makes it silently *permissive* — a "no more than
three a week" rule counting zero existing bookings and allowing one it should have refused. When the
cheap check errs in the safe direction, take the cheap check.

**The source check can only elevate the model's own declared claim, never suppress it.**
`generate_manifest` computes `declared_reads_history or _mentions_history(rule_source)` — an `or`,
not an `and` — because the asymmetry above cuts both ways on the model's own account of itself: a
model that under-claims `false` for a rule reading `context.run` (a real reading, not a hallucinated
one — the source genuinely never spells "history") must still be corrected *up* by the source check,
or the false negative the check exists to catch survives the one place it does the most damage. A
model that claims `true` is trusted outright and never marked down against a substring miss, because
downgrading an honest `true` would reintroduce that identical false negative through the opposite
door. Correcting only ever in the permissive-to-restrictive direction is what "deliberately
over-inclusive" means for a *derived* value combined with a *declared* one, not merely for the
substring check in isolation.

**The generation client is chosen by `RULE_GENERATION_CLIENT`, and `stub` is the default.**
`generation.stub.StubLLMClient` answers with a canned, valid rule and a canned suite, deterministically
and with no network or subprocess, and it sits at the `LLMClient` seam — so E2E and the backend suite
drive every layer this feature builds without a live model call, the same split the benchmark already
draws by never running in CI. Defaulting to it means an enabled-but-otherwise-unconfigured backend
runs the whole flow against a canned response instead of billing anyone.

### Every prompt and completion is persisted, in our own database

There is **no LangChain, no LangGraph and no tracing SDK** in this project and none is being added.
`rules/pyproject.toml` declares zero runtime dependencies deliberately, because the package installs
into the backend and anything listed there is a cost the booking API pays forever; the prompts are
user-authored text from a multi-tenant product, so shipping them to a third party is a data-sharing
decision that is free now and expensive to unwind; and a generated rule enforces bookings for years
while a hosted free tier keeps traces for weeks.

So `RecordingClient.on_exchange` writes a `rule_generation_exchanges` row per model call **as the run
progresses**, not once at the end — a process that dies at attempt three still leaves the first two
attempts' prompts behind. **The retry turn is what the recording is for**: on a retry `build_prompt`
hands the model back its own failing source plus the validator or pytest output verbatim, and that is
both the entire prompt-debugging surface and the only evidence of why a rule now enforcing a venue's
bookings reads the way it does. `user_prompt` is therefore stored untruncated.

A system prompt is stored **once, by sha256**, in `prompt_versions` rather than copied onto every
exchange. Size is the smaller half of the argument — the Generator's is ~5.7 kB against up to eight
calls per generation — and the join is the real one: *which system-prompt version produced which
outcome* is the question prompt tuning asks, and the question a pile of near-identical copies cannot
answer without diffing them first. The `agent` label is derived by comparing the system prompt to
`SYSTEM_PROMPT` / `TESTER_SYSTEM_PROMPT`, and the attempt number positionally — each Generator turn
opens an attempt — because the recorder sits at the `LLMClient` seam and must not learn about loops.

What this gives up is the *UX* a tracing product provides: prompt diffing, replay, cost dashboards. A
Postgres table has none of that. The same wrapper is where an exporter would attach if that pain ever
becomes real; self-hosted Langfuse is the candidate, since this project already runs docker-compose
and it keeps tenant prompts on our own infrastructure.

Measured from real stub runs, one generation costs roughly **3.6 kB** across the job and exchange
rows when it passes first time and **4.9 kB** with one retry, plus a one-off ~10 kB of
`prompt_versions` shared by every job forever. A real model's completions run longer than the stub's,
so treat those as the floor. Retention is deliberately **not** decided here — nothing is deployed and
a policy invented inside a task is one nobody agreed to — but the decision now has a number attached.

## Benchmarking

`rules/benchmark.py` is a CLI feeding five golden examples ("max 1 hour", "only on weekends", "max 2
times a week", …) through the generation loop and reporting what happened, as JSON and as a terminal
summary. A `GoldenExample` is a description **and the expectations the rule written for it must
meet** — see "two axes" below. It exists to tune the system prompts before any of this is wired to the web UI — prompt
changes are judged by its numbers, not by inspection. `--client ollama|google|claude-cli` and a
repeatable `--model` are what let one invocation compare backends and models side by side.

It sits at the top level of `rules/`, outside both packages, and `pyproject.toml` distributes
`rules` and `generation` and no loose module beside them — so the benchmark itself stays
unimportable by the booking API even though `generation` no longer is.

**It is invoked by hand and never runs in CI.** It makes live model calls, and `testpaths = ["tests"]`
is what keeps `pytest` away from it. What *is* unit-tested is report assembly from synthetic
`LoopResult`s.

**The per-attempt outcome is what the report is for, not the success rate.** `RULE_REJECTED` on
attempt 1 and `TESTS_FAILED` on attempt 1 say different things about which constraint in the system
prompt a model broke — the dunder ban, the free-name rule, the datetime import — and which one broke
is what tunes the prompt. A single success-rate number erases exactly that, so every attempt's
outcome is kept in order.

**A report has two axes: how the run ended, and what it produced.** `BenchmarkStatus` is the first,
and was for a long time the only one — every one of its four values is a fact about the loop. So a
rule that validated, survived its own adversarial suite and matched `inspect.signature` scored
`VERIFIED` whatever was in it, and the parameter-contract defect above is exactly that: two golden
examples are duration-shaped, both reported `VERIFIED`, and the rules behind them would have taken a
Space off line. **A new golden example would not have caught it** — what was missing was never
another constraint to try, it was something recorded about the answer.

`ArtifactExpectation` is the second axis. Each `GoldenExample` declares what must be true of the
rule written for it (`PARAMS_HONOUR_THEIR_UNITS`, declared by all five; `TAKES_A_PARAMETER`, by the
four whose constraint names a number), and `ExampleReport.artifact_failures` records the ones that
are not. `succeeded` requires **both** axes, while `status` keeps its documented meaning — a run can
report `VERIFIED` with `succeeded=False`, and any other arrangement puts the two facts back into the
one bit that let this slip. The five descriptions and their order are unchanged, because appending
or reordering re-baselines every run ever recorded.

**Both checks are `ast` over the produced source, so they cost no model call.** That is the property
standing practice asks for by name: an assertion that costs a call is one nobody can afford to add,
which is why this axis was missing for as long as it was. It also makes the assertions unit-testable
without spending a day's quota to watch one fail. A checkpoint written before these fields existed
still resumes, but its examples were never checked against any expectation — start a fresh
`--checkpoint` path when the artifact expectations are what a run is measuring.

**Token usage comes from a recording wrapper around the `LLMClient`, not from a change to the loop.**
`run_generation_loop` returns strings and discards each `LLMResponse`; threading metadata out of it
would give the engine's retry loop a benchmark's concern to carry forever. The wrapper sits at the
same seam the loop already calls through. Sums follow the metadata fields' own convention — present
values sum, and all-absent is `None` rather than `0`, so a local model's unknown price is not
reported as free.

**That claim survives, and it now has a second consumer.** `RecordingClient` lives in
`generation/llm.py` rather than inside `benchmark.py`, records both directions rather than only the
response, and serves the benchmark and the backend's job runner from one implementation. The loop is
unchanged — it still returns strings, still discards every `LLMResponse`, and still does not know
that anyone is watching. That is the property the seam was for, and moving the wrapper is what spends
it rather than eroding it.

**An `LLMCallError` aborts the rest of that model's run.** The loop does not retry one because
another prompt does not fix an unreachable daemon or an unpulled model id; the same reasoning holds
one level up, where four more examples would rediscover the same outage and bury the cause under
repetitions of itself. The remaining examples are recorded as skipped, and a different `--model`
still gets its own full run.

**Exit status distinguishes a result from a non-run.** A model that exhausts its retries exits `0` —
"this model cannot hold the contract" is an answer this benchmark exists to get. A backend that could
not be reached exits non-zero, because otherwise an unreachable daemon looks like a run whose numbers
mean something. For the same reason the report records the parameters it ran under, and records
`seed` and `temperature` as unset for a client that never received them: two reports agreeing on a
sampling parameter neither applied is worse than two reports that say nothing about it.

**`--checkpoint` makes a multi-model run safe to kill and re-invoke verbatim.** A full run across
several models is a multi-hour, hand-invoked process with nothing supervising it, and a run that dies
partway with no record of what finished forces a full re-run to get one number. `BenchmarkCheckpoint`
persists every `ExampleReport` to the checkpoint path immediately after it finishes — not batched —
so the file on disk is never more than one in-flight example stale, and re-invoking the identical
command line reuses everything already recorded rather than repeating it. It refuses to resume a
checkpoint written under a different `--client`/`--seed`/`--temperature`/`--retries` rather than
silently mixing two runs that were not produced under comparable conditions — the same reasoning
`BenchmarkReport` already records its own parameters for.

Ollama is passed a fixed `seed` and `temperature` so two runs are comparable — a benchmark whose
rerun differs for unrecorded reasons cannot settle the question it was built to settle.

**`--timeout` bounds a call on whichever client `--client` selected**, and `--base-url` still does
not: a base URL is a claim about where a *local daemon* lives, while every client here bounds a call
with a wall clock and a hosted model's own default is not necessarily long enough for it (see the
model paragraphs above). Unset means "leave that client's own default alone" rather than a number
this CLI picks on all three clients' behalf.
