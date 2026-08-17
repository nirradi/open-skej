"""Task 10.2: a shape per Space — the table, the default, draft and live.

Postgres-only, following ``tests/test_spaces_api.py`` and ``tests/test_identity_models.py``: the
two partial unique indexes this table leans on (``uq_space_calendar_shapes_live`` /
``uq_space_calendar_shapes_draft``) need ``postgresql_where``, which SQLite ignores outright, so
the whole module skips when ``DATABASE_URL`` is unset.

Covers the service layer (``app.identity.service.live_shape`` / ``draft_shape`` /
``upsert_draft`` / ``publish_draft`` / ``discard_draft``), the two indexes themselves at the
database level, and that ``create_space`` never produces a Space with no live shape row — the
"a Space with no shape must not be a reachable state" invariant
``.claude/rules/calendar-shape.md`` states.
"""

import os
from typing import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shape import InvalidShapeError, Shape, validate_shape
from shape.types import DEFAULT_SHAPE

from app.db.models import Base
from app.identity import service
from app.identity.models import ShapeStatus, Space, SpaceCalendarShape, User

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; Space calendar shape tests need `docker compose up -d`",
)

# A shape document that is structurally JSON but fails `validate_shape` — an operating block with
# no `days` at all. Used to drive `live_shape`'s re-validation-on-read path without going through
# `upsert_draft`, which would refuse to write it in the first place.
_BROKEN_DOCUMENT = {
    "version": 1,
    "operating_blocks": [
        {
            "days": [],
            "start_time": "09:00",
            "end_time": "17:00",
            "allowed_durations_mins": [60],
        }
    ],
    "blackout_windows": [],
}

# A second, legal document distinguishable from `DEFAULT_SHAPE` at a glance, for tests that need
# to publish a draft and confirm the *new* content is what ends up live.
_ALTERNATE_DOCUMENT = {
    "version": 1,
    "operating_blocks": [
        {
            "days": ["SAT", "SUN"],
            "start_time": "10:00",
            "end_time": "14:00",
            "allowed_durations_mins": [30, 60],
        }
    ],
    "blackout_windows": [],
}


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


def _make_user(session: Session, sub: str, email: str) -> User:
    user = User(auth0_sub=sub, email=email, name=email.split("@")[0].title())
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def alice(session: Session) -> User:
    return _make_user(session, "auth0|alice", "alice@example.com")


@pytest.fixture
def space(session: Session, alice: User) -> Space:
    return service.create_space(session, alice, name="Court A", description="Alice's court")


def _shape_rows(session: Session, space: Space) -> list[SpaceCalendarShape]:
    return list(
        session.execute(
            select(SpaceCalendarShape)
            .where(SpaceCalendarShape.space_id == space.id)
            .order_by(SpaceCalendarShape.id)
        )
        .scalars()
        .all()
    )


# --- create_space seeds a live default shape --------------------------------------------------


def test_create_space_yields_exactly_one_live_shape_equal_to_default(
    session: Session, space: Space
):
    rows = _shape_rows(session, space)
    assert len(rows) == 1
    assert rows[0].status == ShapeStatus.LIVE
    assert rows[0].document == DEFAULT_SHAPE
    assert rows[0].published_at is not None


def test_create_space_default_shape_is_the_one_live_shape_returns(session: Session, space: Space):
    shape = service.live_shape(session, space)
    assert isinstance(shape, Shape)
    assert shape == validate_shape(DEFAULT_SHAPE)


# --- live_shape re-validates on read ------------------------------------------------------------


def test_live_shape_raises_when_the_stored_document_no_longer_validates(
    session: Session, space: Space
):
    live = session.execute(
        select(SpaceCalendarShape).where(
            SpaceCalendarShape.space_id == space.id,
            SpaceCalendarShape.status == ShapeStatus.LIVE,
        )
    ).scalar_one()
    live.document = _BROKEN_DOCUMENT
    session.commit()

    with pytest.raises(InvalidShapeError):
        service.live_shape(session, space)


# --- draft_shape / upsert_draft --------------------------------------------------------------


def test_draft_shape_is_none_until_a_draft_is_written(session: Session, space: Space, alice: User):
    assert service.draft_shape(session, space) is None


def test_upsert_draft_writes_a_draft_row_leaving_live_untouched(
    session: Session, space: Space, alice: User
):
    draft = service.upsert_draft(session, space, _ALTERNATE_DOCUMENT, alice)

    assert draft.status == ShapeStatus.DRAFT
    assert draft.document == _ALTERNATE_DOCUMENT
    assert draft.created_by_user_id == alice.id
    assert draft.published_at is None

    rows = _shape_rows(session, space)
    assert len(rows) == 2
    assert {row.status for row in rows} == {ShapeStatus.LIVE, ShapeStatus.DRAFT}
    live = next(row for row in rows if row.status == ShapeStatus.LIVE)
    assert live.document == DEFAULT_SHAPE


def test_upsert_draft_replaces_the_existing_draft_rather_than_adding_a_second(
    session: Session, space: Space, alice: User
):
    first = service.upsert_draft(session, space, _ALTERNATE_DOCUMENT, alice)
    second = service.upsert_draft(session, space, DEFAULT_SHAPE, alice, conversation_id=42)

    assert first.id == second.id
    assert second.document == DEFAULT_SHAPE
    assert second.source_conversation_id == 42

    rows = _shape_rows(session, space)
    assert sum(1 for row in rows if row.status == ShapeStatus.DRAFT) == 1


def test_upsert_draft_refuses_an_invalid_document_and_writes_nothing(
    session: Session, space: Space, alice: User
):
    with pytest.raises(InvalidShapeError):
        service.upsert_draft(session, space, _BROKEN_DOCUMENT, alice)

    assert service.draft_shape(session, space) is None


def test_upsert_draft_refuses_on_an_archived_space(session: Session, space: Space, alice: User):
    service.archive_space(session, space)

    with pytest.raises(service.SpaceArchivedError):
        service.upsert_draft(session, space, _ALTERNATE_DOCUMENT, alice)


# --- publish_draft moves all three rows atomically --------------------------------------------


def test_publish_draft_moves_live_to_superseded_and_draft_to_live(
    session: Session, space: Space, alice: User
):
    draft = service.upsert_draft(session, space, _ALTERNATE_DOCUMENT, alice)
    old_live_id = next(
        row.id for row in _shape_rows(session, space) if row.status == ShapeStatus.LIVE
    )

    published = service.publish_draft(session, space, alice)

    assert published.id == draft.id
    assert published.status == ShapeStatus.LIVE
    assert published.published_at is not None
    assert published.published_by_user_id == alice.id

    rows = {row.id: row for row in _shape_rows(session, space)}
    assert rows[old_live_id].status == ShapeStatus.SUPERSEDED
    assert rows[draft.id].status == ShapeStatus.LIVE
    assert len(rows) == 2

    live_shape = service.live_shape(session, space)
    assert live_shape == validate_shape(_ALTERNATE_DOCUMENT)
    assert service.draft_shape(session, space) is None


def test_publish_draft_with_no_draft_raises_and_changes_nothing(
    session: Session, space: Space, alice: User
):
    before = _shape_rows(session, space)

    with pytest.raises(service.NoDraftToPublishError):
        service.publish_draft(session, space, alice)

    after = _shape_rows(session, space)
    assert [(row.id, row.status) for row in before] == [(row.id, row.status) for row in after]


def test_publish_draft_refuses_on_an_archived_space(session: Session, space: Space, alice: User):
    service.upsert_draft(session, space, _ALTERNATE_DOCUMENT, alice)
    service.archive_space(session, space)

    with pytest.raises(service.SpaceArchivedError):
        service.publish_draft(session, space, alice)


# --- discard_draft -----------------------------------------------------------------------------


def test_discard_draft_deletes_the_draft_row(session: Session, space: Space, alice: User):
    service.upsert_draft(session, space, _ALTERNATE_DOCUMENT, alice)

    service.discard_draft(session, space)

    assert service.draft_shape(session, space) is None
    rows = _shape_rows(session, space)
    assert len(rows) == 1
    assert rows[0].status == ShapeStatus.LIVE


def test_discard_draft_is_a_no_op_when_there_is_no_draft(session: Session, space: Space):
    service.discard_draft(session, space)  # must not raise
    assert service.draft_shape(session, space) is None


# --- the two partial unique indexes, at the database level ------------------------------------


def test_a_second_live_shape_for_the_same_space_is_refused(session: Session, space: Space):
    session.add(
        SpaceCalendarShape(space_id=space.id, document=DEFAULT_SHAPE, status=ShapeStatus.LIVE)
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_second_draft_for_the_same_space_is_refused(session: Session, space: Space, alice: User):
    service.upsert_draft(session, space, _ALTERNATE_DOCUMENT, alice)

    session.add(
        SpaceCalendarShape(space_id=space.id, document=DEFAULT_SHAPE, status=ShapeStatus.DRAFT)
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_superseded_shape_does_not_count_against_either_index(
    session: Session, space: Space, alice: User
):
    """After a publish, the Space holds a `superseded` row and a `live` row for the same
    space_id — proving the partial indexes are scoped to their own status rather than to
    space_id alone."""
    service.upsert_draft(session, space, _ALTERNATE_DOCUMENT, alice)
    service.publish_draft(session, space, alice)

    # A fresh draft and a fresh publish must both still be legal — the superseded row from the
    # first publish must not be counted against either partial index.
    service.upsert_draft(session, space, DEFAULT_SHAPE, alice)
    service.publish_draft(session, space, alice)

    rows = _shape_rows(session, space)
    statuses = sorted(row.status.value for row in rows)
    assert statuses == ["live", "superseded", "superseded"]
