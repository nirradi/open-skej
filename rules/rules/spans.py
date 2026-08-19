"""Merging contiguous spans into runs.

Generic interval-merging math with no engine-specific meaning of its own — a "span" here is just a
``(start, end)`` pair of ``datetime``s, and a "run" is what :func:`merge_adjoining_spans` merges
spans into. Two different callers turn that into something the engine cares about:

* ``app.rules_stub._resolve_run`` (task 8.4) sweeps a request together with a user's history and
  builds ``Context.run`` from the one merged span the request falls into.
* ``MaxBookingsPerWeekRule`` / ``MaxBookingsPerMonthRule`` (task 8.6, ``frequency.py``) sweep a
  request together with ``context.history.bookings`` at evaluate time and count how many merged
  runs start inside their window — see that module's docstring for why the merge happens inside
  the rule rather than being folded into ``Context.history`` ahead of time by the adapter.

Living here rather than in either caller is what lets both share one sweep instead of each
computing its own closure — the adapter installs into the backend as ``rules`` (an editable
sibling package), so a module here is exactly as reachable from ``app.rules_stub`` as from
``rules.frequency``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

__all__ = ["MergedSpan", "merge_adjoining_spans"]


@dataclass(frozen=True)
class MergedSpan:
    """One contiguous run out of :func:`merge_adjoining_spans`'s closure."""

    start_at: datetime
    end_at: datetime
    booking_count: int


def merge_adjoining_spans(
    spans: list[tuple[datetime, datetime]], tolerance: timedelta
) -> list[MergedSpan]:
    """Merge ``spans`` into every contiguous run they form, closing a gap up to ``tolerance``.

    Returns **all** merged runs, not only the one a particular span falls into — this is what lets
    a caller project the closure two different ways (one run vs. every run in a window) from a
    single sweep.

    Two spans join when the gap between their sorted order is **zero** (exact abutment),
    **negative** (an overlap — reachable once the input is drawn from more than one Resource, since
    two Resources' bookings are never mutually exclusive in time the way two bookings on one
    Resource are), or **smaller than ``tolerance``**. The join is **transitive**: three spans each
    within tolerance of the next merge into one run even if the first and third are not within
    tolerance of each other, because merging is a single left-to-right sweep over the sorted spans
    rather than a pairwise comparison — 17-18 and 18-19 held, a request for 19-20, is one 17-20 run.

    Why a tolerance at all, when ``max-duration-cannon.md``'s decision 3 chose exact abutment: that
    decision rests on every booking landing on a grid, which makes a sub-grid gap unconstructable.
    A Space's calendar shape (``rules/shape/``, ``.claude/rules/calendar-shape.md``) can offer
    several durations or several differently anchored blocks, so a Space configuring one has
    start times that are not all a fixed distance apart — a 5-minute gap between 17:00-18:00 and
    18:05-19:05 would break the run, nobody could ever construct a booking to fill exactly that
    gap, and every run-based rule gets a free escape hatch. A tolerance equal to the date's own
    smallest offered duration closes it: any gap a legal booking could actually occupy is at least
    that long, so a gap shorter than it is not "two sessions with a short break between them", it
    is dead space nothing could ever book — indistinguishable from no gap at all. A caller with no
    operating block to resolve on a date passes ``tolerance == timedelta(0)``, which is exactly
    decision 3's original exact-abutment rule, so this is a strict generalisation of it rather than
    a loosening.
    """
    if not spans:
        return []

    ordered = sorted(spans, key=lambda span: span[0])
    merged: list[MergedSpan] = []
    run_start, run_end = ordered[0]
    run_count = 1

    for start_at, end_at in ordered[1:]:
        gap = start_at - run_end
        if gap <= timedelta(0) or gap < tolerance:
            run_end = max(run_end, end_at)
            run_count += 1
        else:
            merged.append(MergedSpan(run_start, run_end, run_count))
            run_start, run_end, run_count = start_at, end_at, 1

    merged.append(MergedSpan(run_start, run_end, run_count))
    return merged
