"""Tests for `app.rule_catalog` — the load path that turns a `generated_rule_types` row back into
a constructible `RuleType`, and the process-wide catalog built on top of it.

Most of this needs no database: an unpersisted `GeneratedRuleType(...)` is a plain Python object, so
`catalog.hoist(row)` is tested directly against rows built by hand. `_signed_row` below is the one
non-obvious piece of machinery — it computes a row's `source_sha256`, `executable_bytecode` and
`bytecode_magic` from real source so a test can corrupt exactly one field and know the other two are
still genuinely correct, rather than every field being suspect at once.

The end-to-end path — a row inserted into a real table, reloaded, and a booking judged against the
rule it hoists to — needs Postgres and is marked accordingly, following `test_rules_api.py` /
`test_rules_stub.py`'s own fixture pattern (`conftest.requires_postgres`).
"""

import builtins
import hashlib
import importlib.util
import logging
import marshal
from datetime import datetime, timedelta, timezone

import pytest
from rules import (
    RULE_ERROR_MESSAGE,
    CalendarContext,
    Context,
    HistoryContext,
    UserContext,
    Weekday,
    evaluate_request,
)

from rules.safety import SAFE_BUILTINS

from app.identity.models import GeneratedRuleType, GeneratedRuleTypeStatus
from app.rule_catalog import HoistError, RuleCatalog, _restricted_namespace
from app.rules_stub import BookingRequest, _build_local_frame, _engine_request, _local_date

from .conftest import requires_postgres

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

# A small, genuinely valid generated rule: reads `context.local` directly, exactly as 7.3/7.5 taught
# the generator to, and needs no import at all.
WEEKEND_SOURCE = (
    "class NoWeekendBookingsRule(BaseRule):\n"
    "    def evaluate(self, request, context):\n"
    "        if context.local.weekday >= 5:\n"
    "            return RuleResult.deny('No bookings on weekends.')\n"
    "        return RuleResult.allow()\n"
)

# Calls a name `validate_source` blocks outright — never reaches step 2.
UNSAFE_SOURCE = (
    "class ReadsAFileRule(BaseRule):\n"
    "    def evaluate(self, request, context):\n"
    "        open('/etc/passwd').read()\n"
    "        return RuleResult.allow()\n"
)

# `repr` is neither a `BLOCKED_NAMES` entry nor dunder-prefixed, so `validate_source` admits it —
# but it is not in `SAFE_BUILTINS` either, so the restricted namespace `hoist` executes it in has no
# such name. The rule hoists cleanly and only fails once it actually runs.
ESCAPES_TO_REPR_SOURCE = (
    "class UsesReprRule(BaseRule):\n"
    "    def evaluate(self, request, context):\n"
    "        return RuleResult.deny(repr(request))\n"
)

# A rule that imports, which the Generator's system prompt explicitly requires it to do for any
# name outside the seven engine free names. `validate_source` permits `datetime`; the restricted
# namespace's guarded `__import__` has to permit it too, or every such rule is unhoistable.
IMPORTS_DATETIME_SOURCE = (
    "from datetime import timedelta\n"
    "\n"
    "class MaxTwoHoursRule(BaseRule):\n"
    "    def evaluate(self, request, context):\n"
    "        if request.end_at - request.start_at > timedelta(hours=2):\n"
    "            return RuleResult.deny('At most 2 hours.')\n"
    "        return RuleResult.allow()\n"
)

# The mirror image: `validate_source` rejects this outright, so the guarded `__import__` is not what
# stops it here. The guard exists for the case no source check can cover — see
# `app.rule_catalog._guarded_import` — and is exercised directly in its own test below.
IMPORTS_OS_SOURCE = (
    "import os\n"
    "\n"
    "class ReadsEnvRule(BaseRule):\n"
    "    def evaluate(self, request, context):\n"
    "        return RuleResult.deny(os.environ.get('HOME'))\n"
)

NO_RULE_CLASS_SOURCE = "x = 1\n"

TWO_RULE_CLASSES_SOURCE = (
    "class FirstRule(BaseRule):\n"
    "    def evaluate(self, request, context):\n"
    "        return RuleResult.allow()\n"
    "\n"
    "class SecondRule(BaseRule):\n"
    "    def evaluate(self, request, context):\n"
    "        return RuleResult.allow()\n"
)


def _signed_row(source: str, **overrides) -> GeneratedRuleType:
    """An unpersisted `GeneratedRuleType` whose hash, bytecode and magic genuinely match `source`.

    Every hoist-rejection test starts from a row built this way — one that would hoist cleanly —
    and corrupts exactly the field the test is about via `overrides`, so a failure is provably
    caused by that one field rather than by an unrelated mistake in the fixture itself.
    """
    fields = dict(
        id=1,
        rule_type=f"generated-{abs(hash(source)) % 1_000_000}",
        label="A generated rule",
        description=None,
        prompt="a rule, for testing",
        human_code=source,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        executable_bytecode=marshal.dumps(compile(source, "<generated>", "exec")),
        python_version="3.12.0",
        bytecode_magic=importlib.util.MAGIC_NUMBER.hex(),
        param_schema=[],
        reads_history=False,
        priority=100,
        status=GeneratedRuleTypeStatus.ACTIVE,
        created_by_space_id=1,
        created_by_user_id=1,
    )
    fields.update(overrides)
    return GeneratedRuleType(**fields)


def _context(booking: BookingRequest, tz_name: str = "UTC") -> Context:
    """A minimal, valid `Context` for `booking`, built through the same private helpers
    `test_rules_stub.py` already uses — this module has no interest in local-frame arithmetic of
    its own, only in whether a hoisted rule reads the frame it is handed correctly.
    """
    engine_request = _engine_request(booking)
    on_date = _local_date(engine_request.start_at, tz_name)
    return Context(
        user=UserContext(user_id=booking.user_id),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=NOW),
        local=_build_local_frame(engine_request, tz_name, on_date),
        history=HistoryContext(),
    )


# A Saturday and a Wednesday, both in 2026-07, both comfortably inside every test's fixed `NOW`.
SATURDAY = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
WEDNESDAY = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


def _booking(start: datetime) -> BookingRequest:
    return BookingRequest(start_at=start, end_at=start + timedelta(hours=1))


# --- hoist(): each rejection reason, asserted from `hoist` directly and from `reload` skipping it


HOIST_FAILURE_CASES = {
    "fails_validate_source": _signed_row(UNSAFE_SOURCE, rule_type="reads-a-file"),
    "source_sha256_mismatch": _signed_row(
        WEEKEND_SOURCE, rule_type="weekend-a", source_sha256="0" * 64
    ),
    "bytecode_magic_mismatch": _signed_row(
        WEEKEND_SOURCE, rule_type="weekend-b", bytecode_magic="ffffffff"
    ),
    "bytecode_does_not_unmarshal": _signed_row(
        WEEKEND_SOURCE, rule_type="weekend-c", executable_bytecode=b"not valid marshal data"
    ),
    "no_baserule_subclass": _signed_row(NO_RULE_CLASS_SOURCE, rule_type="no-rule"),
    "two_baserule_subclasses": _signed_row(TWO_RULE_CLASSES_SOURCE, rule_type="two-rules"),
    "malformed_param_schema_entry": _signed_row(
        WEEKEND_SOURCE, rule_type="weekend-d", param_schema=[{"name": "x"}]
    ),
}


@pytest.mark.parametrize("row", HOIST_FAILURE_CASES.values(), ids=HOIST_FAILURE_CASES.keys())
def test_hoist_rejects_a_broken_row(row):
    with pytest.raises(HoistError):
        RuleCatalog().hoist(row)


@pytest.mark.parametrize("row", HOIST_FAILURE_CASES.values(), ids=HOIST_FAILURE_CASES.keys())
def test_reload_skips_a_broken_row_and_logs_rather_than_raising(row, caplog):
    class _OneRowSession:
        def execute(self, _statement):
            return self

        def scalars(self):
            return self

        def all(self):
            return [row]

    catalog = RuleCatalog()
    with caplog.at_level(logging.ERROR):
        catalog.reload(_OneRowSession())

    assert catalog.get(row.rule_type) is None
    assert any(str(row.id) in message for message in caplog.messages)


def test_hoist_names_the_two_baserule_subclasses_it_found():
    """The error names *which* classes collided, not just that there were two — the whole point of
    a message a human is meant to fix, rather than a bare boolean."""
    row = HOIST_FAILURE_CASES["two_baserule_subclasses"]
    with pytest.raises(HoistError, match="FirstRule.*SecondRule|SecondRule.*FirstRule"):
        RuleCatalog().hoist(row)


# --- the restricted namespace is real -----------------------------------------------------------


def test_restricted_namespace_rejects_open_at_validate_source():
    row = HOIST_FAILURE_CASES["fails_validate_source"]
    with pytest.raises(HoistError, match="open"):
        RuleCatalog().hoist(row)


def test_a_hoisted_rule_reaching_outside_safe_builtins_denies_rather_than_crashes():
    """`repr` passes `validate_source` (it is neither blocked nor dunder-prefixed) and so hoists
    fine — but the namespace `hoist` executes it in binds only `SAFE_BUILTINS`, so calling it at
    evaluation time raises `NameError`. The controller's containment (`rules.controller`) is what
    turns that into an ordinary denial; this asserts the denial, not the underlying exception,
    which is the whole point of that containment existing.
    """
    row = _signed_row(ESCAPES_TO_REPR_SOURCE, rule_type="escapes-to-repr")
    rule_type = RuleCatalog().hoist(row)
    rule = rule_type.build({})

    booking = _booking(WEDNESDAY)
    engine_request = _engine_request(booking)
    context = _context(booking)

    result = evaluate_request(engine_request, context, (rule,))

    assert not result.passed
    assert result.fail_reason == RULE_ERROR_MESSAGE


def test_a_rule_may_import_from_the_allowlist():
    """A generated rule that names anything outside the seven engine free names has to import it —
    the Generator's system prompt says so explicitly. `datetime` is on `validate_source`'s import
    allowlist, so the restricted namespace must be able to execute the import too; a namespace with
    no `__import__` at all would make every such rule unhoistable while hardening nothing."""
    row = _signed_row(IMPORTS_DATETIME_SOURCE, rule_type="max-two-hours")
    rule = RuleCatalog().hoist(row).build({})

    long_booking = BookingRequest(start_at=WEDNESDAY, end_at=WEDNESDAY + timedelta(hours=3))
    result = evaluate_request(_engine_request(long_booking), _context(long_booking), (rule,))
    assert not result.passed
    assert result.fail_reason == "At most 2 hours."


def test_a_rule_importing_outside_the_allowlist_is_rejected_at_validate_source():
    row = _signed_row(IMPORTS_OS_SOURCE, rule_type="reads-env")
    with pytest.raises(HoistError, match="validate_source"):
        RuleCatalog().hoist(row)


@pytest.mark.parametrize("name", ["os", "sys", "subprocess", "importlib"])
def test_the_guarded_import_refuses_a_module_outside_the_allowlist(name):
    """The runtime half of the import allowlist, tested directly rather than through a row.

    `validate_source` already rejects the source above, so no ordinary row reaches this guard. It
    covers the one gap the source checks cannot: `source_sha256` binds `human_code` to itself, but
    nothing proves `executable_bytecode` was compiled from that source, and the blob is what
    actually runs (`_guarded_import`'s own docstring).
    """
    namespace = _restricted_namespace()
    with pytest.raises(ImportError, match="may import only"):
        namespace["__builtins__"]["__import__"](name)


def test_the_guarded_import_refuses_a_relative_import():
    namespace = _restricted_namespace()
    with pytest.raises(ImportError, match="may import only"):
        namespace["__builtins__"]["__import__"]("datetime", None, None, (), 1)


def test_the_restricted_namespace_withholds_the_real_builtins():
    """Whatever else it binds, the namespace must not be a route to the real `builtins` module —
    `__build_class__` and `__import__` are execution machinery, not an open door."""
    restricted = _restricted_namespace()["__builtins__"]

    assert set(restricted) == set(SAFE_BUILTINS) | {"__build_class__", "__import__"}
    assert restricted["__import__"] is not builtins.__import__
    for blocked in ("eval", "exec", "compile", "open", "getattr", "globals", "__loader__"):
        assert blocked not in restricted


# --- a happy-path hoist --------------------------------------------------------------------------


def test_a_valid_row_hoists_to_a_working_rule_type():
    row = _signed_row(WEEKEND_SOURCE, rule_type="no-weekend-bookings")
    rule_type = RuleCatalog().hoist(row)

    assert rule_type.rule_type == "no-weekend-bookings"
    assert rule_type.priority == 100
    assert rule_type.needs_local_resolution is False
    assert rule_type.is_single is False
    assert rule_type.reads_history is False
    assert rule_type.params == ()

    rule = rule_type.build({})

    weekday_booking = _booking(WEDNESDAY)
    weekday_result = evaluate_request(
        _engine_request(weekday_booking), _context(weekday_booking), (rule,)
    )
    assert weekday_result.passed

    weekend_booking = _booking(SATURDAY)
    weekend_result = evaluate_request(
        _engine_request(weekend_booking), _context(weekend_booking), (rule,)
    )
    assert not weekend_result.passed
    assert weekend_result.fail_reason == "No bookings on weekends."


# --- lookup(): throttled self-healing reload ------------------------------------------------------


def test_lookup_reloads_at_most_once_per_two_consecutive_misses(monkeypatch):
    catalog = RuleCatalog()
    reload_calls = []
    monkeypatch.setattr(catalog, "reload", lambda session: reload_calls.append(session))

    assert catalog.lookup("does-not-exist") is None
    assert catalog.lookup("does-not-exist") is None

    assert len(reload_calls) == 1


def test_lookup_swallows_a_failure_opening_the_session(monkeypatch, caplog):
    """A database that cannot be reached must not raise past `lookup` — the miss stands and the
    caller (`_build_canon`) fails closed on it, exactly as it would for any other unregistered
    `rule_type`."""
    catalog = RuleCatalog()

    def _broken_factory():
        raise RuntimeError("DATABASE_URL is not set")

    monkeypatch.setattr("app.db.session.get_session_factory", _broken_factory)

    with caplog.at_level(logging.ERROR):
        result = catalog.lookup("does-not-exist")

    assert result is None
    assert "does-not-exist" in caplog.text


# --- all(): ordering ----------------------------------------------------------------------------


def test_all_sorts_hoisted_types_after_every_hand_written_one():
    catalog = RuleCatalog()
    row = _signed_row(WEEKEND_SOURCE, rule_type="zzz-should-still-sort-last-among-equals")
    catalog._hoisted = {row.rule_type: catalog.hoist(row)}

    priorities = [rule_type.priority for rule_type in catalog.all()]
    assert priorities == sorted(priorities)
    # Every hand-written type is registered below priority 100; the hoisted one shares 100 with
    # nothing else in this catalog, so it must be exactly last.
    assert catalog.all()[-1].rule_type == row.rule_type


def test_all_breaks_ties_between_generated_types_by_rule_type_id():
    catalog = RuleCatalog()
    row_a = _signed_row(WEEKEND_SOURCE, rule_type="b-second")
    row_b = _signed_row(WEEKEND_SOURCE, rule_type="a-first")
    catalog._hoisted = {
        row_a.rule_type: catalog.hoist(row_a),
        row_b.rule_type: catalog.hoist(row_b),
    }

    generated = [rule_type.rule_type for rule_type in catalog.all() if rule_type.priority == 100]
    assert generated == ["a-first", "b-second"]


# --- end to end, against real Postgres ----------------------------------------------------------


@requires_postgres
class TestCatalogAgainstPostgres:
    """`catalog.reload` against a real `generated_rule_types` row, and `rules_stub.evaluate`
    reading through `SpaceRuleConfig.lookup` to reach it — the two halves 7.6 wires together.

    These build the schema through their own `pg_session` fixture rather than reusing `conftest`'s
    `driver`: that fixture seeds its default Resource and user at **explicit** primary keys, which
    leaves the identity sequences sitting at zero, so the very next auto-assigned insert collides
    on id 1. Nothing here needs those seeded rows — only the tables and a Space and User for the
    provenance columns to point at — so this follows `test_rules_api.py`'s plain drop/create
    pattern instead.
    """

    @pytest.fixture
    def pg_session(self, pg_engine):
        from sqlalchemy.orm import Session as OrmSession

        from app.db.models import Base

        Base.metadata.drop_all(pg_engine)
        Base.metadata.create_all(pg_engine)
        try:
            with OrmSession(pg_engine, expire_on_commit=False) as session:
                yield session
        finally:
            Base.metadata.drop_all(pg_engine)

    @staticmethod
    def _provenance(session, sub: str, space_name: str):
        """A committed `User` and `Space` for `created_by_*` to reference — both NOT NULL, and
        `created_by_space_id` deliberately so (`OVERVIEW.md`), so a row cannot be inserted without
        them."""
        from app.identity.models import Space, User

        owner = User(auth0_sub=sub, email=f"{sub.split('|')[-1]}@example.com")
        session.add(owner)
        session.flush()
        space = Space(name=space_name, created_by_user_id=owner.id, timezone="UTC")
        session.add(space)
        session.flush()
        return owner, space

    def _insert(self, session, source: str, rule_type: str, sub: str, space_name: str) -> None:
        owner, space = self._provenance(session, sub, space_name)
        row = _signed_row(
            source,
            rule_type=rule_type,
            created_by_space_id=space.id,
            created_by_user_id=owner.id,
        )
        del row.id  # let the table assign a real primary key
        session.add(row)
        session.commit()

    def test_a_hoisted_generated_rule_denies_and_allows_through_rules_stub(self, pg_session):
        from app.rules_stub import SpaceRuleConfig, SpaceRuleRow
        from app.rules_stub import evaluate as rules_stub_evaluate

        self._insert(
            pg_session, WEEKEND_SOURCE, "pg-no-weekend-bookings", "auth0|cat1", "Catalog test space"
        )

        catalog = RuleCatalog()
        catalog.reload(pg_session)

        config = SpaceRuleConfig(
            timezone="UTC",
            rules=(SpaceRuleRow(id=1, rule_type="pg-no-weekend-bookings", params={}),),
            lookup=catalog.lookup,
        )

        weekend_result = rules_stub_evaluate(
            BookingRequest(start_at=SATURDAY, end_at=SATURDAY + timedelta(hours=1)),
            config,
            now=WEDNESDAY - timedelta(days=1),
        )
        assert not weekend_result.allowed
        assert weekend_result.message == "No bookings on weekends."

        weekday_result = rules_stub_evaluate(
            BookingRequest(start_at=WEDNESDAY, end_at=WEDNESDAY + timedelta(hours=1)),
            config,
            now=WEDNESDAY - timedelta(days=1),
        )
        assert weekday_result.allowed

    def test_a_space_rules_row_naming_an_unhoistable_type_denies_with_the_generic_message(
        self, pg_session
    ):
        from app.rules_stub import SpaceRuleConfig, SpaceRuleRow
        from app.rules_stub import evaluate as rules_stub_evaluate

        self._insert(pg_session, UNSAFE_SOURCE, "pg-unhoistable", "auth0|cat2", "Broken rule space")

        catalog = RuleCatalog()
        catalog.reload(pg_session)

        config = SpaceRuleConfig(
            timezone="UTC",
            rules=(SpaceRuleRow(id=1, rule_type="pg-unhoistable", params={}),),
            lookup=catalog.lookup,
        )

        result = rules_stub_evaluate(
            BookingRequest(start_at=WEDNESDAY, end_at=WEDNESDAY + timedelta(hours=1)),
            config,
            now=WEDNESDAY - timedelta(days=1),
        )
        assert not result.allowed
        assert result.message == RULE_ERROR_MESSAGE

    def test_a_retired_row_is_not_hoisted(self, pg_session):
        """`status` is the whole removal mechanism — a retired type must vanish from the catalog
        without its row being deleted, since `space_rules` rows still name it by string id."""
        self._insert(pg_session, WEEKEND_SOURCE, "pg-retired", "auth0|cat3", "Retiring space")
        row = pg_session.query(GeneratedRuleType).filter_by(rule_type="pg-retired").one()

        catalog = RuleCatalog()
        catalog.reload(pg_session)
        assert catalog.get("pg-retired") is not None

        row.status = GeneratedRuleTypeStatus.RETIRED
        pg_session.commit()
        catalog.reload(pg_session)
        assert catalog.get("pg-retired") is None
