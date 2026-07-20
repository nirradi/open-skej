# Stream 3: Rule Engine & AI Generation — Implementation Plan

## Context

Stream 3 owns the isolated Python execution environment for booking rules: the strict data
interfaces rules are written against, the controller that runs them, a safe-execution boundary for
AI-generated code, and a dev-time generation loop (Generator + Tester) that produces the hardcoded
"Golden Canon" of rules.

Current state of the repo:

* `rules/` is a bare Python package — `rules/rules/__init__.py` is empty, `rules/tests/` holds a
  single `test_placeholder.py`. `rules/requirements.txt` pins only tooling (black, flake8, pytest).
* CI (`.github/workflows/ci.yml`) already has a `rule-engine` job running `black --check`, `flake8`,
  `pytest` in `rules/` on Python 3.12. Every task below must keep it green.
* **Stream 1 already shipped a working stub** at `app/backend/app/rules_stub.py` and it is wired into
  `POST /bookings`. It defines `BookingRequest`, `Context(history, now)`, `RuleResult(allowed,
  message)`, a `RULES` tuple, and `evaluate(booking, context) -> RuleResult` with fail-fast
  semantics. Its four rules (past, horizon, max duration, availability hours) are real behaviour the
  E2E suite asserts on. **Stream 3's interfaces must be a superset of that contract**, so integration
  is a swap of one import rather than a rewrite of the booking router.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Interface shape | One `Context` object aggregating `user` / `calendar` / `history` / `now`, not four positional params | `.claude/rules/stream-3-rules.md` specifies `evaluate(request, user, calendar, history, **kwargs)`. But Stream 1's shipped stub is `evaluate(booking, context)` and is already called from the router and asserted by E2E tests. Aggregating into `Context` satisfies the spec's *information* requirement while keeping the call signature Stream 1 already depends on. Adding a param to `BaseRule.evaluate` later is a breaking change across every rule; adding a field to `Context` is not. |
| Where the canon lives | `rules/` package, imported by the backend | Keeps the engine independently testable and CI-isolated. The backend depends on `rules`, never the reverse. |
| Backend integration | Deferred to a single final task (3.10), behind the same `evaluate` signature | Stream 1's E2E suite is the regression test for the swap. Doing it last means every prior task is pure-Python and cannot break the running app. |
| Rule parameterisation | Rules are **classes** with `__init__` params, instantiated into a canon list — not module-level constants | The stub hardcodes `MAX_BOOKING_DURATION` as a module constant. Real rules need per-Space values ("2h here, 45min there"), which Stream 2 will eventually supply. Parameters on the instance is the only shape that survives that. |
| Safe execution | AST allowlist **and** subprocess isolation — both, not either | AST validation alone is bypassable (`getattr(__builtins__, ...)`, decorators, comprehension scoping). Subprocess alone doesn't stop a rule from burning CPU inside a request. AST rejects the obvious at authoring time; the subprocess bounds the damage at test time. |
| When generated code runs in-process | **Never, until a human has reviewed and committed it** | Per `DEFERRED.md` §3, admin-authored runtime rules are out of scope. The AI loop is a *developer* tool that emits a `.py` file for review. Nothing generated is imported by the app without passing through a PR. This removes the entire class of "prompt injection becomes RCE" risk from the MVP. |
| Generator model | `claude-opus-4-8` default, model configurable, `claude-haiku-4-5` as the cheap comparator in `benchmark.py` | **`.claude/rules/stream-3-rules.md` recommends "Claude 3 Haiku" — that model (`claude-3-haiku-20240307`) retired 2026-04-19 and now 404s.** Its live successor is `claude-haiku-4-5` ($1/$5 per MTok). Defaulting to Opus rather than Haiku is a deliberate deviation: a subtly wrong rule silently mis-enforces real bookings, and each Tester retry costs a full generate+test cycle, so the cheaper model is not obviously cheaper end-to-end. `benchmark.py` (task 3.9) measures this rather than assuming it — if Haiku 4.5 hits the same success rate, flip the default and record it here. |
| Time bounds | `HistoryContext` capped at current calendar month **or** ±1 rolling week, whichever is wider | Per the stream brief. The cap is enforced when the context is *built* (backend side), and re-asserted as an invariant in `HistoryContext` so a rule cannot silently rely on data it will not get in production. |
| What history contains | **Everything in `HistoryContext` counts.** The caller filters before building it | Decided 2026-07-20. The engine does not inspect booking status, and `BookingRecord` carries no `status` field. If a booking should not count toward a limit, it is not in the context. This keeps the rule engine ignorant of a schema that will keep changing — a future `deleted` flag, a `no_show` flag, or a tentative-hold state would each silently invalidate rules that filtered internally, and the engine would have no way to know. Filtering stays with the layer that owns the schema. |
| Timezone | **UTC everywhere.** All datetimes UTC-aware; naive datetimes rejected at construction | Decided 2026-07-20. Timezone is a presentation concern owned by the UI; every backend entity, artifact, and rule input is UTC. `CalendarContext` therefore carries no timezone field, and week boundaries are UTC boundaries. This removes DST from the rule engine entirely — the single most likely source of subtle generated-rule bugs. |

**Interface contract (established in 3.1 — every later task depends on it):**

```python
@dataclass(frozen=True)
class UserContext:      user_id: str
@dataclass(frozen=True)
class CalendarContext:  week_starts_on: Weekday; now: datetime      # UTC
@dataclass(frozen=True)
class HistoryContext:   bookings: tuple[BookingRecord, ...]         # capped + pre-filtered
@dataclass(frozen=True)
class BookingRequest:   user_id: str; resource_id: str; start_at: datetime; end_at: datetime
@dataclass(frozen=True)
class RuleResult:       passed: bool; fail_reason: str | None

class BaseRule(ABC):
    @abstractmethod
    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult: ...
```

`UserContext` carries `user_id` only. The stream brief also lists `role` and `tier`, but no rule in
this phase branches on either, and Stream 2 owns roles — a field nothing reads is a field that will
be wrong by the time something does. Add them when a rule needs them.

`RuleResult.fail_reason` is **user-facing copy** shown verbatim in the UI — never an exception repr,
never a rule class name. `passed=True` implies `fail_reason is None`; enforced in `__post_init__`.

> Naming note: the stub uses `RuleResult(allowed, message)`; the stream brief specifies
> `(pass, fail_reason)`. `pass` is a Python keyword and cannot be a field name. Task 3.1 uses
> `(passed, fail_reason)` and task 3.10 adapts at the boundary.

## Task Breakdown

Each task is one PR, delegated to a headless Sonnet sub-agent and reviewed before merge.

- [x] **3.1 — Core interfaces.** _(DONE — PR #19)_ Create `rules/rules/interfaces.py` with the dataclasses above plus a
  `Weekday` enum and a `Context` aggregate. All frozen, all validated in `__post_init__`: naive
  datetimes rejected, `start_at < end_at`, `passed`/`fail_reason` consistency, `HistoryContext`
  window invariant. No `Role`/`Tier` enums — see the contract note above. No rule logic in this file.
  Unit tests must cover every rejection path, not just the happy construction.

- [x] **3.2 — Controller (`evaluate_request`).** _(DONE — PR #21)_ `rules/rules/controller.py` exposing
  `evaluate_request(request, context, canon) -> RuleResult`. Runs rules in order, **fail-fast** on
  the first `passed=False`. A rule that raises must be caught and converted to a denial with a
  generic friendly message (never leak the traceback to the user) while logging the real error —
  a buggy rule must not 500 the booking endpoint. Tests: ordering, short-circuit (assert later rules
  are never called), empty canon passes, raising rule is contained.
  **Also cross-check request against context** (surfaced reviewing 3.1): `Context` cannot validate
  that `history.bookings` belong to the requesting user and resource, because the request is not
  visible at construction time. The controller is the first place both are in scope, so it must
  reject a mismatch loudly. Without this, a mis-built context silently counts a *different* user's
  bookings toward this user's weekly cap.

- [ ] **3.3 — AST safety validator.** `rules/rules/safety.py` exposing
  `validate_source(src) -> None | raises UnsafeRuleError`. Rejects: `import`/`from` outside an
  allowlist (`datetime`, `zoneinfo`, `math`), all dunder attribute access, `exec`/`eval`/`compile`/
  `open`/`__import__`/`getattr`/`globals`/`locals`, decorators, `global`/`nonlocal`, `while` loops
  (unbounded), and comprehensions over non-local names. Tests must include a **bypass-attempt suite** —
  each blocked construct spelled three ways.

- [ ] **3.4 — Subprocess sandbox runner.** `rules/rules/sandbox.py` — runs a candidate rule module in
  a fresh `subprocess` with a wall-clock timeout, a memory cap, no inherited env, and cwd set to a
  temp dir. Returns structured pass/fail/timeout/crash. This is what the Tester agent's pytest runs
  execute inside. Tests: timeout fires on an infinite loop, crash is reported not raised, clean run
  returns results.

- [ ] **3.5 — First hardcoded canon rules (hand-written).** Port the four stub rules into real
  `BaseRule` subclasses with constructor parameters: `MaxDurationRule(max_duration)`,
  `AvailabilityHoursRule(open, close)`, `NotInThePastRule()`, `BookingHorizonRule(days)`. Messages
  must match the stub's copy so Stream 1's E2E assertions still pass after 3.10. Written by hand,
  not generated — these are the reference the AI loop is measured against.

- [ ] **3.6 — History-dependent canon rules (hand-written).** `MaxBookingsPerWeekRule(n)` and
  `MaxBookingsPerMonthRule(n)`, consuming `HistoryContext` and `CalendarContext.week_starts_on`.
  Tests must pin the boundaries explicitly: the nth booking passes and the (n+1)th fails, a booking
  one second before the week boundary does not count toward the current week, and month boundaries
  behave across a year rollover. **No status filtering** — every entry in `HistoryContext` counts
  (see Decisions). All boundaries are UTC; no DST cases exist.

- [ ] **3.7 — Agent A (Generator).** `rules/generation/generator.py`. Anthropic SDK call taking a
  natural-language rule description and returning Python source for a `BaseRule` subclass. System
  prompt per the stream brief: parameterised, `datetime`-only, `HistoryContext`-driven. Output runs
  through `validate_source` (3.3) before it is written anywhere. Model ID and effort configurable;
  API key from env, never committed. Unit tests mock the API client — **no test may make a live call.**

- [ ] **3.8 — Agent B (Tester) + the retry loop.** `rules/generation/tester.py` and `loop.py`.
  Tester generates `pytest` functions (positive cases + adversarial edge cases: timezone overlap,
  the n+1th booking, week boundaries). `loop.py` executes them against the candidate via the 3.4
  sandbox and feeds failures back to Agent A, **max 3 retries**, then gives up and reports. Emits the
  final artifact to `rules/generated/` for human review — never imports it. Tests mock both agents
  and assert the retry accounting and the give-up path.

- [ ] **3.9 — Benchmark scaffold.** `rules/benchmark.py` CLI feeding 5 golden examples ("max 1 hour",
  "only on weekends", "max 2 times a week", "no more than 3 hours total per day", "at most 2 bookings
  on the same day") through the loop. Logs per-example success rate, retry count, token usage, latency,
  and cost to a JSON report. **Runs Opus 4.8 and Haiku 4.5 side by side** so the model decision above
  is settled by data. Excluded from the CI `pytest` run (it makes live API calls); invoked manually.

- [ ] **3.10 — Backend integration.** Add `rules` as a backend dependency. Replace the body of
  `app/backend/app/rules_stub.py` with an adapter that builds a `Context` from the request + booking
  history and delegates to `evaluate_request` with the 3.5/3.6 canon. **Keep the module's existing
  public names and `RuleResult(allowed, message)` shape** so `routers/bookings.py` is untouched.
  The adapter must **`.astimezone(timezone.utc)` every datetime** before constructing engine types:
  3.1 rejects non-zero UTC offsets outright, while Stream 1's stub accepts any aware datetime — so a
  value the stub tolerated will now raise. Acceptance: the full Stream 1 E2E Playwright suite passes
  unchanged.

## Resolved Product Questions

All four questions raised during planning were answered on 2026-07-20. Recorded here so the
reasoning survives; the Decisions table above is the operative version.

1. **Do `role` and `tier` do anything in the MVP?** No — so they are not in `UserContext`. Fields
   nothing reads drift out of sync with reality before anything starts reading them. `UserContext`
   is `user_id` only; role and tier arrive when a rule needs them.
2. **Do cancelled bookings count toward "twice a week"?** Wrong layer to ask. **Everything in
   `HistoryContext` counts, and the caller filters before building it.** The engine never inspects
   booking status. The reasoning generalises past cancellation: history will accumulate states this
   phase can't predict — a `deleted` flag, a no-show marker, a tentative hold — and a rule engine
   that filtered internally would silently mis-enforce the moment one appeared, with no signal that
   it had. Filtering belongs to the layer that owns the schema.
3. **Per-Space rule configuration — who stores it?** Out of scope for this phase; logged in
   `DEFERRED.md` §4. The canon is a module-level hardcoded list with literal parameters. Rules stay
   parameterised by constructor argument so wiring per-Space config later is a change to the canon
   list, not to any rule.
4. **Whose timezone governs `AvailabilityHoursRule`?** UTC. Timezone is a UI-layer presentation
   concern; every backend entity, artifact, and rule input is UTC. `CalendarContext` has no timezone
   field and the rule engine has no DST cases.
