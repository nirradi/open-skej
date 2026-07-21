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

## Safe execution

Generated code is validated by AST before it runs — dangerous imports (`os`, `sys`) are rejected —
and candidate rules execute in a subprocess sandbox. Validation is static and the sandbox is the
backstop; neither alone is the answer.

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
