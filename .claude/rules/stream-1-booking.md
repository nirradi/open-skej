---
description: Full stack E2E booking flow implementation
glob: "app/**/*"
---
# Stream 1: Core E2E Booking (The "Happy/Sad Path")

## Objective
Build the full-stack implementation of the scheduling calendar, handling both successful database persistence and friendly rejection messages. 

## Boundaries & Constraints
* Bypass real user authentication for now (assume a single default user and a single default "Space").
* Use an abstracted data driver with SQLite for local persistence.
* Use a stubbed/dummy rule engine that conditionally passes or fails based on simple mock logic.
* The integration stream will later wire the user token to the driver for proper SQL scoping.

## Frontend Responsibilities
* Implement a calendar UI (e.g., `react-big-calendar` or similar).
* Support basic UI configuration (size of time slots, general availability hours).
* Create the booking submission flow (clicking a slot, hitting "Book").
* Render a success state (toast/UI update) when a booking persists.
* Render a friendly error state displaying the specific denial message when rejected.

## Backend Responsibilities
* Create `POST /bookings` and `GET /bookings` endpoints.
* Implement a local SQLite driver to persist and retrieve mock bookings.
* Route the `POST` request through a dummy rule validation function before saving.

## E2E Testing Responsibilities (Playwright)
* Initialize a standalone Playwright testing suite in a new `/app/e2e` directory (with its own `package.json` and `playwright.config.ts`).
* Configure Playwright to test against the locally running development environment (routing to both the Frontend UI and FastAPI backend).
* **Implement Core Proof Tests:**
  * **Test 1 - UI Rendering:** Verify the calendar loads correctly, displaying the appropriate grid based on configured time slots.
  * **Test 2 - The Happy Path (Approved):** Simulate a user selecting an available time slot and clicking "Book". Verify that the backend persists the data, a success toast/message appears, and the calendar visually reflects the slot as booked.
  * **Test 3 - The Sad Path (Denied):** Simulate a booking that triggers the stubbed rule engine's denial state. Verify that the specific, friendly error message returned by the backend is displayed to the user, and the slot remains available on the calendar.