"""SQLAlchemy models for Stream 2's identity and access schema.

These are the identity and access tables: who a user is, what Spaces exist, what
Resources those Spaces hold, and who is allowed into them. Booking mechanics stay
in ``app/db/models.py``, but ``bookings.resource_id`` and ``bookings.user_id`` are
real foreign keys onto ``resources.id`` and ``users.id`` — both defined here — so
a booking is against a real Resource, made by a real user.

``Base``, ``UtcDateTime`` and ``utcnow`` are imported from ``app.db.models``
rather than redefined. One declarative base means one metadata registry, which is
what lets those foreign keys be written without a cross-base reference — and, now
that the booking store is folded into Alembic, what lets a single migration
history own the whole schema with no table-scoping filter.

Design notes that apply throughout:

* **Enums use ``native_enum=False`` with ``create_constraint=True``**, matching
  the ``BookingStatus`` precedent. The values land as plain strings backed by a
  ``CHECK``, so there is no Postgres ``TYPE`` to ``ALTER`` when a role or status
  is added later — an in-place enum change is one of the more painful migrations
  to write, and a CHECK constraint swap is not. It also keeps the partial-index
  predicates below (``WHERE status = 'pending'``) as ordinary string comparisons.
* **Nothing is ever deleted.** A Space ends at ``archived_at``; an access request
  and an invitation both keep their decided rows as history. Consequently no
  foreign key here carries ``ON DELETE CASCADE``: there is no delete to cascade,
  and a cascade would quietly destroy the audit trail if one were ever added.
* **Timestamps use ``UtcDateTime``**, which rejects naive datetimes outright, so
  a local time cannot silently be stored as if it were UTC.
"""

import enum
import secrets
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base, UtcDateTime, utcnow

# Number of random bytes behind a Space's ``public_id``. 16 bytes is 128 bits of
# entropy, rendered by ``secrets.token_urlsafe`` as 22 URL-safe characters. The
# link *is* the capability that grants access to a Space, so this has to be
# infeasible to guess or enumerate; 128 bits is the same margin a UUIDv4 offers.
_PUBLIC_ID_BYTES = 16


def generate_public_id() -> str:
    """A fresh unguessable Space identifier.

    ``secrets`` rather than ``random``: the latter is a Mersenne Twister seeded
    from predictable state, and observing a handful of its outputs is enough to
    reconstruct the sequence — which for a capability URL would mean an attacker
    could derive every other Space's link from one they were legitimately given.
    """
    return secrets.token_urlsafe(_PUBLIC_ID_BYTES)


class MembershipRole(str, enum.Enum):
    """A user's authority within one Space.

    Scoped per Space rather than globally: there is no superuser, so two tenants
    on the same deployment stay genuinely independent. ``owner`` may archive the
    Space, ``admin`` may manage members, invitations and access requests, and
    ``member`` may only book.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class AccessRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class InvitationStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"


class GeneratedRuleTypeStatus(str, enum.Enum):
    """Whether a generated rule type is eligible to be hoisted into the catalog.

    Two values, not a delete. ``space_rules.rule_type`` rows reference a generated type by its
    string id, so removing the row outright would strand every instance pointing at it — see
    ``GeneratedRuleType``'s own docstring for why this is retire-not-delete rather than a
    contradiction of this schema's "nothing is deleted" convention.
    """

    ACTIVE = "active"
    RETIRED = "retired"


class RuleGenerationJobStatus(str, enum.Enum):
    """How far one generation run has got.

    ``queued`` and ``running`` are the two *in-flight* values, and the pair is load-bearing
    rather than descriptive: the partial unique index on ``rule_generation_jobs`` is written
    over exactly these two, so a Space may hold at most one live job while keeping every
    finished one.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ShapeStatus(str, enum.Enum):
    """Where one version of a Space's calendar shape sits in its own lifecycle.

    ``draft`` is a chat turn's own working copy, not yet enforced; ``live`` is the version the
    availability gate (task 10.3) and the calendar actually read; ``superseded`` is a former
    ``live`` row, kept rather than deleted so publish history answers "what changed and when"
    without a second table. Publishing (:func:`app.identity.service.publish_draft`) is the only
    transition between all three, and it moves exactly two rows in one write: the current
    ``live`` becomes ``superseded`` and the ``draft`` becomes ``live``.
    """

    DRAFT = "draft"
    LIVE = "live"
    SUPERSEDED = "superseded"


class PromptAgent(str, enum.Enum):
    """Which agent in the generation loop authored a stored prompt.

    Three, not two: the loop's Generator and Tester each carry their own system prompt, and
    7.8's manifest step will be a third. Naming it now means the enum does not have to change
    under rows that already exist.
    """

    GENERATOR = "generator"
    TESTER = "tester"
    MANIFEST = "manifest"
    SHAPE = "shape"


class ShapeConversationStatus(str, enum.Enum):
    """Whether a shape conversation can still accept turns.

    ``open`` is deliberately the only in-flight state.  A partial unique index over it makes
    one conversation per Space a database fact, rather than a racy router-side read.  Publishing
    or discarding closes the conversation; its messages and exchanges stay as the provenance of
    the draft that was considered.
    """

    OPEN = "open"
    CLOSED = "closed"


class ShapeMessageRole(str, enum.Enum):
    """The two speakers persisted in a shape conversation transcript."""

    USER = "user"
    ASSISTANT = "assistant"


class ShapeExchangeStatus(str, enum.Enum):
    """The durable state of a model call whose prompt was already dispatched."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


def _string_enum(enum_cls: type[enum.Enum], name: str, length: int) -> Enum:
    """An ``Enum`` column type stored as its string value with a CHECK behind it.

    ``values_callable`` is what makes the stored value ``"owner"`` rather than
    the member *name* ``"OWNER"`` — without it the partial-index predicates and
    check constraints in this module, which are written against the lowercase
    values, would silently never match anything.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


_ROLE_TYPE = _string_enum(MembershipRole, "membership_role", 16)
_ACCESS_REQUEST_STATUS_TYPE = _string_enum(AccessRequestStatus, "access_request_status", 16)
_INVITATION_STATUS_TYPE = _string_enum(InvitationStatus, "invitation_status", 16)
_GENERATED_RULE_TYPE_STATUS_TYPE = _string_enum(
    GeneratedRuleTypeStatus, "generated_rule_type_status", 16
)
_RULE_GENERATION_JOB_STATUS_TYPE = _string_enum(
    RuleGenerationJobStatus, "rule_generation_job_status", 16
)
_PROMPT_AGENT_TYPE = _string_enum(PromptAgent, "prompt_agent", 16)
_SHAPE_STATUS_TYPE = _string_enum(ShapeStatus, "space_calendar_shape_status", 16)
_SHAPE_CONVERSATION_STATUS_TYPE = _string_enum(
    ShapeConversationStatus, "space_shape_conversation_status", 16
)
_SHAPE_MESSAGE_ROLE_TYPE = _string_enum(ShapeMessageRole, "space_shape_message_role", 16)
_SHAPE_EXCHANGE_STATUS_TYPE = _string_enum(ShapeExchangeStatus, "space_shape_exchange_status", 16)


class User(Base):
    """A person, provisioned just-in-time on their first authenticated request.

    ``auth0_sub`` is the join key to Auth0 and the only stable identifier here.
    Email is deliberately *not* unique: Auth0 lets the same address arrive under
    two different ``sub`` values (a database signup and a Google login are
    separate identities unless account linking is configured), and a unique
    constraint on email would turn that ordinary situation into a hard login
    failure. Uniqueness therefore lives on ``auth0_sub`` alone, and email is
    treated as mutable — refreshed from the token on every login.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    auth0_sub: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, auth0_sub={self.auth0_sub!r}, email={self.email!r})"


class Space(Base):
    """A venue holding many Resources — a club, a building, a shared lab.

    A Space is *not* itself the thing booked: it is the tenant boundary and the
    unit of admission. The bookable calendars are its :class:`Resource` rows (a
    club has two tennis courts; a lab has three instruments), and a member of the
    Space may book any of them. Membership, roles, invitations and access
    requests are all Space-level for exactly that reason — you are admitted to the
    venue, not to one court — which is what keeps the whole authorization model
    unchanged by the venue/Resource split.

    Spaces are **not discoverable**: there is no endpoint that lists them all, so
    the only way to reach one you are not already a member of is to be handed its
    ``public_id`` link. That makes ``public_id`` a bearer capability, which is why
    it is a random token and why the integer ``id`` — sequential, and therefore
    enumerable — is never exposed over the API. ``public_id`` lives on the Space
    and not on a Resource because admission is Space-level; a Resource is reachable
    only once you are already inside the venue and needs no unguessable id.

    ``timezone`` is the venue's IANA zone (``Europe/Berlin``, never a fixed
    offset). It lives here because a venue is in one physical place, and it
    exists to resolve this Space's own *operating hours* — local wall-clock
    config — to a UTC instant per date. Stored instants everywhere else carry no
    zone; this is the one place a zone is a property of the data, because
    operating hours are a rule that lands on a different UTC moment as the
    calendar and DST move. An offset column would be the version of this that
    looks right in July and is wrong in January. It is the one genuinely
    per-Space column left on this table: operating hours, slot interval, and
    every rule-engine limit are configured instead as rows on
    :class:`SpaceRule` below, one row per *instance* of a registered rule
    type, which is what lets a Space hold any number of instances of a type
    and scope each one with ``applies_to`` rather than one fixed value for
    the whole venue.

    ``archived_at`` is the sole end-state. An archived Space rejects new bookings
    on any of its Resources; existing future bookings stay and remain cancellable.
    Deleting is never an option here — it would decide the fate of bookings made
    against the venue's Resources, and the audit trail is kept instead.
    """

    __tablename__ = "spaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, default=generate_public_id)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # An IANA zone name, defaulting to UTC. ``server_default`` so the column can
    # be added NOT NULL to a table that already holds rows, and so a Space created
    # by a path that does not set it still lands on a valid zone.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default=text("'UTC'"))
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    archived_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        # Guards against a caller passing an explicit short public_id and
        # quietly reducing a 128-bit capability to something brute-forceable.
        # The generator always produces 22 characters.
        CheckConstraint("length(public_id) >= 20", name="ck_spaces_public_id_length"),
    )

    def __repr__(self) -> str:
        return f"Space(id={self.id!r}, public_id={self.public_id!r}, name={self.name!r})"


class SpaceRule(Base):
    """One configured instance of a rule type, governing one Space.

    Rule configuration lives here rather than as fixed columns on ``Space``,
    one row per *instance* of a rule type declared in
    ``rules.registry.REGISTRY`` — so a Space can hold any number of
    instances of a type (a tighter ``max_duration`` on weekday evenings and a
    looser one on a quiet Sunday morning, as two rows) instead of one fixed
    value for the whole venue.

    ``rule_type`` is the registry's **stable string id** (``max_duration``,
    ``max_bookings_per_week``, …) — never a Python class name, so renaming the class a
    type happens to be implemented by cannot silently orphan every row that
    named it. ``params`` is the JSONB blob ``RuleType.build`` validates and
    reads; its shape is entirely owned by the registered type, which is what
    keeps this table generic over a type registered after this migration
    ships.

    ``applies_to`` narrows *when* the instance is active, and is a new shape
    with nothing else in the codebase to reverse-engineer it from, so it is
    documented here. It carries **at most one facet** — the pair (a weekday
    set intersected with a date set) is reserved, not built: neither an
    intersecting nor a union reading of the two has an obviously right
    meaning and nobody has asked for it (``ops/plans/stream-6-plan.md``, "Out
    of scope"). The three legal shapes:

    * ``None`` — always applies. The shape every row this migration
      backfills is given, and the common case going forward.
    * ``{"weekdays": [0, 2, 4]}`` — a list of ``datetime.date.weekday()``
      integers, ``0`` = Monday, matching ``rules.Weekday``'s own numbering.
      The instance applies only when the booking's own **local** date (in
      the Space's zone — never UTC; see ``app.rules_stub._local_date``) has
      a weekday in this list.
    * ``{"dates": ["2026-12-25"]}`` — a list of ISO ``date`` strings
      (``date.isoformat()``). The instance applies only when the booking's
      local date is in this list.

    ``enabled`` is the pause switch, and pausing is the entire mechanism for
    "not today": a disabled row is never assembled into the canon, full
    stop. It is not the same operation as deleting the row — a paused rule
    instance is a decision an admin can reverse, and it says nothing about
    whether the row should still exist.

    No ``ON DELETE CASCADE`` on ``space_id``, matching every other foreign
    key onto ``spaces.id`` in this schema (``.claude/rules/identity-and-
    access.md``, "nothing is deleted") — a cascade would silently destroy a
    Space's configuration the moment the row referencing it went away, which
    is exactly the failure mode that rule protects against elsewhere. The row
    *itself*, unlike a booking or a membership, may be deleted outright: this
    table's "nothing is deleted" analogue is ``enabled``, not row survival —
    "nothing is deleted" is about admission history (who asked, who was let
    in), and a retired rule instance was never that.
    """

    __tablename__ = "space_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    rule_type: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # None means "always" — see the docstring above for the two narrower shapes.
    applies_to: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        # "Which rules govern this Space?" — every read this table serves —
        # filters on space_id alone.
        Index("ix_space_rules_space", "space_id"),
    )

    def __repr__(self) -> str:
        return (
            f"SpaceRule(id={self.id!r}, space_id={self.space_id!r},"
            f" rule_type={self.rule_type!r}, enabled={self.enabled!r})"
        )


class SpaceCalendarShape(Base):
    """One version of a Space's calendar shape — structure, not policy (``.claude/rules/calendar-
    shape.md``: "A shape says what the venue offers. A rule says who may take it, and how much of
    it.").

    One row per **version**, never one row per Space, so publish history is what a Space's own
    rows already are rather than a second table: ``created_at`` on every row of one Space, in
    order, is the whole "what changed and when" answer. ``document`` is the JSON
    ``rules.shape.validate_shape`` accepts and produces — stored raw and re-validated on every
    read (:func:`app.identity.service.live_shape`), never trusted from having validated once at
    write time, the identical discipline ``app.rule_catalog`` already applies to a stored
    generated rule's source.

    **Two partial unique indexes, not a CHECK, and not a read-then-write guard in the service
    layer.** ``uq_space_calendar_shapes_live`` and ``uq_space_calendar_shapes_draft`` are each
    unique on ``space_id`` filtered to their own status — at most one live and at most one draft
    row per Space, enforced by the database itself. This is the identical shape and the identical
    reasoning ``uq_rule_generation_jobs_in_flight`` already gives: two concurrent writers can both
    pass a read-then-write check and only one of them can pass a unique index, and an admin's chat
    tab left open in two browser windows is exactly that race.

    **Publishing moves two rows in one transaction, never one.** The current ``live`` row becomes
    ``superseded`` and the ``draft`` row becomes ``live`` with ``published_at`` set — both writes
    or neither, because a version that briefly has no ``live`` row (or two) is a state nothing
    downstream of the availability gate is prepared to see.

    ``created_by_user_id`` and ``published_by_user_id`` are **nullable**, unlike almost every other
    provenance column in this schema: the 10.2 migration backfills one ``live`` row per
    pre-existing Space with neither set, since a system backfill has no acting user (OVERVIEW
    decision 3 — there is no production data, so nothing is derived, but the table still has to
    exist under every Space from the moment it ships). A row written through the API or the chat
    (task 10.8) always sets both.

    ``source_conversation_id`` is a nullable foreign key to the conversation that authored this
    draft. It is null for the default row this table's own migration writes and for any row written
    outside a chat turn. The foreign key preserves provenance without creating a delete cascade:
    conversations are closed and retained, never removed.

    No ``ON DELETE CASCADE`` on ``space_id``, matching every other foreign key onto ``spaces.id``
    in this schema — nothing here is deleted, so there is no delete to cascade, and a cascade
    would silently destroy a Space's whole shape history the moment the Space referencing it were
    ever removed by some future path.
    """

    __tablename__ = "space_calendar_shapes"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[ShapeStatus] = mapped_column(_SHAPE_STATUS_TYPE)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), default=None)
    published_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)
    published_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), default=None
    )
    source_conversation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("space_shape_conversations.id"), default=None
    )

    __table_args__ = (
        # "Which shape versions belong to this Space?" — every read this table serves — filters
        # on space_id alone, matching the ``ix_space_rules_space`` / ``ix_resources_space``
        # convention.
        Index("ix_space_calendar_shapes_space", "space_id"),
        # At most one live shape per Space. See the class docstring for why this is an index
        # rather than a check performed in the service layer.
        Index(
            "uq_space_calendar_shapes_live",
            "space_id",
            unique=True,
            postgresql_where=text("status = 'live'"),
        ),
        # At most one draft per Space — the identical reasoning, over the other status a Space
        # may hold at most one live instance of.
        Index(
            "uq_space_calendar_shapes_draft",
            "space_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"SpaceCalendarShape(id={self.id!r}, space_id={self.space_id!r},"
            f" status={self.status.value!r})"
        )


class SpaceShapeConversation(Base):
    """One admin's bounded, Space-scoped shape-authoring conversation.

    A conversation is a transcript and provenance record, not a second draft store: the one
    mutable candidate remains ``SpaceCalendarShape(status='draft')``.  The partial unique index
    permits exactly one ``open`` conversation per Space while retaining all closed conversations
    for the history of a published or discarded draft.
    """

    __tablename__ = "space_shape_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[ShapeConversationStatus] = mapped_column(
        _SHAPE_CONVERSATION_STATUS_TYPE, default=ShapeConversationStatus.OPEN
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        Index("ix_space_shape_conversations_space", "space_id"),
        Index(
            "uq_space_shape_conversations_open",
            "space_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )


class SpaceShapeMessage(Base):
    """One visible user or assistant turn in a shape conversation.

    The model's raw completion belongs in the exchange table below; the assistant message stores
    the concise summary the chat renders, linked to the draft version that resulted from it.
    """

    __tablename__ = "space_shape_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("space_shape_conversations.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    role: Mapped[ShapeMessageRole] = mapped_column(_SHAPE_MESSAGE_ROLE_TYPE)
    content: Mapped[str] = mapped_column(Text)
    resulting_shape_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("space_calendar_shapes.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    __table_args__ = (
        Index(
            "uq_space_shape_messages_conversation_ordinal",
            "conversation_id",
            "ordinal",
            unique=True,
        ),
    )


class SpaceShapeExchange(Base):
    """One shape-agent dispatch, persisted before the model receives it.

    ``prompt_versions`` owns the de-duplicated system prompt, exactly as it does for rule
    generation.  A row begins ``pending`` before the model call, then becomes ``completed`` with
    its response or ``failed`` with its transport error.  Thus a process dying mid-turn leaves the
    exact prompt it had sent rather than only completed calls.
    """

    __tablename__ = "space_shape_exchanges"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("space_shape_conversations.id"), index=True
    )
    prompt_version_id: Mapped[int] = mapped_column(ForeignKey("prompt_versions.id"))
    user_prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[ShapeExchangeStatus] = mapped_column(
        _SHAPE_EXCHANGE_STATUS_TYPE, default=ShapeExchangeStatus.PENDING
    )
    response_text: Mapped[Optional[str]] = mapped_column(Text, default=None)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    model: Mapped[str] = mapped_column(String(255))
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class GeneratedRuleType(Base):
    """One rule type produced by the AI generation loop, available to every Space.

    Global rather than Space-scoped — a generated type joins the catalog every Space can pick
    an instance from, exactly like the eight hand-written types in ``rules.REGISTRY``
    (``ops/plans/stream-7/OVERVIEW.md``, "Generated rules are global, with provenance
    recorded"). ``created_by_space_id`` (NOT NULL) and ``created_by_user_id`` record who made it
    so a later migration can scope generated types down to their creating Space without an
    archaeology exercise over rows that never recorded the answer — neither column carries
    ``ON DELETE CASCADE``, matching every other foreign key in this schema.

    ``rule_type`` is generated from the label at write time (7.7's job, not this table's) and is
    **unique** and never reused: ``space_rules.rule_type`` stores this string, so handing the
    same id to two different rules would silently repoint every row already using the first one
    at the second.

    **``python_version`` is what the owner asked for; ``bytecode_magic`` is what the load path
    actually compares.** ``marshal``'s format tracks the interpreter's bytecode magic number, not
    its version string, so the load path (``app.rule_catalog``) hoists a row only when
    ``bytecode_magic`` matches the running interpreter's own — ``python_version`` is not
    re-derived from it because a human reading a report wants "3.12.4", not a magic number's hex.
    ``executable_bytecode`` is ``marshal.dumps(compile(human_code, ...))``; ``human_code`` is kept
    alongside it, unmodified by the sandbox prelude, for display and for ``validate_source`` to
    re-check at every load (see ``app.rule_catalog``) — the blob is what runs, the source is what
    is provably safe.

    **Retire, never delete.** ``status`` is the entire removal mechanism, exactly as ``enabled``
    is for ``SpaceRule`` — except here retiring is permanent rather than a pause, because
    un-retiring a type would resurrect it under instances that may have been reconfigured or
    removed in the meantime. This does not contradict Stream 6's "a rule instance is really
    deleted" (``SpaceRule.__doc__``): an instance is a configuration choice nobody else points
    at, while a type is a thing other rows (``space_rules.rule_type``) point at by string id, so
    deleting the row out from under them would turn every live reference into an unregistered
    type — the exact failure ``rule_type`` uniqueness above exists to prevent one row from
    causing twice.

    No index on ``status`` — the only query against it is the catalog's own
    ``WHERE status = 'active'`` reload, and this table holds a handful of generated types at any
    real scale (a per-write human review gate keeps the count low), so a sequential scan costs
    nothing worth trading for an index every insert would then also pay for.
    """

    __tablename__ = "generated_rule_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_type: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, default=None)
    prompt: Mapped[str] = mapped_column(Text)
    human_code: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64))
    executable_bytecode: Mapped[bytes] = mapped_column(LargeBinary)
    python_version: Mapped[str] = mapped_column(String(32))
    bytecode_magic: Mapped[str] = mapped_column(String(16))
    # A list of RuleParam-shaped dicts (7.8 fills these in); empty until then, matching every
    # generated type's schema-less starting point.
    param_schema: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    reads_history: Mapped[bool] = mapped_column(Boolean)
    # 100 for every generated type — sorts after all eight hand-written types, so a
    # deliberately-worded hand-written denial always wins a fail-fast tie
    # (``.claude/rules/rule-engine.md``, "Generated types sort after every hand-written type").
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"))
    status: Mapped[GeneratedRuleTypeStatus] = mapped_column(
        _GENERATED_RULE_TYPE_STATUS_TYPE, default=GeneratedRuleTypeStatus.ACTIVE
    )
    created_by_space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return (
            f"GeneratedRuleType(id={self.id!r}, rule_type={self.rule_type!r},"
            f" status={self.status.value!r})"
        )


class Resource(Base):
    """One of N indistinguishable courts inside a Space — a unit of bookable
    capacity, and nothing more.

    A Resource is what a booking is actually made against: ``bookings.resource_id``
    is a foreign key onto this table, and the overlap invariant is keyed on it, so
    two courts booked at the same hour do not collide while the same court twice
    does. That overlap constraint is the *only* thing that still distinguishes two
    Resources in the same Space — it belongs to exactly one :class:`Space` (its
    venue) and carries **no configuration and no permissions of its own**.
    Operating hours, slot interval, and every rule limit live on the Space; a
    member of the Space may book any Resource in it.

    A Resource has no ``public_id``. Admission is Space-level, so nothing reaches
    a Resource without first being inside its Space; there is no capability URL to
    protect and so no unguessable id to mint. Cross-tenant safety comes from
    resolving every Resource route through ``require_space_role`` on the parent
    Space, which returns 404 (never 403) for a Resource that exists but is not
    yours — the same oracle-free rule the Space routes already follow.

    ``archived_at`` retires a Resource without deleting it, matching the Space's
    own end-state; there is no delete and so no cascade.
    """

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    archived_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        # "Which Resources are in this Space?" — the Space page and the Resource
        # picker — filters on space_id alone.
        Index("ix_resources_space", "space_id"),
    )

    def __repr__(self) -> str:
        return f"Resource(id={self.id!r}, space_id={self.space_id!r}, name={self.name!r})"


class SpaceMembership(Base):
    """The authorization record: this user, in this Space, at this role.

    This table — not Auth0 — is the source of truth for permissions. Auth0 proves
    *identity*; per-Space roles stored in Auth0 would mean a Management API round
    trip on every membership change, and an outage there would become an outage
    here.
    """

    __tablename__ = "space_memberships"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    role: Mapped[MembershipRole] = mapped_column(_ROLE_TYPE, default=MembershipRole.MEMBER)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    __table_args__ = (
        # One membership per user per Space. Without this, approving the same
        # access request twice — or a race between two admins — would leave a
        # user holding two rows at different roles, and every permission check
        # would then depend on which row it happened to read first.
        Index("uq_space_memberships_space_user", "space_id", "user_id", unique=True),
        # "Which Spaces do I belong to?" — the GET /spaces query — filters on
        # user_id alone, which the composite index above cannot serve because
        # user_id is not its leading column.
        Index("ix_space_memberships_user", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"SpaceMembership(space_id={self.space_id!r},"
            f" user_id={self.user_id!r}, role={self.role.value!r})"
        )


class SpaceAccessRequest(Base):
    """A cold link-holder asking to be let into a Space.

    Decided rows are kept rather than deleted, so an admin can see that a user
    was denied last month before approving them today. That retention is exactly
    what rules out a plain ``UNIQUE (space_id, user_id)``: it would permit only
    one request ever, so a user denied once could never ask again.

    The partial index below is the precise constraint instead — at most one
    *pending* request per user per Space, with any number of decided ones
    alongside it. Postgres is the only backend Stream 2 targets, so the
    ``postgresql_where`` predicate is not a portability compromise.
    """

    __tablename__ = "space_access_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[AccessRequestStatus] = mapped_column(
        _ACCESS_REQUEST_STATUS_TYPE, default=AccessRequestStatus.PENDING
    )
    message: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)
    decided_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), default=None)

    __table_args__ = (
        Index(
            "uq_space_access_requests_pending",
            "space_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # The admin review queue reads one Space's requests filtered by status.
        Index("ix_space_access_requests_space_status", "space_id", "status"),
        # A decided request must record when and by whom; a pending one must
        # record neither. Task 2.6 approves a request and creates the membership
        # in one transaction, and this constraint is what stops a half-applied
        # decision — status flipped, decider unrecorded — from persisting.
        CheckConstraint(
            "(status = 'pending' AND decided_at IS NULL AND decided_by_user_id IS NULL)"
            " OR (status IN ('approved', 'denied')"
            " AND decided_at IS NOT NULL AND decided_by_user_id IS NOT NULL)",
            name="ck_space_access_requests_decision_complete",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"SpaceAccessRequest(id={self.id!r}, space_id={self.space_id!r},"
            f" user_id={self.user_id!r}, status={self.status.value!r})"
        )


class SpaceInvitation(Base):
    """A pre-approval, addressed to an email that may not have an account yet.

    An invitation is keyed on email rather than ``user_id`` precisely because the
    invitee usually does not exist in ``users`` at the time it is written — the
    row is claimed on their first login, when the JWT finally supplies a
    verified address. That claim is a lookup by email, so the address is stored
    lowercased and the check constraint below enforces it: matching
    case-insensitively at query time instead would mean either a
    ``lower(email)`` scan or a silently missed invitation for
    ``Alice@Example.com``.

    Revoked and accepted rows are retained, so — as with access requests — the
    uniqueness that matters is over pending rows only: an address that was
    invited and revoked can be invited again.
    """

    __tablename__ = "space_invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[MembershipRole] = mapped_column(_ROLE_TYPE, default=MembershipRole.MEMBER)
    status: Mapped[InvitationStatus] = mapped_column(
        _INVITATION_STATUS_TYPE, default=InvitationStatus.PENDING
    )
    invited_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        Index(
            "uq_space_invitations_pending",
            "space_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # The login-time claim in task 2.3 asks "are there pending invitations
        # for this address?" across all Spaces, so email leads the index.
        Index("ix_space_invitations_email_status", "email", "status"),
        CheckConstraint("email = lower(email)", name="ck_space_invitations_email_lowercase"),
        CheckConstraint(
            "(status = 'accepted' AND accepted_at IS NOT NULL)"
            " OR (status IN ('pending', 'revoked') AND accepted_at IS NULL)",
            name="ck_space_invitations_accepted_at_matches_status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"SpaceInvitation(id={self.id!r}, space_id={self.space_id!r},"
            f" email={self.email!r}, status={self.status.value!r})"
        )


class RuleGenerationJob(Base):
    """One admin's request to have a rule type written, and what became of it.

    Generation is generate → adversarial suite → sandbox run, up to three retries: minutes of
    wall clock and up to eight model calls. That cannot be the body of an HTTP request, so it is
    a row an admin polls (``ops/plans/stream-7/OVERVIEW.md``, "Generation is a job, not a
    request"). The runner is in-process — no broker — and ``app.rule_generation`` owns the
    transitions.

    **The job is Space-scoped; the rule type it produces is not.** ``space_id`` records who was
    authorised to spend the model calls, while ``generated_rule_types`` rows are global and carry
    their own provenance columns. The two are deliberately not the same statement, and reading
    this FK as scoping the artifact would be wrong.

    ``attempts`` keeps one entry per pass — ``number``, ``outcome`` and the capped ``failure``
    text — and deliberately **not** the rule or test source. Those are already stored verbatim in
    ``rule_generation_exchanges``: the source is a Generator response, and on a retry it is
    quoted back inside the next turn's ``user_prompt``. Copying it here would duplicate the
    largest field in the schema up to three times per job to say nothing new, while the failure
    text is what an admin actually reads to understand why their rule did not land.

    ``error`` is the last failure in prose, for display; ``attempts`` is the history behind it.
    A failed job has both and no ``generated_rule_type_id`` — only ``AttemptOutcome.PASSED``
    writes a type, so a timeout and a crash leave the catalog untouched.
    """

    __tablename__ = "rule_generation_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[RuleGenerationJobStatus] = mapped_column(
        _RULE_GENERATION_JOB_STATUS_TYPE, default=RuleGenerationJobStatus.QUEUED
    )
    # A list of {"number", "outcome", "failure"} dicts, in order. See the class docstring for
    # why the rule and test source are not among them.
    attempts: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    generated_rule_type_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("generated_rule_types.id"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        # At most one live job per Space, and no constraint at all on finished ones — the same
        # shape and the same reasoning as ``uq_space_access_requests_pending``. A plain
        # ``UNIQUE (space_id)`` would let a Space generate exactly one rule type ever; this lets
        # it generate again tomorrow while stopping it queueing five at once today.
        #
        # It is also why ``sweep_orphaned_generation_jobs`` exists: a process that dies mid-run
        # leaves a ``running`` row that this index turns into a permanent outage for that one
        # tenant, so the sweep at boot is not tidiness, it is the release valve.
        Index(
            "uq_rule_generation_jobs_in_flight",
            "space_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"RuleGenerationJob(id={self.id!r}, space_id={self.space_id!r},"
            f" status={self.status.value!r})"
        )


class PromptVersion(Base):
    """One system prompt, stored once and referenced by every exchange that used it.

    The Generator's system prompt is ~5.7 kB and one generation is up to eight calls across two
    agents — roughly 46 kB of near-identical bytes per rule if each exchange carried its own
    copy. Keying on ``sha256`` stores it once.

    Size is the smaller half of the argument. The real one is the join prompt tuning needs:
    *which system-prompt version produced which outcome*, which is the question
    ``rules/benchmark.py``'s per-attempt reporting is already built around and which a pile of
    verbatim copies cannot answer without diffing them first. Editing a system prompt mints a new
    row here rather than mutating one, so old exchanges keep pointing at the text that actually
    produced them.

    ``first_seen_at``, not ``created_at``: the row is written the first time a hash is *observed*,
    which is not when the prompt was authored.
    """

    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    agent: Mapped[PromptAgent] = mapped_column(_PROMPT_AGENT_TYPE)
    prompt_text: Mapped[str] = mapped_column("text", Text)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    def __repr__(self) -> str:
        return (
            f"PromptVersion(id={self.id!r}, agent={self.agent.value!r},"
            f" sha256={self.sha256[:12]!r}…)"
        )


class RuleGenerationExchange(Base):
    """One call to the model and its answer, kept because the retry turn is the whole story.

    Without this table every prompt the feature sends is lost the moment the call returns, and
    the exchange that matters most goes with it: on a retry ``build_prompt`` hands the model back
    its own failing source plus the validator or pytest output verbatim, and *that* — what we
    told it after it failed — is the entire prompt-debugging surface. It is also the only
    evidence of why a rule now enforcing a venue's bookings reads the way it does.

    ``user_prompt`` is therefore stored **verbatim and untruncated**. It is the turn that varies;
    the system prompt it was paired with is one FK away in ``prompt_versions``, and the two
    together reconstruct the exact call.

    ``attempt`` is derived positionally by ``app.rule_generation`` — each Generator exchange opens
    a new attempt — rather than carried by the recorder. ``generation.RecordingClient`` sits at
    the ``LLMClient`` seam and knows nothing about loops or attempts, which is precisely the
    property that lets one recorder serve both the benchmark and the backend
    (``.claude/rules/rule-engine.md``).

    The three metric columns are nullable rather than defaulting to zero, the same convention
    ``LLMResponse`` keeps: a backend that does not report token counts must not read as one that
    reported none. **No row here ever contains the API key.**
    """

    __tablename__ = "rule_generation_exchanges"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("rule_generation_jobs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    agent: Mapped[PromptAgent] = mapped_column(_PROMPT_AGENT_TYPE)
    prompt_version_id: Mapped[int] = mapped_column(ForeignKey("prompt_versions.id"))
    user_prompt: Mapped[str] = mapped_column(Text)
    response_text: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    def __repr__(self) -> str:
        return (
            f"RuleGenerationExchange(id={self.id!r}, job_id={self.job_id!r},"
            f" attempt={self.attempt!r}, agent={self.agent.value!r})"
        )
