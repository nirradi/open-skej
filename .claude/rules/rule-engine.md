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
* **`CalendarContext`** — `week_starts_on` (a `Weekday` enum) and `now`. **No timezone field.**
* **`BookingRequest`** / **`BookingRecord`** — `user_id`, `resource_id`, `start_at`, `end_at`.
* **`HistoryContext`** — `bookings`, the caller's pre-filtered, pre-capped list. **Everything in it
  counts.** It is filtered to the requesting **user**, never to one resource: a caller may legitimately
  draw it from several resources at once — the backend counts a frequency cap across every Resource in
  a Space, not per court — and the controller's cross-check enforces only that the user matches, not
  the resource. `BookingRecord` has no status field and the engine never inspects one: filtering
  belongs to the layer that owns the schema, so a future `deleted` or no-show flag cannot silently
  obsolete every rule that forgot to check it.
* **`Context`** — aggregates `user` / `calendar` / `history` and enforces the history-window
  invariant.
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

## Controller

`evaluate_request()` is the single entry point the backend calls. In order: cross-check the request
against the context (`Context` cannot do this itself — the request is not in scope when a context is
built), run the canon in order **fail-fast** (the first denial wins and nothing after it runs), and
contain a buggy rule.

## Backend integration

`rules` is installed into the backend as an editable sibling package (`-e ../../rules`), and only the
`rules` package is distributed — never `generation`. "Nothing generated is imported by the app" is
therefore a fact about the packaging, not a promise a reviewer must keep.

`app/backend/app/rules_stub.py` is the adapter, and the whole of it. It holds no rule logic; it
translates between the HTTP boundary and `evaluate_request`, and four translations live there and
nowhere else. **It converts every datetime to UTC** (`.astimezone(timezone.utc)`) before building
engine types — the engine rejects a non-zero offset outright, so a booking a client sends as
`+02:00` must be converted, and is then judged on its UTC wall clock. **It supplies the allow-path
message**: `RuleResult(passed=True)` carries no copy, but the API shows friendly text on success. **It
assembles the canon per Space** from that Space's own configuration rather than running
`DEFAULT_CANON` — a null column omits its rule, and a Space's local operating hours are resolved to a
UTC window per booking date before `AvailabilityHoursRule` is built. **It passes Space-wide history**
only when the Space's canon includes a counting rule: the router loads the user's bookings across
every Resource in the Space, capped to `history_window`, and passes them in; with no counting rule
configured, history stays empty and no query runs. **It resolves the counting windows** in the
Space's own zone — the local week and local calendar month containing the booking, converted to UTC
instants and handed to `MaxBookingsPerWeekRule` / `MaxBookingsPerMonthRule`, which no longer derive
them. `DEFAULT_CANON` is no longer what the API runs — it remains the *reference* canon the
generation loop is measured against and the source of the default values a Space that overrides
nothing would use.

`ContextMismatchError` is deliberately not caught at this boundary: the adapter builds both the
request and the context from one booking, so a mismatch is an adapter bug and must reach the error
tracker, not be served as a polite refusal.

**Every Space's operating hours resolve.** `resolve_operating_hours` has no failure mode and raises
nothing: a local window whose UTC image lands on two calendar dates is *represented*, not refused.
There is no `MidnightWrapError` and no containment path in the adapter for one.

That error used to exist and denied every booking against any Space it fired on. It fired far more
widely than its name suggests — not only the UTC+13/+14 zones, but any venue whose opening hour is
earlier than its own UTC offset, or whose closing hour is late enough to cross the boundary the other
way. `Australia/Sydney` could not open before 11:00 and `Pacific/Honolulu` could not close after
about 13:00; ordinary hours either side made the venue permanently unbookable, with the engine's
generic copy and nothing naming the configuration as the cause.

## The canon

`canon.py` holds the four hand-written request-local rules: `NotInThePastRule()`,
`BookingHorizonRule(days)`, `MaxDurationRule(max_duration)`, `AvailabilityHoursRule(opens_at,
closes_at)`. They are written by hand rather than generated — they are the reference the generation
loop is measured against, and the worked example of the rule shape.

**Parameters live on the instance, never as module constants.** A Space allowing 45-minute bookings
and one allowing two hours are the same rule with different arguments, so per-Space configuration is
a change to how the canon is built rather than a change to any rule. `DEFAULT_CANON` is the reference
assembly of these four at their default values; the canon the API actually runs is built per Space
(see Backend integration), where a null column omits its rule entirely. `NotInThePastRule` is the
only one always present — you can never book the past, whatever a Space configures.

**The assembled order is `(NotInThePast, BookingHorizon, MaxDuration, AvailabilityHours,
MaxBookingsPerWeek, MaxBookingsPerMonth)`, and it arbitrates user-facing copy.** The controller is
fail-fast, so the first rule to deny decides the single message shown when a request breaks several
rules at once. The date rules run first because they reject a booking on *when* it is, which no
shortening or shifting within the day can fix; telling someone to trim a three-hour booking that sits
90 days out sends them to fix the one thing that is not the problem. Duration and availability hours
are remedies the user can apply to an otherwise bookable date, so they follow. Past and horizon are
mutually exclusive and never arbitrate against each other. **The counting rules come last** because a
frequency cap is the one denial no change to *this* request can fix — no shorter, earlier or later
booking clears it — so every rule naming a fixable problem gets first refusal.

**Denial copy is contract, not wording.** `app/e2e/tests/03-sad-path.spec.ts` asserts the
max-duration message as a full-string match and reproduces the singular/plural and `" and "` join of
the engine's duration formatting. Rewording a canon message is a breaking change to a test in another
package.

**Availability hours are UTC hours.** `opens_at` and `closes_at` are UTC clock times and
`start_at.time()` is a UTC wall clock, so a Space opening at 06:00 local does not open at
`time(6, 0)` unless it sits on UTC. The adapter resolves a Space's local hours to a UTC window for
the booking's own date before constructing the rule (see Backend integration); the engine itself has
no timezone to convert from, and rendering those bounds in a viewer's timezone stays the UI's job.

**An inverted pair means the window crosses a UTC day, and is not an error.** `opens_at > closes_at`
says "opens on one UTC date, closes on the next" — the only way that can be said once the date is
dropped from a pair of `time` values. It is the ordinary case for a venue far enough from UTC, not
a broken configuration. `AvailabilityHoursRule` therefore never compares the two bounds against each
other: it first decides **which occurrence** of the recurring daily window the booking's own instant
falls in (`_occurrence_for`), and both bounds it then compares come from that one decision. The old
shape anchored `opens_at` and `closes_at` to `request.start_at.date()` independently, so for a
request crossing the boundary one bound could be dated a day off the other — which is what
mislabelled the denial reason, naming the closing bound for a booking that was refused for being too
early.

Because the inversion is load-bearing, **local ordering is enforced at the write boundary instead**:
a Space whose `opens_at` is at or after its `closes_at` on its own wall clock is refused there (see
`identity-and-access.md`), because by the time hours reach the engine an inversion is indistinguishable
from the legitimate case above. That check is also the one place a venue open past its *local*
midnight would be admitted deliberately, rather than arriving by accident through a typo.

`frequency.py` holds the rules that count: `MaxBookingsPerWeekRule(n, window_start, window_end)` and
`MaxBookingsPerMonthRule(n, window_start, window_end)`, the only ones whose verdict depends on
anything beyond the request.
They are **exported but deliberately absent from `DEFAULT_CANON`**, the reference canon of the four
request-local rules in `canon.py`. The API does not run `DEFAULT_CANON`; it assembles a per-Space
canon that *includes* these two when a Space sets `max_bookings_per_week` / `max_bookings_per_month`.
Keeping them out of the reference canon is what lets the end-to-end suite assert against a seeded Space
configured to those four rules' values without a counting rule silently changing the outcome.

**Neither rule derives its own window — both are handed one.** The window is a half-open
`[window_start, window_end)` pair of UTC instants passed at construction, and the rule does nothing
but count into it. Deriving it here is what made a local week wrong: a Sydney booking at 00:30 on
Monday local is 13:30 **Sunday** in UTC, so a window snapped to UTC midnight put it in the previous
week and a cap of one admitted a second booking in the same Sydney week. A local week has no fixed
UTC representation and the engine has no timezone to find one with, so the layer that does resolves
it. This is the same boundary split availability hours already follow, and `CalendarContext` still
carries **no timezone field** for the same reason it never did — a zone there would invite every
rule, generated ones included, to convert for itself.

An inverted pair is a **caller bug** here and is rejected at construction, unlike
`AvailabilityHoursRule`, where inversion means "this window crosses a UTC day". A counting window is
two absolute instants, not a recurring daily one, so it has no wrap to describe.

**A booking is counted against the window it starts in, and the window is anchored on the request
rather than on `now`.** A request three weeks out is judged against that week's bookings, not this
week's; anchoring on `now` would refuse next month's first booking because of this month's traffic.
That property now lives in the caller that computes the bounds — `rules_stub._build_canon` resolves
both windows from the booking's own **local** date. Windows are half-open, so a booking straddling a
boundary counts once, against the side it begins on.

**A local week is seven local days, not `start + 7 days`.** Across a DST transition it is 167 or 169
hours, and adding a fixed `timedelta` to the start would put the closing boundary an hour inside the
neighbouring week — the same class of error as the UTC snapping this replaced, one line further
down. The adapter resolves both ends from local midnight independently. `week_starts_on` stays a
calendar convention on `CalendarContext`, read by the adapter when it resolves the week; no rule
reads it any more.

**The bound counts the request itself.** With a limit of two and two bookings already in the window,
the third is refused — checking the existing count alone would admit the booking that takes the user
over the line.

Because the window follows the request, a request beyond the history window is measured against a
history the caller has no bookings for, and passes. That is the documented bound of the engine's
promise — evaluation costs at most one calendar month of history — not a gap in these rules.

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
  emits a Python class inheriting `BaseRule`, relying only on `HistoryContext` and standard
  `datetime` math, with **parameterized** variables so the rule is reusable.

`rules/generation/` is a **sibling package of `rules`**, not part of it. `rules` is what the booking
API imports and runs in-process; this is what a developer runs at a terminal to produce a candidate.
The separation is what makes "nothing generated is imported by the app" a property of the layout
rather than a promise.

**The model is called through an `LLMClient` seam** — one method, `complete(system, prompt, model)` —
not an SDK directly. Two implementations ship. `ClaudeCliClient` shells out to
`claude -p --output-format json` and so needs no API key, only an authenticated CLI: an acceptable
dependency for a developer tool whose output is a file a human reviews, and one the booking API never
carries. `OllamaClient` calls a model served by a local Ollama daemon over its HTTP API, so it needs
neither a key nor a cloud account.

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

**A failed CLI call is identified by `is_error` and the exit code, never by `subtype`.** A run that
404s on an unknown model id exits 1 and reports `is_error: true` while still reporting
`subtype: "success"`; reading the subtype would pass a human-readable error string on as if it were
rule source, to be rejected later for a syntax error and blamed on the model.

**Generated source is validated inside `generate_rule`**, after the markdown fence is stripped, so no
caller can hold unvalidated candidate source. A rejection raises `RuleRejectedError` carrying the
validator's message verbatim — it names the construct and its line, which is exactly what the retry
loop hands back, and paraphrasing would cost the model the detail that lets it fix the candidate.

**The system prompt states every constraint the validator enforces**, because enforcement without
instruction means every candidate fails and the retry budget is spent rediscovering a rule that could
have been stated once. Two of them are counter-intuitive and were observed failing against a live
model: `super().__init__()` is rejected by the dunder-attribute ban, so a generated `__init__` must
not call it; and **only the engine types are free names** — a rule naming `timedelta` must still
import it, or it passes the validator (a syntax check) and dies with `NameError` on load, including
from a default argument.
* **Tester (adversary)** — takes a candidate and the original description and writes a `pytest`
  module against it: positive cases, the bound asserted on both sides, window edges pinned to the
  instant, and a **fail-closed probe** — a rule fed input it cannot evaluate must deny or raise,
  never pass.
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
really loaded.

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

**Model: `claude-opus-4-8` by default, model ID configurable.**

> Do **not** use `claude-3-haiku-20240307` — retired 2026-04-19, now returns 404. Its live successor
> is `claude-haiku-4-5` ($1/$5 per MTok).

Opus is the default deliberately: a subtly wrong rule silently mis-enforces real bookings, and every
Tester retry costs a full generate-plus-test cycle, so the cheaper model is not obviously cheaper end
to end. `benchmark.py` compares models on the golden examples, and what it can
compare is bounded by which clients exist: `OllamaClient` serves it, `ClaudeCliClient` cannot (the
harness preamble above), and no SDK-backed client ships — so a local model is what the numbers
currently describe. **When a benchmark run settles which model holds the contract, flip the default
and rewrite this paragraph.** Settle it with the benchmark, not by assumption.

## Benchmarking

`rules/benchmark.py` is a CLI feeding five golden examples ("max 1 hour", "only on weekends", "max 2
times a week", …) through the generation loop and reporting what happened, as JSON and as a terminal
summary. It exists to tune the system prompts before any of this is wired to the web UI — prompt
changes are judged by its numbers, not by inspection. `--client ollama|claude-cli` and a repeatable
`--model` are what let one invocation compare backends and models side by side.

It sits at the top level of `rules/`, outside both packages, and `pyproject.toml` distributes only
`rules` — so a benchmark is no more importable by the booking API than `generation` is.

**It is invoked by hand and never runs in CI.** It makes live model calls, and `testpaths = ["tests"]`
is what keeps `pytest` away from it. What *is* unit-tested is report assembly from synthetic
`LoopResult`s.

**The per-attempt outcome is what the report is for, not the success rate.** `RULE_REJECTED` on
attempt 1 and `TESTS_FAILED` on attempt 1 say different things about which constraint in the system
prompt a model broke — the dunder ban, the free-name rule, the datetime import — and which one broke
is what tunes the prompt. A single success-rate number erases exactly that, so every attempt's
outcome is kept in order.

**Token usage comes from a recording wrapper around the `LLMClient`, not from a change to the loop.**
`run_generation_loop` returns strings and discards each `LLMResponse`; threading metadata out of it
would give the engine's retry loop a benchmark's concern to carry forever. The wrapper sits at the
same seam the loop already calls through. Sums follow the metadata fields' own convention — present
values sum, and all-absent is `None` rather than `0`, so a local model's unknown price is not
reported as free.

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

Ollama is passed a fixed `seed` and `temperature` so two runs are comparable — a benchmark whose
rerun differs for unrecorded reasons cannot settle the question it was built to settle.
