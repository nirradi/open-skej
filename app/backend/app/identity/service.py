"""Space and membership operations, with the invariants that outlive any handler.

The router adapts HTTP; this module owns the rules that must hold no matter who
is asking. Two of them are worth stating up front.

**A Space always has at least one owner.** Only an owner may archive a Space, so
a Space with no owner is permanently unarchivable and unmanageable — there is no
global superuser to repair it and no ownership-transfer endpoint, both by design.
The invariant is therefore enforced here rather than in the router, because it
has to hold for the ``PATCH`` path and the ``DELETE`` path and anything task 2.6
or 2.7 adds later, and a check that lives in one handler protects only that
handler.

**Archiving is not deletion.** Reads of an archived Space keep working; every
mutation is refused with 409. An archive is a record that something is finished,
and a record you cannot read is not much of a record.

Exceptions here are plain domain errors, translated to status codes by the
router — the same split ``app.db.driver`` uses with ``OverlapError``.
"""

from datetime import datetime
from typing import Optional, Sequence

from rules import ParamKind
from shape import DEFAULT_SHAPE, Shape, validate_shape
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Booking, BookingStatus, utcnow
from app.identity.authz import role_at_least
from app.identity.models import (
    AccessRequestStatus,
    InvitationStatus,
    MembershipRole,
    Resource,
    ShapeConversationStatus,
    ShapeMessageRole,
    ShapeStatus,
    Space,
    SpaceAccessRequest,
    SpaceCalendarShape,
    SpaceInvitation,
    SpaceMembership,
    SpaceShapeConversation,
    SpaceShapeMessage,
    SpaceRule,
    User,
)
from app.identity.schemas import PreviewStatus, ResourceUpdate, SpaceRuleUpdate, SpaceUpdate
from app.rule_catalog import catalog
from app.rules_stub import SpaceRuleConfig, SpaceRuleRow

# The name the auto-created first Resource is given. A fresh Space is a venue with
# one bookable calendar rather than an empty shell, so the admin's primary flow
# never meets an empty state and the schema never has to represent a Space with no
# Resource even though it could.
FIRST_RESOURCE_NAME = "Main"


class SpaceArchivedError(Exception):
    """A mutation was attempted on a Space that has been archived."""


class DraftChangedError(Exception):
    """The draft changed while a synchronous shape turn was waiting for its model response."""


class ConversationClosedError(Exception):
    """A shape turn tried to publish its result after its conversation was closed."""


class UnknownRuleTypeError(Exception):
    """A ``rule_type`` was submitted that ``app.rule_catalog.catalog`` does not know —
    neither one of the eight hand-written types in ``rules.REGISTRY`` nor a
    generated type this process has hoisted.

    Raised by ``POST``/``PATCH`` on ``/spaces/{public_id}/rules``, the only
    path that lets a caller name an arbitrary ``rule_type`` string directly.
    The router translates this to 422: a bad type name is a client mistake,
    not a state the database should ever be asked to hold.
    """

    def __init__(self, rule_type: str) -> None:
        super().__init__(rule_type)
        self.rule_type = rule_type


class InvalidRuleParamsError(Exception):
    """A rule instance's ``params`` do not satisfy its type's own schema.

    Covers every shape failure ``_validate_rule_params`` checks: a missing
    required parameter, an unknown key, and a wrong-kind or out-of-bounds
    value. The check is entirely generic — there is no rule-type-specific
    case left on this path, the two that were here having retired with
    ``session_length`` and ``availability_hours`` (task 10.5,
    ``.claude/rules/calendar-shape.md``). ``message`` is the
    ready-to-serve 422 detail, naming the
    specific offending parameter — the router has no schema knowledge of its
    own to build one from, so this exception carries the finished sentence
    rather than structured fields the router would have to reassemble.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RuleNotFoundError(Exception):
    """No ``SpaceRule`` with that id belongs to this Space.

    Raised identically for an id that names nothing and one that names a row
    in another Space — the same 404-not-403 treatment
    :class:`ResourceNotFoundError` gives a foreign Resource id, for the same
    reason: the lookup is scoped to ``space_id`` in one query, so a foreign
    id discloses nothing about being live elsewhere.
    """


class ResourceNotFoundError(Exception):
    """No Resource with that id belongs to this Space.

    Raised for a resource id that names nothing *and* for one that names a
    Resource in another Space — the two are the same outcome by design. The
    lookup is a single query scoped to ``space_id``, so a foreign id falls out as
    "not found" on the same code path and in the same time as a missing one, and
    the integer id discloses nothing about whether it is live elsewhere. This is
    the Space-level 404-not-403 rule extended one level down to the Resource.
    """


class ResourceArchivedError(Exception):
    """A mutation was attempted on a Resource that has been archived."""


class MemberNotFoundError(Exception):
    """The addressed user has no membership in this Space."""


class LastOwnerError(Exception):
    """The change would leave the Space with no owner at all."""


class AlreadyMemberError(Exception):
    """The requester is already inside the Space they are asking to join."""


class DuplicatePendingRequestError(Exception):
    """This user already has a request awaiting a decision on this Space."""


class AccessRequestNotFoundError(Exception):
    """No request with that id belongs to this Space."""


class AccessRequestAlreadyDecidedError(Exception):
    """The request has already been approved or denied.

    Refused rather than treated as idempotent. A second approval would overwrite
    ``decided_by_user_id`` and ``decided_at``, rewriting who let this person in —
    and an admin re-approving a request another admin denied a minute earlier is
    far more likely to be a stale review queue than a considered reversal.
    """


class InvitedUserAlreadyMemberError(Exception):
    """The invited address already belongs to a member of this Space.

    Distinct from :class:`AlreadyMemberError`, which is about the *caller*. Here
    the caller is an admin and the subject is somebody else, so the two produce
    different copy even though both are 409s.
    """


class DuplicatePendingInvitationError(Exception):
    """This address already has an invitation awaiting acceptance on this Space."""


class InvitationNotFoundError(Exception):
    """No invitation with that id belongs to this Space."""


class InvitationAlreadyResolvedError(Exception):
    """The invitation has already been accepted or revoked.

    Refused rather than treated as idempotent, matching
    :class:`AccessRequestAlreadyDecidedError`. Revoking an *accepted* invitation
    is the case that makes silence dangerous: the invitee is already a member, so
    a 204 would tell the admin they had removed access when the membership is
    untouched — and the fix they actually want is ``DELETE .../members/{id}``.
    A no-op 204 would hide that distinction behind a success.
    """


class InvitationRoleTooHighError(Exception):
    """The inviter tried to invite at a role above their own.

    Without this, "invite a user" is a privilege-escalation primitive: an admin
    could invite an address at ``owner``, and — since an invitation is claimed by
    whoever proves control of the address — invite *themselves* at their own
    second address to obtain ownership. The membership routes already forbid an
    admin granting ``owner`` directly (see :class:`OwnerAuthorityRequiredError`);
    an invitation reaching the same end by a longer route would make that rule
    decorative.
    """


class OwnerAuthorityRequiredError(Exception):
    """Only an owner may grant the owner role or act on an existing owner.

    Without this, making "manage members" an admin capability is a privilege
    escalation rather than a delegation: an admin could ``PATCH`` their own
    membership to ``owner``, and from there archive the Space and demote the
    person who created it. An admin could equally demote or remove an existing
    owner outright, so long as a second owner existed to satisfy the last-owner
    check — so that check alone does not contain this.

    Managing *members and admins* is genuinely delegable and remains admin+. The
    owner role is the one boundary an admin must not be able to cross unaided.
    """


class NoDraftToPublishError(Exception):
    """``publish_draft`` was called on a Space holding no draft shape row.

    Refused rather than treated as a no-op. Publishing is the one act that must move a draft to
    live; silently doing nothing would tell an admin "published" while the live shape never
    changed — the same "a no-op must not read as success" reasoning
    :class:`InvitationAlreadyResolvedError` already gives for a comparable state-machine gap.
    """


def _require_active(space: Space) -> None:
    if space.archived_at is not None:
        raise SpaceArchivedError(space.public_id)


def _lock_active_space(session: Session, space: Space) -> None:
    """Serialize a mutation on the Space row and reject a freshly observed archive.

    Shape model calls deliberately happen outside this lock. Every write boundary therefore uses
    this helper instead of trusting the request's potentially stale ORM object.
    """
    archived_at = session.execute(
        select(Space.archived_at).where(Space.id == space.id).with_for_update()
    ).scalar_one()
    if archived_at is not None:
        raise SpaceArchivedError(space.public_id)


def _lock_owners(session: Session, space_id: int) -> list[int]:
    """The user ids of this Space's owners, with their rows locked until commit.

    ``FOR UPDATE`` is the whole point, and a plain transaction would not be
    enough. Under Postgres' default READ COMMITTED, two concurrent demotions of
    two different owners would each read "there are 2 owners", each conclude the
    demotion is safe, and both commit — leaving zero owners. Neither transaction
    ever sees the other's uncommitted write, so neither can notice.

    Locking every owner row makes the second transaction block on the first
    rather than read around it. When it wakes, it re-reads the committed state,
    sees one owner remaining, and is refused. The lock covers the *set* of owners
    rather than just the target for exactly that reason: the conflict is between
    two different rows, so locking only the row being changed would not make the
    two transactions collide at all.
    """
    return list(
        session.execute(
            select(SpaceMembership.user_id)
            .where(
                SpaceMembership.space_id == space_id,
                SpaceMembership.role == MembershipRole.OWNER,
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )


def _load_membership(session: Session, space_id: int, user_id: int) -> Optional[SpaceMembership]:
    """This user's membership, re-read from the database rather than the identity map.

    ``populate_existing`` matters here: these lookups happen *after* the owner
    lock is taken, and the point of reading then is to see what other
    transactions committed while we waited. A cached instance from earlier in the
    session would hand back the stale role and defeat the lock.
    """
    return session.execute(
        select(SpaceMembership)
        .where(
            SpaceMembership.space_id == space_id,
            SpaceMembership.user_id == user_id,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def create_space(
    session: Session, creator: User, *, name: str, description: Optional[str]
) -> Space:
    """Create a Space, make its creator the owner, and give it one Resource, atomically.

    Several writes share one transaction. The owner membership because a Space with
    no owner is unrecoverable — nobody could archive it, manage it, or be added to
    it. The first Resource because a Space is a *venue*: a fresh one with no
    bookable calendar is a dead end, and creating it here means the product never
    produces an empty Space even though the schema could represent one. If any
    write fails, none of them survives.

    It gets **no** ``space_rules`` row at all. "Not enforced" is the absence of a row for every
    rule type, and a fresh venue has asked for no limit on anybody — what makes it bookable on
    arrival is its live ``SpaceCalendarShape`` row holding ``DEFAULT_SHAPE`` (task 10.2,
    ``.claude/rules/calendar-shape.md``), the structure a member books inside. A Space with no
    shape row must not be a reachable state, so that write is in the same transaction as the rest
    rather than a follow-up a caller could observe half-done. The auto-created Resource carries
    neither, since a Resource is config-free by design and every court in a Space shares the
    Space's one shape.
    """
    space = Space(name=name, description=description, created_by_user_id=creator.id)
    session.add(space)
    session.flush()

    session.add(SpaceMembership(space_id=space.id, user_id=creator.id, role=MembershipRole.OWNER))
    session.add(Resource(space_id=space.id, name=FIRST_RESOURCE_NAME))
    # A live default shape, in the same transaction as the rest of Space creation —
    # `.claude/rules/calendar-shape.md`: "A Space with no shape must not be a reachable state."
    # `DEFAULT_SHAPE` lives in `rules/shape/` rather than here so this literal and the migration's
    # own backfill copy (`c07aeccce98c_space_calendar_shapes_table.py`) both read from the one
    # place the value is actually defined.
    session.add(
        SpaceCalendarShape(
            space_id=space.id,
            document=DEFAULT_SHAPE,
            status=ShapeStatus.LIVE,
            published_at=utcnow(),
        )
    )
    session.commit()
    return space


def list_spaces_for_user(
    session: Session, user: User, *, include_archived: bool
) -> list[tuple[Space, MembershipRole]]:
    """Every Space this user belongs to, with their role in each.

    The join to ``space_memberships`` is what makes this safe: it is not a list
    of Spaces filtered by permission afterwards, it is a list of *memberships*,
    so a Space the caller has no row for cannot appear however the query is
    later edited.
    """
    query = (
        select(Space, SpaceMembership.role)
        .join(SpaceMembership, SpaceMembership.space_id == Space.id)
        .where(SpaceMembership.user_id == user.id)
        .order_by(Space.created_at, Space.id)
    )
    if not include_archived:
        query = query.where(Space.archived_at.is_(None))

    return [(space, role) for space, role in session.execute(query).all()]


def update_space(session: Session, space: Space, payload: SpaceUpdate) -> Space:
    """Apply a partial update to ``name``, ``description`` and ``timezone``.
    Omitted fields are left alone.

    Nothing else is a column on ``Space`` any more: operating hours, slot
    interval and every rule-engine limit are rows in ``space_rules``, created,
    edited and deleted only through the rules API (:func:`create_space_rule`,
    :func:`update_space_rule`, and ``DELETE /spaces/{public_id}/rules/{id}``).
    """
    _require_active(space)

    fields = payload.model_fields_set
    if "name" in fields and payload.name is not None:
        space.name = payload.name
    # An explicit null clears the description; absence leaves it untouched. The
    # schema rejects a null name and a null timezone, so only description can be
    # cleared this way.
    if "description" in fields:
        space.description = payload.description
    if "timezone" in fields and payload.timezone is not None:
        space.timezone = payload.timezone

    session.commit()
    return space


def list_space_rules(session: Session, space: Space) -> Sequence[SpaceRule]:
    """Every ``space_rules`` row for this Space — scoped and unscoped,
    enabled and disabled alike — for ``app.rules_stub`` to filter and build a
    canon from.

    Order doesn't matter here: ``app.rules_stub._build_canon`` re-sorts the
    rows it can build by each type's declared priority
    (``.claude/rules/rule-engine.md``), not by the order this query returns
    them in.
    """
    return (
        session.execute(
            select(SpaceRule).where(SpaceRule.space_id == space.id).order_by(SpaceRule.id)
        )
        .scalars()
        .all()
    )


def space_rule_config(session: Session, space: Space, *, shape: Shape) -> SpaceRuleConfig:
    """Build this Space's rule configuration for the engine adapter.

    ``app.rules_stub`` stays ORM-free by its own module docstring, so this is
    the one place a ``Space`` row and its ``space_rules`` rows meet
    ``SpaceRuleConfig``. It could not live inside ``app.rules_stub`` itself:
    that module is deliberately ORM-free (its own docstring — "it receives
    ``SpaceRuleConfig`` ... it does not query for either"), and this
    function's whole job is the query (``list_space_rules``) that
    ``rules_stub`` refuses to own.

    **``shape`` is passed in, not read here, and it is required.** The one
    caller — ``create_resource_booking`` — already holds this Space's
    validated live shape from the availability gate it ran one call earlier,
    so re-reading it would be a second query and a second chance for the
    gate and the engine to disagree about what this one booking is judged
    against. It is required rather than defaulted for the reason
    ``SpaceRuleConfig.shape``'s own docstring gives: an absent shape
    resolves the run's gap tolerance to zero, which is the *permissive*
    direction, and a default here would let a caller take it by omission.

    ``lookup=catalog.lookup`` is passed explicitly rather than left at
    ``SpaceRuleConfig``'s own ``REGISTRY.get`` default (task 7.6): this is
    the caller building a config for a real booking, so it should see every
    generated type this process has hoisted, not only the hand-written ones.
    """
    return SpaceRuleConfig(
        timezone=space.timezone,
        shape=shape,
        rules=tuple(
            SpaceRuleRow(
                id=row.id,
                rule_type=row.rule_type,
                params=row.params,
                applies_to=row.applies_to,
                enabled=row.enabled,
            )
            for row in list_space_rules(session, space)
        ),
        lookup=catalog.lookup,
    )


# ``ShapeVersion`` names the return type task 10.2's own ticket asks for. The natural value is
# the ``SpaceCalendarShape`` row itself — every other service function in this module returns its
# ORM row directly (``Space``, ``SpaceRule``, ``Resource``, ...) rather than a bespoke wrapper, and
# a shape version has no field a caller needs that the row does not already carry. The alias exists
# so 10.3's gate and 10.8's conversation API can spell the ticket's own name for it.
ShapeVersion = SpaceCalendarShape


def live_shape_version(session: Session, space: Space) -> ShapeVersion:
    """This Space's current live shape row.

    The conversation API seeds a new transcript from this stored document, while the booking gate
    uses :func:`live_shape`'s validated value.  Keeping the lookup here means neither consumer
    grows its own version-status query.
    """
    return session.execute(
        select(SpaceCalendarShape).where(
            SpaceCalendarShape.space_id == space.id,
            SpaceCalendarShape.status == ShapeStatus.LIVE,
        )
    ).scalar_one()


def live_shape(session: Session, space: Space) -> Shape:
    """This Space's current live shape, validated fresh on every read.

    Every Space always holds exactly one ``live`` row — ``create_space`` writes one and
    ``publish_draft`` never leaves the gap where none exists — so this is a ``scalar_one()``, not
    an optional lookup; a Space with no live row is a bug elsewhere; a shape with no schema-
    validating row must not exist.

    **Re-validates the stored document on every call rather than trusting that it validated once
    at write time.** A document written before a schema change is a document nobody re-checked, so
    a row whose ``document`` no longer validates raises :class:`shape.InvalidShapeError` here — it
    is not silently patched over and it does not fall back to ``DEFAULT_SHAPE``. This mirrors
    ``app.rule_catalog``'s own "re-validate at every load" discipline for a stored generated rule's
    source, for the identical reason: the caller (task 10.3's availability gate) turns the raise
    into a refusal, which is what "fail closed" asks for here — a shape that cannot be trusted is
    exactly as unusable as a shape that says the venue is never open.
    """
    row = live_shape_version(session, space)
    return validate_shape(row.document)


def draft_shape(session: Session, space: Space) -> Optional[ShapeVersion]:
    """This Space's draft row, or ``None`` when no chat turn has written one yet.

    Unlike :func:`live_shape`, this returns the row itself rather than a validated ``Shape`` — a
    draft is read back by the chat's own preview (task 10.8/10.9), which wants the same document a
    later call to :func:`upsert_draft` would overwrite, not a value already parsed into dataclasses
    it would have to re-serialize to show a diff against.
    """
    return session.execute(
        select(SpaceCalendarShape).where(
            SpaceCalendarShape.space_id == space.id,
            SpaceCalendarShape.status == ShapeStatus.DRAFT,
        )
    ).scalar_one_or_none()


def upsert_draft(
    session: Session,
    space: Space,
    document: dict,
    user: User,
    *,
    conversation_id: Optional[int] = None,
    check_draft_unchanged: bool = False,
    expected_draft_id: Optional[int] = None,
    expected_draft_created_at: Optional[datetime] = None,
    commit: bool = True,
) -> ShapeVersion:
    """Validate ``document`` and write or replace this Space's single draft row.

    Validates **before** writing anything — fail closed, the same discipline every mutating path
    in this module already follows for its own boundary checks: a document that does not parse
    through :func:`shape.validate_shape` never reaches the database, and :class:`shape
    .InvalidShapeError` propagates to the caller (task 10.8's conversation API retries it against
    the model verbatim, exactly as ``rules.safety.UnsafeRuleError`` already is).

    At most one draft exists per Space (``uq_space_calendar_shapes_draft``), so a second turn
    replaces the first in place — updating ``document``, ``created_at`` and
    ``source_conversation_id`` on the existing row — rather than superseding it the way publishing
    does; a draft is a single working copy, not its own version history, and nothing downstream
    reads a superseded *draft*. Conversation turns pass ``commit=False`` so their assistant message
    commits the draft and transcript atomically while the finalization locks remain held.
    """
    # A shape call can take seconds. Re-read and lock database state instead of trusting objects
    # resolved before it started, so archive/discard committed meanwhile wins cleanly.
    _lock_active_space(session, space)
    if conversation_id is not None:
        conversation_status = session.execute(
            select(SpaceShapeConversation.status)
            .where(
                SpaceShapeConversation.id == conversation_id,
                SpaceShapeConversation.space_id == space.id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if conversation_status is not ShapeConversationStatus.OPEN:
            raise ConversationClosedError(conversation_id)
    validate_shape(document)

    draft_query = select(SpaceCalendarShape).where(
        SpaceCalendarShape.space_id == space.id,
        SpaceCalendarShape.status == ShapeStatus.DRAFT,
    )
    if check_draft_unchanged:
        # The comparison and replacement are one atomic optimistic-write check. Without the row
        # lock, two turns can both observe the same snapshot before either UPDATE executes and the
        # second UPDATE silently wins despite the checks below.
        draft_query = draft_query.with_for_update()
    existing = session.execute(draft_query).scalar_one_or_none()
    if check_draft_unchanged and (
        (expected_draft_id is None and existing is not None)
        or (expected_draft_id is not None and existing is None)
        or (
            existing is not None
            and (
                existing.id != expected_draft_id or existing.created_at != expected_draft_created_at
            )
        )
    ):
        raise DraftChangedError(space.public_id)
    if existing is not None:
        existing.document = document
        existing.created_at = utcnow()
        existing.created_by_user_id = user.id
        existing.source_conversation_id = conversation_id
        if commit:
            session.commit()
        else:
            session.flush()
        return existing

    row = SpaceCalendarShape(
        space_id=space.id,
        document=document,
        status=ShapeStatus.DRAFT,
        created_by_user_id=user.id,
        source_conversation_id=conversation_id,
    )
    session.add(row)
    if commit:
        session.commit()
    else:
        # The caller needs the id for its assistant-message foreign key while retaining the Space,
        # conversation and draft locks until that message commits the whole finalization.
        session.flush()
    return row


def publish_draft(session: Session, space: Space, user: User) -> ShapeVersion:
    """Make this Space's draft its live shape, atomically, keeping every prior version.

    Two writes in one transaction: the current ``live`` row becomes ``superseded`` and the
    ``draft`` row becomes ``live`` with ``published_at`` and ``published_by_user_id`` set. Nothing
    is deleted — the version history this leaves behind is the whole point of publish being
    explicit rather than every chat turn going live immediately (OVERVIEW decision 4), and it is
    the only place a later "what changed and when" answer can come from.

    The two updates are flushed **in that order, explicitly** rather than left to the unit of
    work's own ordering. Both partial unique indexes are ordinary (non-deferrable) indexes, checked
    per statement, so flipping the draft to ``live`` before the old live row has already moved to
    ``superseded`` would collide with ``uq_space_calendar_shapes_live`` inside this same
    transaction — a self-inflicted version of the exact race those indexes exist to catch from two
    different callers.

    Raises :class:`NoDraftToPublishError` when there is nothing to publish, rather than treating
    the call as a no-op — see that exception's own docstring.
    """
    _lock_active_space(session, space)

    draft = draft_shape(session, space)
    if draft is None:
        raise NoDraftToPublishError(space.public_id)

    live = session.execute(
        select(SpaceCalendarShape).where(
            SpaceCalendarShape.space_id == space.id,
            SpaceCalendarShape.status == ShapeStatus.LIVE,
        )
    ).scalar_one_or_none()
    if live is not None:
        live.status = ShapeStatus.SUPERSEDED
        session.flush()

    now = utcnow()
    draft.status = ShapeStatus.LIVE
    draft.published_at = now
    draft.published_by_user_id = user.id
    _close_open_shape_conversation(session, space)
    session.commit()
    return draft


def discard_draft(session: Session, space: Space) -> None:
    """Delete this Space's draft row, if it has one.

    A no-op when there is no draft — unlike publishing, discarding has nothing left to do once the
    state it asks for already holds, so there is no ambiguous outcome a silent no-op could hide.
    The draft row is genuinely deleted rather than retired: it is a working copy nobody else
    references and never became a version anyone published, the same "an instance is really
    deleted" reasoning ``SpaceRule``'s own docstring gives for a rule instance nobody points at.
    """
    _lock_active_space(session, space)
    draft = draft_shape(session, space)
    if draft is not None:
        session.delete(draft)
    _close_open_shape_conversation(session, space)
    session.commit()


def create_shape_conversation(session: Session, space: Space, user: User) -> SpaceShapeConversation:
    """Open this Space's one in-flight shape conversation.

    The caller maps the database's unique-index race to the same 409 as the friendly lookup.
    This service deliberately does not pre-check it: the index is the invariant and a duplicated
    check would only create a second place that has to stay in agreement with it.
    """
    _lock_active_space(session, space)
    conversation = SpaceShapeConversation(space_id=space.id, user_id=user.id)
    session.add(conversation)
    session.commit()
    return conversation


def read_shape_conversation(
    session: Session, space: Space, conversation_id: int
) -> SpaceShapeConversation | None:
    """Resolve a conversation in one query scoped to its Space.

    A foreign id and an absent id intentionally both become ``None``.  The router translates
    that one value to its one 404 so neither path becomes a tenant-information oracle.
    """
    return session.execute(
        select(SpaceShapeConversation).where(
            SpaceShapeConversation.id == conversation_id,
            SpaceShapeConversation.space_id == space.id,
        )
    ).scalar_one_or_none()


def list_shape_messages(
    session: Session, conversation: SpaceShapeConversation
) -> list[SpaceShapeMessage]:
    """The visible transcript in its durable, strictly ordered form."""
    return list(
        session.execute(
            select(SpaceShapeMessage)
            .where(SpaceShapeMessage.conversation_id == conversation.id)
            .order_by(SpaceShapeMessage.ordinal)
        )
        .scalars()
        .all()
    )


def append_shape_message(
    session: Session,
    conversation: SpaceShapeConversation,
    *,
    role: ShapeMessageRole,
    content: str,
    resulting_shape_version_id: int | None = None,
) -> SpaceShapeMessage:
    """Persist one visible message before or after its model call.

    The user message is committed before the shape agent runs, so a process failure still leaves
    the request that was about to be sent. Every append locks and rechecks the conversation before
    choosing its ordinal; this serializes transcript writers with finalization and close, while the
    unique ordinal index remains the database backstop.
    """
    conversation_status = session.execute(
        select(SpaceShapeConversation.status)
        .where(SpaceShapeConversation.id == conversation.id)
        .with_for_update()
    ).scalar_one()
    if conversation_status is not ShapeConversationStatus.OPEN:
        raise ConversationClosedError(conversation.id)
    ordinal = (
        session.execute(
            select(func.coalesce(func.max(SpaceShapeMessage.ordinal), 0)).where(
                SpaceShapeMessage.conversation_id == conversation.id
            )
        ).scalar_one()
        + 1
    )
    message = SpaceShapeMessage(
        conversation_id=conversation.id,
        ordinal=ordinal,
        role=role,
        content=content,
        resulting_shape_version_id=resulting_shape_version_id,
    )
    session.add(message)
    session.commit()
    return message


def _close_open_shape_conversation(session: Session, space: Space) -> None:
    """Close the one current conversation inside the publish/discard transaction."""
    conversation = session.execute(
        select(SpaceShapeConversation).where(
            SpaceShapeConversation.space_id == space.id,
            SpaceShapeConversation.status == ShapeConversationStatus.OPEN,
        )
    ).scalar_one_or_none()
    if conversation is not None:
        conversation.status = ShapeConversationStatus.CLOSED
        conversation.closed_at = utcnow()


def _validate_rule_params(rule_type_id: str, params: dict) -> None:
    """Validate ``params`` against ``catalog.lookup(rule_type_id)``'s own schema —
    the API-boundary counterpart of ``app.rules_stub``'s booking-time
    checks, run at write time so a bad row is refused with a 422 naming the
    problem rather than silently stored and only discovered as a denial (or
    a fail-closed 500-avoided-by-luck) the next time someone books.

    Resolved through ``app.rule_catalog.catalog`` (task 7.6) rather than
    ``rules.REGISTRY`` directly, so this write boundary and the booking-time
    read boundary answer "what rule types exist" identically — a generated
    type this process has hoisted is exactly as configurable here as one of
    the eight hand-written ones, and an id neither knows behaves the same
    way in both places too.

    Four checks, in order: an unregistered ``rule_type`` raises
    :class:`UnknownRuleTypeError`; everything else raises
    :class:`InvalidRuleParamsError` naming the specific parameter — an
    unknown key, a missing required one, or one present but the wrong kind
    or below its declared ``minimum``.

    **The validation is entirely generic; no rule-type-specific check
    survives on this path.** The two that were here — ``session_length``'s
    divides-1440 bound and ``availability_hours``'s opening-hours range —
    retired with the types they mirrored, since the calendar shape says what
    both said (task 10.5, ``.claude/rules/calendar-shape.md``). A type whose
    own constructor bounds something beyond a declared ``minimum`` states
    that in its parameter schema or not at all; a second per-type branch
    here would be a second place to configure the same thing.

    Callers pass the **effective** params dict — the full set this row would
    be given, after any PATCH merge — never a partial submission, so
    "missing required" always sees a complete picture.
    """
    rule_type = catalog.lookup(rule_type_id)
    if rule_type is None:
        raise UnknownRuleTypeError(rule_type_id)

    schema = {param.name: param for param in rule_type.params}

    unknown = sorted(set(params) - set(schema))
    if unknown:
        raise InvalidRuleParamsError(
            f"Unknown parameter(s) for rule type {rule_type_id!r}: {', '.join(unknown)}"
        )

    for param in rule_type.params:
        if param.name not in params:
            if param.required:
                raise InvalidRuleParamsError(
                    f"Missing required parameter {param.name!r} for rule type {rule_type_id!r}"
                )
            continue

        value = params[param.name]
        if param.kind in (ParamKind.INTEGER, ParamKind.LOCAL_TIME):
            # `LOCAL_TIME` describes a form widget, not a storage type — the
            # value underneath it is exactly as much a plain `int` as
            # `INTEGER`'s is (`rules/rules/registry.py`'s `ParamKind`
            # docstring), so the two kinds get the identical check here.
            # `type(value) is not int` rather than `isinstance`: a bool is an
            # int under `isinstance` (`True` would silently pass as `1`),
            # and `type()` is what excludes it.
            if type(value) is not int:
                raise InvalidRuleParamsError(f"Parameter {param.name!r} must be an integer")
            if param.minimum is not None and value < param.minimum:
                raise InvalidRuleParamsError(
                    f"Parameter {param.name!r} must be at least {param.minimum}"
                )


def get_space_rule(session: Session, space: Space, rule_id: int) -> SpaceRule:
    """One ``SpaceRule`` of this Space, or raise :class:`RuleNotFoundError`.

    The ``space_id`` term is the access control, mirroring
    :func:`get_resource`: a rule id belonging to another Space returns
    nothing here and so is indistinguishable from one that does not exist,
    in one query so both also take the same time.
    """
    rule = session.execute(
        select(SpaceRule).where(SpaceRule.id == rule_id, SpaceRule.space_id == space.id)
    ).scalar_one_or_none()
    if rule is None:
        raise RuleNotFoundError(rule_id)
    return rule


def create_space_rule(
    session: Session,
    space: Space,
    *,
    rule_type: str,
    params: dict,
    applies_to: Optional[dict],
    enabled: bool,
) -> SpaceRule:
    """Configure a new rule instance for this Space. Refused on an archived
    Space, matching every other mutation in this module.

    Never refuses on a second instance of the same type: ``is_single`` is
    advisory only (``rules/rules/registry.py``), and the engine's flat AND
    makes two instances of one type coherent regardless.
    """
    _require_active(space)
    _validate_rule_params(rule_type, params)

    rule = SpaceRule(
        space_id=space.id,
        rule_type=rule_type,
        params=params,
        applies_to=applies_to,
        enabled=enabled,
    )
    session.add(rule)
    session.commit()
    return rule


def update_space_rule(
    session: Session, space: Space, *, rule_id: int, payload: SpaceRuleUpdate
) -> SpaceRule:
    """Apply a partial update to one rule instance. Omitted fields are left alone.

    ``params``, when present, is merged over the row's own currently stored
    params rather than replacing them, and it is the **effective** dict that
    is validated and stored. That is what ``PATCH`` means: a submission
    naming one parameter of a two-parameter type must not be refused
    "missing required" for the one the caller never meant to touch, nor
    silently drop it. The merge is deliberately type-agnostic — every
    rule-type-specific branch left this path with ``availability_hours`` and
    ``session_length`` (task 10.5) — and it matters for exactly the types
    nobody hand-wrote: every registered type today declares at most one
    parameter, while a generated type may declare several.
    """
    _require_active(space)
    rule = get_space_rule(session, space, rule_id)

    fields = payload.model_fields_set

    if "params" in fields:
        new_params = payload.params
        assert new_params is not None  # SpaceRuleUpdate rejects an explicit null.
        effective_params = dict(rule.params)
        effective_params.update(new_params)
        _validate_rule_params(rule.rule_type, effective_params)
        rule.params = effective_params

    if "applies_to" in fields:
        rule.applies_to = payload.applies_to

    if "enabled" in fields:
        assert payload.enabled is not None  # SpaceRuleUpdate rejects an explicit null.
        rule.enabled = payload.enabled

    session.commit()
    return rule


def delete_space_rule(session: Session, space: Space, *, rule_id: int) -> None:
    """Remove a rule instance outright.

    Unlike everything else in this schema, this is a real ``DELETE``: a
    ``SpaceRule`` row is configuration, not admission history, and
    ``enabled`` is already the pause mechanism (``app.identity.models``,
    ``SpaceRule`` docstring) — a row nobody wants paused-forever should not
    have to exist at all.
    """
    _require_active(space)
    rule = get_space_rule(session, space, rule_id)
    session.delete(rule)
    session.commit()


def archive_space(session: Session, space: Space) -> Space:
    """End a Space. There is no delete, and no un-archive.

    Re-archiving is refused rather than treated as a no-op: the caller believes
    they are ending something live, and silently succeeding would hide that
    somebody else already did it — along with *when*, which ``archived_at``
    would otherwise be quietly overwritten to lose.
    """
    _require_active(space)
    space.archived_at = utcnow()
    session.commit()
    return space


def list_resources(session: Session, space: Space, *, include_archived: bool) -> Sequence[Resource]:
    """The Resources in this Space, oldest first.

    Scoped to ``space_id``, so this is a list of *this* Space's calendars and can
    never surface another tenant's however the query is later edited — the same
    shape that keeps ``list_members`` safe. Reads work on an archived Space, so
    this does not check ``_require_active``.
    """
    query = (
        select(Resource)
        .where(Resource.space_id == space.id)
        .order_by(Resource.created_at, Resource.id)
    )
    if not include_archived:
        query = query.where(Resource.archived_at.is_(None))

    return session.execute(query).scalars().all()


def list_user_bookings_in_space(
    session: Session,
    space: Space,
    user_id: int,
    *,
    lower: datetime,
    upper: datetime,
) -> Sequence[Booking]:
    """This user's confirmed bookings across **every** Resource in ``space``.

    Backs the rule engine's Space-wide frequency caps (``.claude/rules/rule-
    engine.md``): a cap counts every booking the user holds anywhere in the
    venue, across all its Resources, never per Resource, so the query joins
    onto every Resource in the Space rather than filtering to one.

    ``[lower, upper)`` is a half-open **overlap** window, matching
    ``PostgresBookingDriver.list_bookings``: a booking counts if it overlaps
    the window at all, not only if it starts inside it, because the caller is
    expected to pass ``rules.history_window(now)`` and the engine's own
    ``Context`` validates history by overlap, not by containment. The caller
    (the router) is what actually caps the window; this function does not
    re-derive it, so a caller that passes too wide a window gets exactly what
    it asked for rather than a silently narrowed one.

    Cancelled bookings are excluded: a cancelled booking frees the slot it held
    and does not count toward the user's limit, the same treatment
    ``list_bookings`` gives a cancelled row when listing a calendar.
    """
    stmt = (
        select(Booking)
        .join(Resource, Booking.resource_id == Resource.id)
        .where(
            Resource.space_id == space.id,
            Booking.user_id == user_id,
            Booking.status == BookingStatus.CONFIRMED,
            Booking.start_at < upper,
            Booking.end_at > lower,
        )
        .order_by(Booking.start_at, Booking.id)
    )
    return session.execute(stmt).scalars().all()


def get_resource(session: Session, space: Space, resource_id: int) -> Resource:
    """One Resource of this Space, or raise :class:`ResourceNotFoundError`.

    The ``space_id`` term is the access control, not a convenience: a Resource id
    belonging to another Space returns nothing here and so is indistinguishable
    from one that does not exist — see the exception's docstring for why that
    identity is the point. One query, so both also take the same time.
    """
    resource = session.execute(
        select(Resource).where(
            Resource.id == resource_id,
            Resource.space_id == space.id,
        )
    ).scalar_one_or_none()
    if resource is None:
        raise ResourceNotFoundError(resource_id)
    return resource


def create_resource(session: Session, space: Space, *, name: str) -> Resource:
    """Add a bookable calendar to this Space.

    Refused on an archived Space: a finished venue takes no new calendars, the
    same rule every other mutation here follows. A Resource carries no
    configuration of its own — operating hours, slot interval and every rule
    limit live on the Space, so this takes nothing beyond a name.
    """
    _require_active(space)

    resource = Resource(space_id=space.id, name=name)
    session.add(resource)
    session.commit()
    return resource


def update_resource(
    session: Session, space: Space, *, resource_id: int, payload: ResourceUpdate
) -> Resource:
    """Rename a Resource. Refused on an archived Space (409) and on an archived
    Resource (409) — a retired calendar is history, not something to reconfigure.
    """
    _require_active(space)
    resource = get_resource(session, space, resource_id)
    if resource.archived_at is not None:
        raise ResourceArchivedError(resource_id)

    fields = payload.model_fields_set
    if "name" in fields and payload.name is not None:
        resource.name = payload.name

    session.commit()
    return resource


def archive_resource(session: Session, space: Space, *, resource_id: int) -> Resource:
    """Retire a Resource without deleting it, matching the Space's own end-state.

    There is no delete and no un-archive. Re-archiving is refused rather than
    treated as a no-op, mirroring :func:`archive_space`: the caller believes they
    are retiring something live, and silently succeeding would overwrite
    ``archived_at`` and lose *when* it was actually retired.
    """
    _require_active(space)
    resource = get_resource(session, space, resource_id)
    if resource.archived_at is not None:
        raise ResourceArchivedError(resource_id)

    resource.archived_at = utcnow()
    session.commit()
    return resource


def list_members(session: Session, space: Space) -> Sequence[tuple[SpaceMembership, User]]:
    """Everyone in this Space, oldest membership first."""
    return session.execute(
        select(SpaceMembership, User)
        .join(User, User.id == SpaceMembership.user_id)
        .where(SpaceMembership.space_id == space.id)
        .order_by(SpaceMembership.created_at, SpaceMembership.id)
    ).all()


def change_member_role(
    session: Session,
    space: Space,
    *,
    target_user_id: int,
    role: MembershipRole,
    actor_role: MembershipRole,
) -> tuple[SpaceMembership, User]:
    """Set a member's role, refusing to demote the last owner.

    The owner lock is taken *before* the membership is read, so the check and the
    write sit inside one serialised critical section. Reading first and locking
    afterwards would leave exactly the race the lock exists to close.

    ``actor_role`` is required because the route's admin+ gate is not sufficient
    on its own — see :class:`OwnerAuthorityRequiredError`.
    """
    _require_active(space)

    owners = _lock_owners(session, space.id)

    membership = _load_membership(session, space.id, target_user_id)
    if membership is None:
        raise MemberNotFoundError(target_user_id)

    # Granting owner, or touching someone who already is one, takes owner
    # authority. Checked after the membership load so a non-member still gets
    # "no such member" rather than a permission error that would reveal who is
    # and is not in the Space.
    touches_ownership = role is MembershipRole.OWNER or membership.role is MembershipRole.OWNER
    if touches_ownership and actor_role is not MembershipRole.OWNER:
        raise OwnerAuthorityRequiredError(target_user_id)

    demoting_the_last_owner = role is not MembershipRole.OWNER and owners == [target_user_id]
    if demoting_the_last_owner:
        raise LastOwnerError(target_user_id)

    membership.role = role
    session.commit()

    user = session.execute(select(User).where(User.id == target_user_id)).scalar_one()
    return membership, user


def remove_member(
    session: Session, space: Space, *, target_user_id: int, actor_role: MembershipRole
) -> None:
    """Remove a member, refusing to remove the last owner.

    A membership row *is* deleted here, which is the one exception to this
    schema's "nothing is ever deleted" rule. Access requests and invitations keep
    their decided rows because those are a decision history worth auditing; a
    membership is current state, and a revoked one that lingered would have to be
    excluded from every permission query forever after.

    ``actor_role`` gates removal of an owner — see
    :class:`OwnerAuthorityRequiredError`. The last-owner check alone would not
    stop an admin evicting an owner whenever a second owner happened to exist.
    """
    _require_active(space)

    owners = _lock_owners(session, space.id)

    membership = _load_membership(session, space.id, target_user_id)
    if membership is None:
        raise MemberNotFoundError(target_user_id)

    if membership.role is MembershipRole.OWNER and actor_role is not MembershipRole.OWNER:
        raise OwnerAuthorityRequiredError(target_user_id)

    if owners == [target_user_id]:
        raise LastOwnerError(target_user_id)

    session.delete(membership)
    session.commit()


def _pending_request_id(session: Session, space_id: int, user_id: int) -> Optional[int]:
    return session.execute(
        select(SpaceAccessRequest.id).where(
            SpaceAccessRequest.space_id == space_id,
            SpaceAccessRequest.user_id == user_id,
            SpaceAccessRequest.status == AccessRequestStatus.PENDING,
        )
    ).scalar_one_or_none()


def request_access(
    session: Session, space: Space, user: User, *, message: Optional[str]
) -> SpaceAccessRequest:
    """Ask to be let into a Space, on the strength of holding its link.

    Three things are refused, each for a different reason: an archived Space has
    nobody left to review the queue, an existing member has nothing to ask for,
    and a second *pending* request would only duplicate the first in the admin's
    queue. A previously **denied** request is deliberately not among them — the
    partial unique index constrains pending rows only, precisely so that someone
    turned down in March can ask again in June.

    The pre-check and the ``IntegrityError`` handler are not redundant. The check
    produces the useful error for the ordinary case — a user double-clicking the
    button — while the index is what actually holds under two concurrent requests,
    where both transactions can pass the check before either commits. Catching
    the violation converts that race into the same 409 the slower path returns,
    rather than a 500.
    """
    _require_active(space)

    if _load_membership(session, space.id, user.id) is not None:
        raise AlreadyMemberError(user.id)

    if _pending_request_id(session, space.id, user.id) is not None:
        raise DuplicatePendingRequestError(user.id)

    request = SpaceAccessRequest(space_id=space.id, user_id=user.id, message=message)
    session.add(request)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise DuplicatePendingRequestError(user.id)

    return request


def list_access_requests(
    session: Session, space: Space, *, status: Optional[AccessRequestStatus]
) -> Sequence[tuple[SpaceAccessRequest, User]]:
    """This Space's requests, oldest first, optionally filtered by status.

    Unfiltered by default rather than pending-only: the decided rows are kept as
    history exactly so an admin can see that someone was denied before, and
    hiding them by default would waste the retention the schema pays for.
    """
    query = (
        select(SpaceAccessRequest, User)
        .join(User, User.id == SpaceAccessRequest.user_id)
        .where(SpaceAccessRequest.space_id == space.id)
        .order_by(SpaceAccessRequest.created_at, SpaceAccessRequest.id)
    )
    if status is not None:
        query = query.where(SpaceAccessRequest.status == status)

    return session.execute(query).all()


def decide_access_request(
    session: Session,
    space: Space,
    *,
    request_id: int,
    approve: bool,
    decider: User,
) -> tuple[SpaceAccessRequest, User]:
    """Approve or deny a request. On approval, the membership is part of the same commit.

    **This is the invariant the function exists for.** An approved request whose
    membership row never landed is the worst outcome available here: the
    requester is told they are in, the admin sees the queue cleared, and every
    permission check still says no — with no pending row left for anyone to
    notice, since the request is now decided. So the status stamp and the
    ``INSERT`` into ``space_memberships`` share one transaction and one
    ``commit``. If the insert violates the unique index, the whole transaction
    rolls back and the request stays ``pending``, which is a state the system can
    recover from by simply approving again.

    ``with_for_update`` closes the same race the last-owner lock does. Under READ
    COMMITTED two admins could both read the request as pending and both proceed;
    the second would then either duplicate the membership or overwrite the first
    admin's decision stamp. Locking the row makes the second wait, re-read a
    decided request, and be refused.

    The membership is only inserted if one does not already exist, which is not
    defensive padding — an invitation accepted at login (see
    ``app.auth.dependencies``) can create the membership while this request sits
    pending. Approving then is still meaningful: it resolves the queue entry and
    records who decided it.
    """
    _require_active(space)

    request = session.execute(
        select(SpaceAccessRequest)
        .where(
            SpaceAccessRequest.id == request_id,
            SpaceAccessRequest.space_id == space.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    # Scoped to this Space, so an id belonging to another Space's queue reads as
    # "no such request" rather than acting on a neighbouring tenant's row.
    if request is None:
        raise AccessRequestNotFoundError(request_id)

    if request.status is not AccessRequestStatus.PENDING:
        raise AccessRequestAlreadyDecidedError(request_id)

    request.status = AccessRequestStatus.APPROVED if approve else AccessRequestStatus.DENIED
    request.decided_at = utcnow()
    request.decided_by_user_id = decider.id

    if approve and _load_membership(session, space.id, request.user_id) is None:
        session.add(
            SpaceMembership(
                space_id=space.id,
                user_id=request.user_id,
                role=MembershipRole.MEMBER,
            )
        )

    session.commit()

    requester = session.execute(select(User).where(User.id == request.user_id)).scalar_one()
    return request, requester


def _email_belongs_to_a_member(session: Session, space_id: int, email: str) -> bool:
    """Is any user holding this address already inside this Space?

    The join goes through ``users`` because an invitation is addressed to an
    email while a membership is held by a ``user_id``, and the two only meet
    there. ``func.lower`` on the stored side is not redundant with the invitation
    table's lowercase CHECK: this reads ``users.email``, which carries no such
    constraint — it is written from whatever casing the Auth0 token supplied.

    Returns true if *any* matching user is a member. An address can map to
    several ``users`` rows by design (a database signup and a Google login of the
    same address are separate ``sub`` values), and if any one of them is already
    in the Space then inviting the address again would be inviting somebody who
    is demonstrably already here.
    """
    return (
        session.execute(
            select(SpaceMembership.id)
            .join(User, User.id == SpaceMembership.user_id)
            .where(
                SpaceMembership.space_id == space_id,
                func.lower(User.email) == email,
            )
        ).first()
        is not None
    )


def _pending_invitation_id(session: Session, space_id: int, email: str) -> Optional[int]:
    return session.execute(
        select(SpaceInvitation.id).where(
            SpaceInvitation.space_id == space_id,
            func.lower(SpaceInvitation.email) == email,
            SpaceInvitation.status == InvitationStatus.PENDING,
        )
    ).scalar_one_or_none()


def create_invitation(
    session: Session,
    space: Space,
    inviter: User,
    *,
    email: str,
    role: MembershipRole,
    inviter_role: MembershipRole,
) -> SpaceInvitation:
    """Pre-approve an address for this Space at a given role.

    ``email`` is expected already lowercased and stripped — ``InvitationCreate``
    does that at the edge, and the table's CHECK constraint is the backstop.

    Four things are refused. An **archived** Space takes no new members at all.
    An address that already belongs to a member has nothing to be invited to, and
    creating the row anyway would leave a permanently unclaimable invitation in
    the admin's list. A second **pending** invitation would duplicate the first
    with no effect, since the first already admits them. And a role above the
    inviter's own is escalation rather than delegation — see
    :class:`InvitationRoleTooHighError`.

    A **revoked or accepted** invitation for the same address is deliberately not
    among the refusals: the partial unique index constrains pending rows only,
    exactly as with access requests, so an address invited and revoked in March
    can be invited again in June, and a member who was removed can be invited
    back.

    No email is sent. The invitation is a row saying "this address is welcome";
    the inviter shares the Space's ordinary link out of band, and the row is
    claimed at login by ``app.auth.dependencies._claim_pending_invitations`` on
    proof of a *verified* address. That gate is what makes an address-keyed
    pre-approval safe, and nothing here should be read as trusting the address
    itself.
    """
    _require_active(space)

    if not role_at_least(inviter_role, role):
        raise InvitationRoleTooHighError(role)

    if _email_belongs_to_a_member(session, space.id, email):
        raise InvitedUserAlreadyMemberError(email)

    if _pending_invitation_id(session, space.id, email) is not None:
        raise DuplicatePendingInvitationError(email)

    invitation = SpaceInvitation(
        space_id=space.id,
        email=email,
        role=role,
        invited_by_user_id=inviter.id,
    )
    session.add(invitation)
    try:
        session.commit()
    except IntegrityError:
        # Two admins inviting the same address at once: both pass the check
        # above before either commits, and the partial unique index is what
        # actually decides it. Converting the violation into the same 409 the
        # slower path returns keeps a race from surfacing as a 500.
        session.rollback()
        raise DuplicatePendingInvitationError(email)

    return invitation


def list_invitations(
    session: Session, space: Space, *, status: Optional[InvitationStatus]
) -> Sequence[SpaceInvitation]:
    """This Space's invitations, oldest first, optionally filtered by status.

    Unfiltered by default, matching :func:`list_access_requests`: accepted and
    revoked rows are retained as history precisely so an admin can see that an
    address was invited before, and hiding them by default would waste the
    retention the schema pays for.
    """
    query = (
        select(SpaceInvitation)
        .where(SpaceInvitation.space_id == space.id)
        .order_by(SpaceInvitation.created_at, SpaceInvitation.id)
    )
    if status is not None:
        query = query.where(SpaceInvitation.status == status)

    return session.execute(query).scalars().all()


def revoke_invitation(session: Session, space: Space, *, invitation_id: int) -> SpaceInvitation:
    """Withdraw a pending invitation.

    A **status transition, not a delete.** The row is the record that this
    address was invited and by whom, and an admin asking "who invited
    someone@rival.example?" after the fact is exactly the question the retention
    exists to answer — a ``DELETE`` would erase the evidence along with the
    access.

    Only a *pending* invitation can be revoked. An accepted one is refused rather
    than silently succeeding, because revoking it would not remove the membership
    it already produced: the admin would be told the access was withdrawn while
    the person remained in the Space. An already-revoked one is refused for the
    same reason it is refused for a re-archive — the caller believes they are
    ending something live, and a success would hide that somebody else got there
    first.

    ``with_for_update`` closes the race between a revoke and a login claiming the
    same row. Without it, two transactions could both read the invitation as
    pending and one would overwrite the other's transition, leaving an invitation
    marked ``revoked`` whose membership had already been created.
    """
    _require_active(space)

    invitation = session.execute(
        select(SpaceInvitation)
        .where(
            SpaceInvitation.id == invitation_id,
            SpaceInvitation.space_id == space.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    # Scoped to this Space, so an id from another Space's list reads as "no such
    # invitation" rather than acting on a neighbouring tenant's row.
    if invitation is None:
        raise InvitationNotFoundError(invitation_id)

    if invitation.status is not InvitationStatus.PENDING:
        raise InvitationAlreadyResolvedError(invitation_id)

    invitation.status = InvitationStatus.REVOKED
    session.commit()
    return invitation


def preview_status(session: Session, space: Space, user: User) -> PreviewStatus:
    """Where this caller stands with this Space, for the cold link-holder view."""
    membership = session.execute(
        select(SpaceMembership.id).where(
            SpaceMembership.space_id == space.id,
            SpaceMembership.user_id == user.id,
        )
    ).scalar_one_or_none()
    if membership is not None:
        return "member"

    latest = (
        session.execute(
            select(SpaceAccessRequest.status)
            .where(
                SpaceAccessRequest.space_id == space.id,
                SpaceAccessRequest.user_id == user.id,
            )
            .order_by(SpaceAccessRequest.created_at.desc(), SpaceAccessRequest.id.desc())
        )
        .scalars()
        .first()
    )

    if latest is AccessRequestStatus.PENDING:
        return "pending"
    if latest is AccessRequestStatus.DENIED:
        return "denied"
    # An approved request with no membership means the membership was removed
    # afterwards. Reporting "none" rather than "member" is both truthful and
    # useful: it lets them ask again, which "member" would not.
    return "none"
