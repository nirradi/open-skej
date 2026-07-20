# Deferred Topics & Backlog

This document tracks features, architectural optimizations, and edge cases that are explicitly **out of scope** for the current development phase. Agents must not implement these features until they are formally moved to a Stream Plan.

## 1. Basic Configuration-Based Rejection (Resource DB)
* **Description:** Moving basic calendar math (Operating Hours, Grid Alignment, Slot Sizes) out of the dynamic Rule Engine and into static `Resource` database columns validated directly by the FastAPI controller.
* **Current MVP Behavior:** Until this is implemented, the Python Rule Engine is the **sole entity** responsible for running all booking validations. If operating hours or fixed blocks need to be enforced, they must be implemented as Python rules.

## 2. Resource Configuration Admin UI
* **Description:** A dedicated frontend Admin page to edit the basic resource configurations (setting the standard open/close times, timezone, and base slot intervals).
* **Current MVP Behavior:** Resource creation and updates will be handled either via direct database seeding, raw API calls, or hardcoded defaults for Stream 1/2 testing.

## 3. Dynamic Admin-Authored Rules UI and backend
* **Description:** Allowing admins to write natural language in the UI that the AI translates into new active Python rules on the fly.
* **Current MVP Behavior:** Stream 3 relies on a hardcoded "Golden Canon" of AI-generated rules that are tested and deployed by the developers, not dynamically by end-users.

## 4. Per-Space Rule Parameter Storage
* **Description:** Persisting each Space's rule parameters (max duration, booking horizon, weekly caps, availability hours) so the active canon is assembled per-Space from the database rather than from literals in code. Requires the Stream 2 `Spaces` schema to own the configuration table.
* **Current MVP Behavior:** Stream 3's rules are parameterised by constructor argument (`MaxDurationRule(max_duration=2h)`), but the canon itself is a module-level hardcoded list with literal values shared by every Space. Wiring per-Space config later is a change to how the canon list is built — no rule implementation has to change.