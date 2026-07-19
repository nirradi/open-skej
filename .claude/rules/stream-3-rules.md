# Stream 3: Rule Engine Phase 1 (The "Brain Skeleton")

## Objective
Create the pure Python architecture and logic for the rule engine, alongside the initial canon of parameterized rules.

## Boundaries & Constraints
* Strictly backend execution logic.
* No UI work.
* No dynamic Admin-authored rules yet (Phase 2 feature).
* The engine simply ingests a mock booking context and outputs a boolean/message result.

## Responsibilities
* Define the base standard `Rule` interface.
* Create the `BookingRequest` input model (User ID, Resource ID, Start Time, End Time).
* Create the `Context` input model (User's booking history for the relevant period).
* Create the `RuleResult` output model (Allowed: Boolean, Message: String).
* Write the Engine Controller to fetch active rules, inject context, and run them sequentially.
* Hardcode the initial canon of parameterized Python rules (e.g., "Max Bookings Per Period", "Max Duration").
* Write comprehensive unit tests for the engine and the initial rules.