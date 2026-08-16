#!/usr/bin/env python
"""Benchmark ``app.projection.project_days`` against synthetic Spaces, entirely in memory.

**Invoked by hand, never collected by pytest** — the same convention ``rules/benchmark.py``
already follows, for the same reason: this is a measurement tool, not a correctness check, and
``app/backend/pyproject.toml``'s ``testpaths = ["tests"]`` is what keeps it off the CI path. Run it
directly:

    PYTHONPATH=app/backend:../rules venv/bin/python scripts/projection_bench.py
    PYTHONPATH=app/backend:../rules venv/bin/python scripts/projection_bench.py --repeats 9

No database, no docker, no network: the "Space" this script projects against is a
``SpaceRuleConfig`` built in this process, never read from a table, and its canon is the real
``rules.evaluate_request`` running the real rule classes — nothing here reimplements a rule's
semantics or times a mock.

**Why this reuses ``app.rules_stub``'s own private functions rather than re-deriving them.** The
whole question this benchmark answers is "is evaluating the real per-booking machinery this many
times affordable" — a benchmark that measured a hand-rolled, simplified stand-in for
``_build_local_frame`` / ``_resolve_run`` / ``resolve_day_schedule`` would answer a different,
easier question and call it the same name. This script imports those functions directly
(``app.rules_stub`` is the adapter every real booking already goes through) and calls them exactly
as ``rules_stub.evaluate()`` does for one booking, once per synthetic candidate ``project_days``
asks about.

``project_days`` resolves its canon **per day** (``canon_for_day``, see
``app/backend/app/projection.py``'s own docstring) — the identical granularity
``app.rules_stub._build_canon(config, on_date)`` resolves at for one real booking. This script's
``canon_for_day`` closes over one synthetic ``SpaceRuleConfig`` and calls ``_build_canon`` fresh for
each ``DayGrid.date`` in the window, exactly as a real Space with weekday- or date-scoped
``applies_to`` rows would need. The synthetic Space's own hours and session length happen not to
vary by weekday, so the per-day resolution here always returns the identical canon across the
window — the benchmark still measures the real per-day resolution cost (one ``_build_canon`` call
per ``DayGrid``, not per candidate), it is just that this script's own Space does not exercise the
*varying* case. A future case that seeds an ``applies_to``-scoped row to actually vary the canon by
weekday is a natural extension, not something today's signature blocks any more.

**The axes below, and why these and not others.** Slot size and duration-cap presence are the two
the pass condition names directly. Canon (seeded-only vs. seeded-plus-one-more) and history
(empty vs. ~50 bookings) are the two things about a *real* Space most likely to move the number:
an extra rule adds a fixed per-candidate cost, and non-empty history adds a cost
(``_resolve_run``'s sweep) that scales with how much of it there is, on *every* candidate, since
``project_days`` builds a fresh context per candidate rather than caching one across a slot's scan
lengths (``app/backend/app/projection.py``, "What this module deliberately does not do"). Window is
fixed at 7 days throughout, since a 7-day window at 30-minute slots with a duration cap is the
specific pass condition asked for; it is not varied because nothing in this benchmark's job asks
what happens at a different window length.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Make `app` and `rules` importable regardless of the caller's cwd, mirroring what the backend's
# own editable install (`-e ../../rules`) and package layout already give a normal `python -m`
# invocation — a standalone script has neither, so it is done by hand here rather than requiring
# every invocation to set PYTHONPATH just to reach a sibling directory it already knows the path to.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]  # .../app/backend
_RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"  # .../rules, sibling of .../app
for _path in (_BACKEND_ROOT, _RULES_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.rules_stub import (  # noqa: E402
    WEEK_STARTS_ON,
    SpaceRuleConfig,
    SpaceRuleRow,
    _build_canon,
    _build_local_frame,
    _local_date,
    _resolve_run,
    resolve_day_schedule,
)
from app.projection import DayGrid, project_days  # noqa: E402
from rules import (  # noqa: E402
    BaseRule,
    BookingRecord as EngineBookingRecord,
    BookingRequest as EngineBookingRequest,
    CalendarContext,
    Context as EngineContext,
    HistoryContext,
    RuleResult,
    UserContext,
)

__all__ = [
    "SimulatedGeneratedRule",
    "BenchmarkCase",
    "BenchmarkResult",
    "build_synthetic_history",
    "run_case",
    "run_all",
    "format_table",
    "main",
]

# --- the synthetic Space -------------------------------------------------------------------
#
# One fixed Space throughout: real, non-UTC timezone (so the timezone-conversion cost inside
# `_build_local_frame` is genuinely paid, not skipped by an accidental UTC shortcut), the exact
# two `space_rules` rows `identity.service.create_space` seeds for every new Space
# (`_DEFAULT_OPENS_AT_MINUTES` / `_DEFAULT_CLOSES_AT_MINUTES` / `_DEFAULT_SESSION_MINUTES`,
# `app/backend/app/identity/service.py`), plus an optional `max_duration` row and an optional
# extra rule standing in for a generated one.

SPACE_TZ = "America/New_York"
USER_ID = "bench-user"
RESOURCE_ID = "bench-resource"

OPENS_AT_MINUTES = 9 * 60
CLOSES_AT_MINUTES = 17 * 60
SESSION_MINUTES = 60
MAX_DURATION_MINUTES = 120

#: The window this benchmark always projects: a Monday through the following Sunday, chosen with
#: no daylight-saving transition inside it so `_build_local_frame`'s cost reflects an ordinary
#: week, not the DST edge case `app/backend/app/projection.py`'s own docstring already calls out
#: as out of scope for `DayGrid`.
WINDOW_START = date(2026, 8, 17)
WINDOW_DAYS = 7

NOW_UTC = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


class SimulatedGeneratedRule(BaseRule):
    """A stand-in for an AI-generated rule, written to cost about what one plausibly would —
    a handful of branches over ``context.local`` and a linear scan of
    ``context.history.bookings`` — not a rule anyone would actually configure.

    Modelled on "close the Space on these dates, and cap total weekend court time per day":
    denies outright on ``blocked_dates`` (a maintenance closure), and on a Saturday or Sunday
    denies once this booking would push the day's own booked minutes — summed from history the
    same way ``MaxDurationPerDayRule`` does (``rules/rules/frequency.py``), never merged into
    runs, since a generated rule has no reason to know about ``context.run`` unless it was
    specifically taught to — past ``weekend_daily_cap_minutes``. Reads only ``context.local`` and
    ``context.history``, exactly the two sources a generated rule is allowed to
    (``.claude/rules/rule-engine.md``, "Generator").
    """

    def __init__(self, blocked_dates: frozenset[date], weekend_daily_cap_minutes: int) -> None:
        self.blocked_dates = blocked_dates
        self.weekend_daily_cap_minutes = weekend_daily_cap_minutes

    def evaluate(self, request: EngineBookingRequest, context: EngineContext) -> RuleResult:
        local = context.local

        if local.day_start.date() in self.blocked_dates:
            return RuleResult.deny(
                "This Space is closed on this date for scheduled maintenance."
                " Please pick a different day."
            )

        if local.weekday in (5, 6):  # Saturday, Sunday
            booked_minutes = request.duration.total_seconds() / 60
            for booking in context.history.bookings:
                if local.day_start <= booking.start_at < local.day_end:
                    booked_minutes += (booking.end_at - booking.start_at).total_seconds() / 60
            if booked_minutes > self.weekend_daily_cap_minutes:
                return RuleResult.deny(
                    "Weekend court time is capped at "
                    f"{self.weekend_daily_cap_minutes} minutes per day, and this booking would"
                    f" bring the day's total to {booked_minutes:.0f} minutes."
                    " Please shorten it or pick a weekday."
                )
        return RuleResult.allow()


def build_synthetic_history(count: int) -> tuple[EngineBookingRecord, ...]:
    """``count`` one-hour bookings for ``USER_ID``, spread across the calendar month ``NOW_UTC``
    falls in — inside ``rules.history_window(NOW_UTC)`` (the current month, here wider than the
    7-day rolling window either side), which is the bound ``Context`` enforces on every entry
    (``rules/rules/interfaces.py``). Spread one per ~14 hours so ``count=50`` fits comfortably
    inside a 31-day month without overlapping itself, alternating resources the way a member who
    plays more than one court realistically would.
    """
    records = []
    month_start = NOW_UTC.replace(day=1, hour=7, minute=0, second=0, microsecond=0)
    for index in range(count):
        start_at = month_start + timedelta(hours=14 * index)
        records.append(
            EngineBookingRecord(
                user_id=USER_ID,
                resource_id=RESOURCE_ID if index % 2 == 0 else f"{RESOURCE_ID}-2",
                start_at=start_at,
                end_at=start_at + timedelta(hours=1),
            )
        )
    return tuple(records)


def _make_context_builder(
    config: SpaceRuleConfig,
    history: tuple[EngineBookingRecord, ...],
):
    """The ``make_request_and_context`` callable ``project_days`` calls once per candidate.

    Mirrors ``app.rules_stub.evaluate()``'s own context construction exactly — ``_build_local_frame``,
    ``resolve_day_schedule`` for the run's gap tolerance, ``_resolve_run`` — because that is the
    machinery a real booking pays for and the whole point of this benchmark is to price it, not a
    simplified stand-in for it (module docstring). The two arguments arrive as **naive local
    wall-clock datetimes**, per ``app/backend/app/projection.py``'s own contract; localizing them
    to ``SPACE_TZ`` and converting to UTC here is this function's job, exactly as the real adapter
    is the only thing in the system allowed to know a timezone.
    """
    zone = ZoneInfo(config.timezone)

    def make_request_and_context(
        start_local: datetime, end_local: datetime
    ) -> tuple[EngineBookingRequest, EngineContext]:
        start_at = start_local.replace(tzinfo=zone).astimezone(timezone.utc)
        end_at = end_local.replace(tzinfo=zone).astimezone(timezone.utc)

        request = EngineBookingRequest(
            user_id=USER_ID, resource_id=RESOURCE_ID, start_at=start_at, end_at=end_at
        )
        on_date = _local_date(start_at, config.timezone)
        local = _build_local_frame(request, config.timezone, on_date)
        tolerance_minutes = resolve_day_schedule(config, on_date).session_minutes or 0
        run = _resolve_run(request, history, timedelta(minutes=tolerance_minutes))
        context = EngineContext(
            user=UserContext(user_id=USER_ID),
            calendar=CalendarContext(week_starts_on=WEEK_STARTS_ON, now=NOW_UTC),
            local=local,
            run=run,
            history=HistoryContext(bookings=history),
        )
        return request, context

    return make_request_and_context


def _build_config(*, with_duration_cap: bool, with_session_length: bool = True) -> SpaceRuleConfig:
    rows = [
        SpaceRuleRow(
            id=1,
            rule_type="availability_hours",
            params={"opens_at_minutes": OPENS_AT_MINUTES, "closes_at_minutes": CLOSES_AT_MINUTES},
        ),
    ]
    if with_session_length:
        rows.append(
            SpaceRuleRow(
                id=2, rule_type="session_length", params={"session_minutes": SESSION_MINUTES}
            )
        )
    if with_duration_cap:
        rows.append(
            SpaceRuleRow(
                id=3,
                rule_type="max_duration",
                params={"max_duration_minutes": MAX_DURATION_MINUTES},
            )
        )
    return SpaceRuleConfig(timezone=SPACE_TZ, rules=tuple(rows))


def _build_grids(slot_minutes: int) -> tuple[DayGrid, ...]:
    slot_count = (CLOSES_AT_MINUTES - OPENS_AT_MINUTES) // slot_minutes
    return tuple(
        DayGrid(
            date=WINDOW_START + timedelta(days=offset),
            slot_minutes=slot_minutes,
            first_slot_minutes=OPENS_AT_MINUTES,
            slot_count=slot_count,
        )
        for offset in range(WINDOW_DAYS)
    )


@dataclass(frozen=True)
class BenchmarkCase:
    """One point in the axis grid this script reports."""

    slot_minutes: int
    with_duration_cap: bool
    with_extra_rule: bool
    history_count: int
    early_stop: bool
    #: ``False`` only for the supplementary "hours only" cases below — every case in the main
    #: matrix uses the real seeded canon, which always includes ``session_length`` (module
    #: docstring, "the exact two `space_rules` rows `create_space` seeds").
    with_session_length: bool = True

    @property
    def label(self) -> str:
        canon_label = "hours-only" if not self.with_session_length else (
            "seeded+ai" if self.with_extra_rule else "seeded   "
        )
        return (
            f"slot={self.slot_minutes:>2}m  "
            f"cap={'yes' if self.with_duration_cap else 'no ':<3}  "
            f"canon={canon_label:<10}  "
            f"history={self.history_count:>2}  "
            f"early_stop={'Y' if self.early_stop else 'N'}"
        )


@dataclass(frozen=True)
class BenchmarkResult:
    case: BenchmarkCase
    calls: int
    median_ms: float
    us_per_call: float
    repeats: int


def run_case(case: BenchmarkCase, *, repeats: int) -> BenchmarkResult:
    """Run ``project_days`` for ``case`` ``repeats`` times and report the median wall time.

    ``canon_for_day`` calls ``_build_canon(config, on_date)`` fresh for every ``DayGrid.date`` —
    the identical per-day resolution ``rules_stub.evaluate()`` performs for one real booking (module
    docstring) — so this timed section pays that cost once per day, not once for the whole window
    and not once per candidate. History and the grid are built once outside the loop; only
    ``project_days`` itself is inside the timing loop.
    """
    config = _build_config(
        with_duration_cap=case.with_duration_cap, with_session_length=case.with_session_length
    )
    extra_rule = (
        SimulatedGeneratedRule(
            blocked_dates=frozenset({WINDOW_START + timedelta(days=3)}),
            weekend_daily_cap_minutes=180,
        )
        if case.with_extra_rule
        else None
    )

    def canon_for_day(on_date: date) -> tuple[BaseRule, ...]:
        canon = _build_canon(config, on_date)
        if extra_rule is not None:
            canon = canon + (extra_rule,)
        return canon

    history = build_synthetic_history(case.history_count)
    make_request_and_context = _make_context_builder(config, history)
    grids = _build_grids(case.slot_minutes)

    durations_ms: list[float] = []
    calls = 0
    for _ in range(repeats):
        counter = [0]
        start = time.perf_counter()
        project_days(
            canon_for_day=canon_for_day,
            make_request_and_context=make_request_and_context,
            days=grids,
            early_stop=case.early_stop,
            call_counter=counter,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        durations_ms.append(elapsed_ms)
        calls = counter[0]  # identical every repeat — the algorithm is deterministic

    median_ms = statistics.median(durations_ms)
    us_per_call = (median_ms * 1000) / calls if calls else 0.0
    return BenchmarkResult(
        case=case, calls=calls, median_ms=median_ms, us_per_call=us_per_call, repeats=repeats
    )


def run_all(*, repeats: int) -> list[BenchmarkResult]:
    results = []
    for slot_minutes in (30, 15):
        for with_duration_cap in (True, False):
            for with_extra_rule in (False, True):
                for history_count in (0, 50):
                    for early_stop in (True, False):
                        case = BenchmarkCase(
                            slot_minutes=slot_minutes,
                            with_duration_cap=with_duration_cap,
                            with_extra_rule=with_extra_rule,
                            history_count=history_count,
                            early_stop=early_stop,
                        )
                        results.append(run_case(case, repeats=repeats))
    return results


def run_supplementary(*, repeats: int) -> list[BenchmarkResult]:
    """The literal "bounded only by closing time" worst case, isolated from ``session_length``.

    Every case in ``run_all`` uses the real seeded canon, which always includes a
    ``session_length`` row — and that row turns out to dominate the result: with a 60-minute
    session anchored on a 30- or 15-minute render grid, half or more of every day's candidate
    starts are off the session grid entirely and are denied on their very first scanned length,
    and the ones that *are* on-grid have their scan cut short by ``early_stop`` the moment the next
    length lands off-grid — almost always long before a duration cap or closing time would have
    stopped it anyway. That is a real, reportable property of the seeded canon (see this script's
    own output), but it means ``run_all`` never actually exercises the scan the assignment's pass
    condition calls "the worst case … bounded only by closing time" — that description assumes
    nothing shorter than closing time truncates the scan first, which is only true once
    ``session_length`` is out of the picture. These four cases isolate exactly that: the seeded
    ``availability_hours`` row alone, with and without ``max_duration``, at 30-minute slots.
    """
    results = []
    for with_duration_cap in (True, False):
        for early_stop in (True, False):
            case = BenchmarkCase(
                slot_minutes=30,
                with_duration_cap=with_duration_cap,
                with_extra_rule=False,
                history_count=0,
                early_stop=early_stop,
                with_session_length=False,
            )
            results.append(run_case(case, repeats=repeats))
    return results


def format_table(results: list[BenchmarkResult], *, repeats: int) -> str:
    lines = [
        f"Window: {WINDOW_DAYS} days from {WINDOW_START.isoformat()}, Space tz={SPACE_TZ},"
        f" operating hours {OPENS_AT_MINUTES // 60:02d}:00-{CLOSES_AT_MINUTES // 60:02d}:00.",
        f"Each row is the median of {repeats} repeats of one project_days(...) call.",
        "",
        f"{'case':<62}{'calls':>8}{'median_ms':>12}{'us/call':>10}",
        "-" * 92,
    ]
    for result in results:
        lines.append(
            f"{result.case.label:<62}{result.calls:>8}{result.median_ms:>12.2f}"
            f"{result.us_per_call:>10.2f}"
        )

    pass_condition = next(
        r
        for r in results
        if r.case.slot_minutes == 30
        and r.case.with_duration_cap
        and not r.case.with_extra_rule
        and r.case.history_count == 0
        and r.case.early_stop
    )
    worst_case = max(results, key=lambda r: r.median_ms)
    lines += [
        "",
        "Pass condition — 7-day window, 30-minute slots, max_duration set, canon only,"
        " no history, early_stop:",
        f"  {pass_condition.median_ms:.2f} ms"
        f" ({'PASS' if pass_condition.median_ms < 200 else 'FAIL'} — under 200 ms)"
        f", {pass_condition.calls} evaluate_request calls.",
        "",
        f"Worst case observed: {worst_case.case.label} -> {worst_case.median_ms:.2f} ms,"
        f" {worst_case.calls} calls.",
    ]
    return "\n".join(lines)


def format_supplementary_table(results: list[BenchmarkResult]) -> str:
    lines = [
        "Supplementary: availability_hours alone (no session_length) — the literal case the"
        " assignment calls 'bounded only by closing time'. See run_supplementary's docstring.",
        "",
        f"{'case':<62}{'calls':>8}{'median_ms':>12}{'us/call':>10}",
        "-" * 92,
    ]
    for result in results:
        lines.append(
            f"{result.case.label:<62}{result.calls:>8}{result.median_ms:>12.2f}"
            f"{result.us_per_call:>10.2f}"
        )
    worst_case = max(results, key=lambda r: r.median_ms)
    lines += [
        "",
        f"Closing-time-bound worst case: {worst_case.case.label} -> {worst_case.median_ms:.2f} ms,"
        f" {worst_case.calls} calls.",
    ]
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repeats",
        type=int,
        default=5,
        help="How many times to run each case before taking the median (default: 5).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).with_name("projection_bench_results.md"),
        help="Where to write the results table as Markdown (default: alongside this script).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    results = run_all(repeats=args.repeats)
    table = format_table(results, repeats=args.repeats)
    print(table)

    supplementary = run_supplementary(repeats=args.repeats)
    supplementary_table = format_supplementary_table(supplementary)
    print("\n" + supplementary_table)

    args.out.write_text(
        "# Projection benchmark results\n\n"
        "Generated by `app/backend/scripts/projection_bench.py`. See that script and\n"
        "`app/backend/app/projection.py` for what is measured and why.\n\n"
        "```\n" + table + "\n```\n\n"
        "```\n" + supplementary_table + "\n```\n"
    )
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
