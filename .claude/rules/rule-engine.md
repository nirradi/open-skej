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
  counts.** `BookingRecord` has no status field and the engine never inspects one: filtering belongs
  to the layer that owns the schema, so a future `deleted` or no-show flag cannot silently obsolete
  every rule that forgot to check it.
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

## The canon

`canon.py` holds the four hand-written rules every Space enforces: `NotInThePastRule()`,
`BookingHorizonRule(days)`, `MaxDurationRule(max_duration)`, `AvailabilityHoursRule(opens_at,
closes_at)`. They are written by hand rather than generated — they are the reference the generation
loop is measured against, and the worked example of the rule shape.

**Parameters live on the instance, never as module constants.** A Space allowing 45-minute bookings
and one allowing two hours are the same rule with different arguments, so per-Space configuration
becomes a change to how the canon is built rather than a change to any rule. `DEFAULT_CANON` supplies
the values in force today.

**The order is `(NotInThePast, BookingHorizon, MaxDuration, AvailabilityHours)`, and it arbitrates
user-facing copy.** The controller is fail-fast, so the first rule to deny decides the single message
shown when a request breaks several rules at once. The date rules run first because they reject a
booking on *when* it is, which no shortening or shifting within the day can fix; telling someone to
trim a three-hour booking that sits 90 days out sends them to fix the one thing that is not the
problem. Duration and availability hours are remedies the user can apply to an otherwise bookable
date, so they follow. Past and horizon are mutually exclusive and never arbitrate against each other.

**Denial copy is contract, not wording.** `app/e2e/tests/03-sad-path.spec.ts` asserts the
max-duration message as a full-string match and reproduces the singular/plural and `" and "` join of
the engine's duration formatting. Rewording a canon message is a breaking change to a test in another
package.

**Availability hours are UTC hours.** `opens_at` and `closes_at` are UTC clock times and
`start_at.time()` is a UTC wall clock, so a Space opening at 06:00 local does not open at
`time(6, 0)` unless it sits on UTC. Rendering those bounds in a viewer's timezone is the UI's job;
the engine has no timezone to convert from.

`frequency.py` holds the rules that count: `MaxBookingsPerWeekRule(n)` and
`MaxBookingsPerMonthRule(n)`, the only ones whose verdict depends on anything beyond the request.
They are **exported but deliberately absent from `DEFAULT_CANON`** — the four rules in `canon.py` are
what the end-to-end suite asserts against, and adding a booking limit to the default canon would
change behaviour those tests depend on.

**A booking is counted against the window it starts in, and the window is anchored on the request
rather than on `now`.** A request three weeks out is judged against that week's bookings, not this
week's; anchoring on `now` would refuse next month's first booking because of this month's traffic.
Windows are half-open `[start, end)`, so a booking straddling a boundary counts once, against the
side it begins on.

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
* **Tester (adversary)** — takes the generated code and writes `pytest` functions: positive cases
  that should pass and negative edge cases that should fail (timezone overlaps, the third booking).
* **The loop** — run the Tester's tests against the Generator's code in the sandbox. On failure feed
  the stack trace back to the Generator, **maximum 3 retries**.

**Model: `claude-opus-4-8` by default, model ID configurable.**

> Do **not** use `claude-3-haiku-20240307` — retired 2026-04-19, now returns 404. Its live successor
> is `claude-haiku-4-5` ($1/$5 per MTok).

Opus is the default deliberately: a subtly wrong rule silently mis-enforces real bookings, and every
Tester retry costs a full generate-plus-test cycle, so the cheaper model is not obviously cheaper end
to end. `benchmark.py` runs Opus 4.8 and Haiku 4.5 side by side on the golden examples. **If Haiku
4.5 matches the success rate, flip the default and rewrite this paragraph.** Settle it with the
benchmark, not by assumption.

## Benchmarking

`benchmark.py` is a CLI feeding five golden examples ("max 1 hour", "only on weekends", "max 2 times
a week", …) through the generation loop and logging success rate, token usage, and latency. It exists
to tune the system prompts before any of this is wired to the web UI — prompt changes are judged by
its numbers, not by inspection.
