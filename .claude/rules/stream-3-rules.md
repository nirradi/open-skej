---
description: Architecture, interfaces, and AI generation loop for the Python rule engine.
glob: "rules/**/*"
---

# Stream 3: Rule Engine & AI Generation (The "Brain Skeleton")

## Objective
Build the isolated Python execution environment for booking rules. Implement the base interfaces, a benchmarking scaffold, and an autonomous AI loop (Generator + Tester) to create and verify new rules safely.

## Boundaries & Constraints
* Strictly backend execution logic (isolated in `/rules`).
* Time bounds: Rules will not evaluate history beyond the current calendar month or a week rolling window.
* Safe Execution: Generated Python code must not use dangerous imports (`os`, `sys`).

## Phase 1: Core Interfaces — SHIPPED (task 3.1, PR #19)

**`rules/rules/interfaces.py` is the authoritative contract. Read it before writing any rule.**
The descriptions below record intent; where they and the code disagree, the code wins.

* **`UserContext`**: `user_id` **only**. Role and tier are deliberately absent — Stream 2 owns roles
  and no rule branches on either yet. A test asserts their absence; add them when a rule needs them.
* **`CalendarContext`**: `week_starts_on` (a `Weekday` enum), `now`. **No timezone field.**
* **`BookingRequest`** / **`BookingRecord`**: `user_id`, `resource_id`, `start_at`, `end_at`.
* **`HistoryContext`**: `bookings` — the caller's pre-filtered, pre-capped list. **Everything in it
  counts.** `BookingRecord` has no status field; the engine never inspects one. Filtering belongs to
  the layer that owns the schema, so a future `deleted`/no-show flag cannot silently obsolete a rule.
* **`Context`**: aggregates `user` / `calendar` / `history`. Enforces the history-window invariant.
* **`RuleResult`**: `passed` (bool), `fail_reason` (str | None — friendly copy shown verbatim in the
  UI). Named `passed` because `pass` is a Python keyword.
* **`BaseRule`**: abstract, requiring **`evaluate(self, request, context) -> RuleResult`**. The
  aggregate `Context` replaces four positional params so a later task can add a new kind of context
  without breaking the signature of every rule ever written.

### UTC everywhere
Every datetime crossing this boundary must be timezone-aware **with a zero UTC offset**. Naive
datetimes and non-zero offsets are both rejected at construction. Timezone is a UI presentation
concern; no backend entity carries one. This is not pedantry: rules read `.hour` for availability
windows, so a `+02:00` value would yield a *local* hour and silently mis-enforce opening times.
Callers must `.astimezone(timezone.utc)` at the boundary.

## Phase 2: The AI Generation & Verification Loop
Design an automated architectural loop to generate rules safely. 
* **Model Recommendation:** Default to **`claude-opus-4-8`**; make the model ID configurable.
  > ⚠️ **Do not use `claude-3-haiku-20240307`.** An earlier draft of this file recommended "Claude 3
  > Haiku"; that model **retired on 2026-04-19 and now returns 404**. Its live successor is
  > **`claude-haiku-4-5`** ($1/$5 per MTok).
  >
  > The default is Opus rather than Haiku deliberately: a subtly wrong rule silently mis-enforces
  > real bookings, and every Tester retry costs a full generate-plus-test cycle, so the cheaper model
  > is not obviously cheaper end to end. `benchmark.py` (task 3.9) runs Opus 4.8 and Haiku 4.5 side by
  > side on the golden examples; **if Haiku 4.5 matches the success rate, flip the default and record
  > the result here.** Settle it with the benchmark, not by assumption.
* **Agent A (The Generator):** * *Input:* Natural language prompt (e.g., "Users can only book twice a rolling week").
  * *System Prompt:* "You are an expert Python developer for Open-Skej. Output a Python class inheriting from `BaseRule`. Ensure it relies only on the `HistoryContext` and standard `datetime` math. Output parameterized variables so the rule is reusable."
* **Agent B (The Tester / Adversary):**
  * *Input:* The generated Python code from Agent A.
  * *System Prompt:* "Write `pytest` functions to verify this rule. Create positive cases (should pass) and negative cases (edge cases that should fail, like timezone overlaps or 3rd bookings)."
* **The Loop:** Execute Agent B's tests against Agent A's code in a safe subprocess. If tests fail, feed the stack trace back to Agent A for a maximum of 3 retries.

## Phase 3: Benchmarking Scaffold
* Create a CLI script (`benchmark.py`) that feeds 5 "Golden Examples" (e.g., "max 1 hour", "only on weekends", "max 2 times a week") into the Generation Loop.
* Log the success rate, token usage, and latency to optimize the system prompts before wiring it to the web UI.

## Controller Logic
* Write the `evaluate_request()` function. 
* It should sequentially execute all active rules for the resource.
* It must "Fail-Fast" (return immediately on the first rule that returns `pass=False`).