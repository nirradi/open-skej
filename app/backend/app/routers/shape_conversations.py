"""The synchronous, admin-only calendar-shape conversation API.

This router is intentionally unlike ``rule_drafts``.  A rule draft is a minutes-long generation,
test, and sandbox job, so it returns 202 and is polled.  A shape turn is one bounded model call
and schema-validation retry, so the reply carries the changed draft immediately for the studio to
preview.  Both retain their prompts and completions through ``RecordingClient``; neither sends
multi-tenant prompt data to a tracing service.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from generation.errors import LLMCallError
from shape import InvalidShapeError, ShapeAgentResponseError, is_unbookable, validate_shape
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.identity import service
from app.identity.authz import SpaceContext, require_space_role
from app.identity.models import (
    MembershipRole,
    ShapeConversationStatus,
    ShapeMessageRole,
    SpaceShapeConversation,
)
from app.identity.schemas import (
    CalendarShapePublish,
    ShapeConversationCreate,
    ShapeConversationRead,
    ShapeConversationTurnCreate,
    ShapeConversationTurnRead,
    ShapeVersionRead,
)
from app.settings import get_settings
from app.shape_conversations import build_shape_client, run_shape_turn, shape_to_document

router = APIRouter(prefix="/spaces", tags=["shape-conversations"])

SessionDep = Annotated[Session, Depends(get_session)]
AdminContext = Annotated[SpaceContext, Depends(require_space_role(MembershipRole.ADMIN))]

CONVERSATION_IN_FLIGHT_DETAIL = (
    "A shape conversation is already open for this Space. Finish it by publishing or discarding "
    "the draft before starting another."
)
CONVERSATION_NOT_FOUND_DETAIL = "No such shape conversation in this Space."
CONVERSATION_CLOSED_DETAIL = (
    "This shape conversation is closed. Start a new conversation to continue."
)
TURN_IN_PROGRESS_DETAIL = "Another turn changed this conversation. Refresh it before trying again."
NO_DRAFT_DETAIL = "This Space has no draft shape to publish."
UNBOOKABLE_DETAIL = (
    "This draft offers no bookable time. Set allow_unbookable to true only when deliberately "
    "closing the venue."
)
ASSISTANT_UNAVAILABLE_DETAIL = "The shape assistant is temporarily unavailable. Please try again."
ASSISTANT_INVALID_DETAIL = "The shape assistant returned an invalid shape. Please try again."
ARCHIVED_DETAIL = "This Space is archived and can no longer be changed."


def _archived() -> HTTPException:
    """The stable 409 every shape-authoring mutation returns for an archived Space."""
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ARCHIVED_DETAIL)


def _conversation_read(
    session: Session, context: SpaceContext, conversation: SpaceShapeConversation
) -> ShapeConversationRead:
    """Build the one read shape, including the live baseline Publish compares exactly."""
    return ShapeConversationRead.build(
        conversation,
        service.list_shape_messages(session, conversation),
        service.draft_shape(session, context.space),
        service.live_shape_version(session, context.space),
    )


@router.post(
    "/{public_id}/shape-conversations",
    response_model=ShapeConversationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_shape_conversation(
    _payload: ShapeConversationCreate, context: AdminContext, session: SessionDep
) -> ShapeConversationRead:
    """Open the Space's one conversation, seeded from its current live shape. Admin+."""
    if context.space.archived_at is not None:
        raise _archived()
    try:
        conversation = service.create_shape_conversation(session, context.space, context.user)
    except service.SpaceArchivedError:
        raise _archived()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=CONVERSATION_IN_FLIGHT_DETAIL
        )
    return _conversation_read(session, context, conversation)


@router.get("/{public_id}/shape-conversations/current", response_model=ShapeConversationRead | None)
def read_open_shape_conversation(
    context: AdminContext, session: SessionDep
) -> ShapeConversationRead | None:
    """Return this Space's one open conversation, or ``null`` when none exists. Admin+.

    It is deliberately a recovery read, not an id discovery route: a caller is
    already an admin of this Space, the query is scoped to that Space, and an
    absent open conversation is an ordinary ``null``. The POST route keeps its
    named 409 when concurrent creators race the partial unique index.
    """
    conversation = service.open_shape_conversation(session, context.space)
    return _conversation_read(session, context, conversation) if conversation is not None else None


@router.get(
    "/{public_id}/shape-conversations/{conversation_id}", response_model=ShapeConversationRead
)
def read_shape_conversation(
    conversation_id: int, context: AdminContext, session: SessionDep
) -> ShapeConversationRead:
    """Return the reload-safe transcript and current draft, with a Space-scoped 404. Admin+."""
    conversation = service.read_shape_conversation(session, context.space, conversation_id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=CONVERSATION_NOT_FOUND_DETAIL
        )
    return _conversation_read(session, context, conversation)


@router.post(
    "/{public_id}/shape-conversations/{conversation_id}/turns",
    response_model=ShapeConversationTurnRead,
)
def create_shape_conversation_turn(
    conversation_id: int,
    payload: ShapeConversationTurnCreate,
    context: AdminContext,
    session: SessionDep,
) -> ShapeConversationTurnRead:
    """Run one bounded shape turn and return its new draft in this response. Admin+."""
    if context.space.archived_at is not None:
        raise _archived()
    conversation = service.read_shape_conversation(session, context.space, conversation_id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=CONVERSATION_NOT_FOUND_DETAIL
        )
    if conversation.status is not ShapeConversationStatus.OPEN:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=CONVERSATION_CLOSED_DETAIL)

    try:
        service.append_shape_message(
            session, conversation, role=ShapeMessageRole.USER, content=payload.message
        )
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=TURN_IN_PROGRESS_DETAIL)
    except service.ConversationClosedError:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=CONVERSATION_CLOSED_DETAIL)
    draft = service.draft_shape(session, context.space)
    expected_draft_id = draft.id if draft is not None else None
    expected_draft_created_at = draft.created_at if draft is not None else None
    current_document = (
        draft.document
        if draft is not None
        else service.live_shape_version(session, context.space).document
    )
    settings = get_settings()
    try:
        shape, summary, question = run_shape_turn(
            session,
            conversation,
            current_document=current_document,
            client=build_shape_client(settings),
            model=settings.rule_generation_model,
        )
    except LLMCallError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=ASSISTANT_UNAVAILABLE_DETAIL
        )
    except (InvalidShapeError, ShapeAgentResponseError):
        # Both recorded completions remain in `space_shape_exchanges`; the caller gets stable chat
        # copy instead of a validator implementation detail.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=ASSISTANT_INVALID_DETAIL
        )

    try:
        version = service.upsert_draft(
            session,
            context.space,
            shape_to_document(shape),
            context.user,
            conversation_id=conversation.id,
            check_draft_unchanged=True,
            expected_draft_id=expected_draft_id,
            expected_draft_created_at=expected_draft_created_at,
            commit=False,
        )
    except service.SpaceArchivedError:
        # The Space can archive while the bounded model call is running. The up-front check keeps
        # the ordinary case side-effect-free; this preserves the same 409 for that narrow race.
        raise _archived()
    except service.DraftChangedError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=TURN_IN_PROGRESS_DETAIL)
    except service.ConversationClosedError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=CONVERSATION_CLOSED_DETAIL)
    service.append_shape_message(
        session,
        conversation,
        role=ShapeMessageRole.ASSISTANT,
        content=summary,
        question=question,
        resulting_shape_version_id=version.id,
    )
    return ShapeConversationTurnRead(
        summary=summary,
        question=question,
        draft=ShapeVersionRead.build(version),
    )


@router.post("/{public_id}/calendar-shape/publish", response_model=ShapeVersionRead)
def publish_calendar_shape(
    payload: CalendarShapePublish, context: AdminContext, session: SessionDep
) -> ShapeVersionRead:
    """Publish the draft, unless it is deliberately acknowledged as an all-closed venue. Admin+."""
    if context.space.archived_at is not None:
        raise _archived()
    draft = service.draft_shape(session, context.space)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NO_DRAFT_DETAIL)
    if is_unbookable(validate_shape(draft.document)) and not payload.allow_unbookable:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=UNBOOKABLE_DETAIL)
    try:
        version = service.publish_draft(session, context.space, context.user)
    except service.SpaceArchivedError:
        raise _archived()
    except service.NoDraftToPublishError:
        # A concurrent publish can remove the draft after the preflight; preserve the named
        # no-draft outcome instead of turning that ordinary race into a 500.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NO_DRAFT_DETAIL)
    return ShapeVersionRead.build(version)


@router.post("/{public_id}/calendar-shape/draft", status_code=status.HTTP_204_NO_CONTENT)
def discard_calendar_shape_draft(context: AdminContext, session: SessionDep) -> Response:
    """Discard the working copy and close its conversation. Admin+."""
    if context.space.archived_at is not None:
        raise _archived()
    try:
        service.discard_draft(session, context.space)
    except service.SpaceArchivedError:
        raise _archived()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
