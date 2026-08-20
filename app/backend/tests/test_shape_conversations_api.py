"""Postgres API coverage for the synchronous calendar-shape conversation flow."""

import json
import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from generation.errors import LLMCallError
from generation.llm import LLMResponse
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.models import Base
from app.db.session import get_session
from app.identity import service
from app.identity.models import (
    MembershipRole,
    ShapeConversationStatus,
    ShapeExchangeStatus,
    ShapeMessageRole,
    Space,
    SpaceCalendarShape,
    SpaceMembership,
    SpaceShapeConversation,
    SpaceShapeExchange,
    SpaceShapeMessage,
    User,
)
from app.main import app

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; the shape-conversation API tests need docker compose up -d",
)


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


class Api:
    def __init__(self, client: TestClient, caller: dict[str, User]) -> None:
        self._client = client
        self._caller = caller

    def as_user(self, user: User) -> TestClient:
        self._caller["user"] = user
        return self._client


@pytest.fixture
def api(session: Session) -> Iterator[Api]:
    caller: dict[str, User] = {}
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: caller["user"]
    try:
        yield Api(TestClient(app), caller)
    finally:
        app.dependency_overrides.clear()


def _user(session: Session, sub: str) -> User:
    user = User(auth0_sub=sub, email=f"{sub}@example.com")
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def alice(session: Session) -> User:
    return _user(session, "alice")


@pytest.fixture
def bob(session: Session) -> User:
    return _user(session, "bob")


@pytest.fixture
def carol(session: Session) -> User:
    return _user(session, "carol")


@pytest.fixture
def space_a(session: Session, alice: User, carol: User) -> Space:
    space = service.create_space(session, alice, name="Court A", description=None)
    session.add(SpaceMembership(space_id=space.id, user_id=carol.id, role=MembershipRole.MEMBER))
    session.commit()
    return space


@pytest.fixture
def space_b(session: Session, bob: User) -> Space:
    return service.create_space(session, bob, name="Court B", description=None)


def _open(api: Api, user: User, space: Space) -> int:
    response = api.as_user(user).post(f"/spaces/{space.public_id}/shape-conversations", json={})
    assert response.status_code == 201
    return response.json()["id"]


def _turn(api: Api, user: User, space: Space, conversation_id: int, message: str):
    return api.as_user(user).post(
        f"/spaces/{space.public_id}/shape-conversations/{conversation_id}/turns",
        json={"message": message},
    )


def test_turn_writes_a_draft_that_the_draft_calendar_serves(
    api: Api, alice: User, space_a: Space
) -> None:
    conversation_id = _open(api, alice, space_a)
    response = _turn(api, alice, space_a, conversation_id, "open at 10")

    assert response.status_code == 200
    assert response.json()["draft"]["status"] == "draft"
    assert response.json()["draft"]["document"]["operating_blocks"][0]["start_time"] == "10:00"

    calendar = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "2026-08-20", "to": "2026-08-20", "draft": "true"},
    )
    assert calendar.status_code == 200
    assert calendar.json()[0]["offered_starts"][0]["start_minutes"] == 600


def test_second_turn_carries_the_first_turn_and_refines_the_draft(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    conversation_id = _open(api, alice, space_a)
    first = _turn(api, alice, space_a, conversation_id, "open at 10")
    assert first.status_code == 200
    response = _turn(api, alice, space_a, conversation_id, "open at 11")

    assert response.status_code == 200
    assert response.json()["draft"]["document"]["operating_blocks"][0]["start_time"] == "11:00"
    # An ordinary turn updates the same draft row. `created_at` is the visible
    # revision marker the studio keys its refetch on, so it must change even
    # though the row id remains stable.
    assert response.json()["draft"]["id"] == first.json()["draft"]["id"]
    assert response.json()["draft"]["created_at"] != first.json()["draft"]["created_at"]
    exchanges = list(
        session.execute(
            select(SpaceShapeExchange)
            .where(SpaceShapeExchange.conversation_id == conversation_id)
            .order_by(SpaceShapeExchange.id)
        ).scalars()
    )
    assert len(exchanges) == 2
    assert "open at 10" in exchanges[-1].user_prompt
    assert "Current complete shape document" in exchanges[-1].user_prompt


def test_live_shape_stays_until_publish_then_version_and_conversation_move(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    conversation_id = _open(api, alice, space_a)
    assert _turn(api, alice, space_a, conversation_id, "open at 10").status_code == 200

    live_before = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar", params={"from": "2026-08-20", "to": "2026-08-20"}
    )
    assert live_before.json()[0]["offered_starts"][0]["start_minutes"] == 0

    published = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/calendar-shape/publish", json={}
    )
    assert published.status_code == 200
    assert published.json()["status"] == "live"
    statuses = list(
        session.execute(
            select(SpaceCalendarShape.status).where(SpaceCalendarShape.space_id == space_a.id)
        ).scalars()
    )
    assert sorted(status.value for status in statuses) == ["live", "superseded"]
    assert (
        session.get(SpaceShapeConversation, conversation_id).status
        is ShapeConversationStatus.CLOSED
    )


def test_publish_refuses_no_draft_archived_and_unbookable_without_override(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    no_draft = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/calendar-shape/publish", json={}
    )
    assert no_draft.status_code == 409

    closed = {"version": 1, "operating_blocks": [], "blackout_windows": []}
    service.upsert_draft(session, space_a, closed, alice)
    refused = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/calendar-shape/publish", json={}
    )
    assert refused.status_code == 409
    accepted = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/calendar-shape/publish", json={"allow_unbookable": True}
    )
    assert accepted.status_code == 200

    service.upsert_draft(session, space_a, closed, alice)
    service.archive_space(session, space_a)
    archived = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/calendar-shape/publish", json={"allow_unbookable": True}
    )
    assert archived.status_code == 409


def test_archived_publish_with_no_draft_uses_the_archived_response(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    service.archive_space(session, space_a)

    response = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/calendar-shape/publish", json={}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "This Space is archived and can no longer be changed."


def test_foreign_conversation_is_404_and_second_open_conversation_is_refused(
    api: Api, alice: User, bob: User, space_a: Space, space_b: Space
) -> None:
    foreign_id = _open(api, bob, space_b)
    foreign = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/shape-conversations/{foreign_id}"
    )
    assert foreign.status_code == 404

    _open(api, alice, space_a)
    second = api.as_user(alice).post(f"/spaces/{space_a.public_id}/shape-conversations", json={})
    assert second.status_code == 409


def test_current_open_conversation_is_admin_scoped_and_nullable(
    api: Api, alice: User, bob: User, carol: User, space_a: Space
) -> None:
    current_url = f"/spaces/{space_a.public_id}/shape-conversations/current"

    empty = api.as_user(alice).get(current_url)
    assert empty.status_code == 200
    assert empty.json() is None

    conversation_id = _open(api, alice, space_a)
    current = api.as_user(alice).get(current_url)
    assert current.status_code == 200
    assert current.json()["id"] == conversation_id
    assert current.json()["draft"] is None
    # The response carries the stored live document for an exact Publish
    # comparison; it is not inferred from whichever week happens to be shown.
    assert current.json()["live"]["status"] == "live"
    assert current.json()["live"]["document"]["version"] == 1

    member = api.as_user(carol).get(current_url)
    assert member.status_code == 403

    outsider = api.as_user(bob).get(current_url)
    assert outsider.status_code == 404


def test_current_open_conversation_recovers_an_unbookable_assistant_question(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    """The live fallback must come from durable question state, not local browser copy."""
    conversation_id = _open(api, alice, space_a)
    draft = service.upsert_draft(
        session,
        space_a,
        {"version": 1, "operating_blocks": [], "blackout_windows": []},
        alice,
        conversation_id=conversation_id,
    )
    service.append_shape_message(
        session,
        session.get(SpaceShapeConversation, conversation_id),
        role=ShapeMessageRole.ASSISTANT,
        content="I closed every offered block.",
        question="Should the venue be closed all week?",
        resulting_shape_version_id=draft.id,
    )

    response = api.as_user(alice).get(f"/spaces/{space_a.public_id}/shape-conversations/current")

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"]["document"]["operating_blocks"] == []
    assert payload["messages"][-1]["question"] == "Should the venue be closed all week?"


def test_discard_deletes_the_draft_but_keeps_and_closes_its_transcript(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    conversation_id = _open(api, alice, space_a)
    assert _turn(api, alice, space_a, conversation_id, "open at 10").status_code == 200

    discarded = api.as_user(alice).post(f"/spaces/{space_a.public_id}/calendar-shape/draft")
    assert discarded.status_code == 204
    session.expire_all()
    assert service.draft_shape(session, space_a) is None
    assert (
        session.get(SpaceShapeConversation, conversation_id).status
        is ShapeConversationStatus.CLOSED
    )
    messages = list(
        session.execute(
            select(SpaceShapeMessage)
            .where(SpaceShapeMessage.conversation_id == conversation_id)
            .order_by(SpaceShapeMessage.ordinal)
        ).scalars()
    )
    assert [message.resulting_shape_version_id for message in messages] == [None, None]


def test_archived_turn_does_not_write_a_message_or_spend_a_model_call(
    api: Api, session: Session, alice: User, space_a: Space, monkeypatch
) -> None:
    import app.routers.shape_conversations as shape_router

    conversation_id = _open(api, alice, space_a)
    service.archive_space(session, space_a)
    monkeypatch.setattr(
        shape_router,
        "build_shape_client",
        lambda _settings: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )

    response = _turn(api, alice, space_a, conversation_id, "open at 10")

    assert response.status_code == 409
    assert response.json()["detail"] == "This Space is archived and can no longer be changed."
    assert (
        session.execute(
            select(SpaceShapeMessage).where(SpaceShapeMessage.conversation_id == conversation_id)
        )
        .scalars()
        .all()
        == []
    )
    assert (
        session.execute(
            select(SpaceShapeExchange).where(SpaceShapeExchange.conversation_id == conversation_id)
        )
        .scalars()
        .all()
        == []
    )


class RetryClient:
    """One malformed completion then the shape-agent's valid strict envelope."""

    default_model = "retry-stub"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(text="not json", model=self.default_model)
        return LLMResponse(
            text=json.dumps(
                {
                    "document": {
                        "version": 1,
                        "operating_blocks": [
                            {
                                "days": ["MON"],
                                "start_time": "10:00",
                                "end_time": "12:00",
                                "allowed_durations_mins": [60],
                            }
                        ],
                        "blackout_windows": [],
                    },
                    "summary": "Open 10:00–12:00 on Mondays with 60-minute bookings.",
                    "question": None,
                }
            ),
            model=self.default_model,
        )


def test_every_model_call_is_recorded_including_the_validation_retry(
    api: Api, session: Session, alice: User, space_a: Space, monkeypatch
) -> None:
    import app.routers.shape_conversations as shape_router

    monkeypatch.setattr(shape_router, "build_shape_client", lambda _settings: RetryClient())
    conversation_id = _open(api, alice, space_a)
    response = _turn(api, alice, space_a, conversation_id, "open at 10")

    assert response.status_code == 200
    exchanges = list(
        session.execute(
            select(SpaceShapeExchange)
            .where(SpaceShapeExchange.conversation_id == conversation_id)
            .order_by(SpaceShapeExchange.id)
        ).scalars()
    )
    assert [exchange.response_text for exchange in exchanges] == [
        "not json",
        exchanges[1].response_text,
    ]
    assert "validation-error" in exchanges[1].user_prompt


class FailingClient:
    default_model = "failing-stub"

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        raise LLMCallError("service unavailable")


def test_transport_failure_keeps_the_exact_pending_prompt_as_a_failed_exchange(
    api: Api, session: Session, alice: User, space_a: Space, monkeypatch
) -> None:
    import app.routers.shape_conversations as shape_router

    monkeypatch.setattr(shape_router, "build_shape_client", lambda _settings: FailingClient())
    conversation_id = _open(api, alice, space_a)

    response = _turn(api, alice, space_a, conversation_id, "open at 10")

    assert response.status_code == 503
    exchange = session.execute(
        select(SpaceShapeExchange).where(SpaceShapeExchange.conversation_id == conversation_id)
    ).scalar_one()
    assert exchange.status is ShapeExchangeStatus.FAILED
    assert exchange.response_text is None
    assert exchange.error == "service unavailable"
    assert "Latest admin request:\nopen at 10" in exchange.user_prompt
    assert service.draft_shape(session, space_a) is None


class CountingClient:
    default_model = "counting-stub"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text="{}", model=self.default_model)


def test_failed_strict_provenance_write_prevents_the_model_and_draft(
    api: Api, session: Session, alice: User, space_a: Space, monkeypatch
) -> None:
    import app.routers.shape_conversations as shape_router
    import app.shape_conversations as shape_service

    client = CountingClient()
    monkeypatch.setattr(shape_router, "build_shape_client", lambda _settings: client)
    monkeypatch.setattr(
        shape_service,
        "_begin_exchange",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("recording unavailable")),
    )
    conversation_id = _open(api, alice, space_a)

    with pytest.raises(RuntimeError, match="recording unavailable"):
        _turn(api, alice, space_a, conversation_id, "open at 10")

    assert client.calls == 0
    assert service.draft_shape(session, space_a) is None


def test_stale_space_object_cannot_write_a_draft_after_another_session_archives_it(
    session: Session, alice: User, space_a: Space
) -> None:
    with Session(bind=session.get_bind(), expire_on_commit=False) as other:
        other_space = other.get(Space, space_a.id)
        service.archive_space(other, other_space)

    with pytest.raises(service.SpaceArchivedError):
        service.upsert_draft(
            session,
            space_a,
            {"version": 1, "operating_blocks": [], "blackout_windows": []},
            alice,
        )
    assert service.draft_shape(session, space_a) is None


def test_stale_space_object_cannot_publish_after_another_session_archives_it(
    session: Session, alice: User, space_a: Space
) -> None:
    service.upsert_draft(
        session,
        space_a,
        {"version": 1, "operating_blocks": [], "blackout_windows": []},
        alice,
    )
    with Session(bind=session.get_bind(), expire_on_commit=False) as other:
        service.archive_space(other, other.get(Space, space_a.id))

    with pytest.raises(service.SpaceArchivedError):
        service.publish_draft(session, space_a, alice)
    assert service.draft_shape(session, space_a) is not None


def test_stale_space_object_cannot_open_conversation_after_another_session_archives_it(
    session: Session, alice: User, space_a: Space
) -> None:
    with Session(bind=session.get_bind(), expire_on_commit=False) as other:
        service.archive_space(other, other.get(Space, space_a.id))

    with pytest.raises(service.SpaceArchivedError):
        service.create_shape_conversation(session, space_a, alice)
    assert session.execute(select(SpaceShapeConversation)).scalar_one_or_none() is None


def test_discarded_conversation_cannot_create_its_first_draft_after_model_returns(
    session: Session, alice: User, space_a: Space
) -> None:
    conversation = service.create_shape_conversation(session, space_a, alice)
    service.append_shape_message(
        session, conversation, role=ShapeMessageRole.USER, content="open at 10"
    )
    service.discard_draft(session, space_a)

    with pytest.raises(service.ConversationClosedError):
        service.upsert_draft(
            session,
            space_a,
            {"version": 1, "operating_blocks": [], "blackout_windows": []},
            alice,
            conversation_id=conversation.id,
            check_draft_unchanged=True,
            expected_draft_id=None,
            expected_draft_created_at=None,
        )
    assert service.draft_shape(session, space_a) is None


def test_turn_finalization_blocks_discard_and_concurrent_append_until_assistant_commits(
    session: Session, alice: User, space_a: Space
) -> None:
    conversation = service.create_shape_conversation(session, space_a, alice)
    service.append_shape_message(
        session, conversation, role=ShapeMessageRole.USER, content="open at 10"
    )
    version = service.upsert_draft(
        session,
        space_a,
        {"version": 1, "operating_blocks": [], "blackout_windows": []},
        alice,
        conversation_id=conversation.id,
        check_draft_unchanged=True,
        commit=False,
    )

    with Session(bind=session.get_bind(), expire_on_commit=False) as other:
        other_space = other.get(Space, space_a.id)
        other.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(OperationalError):
            service.discard_draft(other, other_space)
        other.rollback()

    with Session(bind=session.get_bind(), expire_on_commit=False) as other:
        other_conversation = other.get(SpaceShapeConversation, conversation.id)
        other.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(OperationalError):
            service.append_shape_message(
                other,
                other_conversation,
                role=ShapeMessageRole.USER,
                content="also close Tuesdays",
            )
        other.rollback()

    service.append_shape_message(
        session,
        conversation,
        role=ShapeMessageRole.ASSISTANT,
        content="The venue is closed.",
        resulting_shape_version_id=version.id,
    )
    with Session(bind=session.get_bind(), expire_on_commit=False) as other:
        service.discard_draft(other, other.get(Space, space_a.id))

    session.expire_all()
    assert service.draft_shape(session, space_a) is None
    messages = service.list_shape_messages(session, conversation)
    assert [message.role for message in messages] == [
        ShapeMessageRole.USER,
        ShapeMessageRole.ASSISTANT,
    ]
    assert messages[1].resulting_shape_version_id is None


def test_changed_draft_after_model_input_is_refused_instead_of_overwritten(
    session: Session, alice: User, space_a: Space
) -> None:
    first = service.upsert_draft(
        session,
        space_a,
        {"version": 1, "operating_blocks": [], "blackout_windows": []},
        alice,
    )
    # ``upsert_draft`` updates the row in place, so retain the model-input snapshot rather than
    # reading attributes from this still-live ORM object after the concurrent replacement.
    expected_draft_id = first.id
    expected_draft_created_at = first.created_at
    replacement = {
        "version": 1,
        "operating_blocks": [
            {
                "days": ["MON"],
                "start_time": "10:00",
                "end_time": "12:00",
                "allowed_durations_mins": [60],
            }
        ],
        "blackout_windows": [],
    }
    service.upsert_draft(session, space_a, replacement, alice)

    with pytest.raises(service.DraftChangedError):
        service.upsert_draft(
            session,
            space_a,
            {"version": 1, "operating_blocks": [], "blackout_windows": []},
            alice,
            check_draft_unchanged=True,
            expected_draft_id=expected_draft_id,
            expected_draft_created_at=expected_draft_created_at,
        )
    assert service.draft_shape(session, space_a).document == replacement
