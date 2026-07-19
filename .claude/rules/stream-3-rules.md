---
description: Constraints and plans for the core Rule Engine
glob: "rules/**/*"
---
---
description: Architecture, interfaces, and AI generation loop for the Python rule engine.
glob: "ruleengine/**/*"
---

# Stream 3: Rule Engine & AI Generation (The "Brain Skeleton")

## Objective
Build the isolated Python execution environment for booking rules. Implement the base interfaces, a benchmarking scaffold, and an autonomous AI loop (Generator + Tester) to create and verify new rules safely.

## Boundaries & Constraints
* Strictly backend execution logic (isolated in `/rules`).
* Time bounds: Rules will not evaluate history beyond the current calendar month or a week rolling window.
* Safe Execution: Generated Python code must not use dangerous imports (`os`, `sys`).

## Phase 1: Core Interfaces
Design the strict data classes that the rule snippets will interact with.
* **`UserContext`**: `user_id`, `role` (Admin/User), `tier` (Premium/Basic).
* **`CalendarContext`**: information like when a "week" starts
* **`BookingRequest`**: `resource_id`, `start_time` (datetime, UTC), `end_time` (datetime, UTC).
* **`HistoryContext`**: A lightweight list of the user's previous bookings for this resource (limited to the current month or last/next week).
* **`RuleResult`**: `pass` (Boolean), `fail_reason` (String - friendly error message if denied).
* **`BaseRule`**: The abstract class requiring an `evaluate(request, user, calendar, history, **kwargs)` method.

## Phase 2: The AI Generation & Verification Loop
Design an automated architectural loop to generate rules safely. 
* **Model Recommendation:** Use **Claude 3 Haiku** or **GPT-4o-mini** for generation. They are incredibly cheap, fast, and excellent at short Python snippets.
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