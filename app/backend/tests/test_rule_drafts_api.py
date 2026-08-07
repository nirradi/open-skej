"""Task 7.7: generation as a job — the runner, the recording, and the API over both.

Postgres-only and structured like ``test_rules_api.py``: ``get_current_user`` is overridden
rather than exercised with a real token, and the module skips when ``DATABASE_URL`` is unset.

Two things here are not shaped like the other API suites, both for reasons worth stating.

**The app is rebuilt with generation enabled.** ``/spaces/{id}/rule-drafts`` is registered by a
conditional ``include_router``, so the process-wide ``app.main.app`` every other suite imports
does not have these routes at all — which is the deliberate 404 posture, asserted below in
``test_the_routes_do_not_exist_when_generation_is_disabled``. The ``importlib.reload`` dance is
``test_sandbox_auth.py``'s precedent for exactly this, restore discipline included.

**The role sweep lives here rather than in ``test_spaces_api.py``'s ``ROLE_TABLE``.** That table
is checked for *equality* against the app's OpenAPI schema, and these routes are absent from the
default app, so adding them there would break the guard that makes the table worth having. The
sweep at the bottom of this module is the equivalent, run against the app that does have them.

Every model call goes through ``StubLLMClient`` — canned, deterministic, no network and no
subprocess. That is the whole reason it exists: it sits at the ``LLMClient`` seam, so every layer
above it in these tests is the real one.
"""

import contextlib
import importlib
import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from generation.stub import StubLLMClient

import app.main as main_module
from app.auth.dependencies import get_current_user
from app.db.models import Base
from app.db.session import get_session
from app.identity import service
from app.identity.models import (
    GeneratedRuleType,
    GeneratedRuleTypeStatus,
    MembershipRole,
    PromptAgent,
    PromptVersion,
    RuleGenerationExchange,
    RuleGenerationJob,
    RuleGenerationJobStatus,
    Space,
    SpaceMembership,
    User,
)
from app.rule_catalog import catalog
from app.rule_generation import run_generation_job, sweep_orphaned_generation_jobs
from app.settings import get_settings

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; the rule-drafts tests need `docker compose up -d`",
)

PROMPT = "no booking may be longer than one hour"


@contextlib.contextmanager
def _app_with_generation(enabled: str):
    """``app.main.app`` rebuilt with ``RULE_GENERATION_ENABLED`` set to ``enabled``.

    ``test_sandbox_auth.py``'s precedent for a conditionally-registered router, restore discipline
    included: the environment variable and the settings cache are both put back in the
    ``finally``, and ``app.main`` is reloaded a second time, so a failed assertion inside the
    block cannot leave the rule-drafts routes registered on the module attribute that whatever
    test module imports next will read.
    """
    saved = os.environ.get("RULE_GENERATION_ENABLED")
    os.environ["RULE_GENERATION_ENABLED"] = enabled
    get_settings.cache_clear()
    try:
        yield importlib.reload(main_module).app
    finally:
        if saved is None:
            os.environ.pop("RULE_GENERATION_ENABLED", None)
        else:
            os.environ["RULE_GENERATION_ENABLED"] = saved
        get_settings.cache_clear()
        importlib.reload(main_module)


@pytest.fixture(scope="module")
def generation_app():
    """The app this suite drives: built once, with generation enabled.

    Module-scoped because the reload is the expensive part and nothing here mutates the app
    object beyond dependency overrides, which the ``api`` fixture clears per test.
    """
    with _app_with_generation("true") as built:
        yield built


@pytest.fixture
def engine():
    engine = create_engine(DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def session_factory(engine) -> sessionmaker[Session]:
    """A factory the *runner* uses, deliberately not the request's session.

    ``run_generation_job`` opens its own session because in production it runs after the response
    has been sent, by which point the request-scoped one is closed. Handing it a factory over the
    test engine keeps that shape honest rather than passing it a session it would never have.
    """
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def restore_catalog(session: Session) -> Iterator[None]:
    """Put the process-wide catalog back the way this test found it.

    Not housekeeping. ``run_generation_job`` hoists its new type into ``app.rule_catalog.catalog``
    on purpose — that live hoist is an acceptance criterion — but the catalog is a module-level
    singleton shared by every test in the process, while each test here drops and recreates its
    tables. Without this, a generated type from one test survives in memory after the row behind
    it is gone, and ``test_rules_api.py``'s "``/rule-types`` lists exactly the seven hand-written
    types" starts counting nine.

    Requesting ``session`` is what orders this correctly: the teardown below runs *before* the
    engine fixture drops the tables, so the reload has something to read.

    Retired rather than deleted, and not only to honour the schema's retire-not-delete
    convention: a ``rule_generation_jobs`` row points at the type it produced and carries no
    ``ON DELETE CASCADE``, so deleting the type is a foreign key violation. ``reload`` hoists
    active rows only, which is all this fixture needs.
    """
    yield
    for row in session.execute(select(GeneratedRuleType)).scalars().all():
        row.status = GeneratedRuleTypeStatus.RETIRED
    session.commit()
    catalog.reload(session)


class Api:
    """A ``TestClient`` with a swappable caller — see ``test_spaces_api.py``."""

    def __init__(self, client: TestClient, caller: dict[str, User]) -> None:
        self._client = client
        self._caller = caller

    def as_user(self, user: User) -> TestClient:
        self._caller["user"] = user
        return self._client


@pytest.fixture
def api(generation_app, session: Session) -> Iterator[Api]:
    caller: dict[str, User] = {}

    generation_app.dependency_overrides[get_session] = lambda: session
    generation_app.dependency_overrides[get_current_user] = lambda: caller["user"]
    try:
        yield Api(TestClient(generation_app), caller)
    finally:
        generation_app.dependency_overrides.clear()


def _make_user(session: Session, sub: str, email: str) -> User:
    user = User(auth0_sub=sub, email=email, name=email.split("@")[0].title())
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def alice(session: Session) -> User:
    return _make_user(session, "auth0|alice", "alice@example.com")


@pytest.fixture
def bob(session: Session) -> User:
    return _make_user(session, "auth0|bob", "bob@example.com")


@pytest.fixture
def space_a(session: Session, alice: User) -> Space:
    """Alice's Space. She is its owner."""
    return service.create_space(session, alice, name="Court A", description="Alice's court")


@pytest.fixture
def space_b(session: Session, bob: User) -> Space:
    """Bob's Space. Alice has no relationship with it whatsoever."""
    return service.create_space(session, bob, name="Court B", description="Bob's court")


def _add_member(session: Session, space: Space, user: User, role: MembershipRole) -> None:
    session.add(SpaceMembership(space_id=space.id, user_id=user.id, role=role))
    session.commit()


def _queue_job(session: Session, space: Space, user: User, prompt: str = PROMPT) -> int:
    """A ``queued`` job row, bypassing the API. Returns its id."""
    job = RuleGenerationJob(space_id=space.id, user_id=user.id, prompt=prompt, attempts=[])
    session.add(job)
    session.commit()
    return job.id


def _exchanges(session: Session, job_id: int) -> list[RuleGenerationExchange]:
    session.expire_all()
    return list(
        session.execute(
            select(RuleGenerationExchange)
            .where(RuleGenerationExchange.job_id == job_id)
            .order_by(RuleGenerationExchange.id)
        )
        .scalars()
        .all()
    )


def _reload_job(session: Session, job_id: int) -> RuleGenerationJob:
    session.expire_all()
    return session.get(RuleGenerationJob, job_id)


# --- The happy path ---------------------------------------------------------------------------


def test_a_job_runs_to_succeeded_and_the_type_is_live_in_the_catalog(
    session: Session, session_factory, space_a: Space, alice: User
) -> None:
    """The whole acceptance criterion in one test, including the live hoist.

    ``catalog.get`` rather than ``lookup``: ``lookup`` self-heals by reloading on a miss, so it
    would pass even if the runner never hoisted anything. ``get`` reads the in-process map and
    nothing else, which is what "available without a restart" actually means.
    """
    job_id = _queue_job(session, space_a, alice)

    run_generation_job(job_id, session_factory=session_factory, client=StubLLMClient())

    job = _reload_job(session, job_id)
    assert job.status is RuleGenerationJobStatus.SUCCEEDED
    assert job.error is None
    assert job.generated_rule_type_id is not None
    assert [attempt["outcome"] for attempt in job.attempts] == ["passed"]

    row = session.get(GeneratedRuleType, job.generated_rule_type_id)
    assert row.created_by_space_id == space_a.id
    assert row.created_by_user_id == alice.id
    assert row.prompt == PROMPT
    assert row.priority == 100
    assert row.human_code
    assert row.executable_bytecode

    assert catalog.get(row.rule_type) is not None


def test_a_failing_generation_leaves_the_history_and_no_rule_type(
    session: Session, session_factory, space_a: Space, alice: User
) -> None:
    """A generation that never passes writes nothing to the catalog.

    ``always_unsafe`` makes every Generator turn answer with source the validator rejects, so the
    loop exhausts its retry budget. The assertion that matters is the *absence* of a
    ``generated_rule_types`` row: a timeout is not a success and a crash is not a success, and
    neither is three rejected candidates.
    """
    job_id = _queue_job(session, space_a, alice)

    run_generation_job(
        job_id, session_factory=session_factory, client=StubLLMClient(always_unsafe=True)
    )

    job = _reload_job(session, job_id)
    assert job.status is RuleGenerationJobStatus.FAILED
    assert job.generated_rule_type_id is None
    assert job.error
    assert len(job.attempts) > 1
    assert all(attempt["outcome"] != "passed" for attempt in job.attempts)

    assert session.execute(select(GeneratedRuleType)).scalars().all() == []


# --- The recording ----------------------------------------------------------------------------


def test_every_model_call_is_recorded_in_order_with_its_agent_and_attempt(
    session: Session, session_factory, space_a: Space, alice: User
) -> None:
    """One exchange row per model call — a successful first attempt is Generator then Tester."""
    job_id = _queue_job(session, space_a, alice)

    run_generation_job(job_id, session_factory=session_factory, client=StubLLMClient())

    rows = _exchanges(session, job_id)
    assert [(row.attempt, row.agent) for row in rows] == [
        (1, PromptAgent.GENERATOR),
        (1, PromptAgent.TESTER),
    ]
    assert all(row.user_prompt and row.response_text for row in rows)
    # The stub reports no token counts and no duration, and those columns say so rather than
    # claiming a measurement of zero.
    assert rows[0].input_tokens is None
    assert rows[0].duration_ms is None


def test_a_retried_job_quotes_the_previous_source_and_failure_back_to_the_model(
    session: Session, session_factory, space_a: Space, alice: User
) -> None:
    """The row this whole table exists for.

    ``fail_first_attempts=1`` makes attempt 1 produce source the validator rejects and attempt 2
    produce source that passes. Attempt 2's Generator turn is where ``build_prompt`` hands the
    model back its own failing source *and* the validator's complaint — assert the contents, not
    merely that a second row exists, because a recording that stored an empty retry turn would
    satisfy every weaker assertion while losing the only thing worth keeping.
    """
    job_id = _queue_job(session, space_a, alice)

    run_generation_job(
        job_id, session_factory=session_factory, client=StubLLMClient(fail_first_attempts=1)
    )

    job = _reload_job(session, job_id)
    assert job.status is RuleGenerationJobStatus.SUCCEEDED

    rows = _exchanges(session, job_id)
    generator_turns = [row for row in rows if row.agent is PromptAgent.GENERATOR]
    assert len(generator_turns) == 2
    assert [row.attempt for row in generator_turns] == [1, 2]

    first_source = generator_turns[0].response_text
    retry_prompt = generator_turns[1].user_prompt
    # The previous source, quoted back. Compared on a distinctive line rather than the whole
    # blob, because the loop strips the code fence before quoting it.
    assert "class " in first_source
    signature = next(line for line in first_source.splitlines() if line.startswith("class "))
    assert signature in retry_prompt
    # And the failure that rejected it. The stub's failing candidate imports a module the
    # validator forbids, so the complaint names the import.
    assert "import" in retry_prompt.lower()
    assert len(retry_prompt) > len(rows[0].user_prompt)


def test_two_generations_sharing_a_system_prompt_produce_one_prompt_version_row(
    session: Session, session_factory, space_a: Space, space_b: Space, alice: User, bob: User
) -> None:
    """Store the system prompt once, keyed on its hash — not once per call, not once per job."""
    first = _queue_job(session, space_a, alice)
    run_generation_job(first, session_factory=session_factory, client=StubLLMClient())
    second = _queue_job(session, space_b, bob, prompt="only weekends")
    run_generation_job(second, session_factory=session_factory, client=StubLLMClient())

    session.expire_all()
    versions = session.execute(select(PromptVersion)).scalars().all()
    assert sorted(version.agent for version in versions) == sorted(
        [PromptAgent.GENERATOR, PromptAgent.TESTER]
    )
    assert len({version.sha256 for version in versions}) == 2

    # Four model calls across the two jobs, all pointing at those same two rows.
    exchanges = session.execute(select(RuleGenerationExchange)).scalars().all()
    assert len(exchanges) == 4
    assert {exchange.prompt_version_id for exchange in exchanges} == {
        version.id for version in versions
    }


def test_a_second_generation_reuses_the_slug_with_a_suffix_rather_than_the_id(
    session: Session, session_factory, space_a: Space, space_b: Space, alice: User, bob: User
) -> None:
    """``rule_type`` is unique and never reused, so an identical prompt takes a suffix.

    Reuse would silently repoint every ``space_rules`` row naming the first type at the second.
    """
    first = _queue_job(session, space_a, alice)
    run_generation_job(first, session_factory=session_factory, client=StubLLMClient())
    second = _queue_job(session, space_b, bob)
    run_generation_job(second, session_factory=session_factory, client=StubLLMClient())

    session.expire_all()
    slugs = session.execute(select(GeneratedRuleType.rule_type)).scalars().all()
    assert len(slugs) == 2
    assert len(set(slugs)) == 2
    assert all(len(slug) <= 64 for slug in slugs)


# --- The startup sweep ------------------------------------------------------------------------


def test_an_orphaned_running_job_is_swept_to_failed(
    session: Session, space_a: Space, alice: User
) -> None:
    """A process that died mid-run leaves a row nobody is executing.

    Without the sweep the row polls forever *and* the in-flight unique index makes it a permanent
    block on this Space generating again — which is why the assertion below is not only that the
    job failed but that a fresh job can be queued afterwards.
    """
    job_id = _queue_job(session, space_a, alice)
    job = session.get(RuleGenerationJob, job_id)
    job.status = RuleGenerationJobStatus.RUNNING
    session.commit()

    assert sweep_orphaned_generation_jobs(session) == 1

    swept = _reload_job(session, job_id)
    assert swept.status is RuleGenerationJobStatus.FAILED
    assert "restart" in swept.error

    assert _queue_job(session, space_a, alice) != job_id


# --- The API ----------------------------------------------------------------------------------


def test_submitting_a_prompt_returns_202_and_runs_the_job(
    api: Api, session: Session, space_a: Space, alice: User
) -> None:
    """202, not 201: nothing is created yet, a job has been accepted.

    ``BackgroundTasks`` runs before ``post`` returns under ``TestClient``, so by the time this
    asserts, the whole flow — loop, sandbox, catalog write — has actually run.
    """
    response = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/rule-drafts", json={"prompt": PROMPT}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["prompt"] == PROMPT
    assert body["status"] == "queued"
    assert body["generated_rule_type"] is None

    follow_up = api.as_user(alice).get(f"/spaces/{space_a.public_id}/rule-drafts/{body['id']}")
    assert follow_up.status_code == 200
    assert follow_up.json()["status"] == "succeeded"
    assert follow_up.json()["generated_rule_type"]


def test_a_second_in_flight_job_is_refused_with_409(
    api: Api, session: Session, space_a: Space, alice: User
) -> None:
    """One live generation per Space. The check and the partial unique index say the same thing."""
    _queue_job(session, space_a, alice)

    response = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/rule-drafts", json={"prompt": "only weekends"}
    )

    assert response.status_code == 409
    assert "already being generated" in response.json()["detail"]


def test_a_finished_job_does_not_block_the_next_one(
    api: Api, session: Session, session_factory, space_a: Space, alice: User
) -> None:
    """The index covers live rows only — a Space that generated yesterday generates again today."""
    job_id = _queue_job(session, space_a, alice)
    run_generation_job(job_id, session_factory=session_factory, client=StubLLMClient())

    response = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/rule-drafts", json={"prompt": "only weekends"}
    )

    assert response.status_code == 202


def test_listing_returns_this_spaces_jobs_newest_first(
    api: Api,
    session: Session,
    session_factory,
    space_a: Space,
    space_b: Space,
    alice: User,
    bob: User,
) -> None:
    """So a page reload does not lose a job an admin is waiting on."""
    first = _queue_job(session, space_a, alice)
    run_generation_job(first, session_factory=session_factory, client=StubLLMClient())
    second = _queue_job(session, space_a, alice, prompt="only weekends")
    run_generation_job(second, session_factory=session_factory, client=StubLLMClient())
    _queue_job(session, space_b, bob, prompt="somebody else's")

    body = api.as_user(alice).get(f"/spaces/{space_a.public_id}/rule-drafts").json()

    assert [entry["id"] for entry in body] == [second, first]
    assert all(entry["generated_rule_type"] for entry in body)


def test_a_job_id_from_another_space_is_not_found(
    api: Api, session: Session, space_a: Space, space_b: Space, alice: User, bob: User
) -> None:
    """The identical 404 as a job id that names nothing at all.

    Alice is an admin of her own Space, so ``require_space_role`` passes; what refuses her is the
    query being scoped to ``space_id``. Two branches — "no such job" and "not your job" — would
    be two chances to disagree, and the difference between them is itself information about
    another tenant.
    """
    bobs_job = _queue_job(session, space_b, bob)

    found = api.as_user(alice).get(f"/spaces/{space_a.public_id}/rule-drafts/{bobs_job}")
    missing = api.as_user(alice).get(f"/spaces/{space_a.public_id}/rule-drafts/999999")

    assert found.status_code == 404
    assert missing.status_code == 404
    assert found.json() == missing.json()


def test_an_over_long_prompt_is_refused(api: Api, space_a: Space, alice: User) -> None:
    """A cap at the wire boundary, not a filter — see ``schemas._RULE_PROMPT_MAX``."""
    response = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/rule-drafts", json={"prompt": "x" * 5000}
    )

    assert response.status_code == 422


def test_the_routes_do_not_exist_when_generation_is_disabled(
    session: Session, space_a: Space, alice: User
) -> None:
    """The 404 posture, asserted rather than described.

    An app is built here with the flag explicitly *off* rather than reusing ``main_module.app``:
    the module-scoped ``generation_app`` fixture reloads that module in place, so by the time this
    runs the module attribute names the enabled app. Building the disabled one deliberately is
    also the more honest test — it asserts what an unconfigured deployment serves, which is the
    whole claim, instead of asserting something about fixture ordering.

    404, not 403. A 403 would first require the route to exist in order to refuse it, which would
    make the presence of the capability discoverable by probing for it.
    """
    with _app_with_generation("false") as disabled_app:
        disabled_app.dependency_overrides[get_session] = lambda: session
        disabled_app.dependency_overrides[get_current_user] = lambda: alice
        try:
            client = TestClient(disabled_app)
            assert (
                client.post(
                    f"/spaces/{space_a.public_id}/rule-drafts", json={"prompt": PROMPT}
                ).status_code
                == 404
            )
            assert client.get(f"/spaces/{space_a.public_id}/rule-drafts").status_code == 404
        finally:
            disabled_app.dependency_overrides.clear()


# --- The role sweep ---------------------------------------------------------------------------
#
# `test_spaces_api.py`'s ROLE_TABLE is compared for equality against the app's OpenAPI schema, so
# routes that are conditionally registered cannot be listed there without breaking that guard.
# This is the equivalent, over the app that has them: every rule-drafts route is admin+, which is
# the floor `require_space_role` enforces and not the whole of what each handler checks.

RULE_DRAFT_ROUTES = [
    ("POST", "/spaces/{public_id}/rule-drafts"),
    ("GET", "/spaces/{public_id}/rule-drafts"),
    ("GET", "/spaces/{public_id}/rule-drafts/{draft_id}"),
]


def test_every_rule_draft_route_is_admin_only(
    api: Api, session: Session, space_a: Space, alice: User, bob: User
) -> None:
    """A member of the Space is refused on every route; an admin is not.

    The member case is the one that matters. An admin passing proves the route works; a member
    being refused proves the gate is on it at all, and it is the assertion that would catch a
    fourth route added later without one.
    """
    _add_member(session, space_a, bob, MembershipRole.MEMBER)
    job_id = _queue_job(session, space_a, alice)

    for method, template in RULE_DRAFT_ROUTES:
        path = template.format(public_id=space_a.public_id, draft_id=job_id)
        response = api.as_user(bob).request(method, path, json={"prompt": PROMPT})
        assert response.status_code == 403, f"{method} {template} let a member through"

    listing = api.as_user(alice).get(f"/spaces/{space_a.public_id}/rule-drafts")
    assert listing.status_code == 200
