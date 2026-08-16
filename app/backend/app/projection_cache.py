"""An in-process cache for ``app.projection.project_days``'s rules-only result, per Space and date.

**Why this is safe to cache at all.** ``app.projection.project_days`` (task 1's filtered canon, task
2's split of rule verdicts from Resource overlap) produces a result that depends on exactly three
things: the Space's own configuration, the calendar date being projected, and nothing else — no
member, no Resource, no booking history. Two different members asking about the identical Space and
date get the identical rules-only answer; ``app.routers.bookable`` is what turns that shared answer
into a per-Resource one, by clipping it against one Resource's own bookings *after* it comes out of
this cache (``app.projection.clip_overlap``) — never before, and never inside this module.

**The key.** ``(space_id, on_date, rules_version, catalog_generation)``:

* ``space_id`` and ``on_date`` are the two axes the cached value is actually a function of.
* ``rules_version`` is ``Space.rules_version`` — a plain integer, bumped by
  ``app.identity.service`` on every write to one of this Space's own ``space_rules`` rows (create,
  update, delete; see that module's ``_bump_rules_version``). It only ever increases, so a stale
  entry is simply never looked up again under its old key rather than needing to be found and
  evicted.
* ``catalog_generation`` is ``app.rule_catalog.RuleCatalog.generation`` — bumped once per
  ``reload()``, i.e. whenever the process's view of *generated* rule types changes at all. This is
  deliberately coarser than ``rules_version``: it is process-wide, not scoped to the Spaces that
  actually use the generated type that changed, because nothing in this codebase today records
  which Space's rows name which generated type. Over-invalidating (every Space's cache entry misses
  once after any reload, even one a given Space's own canon never touched) costs a re-scan nobody
  strictly needed; under-invalidating would serve a stale answer past a real change, which is the
  direction this cache must never be wrong in. A production version that tracked generated-type
  usage per Space could scope this to just the affected Spaces; this POC does not attempt that.

**The TTL.** Two registered rule types are relative to the current instant rather than to the
Space's configuration — ``not_in_the_past`` and ``booking_horizon`` — so a cached day's answer ages
even when neither ``rules_version`` nor ``catalog_generation`` has moved: a slot cached as "allowed"
a few minutes ago can have quietly slipped into the past, or past the horizon, by the time it is
served again. ``DEFAULT_TTL_SECONDS`` bounds how stale a served answer can be on that account alone.
60 seconds is chosen, not measured: short enough that the two clock-relative rules are wrong for at
most a minute (and note this is a *display* staleness only — the real gate stays ``evaluate()`` at
booking time, which always runs against the live clock; see ``app.routers.bookable``'s own module
docstring, "Real bookability, not just real rules"), and long enough that the common case — one
member loading a calendar week, or several members loading the same Space's week in quick
succession — still mostly hits the cache instead of re-running the scan. A production deployment
with real traffic data might tune this differently; nothing here claims 60 is the right number, only
that it is a stated one.

**In-process only, deliberately, for this POC.** ``_ENTRIES`` is a plain ``dict`` guarded by a
``threading.Lock`` — safe within one process, invisible to any other. A backend running more than
one process (multiple uvicorn workers, multiple hosts behind a load balancer) would have one cache
per process, each cold on its own workers and each capable of serving a *different* stale answer
than its siblings until every process's TTL independently expires — no correctness problem (every
process still bounds staleness to its own TTL, and ``rules_version``/``catalog_generation`` still
force a miss everywhere a write actually changed something), but a real efficiency and consistency
loss the single-process case does not have. A shared cache (Redis, memcached) keyed identically would
replace this module without changing anything above it — ``app.routers.bookable`` would still call
``get``/``put`` the same way — since nothing about the key or the TTL policy above depends on the
storage being local.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date

from app.projection import DayProjection
from app.rules_stub import NotProjectedRuleType

__all__ = ["DEFAULT_TTL_SECONDS", "ProjectionCache", "projection_cache"]

#: See the module docstring, "The TTL".
DEFAULT_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class _Key:
    space_id: int
    on_date: date
    rules_version: int
    catalog_generation: int


@dataclass(frozen=True)
class _Entry:
    day: DayProjection
    not_projected: tuple[NotProjectedRuleType, ...]
    expires_at: float


class ProjectionCache:
    """A small, thread-safe, in-process ``{key: (DayProjection, notProjected)}`` store. See the
    module docstring for the key, the TTL, and why in-process is a stated POC limitation rather than
    an oversight.

    ``time.monotonic()`` throughout, not the wall clock — a TTL is an elapsed-time budget, and the
    wall clock can jump (NTP, a leap second, a laptop waking from sleep) in a way that would make an
    entry expire early or, worse, look fresh for far longer than ``ttl_seconds`` actually allows.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive; got {ttl_seconds!r}")
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[_Key, _Entry] = {}

    def get(
        self,
        *,
        space_id: int,
        on_date: date,
        rules_version: int,
        catalog_generation: int,
    ) -> tuple[DayProjection, tuple[NotProjectedRuleType, ...]] | None:
        """The cached ``(DayProjection, notProjected)`` for this exact key, or ``None`` on a miss —
        an absent entry and an expired one are the identical answer to the caller, which always
        falls back to actually projecting the day either way."""
        key = _Key(space_id, on_date, rules_version, catalog_generation)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.expires_at <= now:
                return None
            return entry.day, entry.not_projected

    def put(
        self,
        *,
        space_id: int,
        on_date: date,
        rules_version: int,
        catalog_generation: int,
        day: DayProjection,
        not_projected: tuple[NotProjectedRuleType, ...],
    ) -> None:
        """Store ``day``/``not_projected`` under this key, superseding whatever was there —
        including, harmlessly, an entry another thread raced to store first under the identical key
        with an identical value (the projection is a pure function of the key, so a race here can
        only ever overwrite an entry with an equal one, never a conflicting one)."""
        key = _Key(space_id, on_date, rules_version, catalog_generation)
        expires_at = time.monotonic() + self._ttl_seconds
        entry = _Entry(day=day, not_projected=not_projected, expires_at=expires_at)
        with self._lock:
            self._entries[key] = entry

    def clear(self) -> None:
        """Drop every entry. Not part of the invalidation story above — tests use this to start a
        case from a known-empty cache rather than racing the module-level singleton's TTL."""
        with self._lock:
            self._entries.clear()


#: The one live cache every request handler shares — matching ``app.rule_catalog.catalog``'s own
#: module-level-singleton shape, for the identical reason: there is exactly one live view of "what
#: is currently cached" per process, read and written by every request it serves.
projection_cache = ProjectionCache()
