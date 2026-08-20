"""Pydantic schemas for the Space HTTP API.

Kept separate from ``app.identity.models`` for the same reason ``app.schemas`` is
kept separate from ``app.db.models``: those describe how a Space is stored, these
describe what crosses the wire.

**The integer ``Space.id`` appears in none of these.** It is a sequential
primary key and therefore enumerable, whereas ``public_id`` is a 128-bit random
token that *is* the capability granting access to a Space. Leaking the integer
anywhere — a response body, an error message, a ``Location`` header — would hand
an attacker a way to reason about Spaces they were never given a link to. The
protection is structural rather than a matter of remembering: none of the models
below declares an ``id`` field, so Pydantic will not serialise one even when it
is handed an ORM object that has it.

``users.id`` *is* exposed, as ``user_id``, and ``resources.id`` as ``id`` on
:class:`ResourceRead`. That is a different judgement, not an oversight: both are
only ever visible to people already inside the Space, and their routes are
addressed by those ids, so they have to cross the wire. What makes ``Space.id``
different is that it is the one integer an *outsider* could reason about — the
capability is the ``public_id`` link, and admission is Space-level, so a Resource
is never reachable without first being a member of its Space and its sequential
id discloses nothing to anyone who is not already there.
"""

from datetime import date, datetime
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from rules import ParamKind, RuleParam, RuleType
from shape import BlackoutInterval, DayProjection, OfferedStart, OperatingInterval

from app.identity.models import (
    AccessRequestStatus,
    InvitationStatus,
    MembershipRole,
    Resource,
    RuleGenerationJob,
    RuleGenerationJobStatus,
    ShapeConversationStatus,
    ShapeMessageRole,
    Space,
    SpaceCalendarShape,
    SpaceAccessRequest,
    SpaceInvitation,
    SpaceMembership,
    SpaceShapeConversation,
    SpaceShapeMessage,
    SpaceRule,
    User,
)

# The four states a link-holder can be in with respect to a Space, as reported by
# ``GET /spaces/{public_id}/preview``.
PreviewStatus = Literal["none", "pending", "denied", "member"]

_NAME_MAX = 200
_DESCRIPTION_MAX = 4000
_MESSAGE_MAX = 1000
# Matches ``users.email`` and ``space_invitations.email``, both String(320) — the
# maximum length RFC 5321 permits for a full address.
_EMAIL_MAX = 320


class SpaceCreate(BaseModel):
    """The body of ``POST /spaces``.

    Carries no operating-hours or rule-parameter fields — a fresh Space gets
    sensible default hours and slot interval (see ``service.create_space``) so
    it is immediately bookable, and the four rule parameters default to unset.
    Setting either is a follow-up ``PATCH`` through :class:`SpaceUpdate`, not a
    creation-time concern.
    """

    name: str = Field(min_length=1, max_length=_NAME_MAX)
    description: Optional[str] = Field(default=None, max_length=_DESCRIPTION_MAX)


class SpaceUpdate(BaseModel):
    """The body of ``PATCH /spaces/{public_id}``.

    All fields are optional, and *omitted* is deliberately distinct from
    *explicitly null*: omitting ``description`` leaves it alone, while sending
    ``null`` clears it. The router reads ``model_fields_set`` to tell the two
    apart, which is the only way to express "clear this field" in a PATCH without
    inventing a sentinel value.

    ``name`` and ``timezone`` are the exception — neither is nullable in the
    database, so an explicit null for either is a client error rather than an
    instruction, and is rejected here as 422 instead of reaching the database as
    an ``IntegrityError`` 500.

    Carries no operating-hours or rule-parameter fields: those are rows in
    ``space_rules``, read and written only through the rules API
    (``GET``/``POST`` ``/spaces/{public_id}/rules``,
    ``PATCH``/``DELETE`` ``/spaces/{public_id}/rules/{id}``), never through this
    model. ``timezone`` is the one property of a Space that is genuinely a
    scalar column rather than a rule instance.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=_NAME_MAX)
    description: Optional[str] = Field(default=None, max_length=_DESCRIPTION_MAX)
    timezone: Optional[str] = Field(default=None, min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"{value!r} is not a known IANA timezone name")
        return value

    @model_validator(mode="after")
    def _reject_explicit_null_name(self) -> "SpaceUpdate":
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name may not be null; omit it to leave it unchanged")
        if "timezone" in self.model_fields_set and self.timezone is None:
            raise ValueError("timezone may not be null; omit it to leave it unchanged")
        return self


class SpaceRead(BaseModel):
    """A Space as seen from inside — the response for members only.

    ``my_role`` travels with the Space so the frontend can decide which controls
    to render without a second round trip. It is a convenience, never a security
    boundary: every privileged route re-checks the role server-side.

    Carries no operating-hours or rule-parameter fields. What a Space enforces
    is rows in ``space_rules``, of arbitrary number and each possibly scoped to
    particular weekdays or dates via ``applies_to`` — there is no longer one
    scalar value a field on this model could name, so that configuration is
    read only through the rules API (``GET /spaces/{public_id}/rules``, and
    ``GET /rule-types`` for what each type means). ``timezone`` is the one
    property of a Space that is genuinely a scalar column rather than a rule
    instance.
    """

    model_config = ConfigDict(from_attributes=True)

    public_id: str
    name: str
    description: Optional[str]
    timezone: str
    created_at: datetime
    archived_at: Optional[datetime]
    my_role: MembershipRole

    @classmethod
    def build(cls, space: Space, role: MembershipRole) -> "SpaceRead":
        return cls(
            public_id=space.public_id,
            name=space.name,
            description=space.description,
            timezone=space.timezone,
            created_at=space.created_at,
            archived_at=space.archived_at,
            my_role=role,
        )


class SpacePreview(BaseModel):
    """The cold link-holder view: enough to decide whether to ask to come in.

    Deliberately thin. Anyone holding the link can fetch this without being a
    member, so everything here is disclosed to whoever the link was forwarded to.
    That rules out the member list, member counts, and any hint of the bookings
    inside — a count alone would tell an outsider how busy a private resource is,
    and a member list would leak who uses it.
    """

    public_id: str
    name: str
    description: Optional[str]
    status: PreviewStatus


class ResourceCreate(BaseModel):
    """The body of ``POST /spaces/{public_id}/resources``.

    ``name`` only. A Resource is one of N indistinguishable courts and carries
    no configuration of its own — operating hours, slot interval, and every rule
    limit live on the Space, not here.
    """

    name: str = Field(min_length=1, max_length=_NAME_MAX)


class ResourceUpdate(BaseModel):
    """The body of ``PATCH /spaces/{public_id}/resources/{resource_id}``.

    ``name`` only, and an explicit null is a client error rejected here as 422
    rather than left to surface as an ``IntegrityError`` 500 — ``name`` is
    ``NOT NULL`` in the database.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=_NAME_MAX)

    @model_validator(mode="after")
    def _reject_explicit_null_name(self) -> "ResourceUpdate":
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name may not be null; omit it to leave it unchanged")
        return self


class ResourceRead(BaseModel):
    """A Resource as seen from inside its Space.

    ``id`` is the integer primary key and it is exposed deliberately: a Resource
    carries no ``public_id`` because admission is Space-level, so this id is the
    handle its own routes are addressed by, and it is only ever visible to people
    already inside the Space. That is the same judgement made for ``user_id`` and
    the opposite of the one made for ``Space.id`` — see this module's docstring.

    No hours, no slot interval, no rule parameters: a Resource is a unit of
    bookable capacity, config-free by design. See :class:`SpaceRead` for where
    that configuration actually lives.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: datetime
    archived_at: Optional[datetime]

    @classmethod
    def build(cls, resource: Resource) -> "ResourceRead":
        return cls.model_validate(resource)


class MemberRead(BaseModel):
    """One membership, as shown to people already inside the Space."""

    user_id: int
    email: str
    name: Optional[str]
    role: MembershipRole
    created_at: datetime

    @classmethod
    def build(cls, membership: SpaceMembership, user: User) -> "MemberRead":
        return cls(
            user_id=user.id,
            email=user.email,
            name=user.name,
            role=membership.role,
            created_at=membership.created_at,
        )


class MembershipUpdate(BaseModel):
    """The body of ``PATCH /spaces/{public_id}/members/{user_id}``."""

    role: MembershipRole


class AccessRequestCreate(BaseModel):
    """The body of ``POST /spaces/{public_id}/access-requests``.

    A message is optional and free text — "I'm on the Tuesday team" — because the
    admin deciding has otherwise only an email address to go on, and an email
    address is not much on which to let someone into a private Space.
    """

    message: Optional[str] = Field(default=None, max_length=_MESSAGE_MAX)


class AccessRequestRead(BaseModel):
    """One access request, as shown to the admins reviewing the queue.

    The requester's ``email`` and ``name`` are joined in rather than left as a
    bare ``user_id``: this is the one screen where an admin decides whether a
    stranger gets into their Space, and a numeric id gives them nothing to decide
    on. Only admin+ ever sees this model — the requester's own view of their
    standing is the single ``status`` word in ``SpacePreview``.
    """

    id: int
    user_id: int
    email: str
    name: Optional[str]
    status: AccessRequestStatus
    message: Optional[str]
    created_at: datetime
    decided_at: Optional[datetime]
    decided_by_user_id: Optional[int]

    @classmethod
    def build(cls, request: SpaceAccessRequest, user: User) -> "AccessRequestRead":
        return cls(
            id=request.id,
            user_id=user.id,
            email=user.email,
            name=user.name,
            status=request.status,
            message=request.message,
            created_at=request.created_at,
            decided_at=request.decided_at,
            decided_by_user_id=request.decided_by_user_id,
        )


class InvitationCreate(BaseModel):
    """The body of ``POST /spaces/{public_id}/invitations``.

    **The email is lowercased here, at the edge**, rather than in the service or
    the router. ``space_invitations.email`` carries a
    ``CHECK (email = lower(email))``, and the login-time claim in
    ``app.auth.dependencies`` matches on the lowercased address, so an
    un-normalised value would either be rejected by the database as a 500 or —
    worse, had the constraint not existed — stored as ``Alice@Example.com`` and
    silently never matched when Alice logged in. Normalising once, where the
    value enters the system, is what makes "stored lowercased" a property of the
    data rather than a habit of whichever caller wrote it.

    Surrounding whitespace is stripped for the same reason: an address pasted out
    of an email client routinely arrives with a trailing space, and a stored
    ``"alice@example.com "`` would never match a token's ``alice@example.com``.

    The shape check is deliberately minimal — one ``@`` with something either
    side, no internal whitespace. It is here to catch a transposed field or an
    obviously empty value, not to adjudicate RFC 5322; ``pydantic[email]`` would
    need a new dependency, and the real proof that an address is deliverable and
    belongs to its holder is Auth0's ``email_verified`` claim at login, which is
    what actually gates the invitation being claimed.
    """

    email: str = Field(min_length=3, max_length=_EMAIL_MAX)
    role: MembershipRole = MembershipRole.MEMBER

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        email = value.strip().lower()

        local, separator, domain = email.partition("@")
        if not separator or not local or not domain or "@" in domain:
            raise ValueError("email must be a single address of the form name@domain")
        if any(character.isspace() for character in email):
            raise ValueError("email may not contain whitespace")

        return email


class InvitationRead(BaseModel):
    """One invitation, as shown to the admins managing the Space.

    Only admin+ ever sees this model. That matters because it lists the email
    addresses of people who are *not* members — an invitation to
    ``someone@rival.example`` is visible here before they have accepted anything,
    and exposing it to ordinary members would disclose who is being recruited
    into the Space.

    There is no ``invitation link`` field because there is no per-invitation
    token: the invitee is admitted by the address on their verified token, so the
    thing the inviter shares is the Space's ordinary ``public_id`` link. A
    per-invitation secret would be a second capability to leak, and it would
    admit whoever held it rather than whoever owns the address.
    """

    id: int
    email: str
    role: MembershipRole
    status: InvitationStatus
    invited_by_user_id: int
    created_at: datetime
    accepted_at: Optional[datetime]

    @classmethod
    def build(cls, invitation: SpaceInvitation) -> "InvitationRead":
        return cls(
            id=invitation.id,
            email=invitation.email,
            role=invitation.role,
            status=invitation.status,
            invited_by_user_id=invitation.invited_by_user_id,
            created_at=invitation.created_at,
            accepted_at=invitation.accepted_at,
        )


# --- Rule types and rule instances (task 6.7). -------------------------------
#
# `GET /rule-types` describes the product's registered rule types
# (`rules.REGISTRY`) and is not Space-scoped; the rest of this section is the
# `/spaces/{public_id}/rules` CRUD onto one Space's `space_rules` rows.

_RULE_TYPE_MAX = 64


def _validate_applies_to(value: Optional[dict]) -> Optional[dict]:
    """Enforce the three legal shapes ``SpaceRule.applies_to`` documents:
    ``None`` (always), ``{"weekdays": [...]}``, or ``{"dates": [...]}`` —
    never both facets, never neither, never an extra key. This is a pure
    shape check with no registry dependency, so it runs here at the wire
    boundary rather than in ``app.identity.service``, which only validates
    ``params`` against a rule type's own schema.
    """
    if value is None:
        return value
    if not isinstance(value, dict):
        raise ValueError("applies_to must be an object or null")

    has_weekdays = "weekdays" in value
    has_dates = "dates" in value
    if has_weekdays and has_dates:
        raise ValueError("applies_to may carry weekdays or dates, never both")
    if not has_weekdays and not has_dates:
        raise ValueError("applies_to must carry exactly one of weekdays or dates")

    extra = set(value) - {"weekdays", "dates"}
    if extra:
        raise ValueError(f"applies_to has unexpected key(s): {', '.join(sorted(extra))}")

    if has_weekdays:
        weekdays = value["weekdays"]
        if not isinstance(weekdays, list) or not weekdays:
            raise ValueError("applies_to.weekdays must be a non-empty list")
        for day in weekdays:
            if type(day) is not int or not (0 <= day <= 6):
                raise ValueError(
                    "applies_to.weekdays must contain integers 0 (Monday) through 6 (Sunday)"
                )
    else:
        dates = value["dates"]
        if not isinstance(dates, list) or not dates:
            raise ValueError("applies_to.dates must be a non-empty list")
        for one_date in dates:
            if not isinstance(one_date, str):
                raise ValueError("applies_to.dates must contain ISO date strings")
            try:
                date.fromisoformat(one_date)
            except ValueError:
                raise ValueError(f"{one_date!r} is not a valid ISO date")

    return value


class RuleParamRead(BaseModel):
    """One parameter a rule type's ``params`` dict accepts.

    Mirrors ``rules.RuleParam`` field-for-field rather than reinventing a
    second shape for it — the same schema a future admin form (6.8) renders
    a field from is what ``app.identity.service`` validates a request's
    ``params`` against, so the two cannot drift into disagreeing about a
    parameter.
    """

    name: str
    kind: ParamKind
    label: str
    unit: Optional[str]
    required: bool
    minimum: Optional[int]

    @classmethod
    def build(cls, param: RuleParam) -> "RuleParamRead":
        return cls(
            name=param.name,
            kind=param.kind,
            label=param.label,
            unit=param.unit,
            required=param.required,
            minimum=param.minimum,
        )


class RuleTypeRead(BaseModel):
    """One registered rule type (``rules.REGISTRY``), as ``GET /rule-types`` serves it.

    Describes the product's available rule types, not any one Space's
    configuration — that is ``SpaceRuleRead`` below.
    """

    rule_type: str
    label: str
    description: str
    priority: int
    reads_history: bool
    needs_local_resolution: bool
    is_single: bool
    params: list[RuleParamRead]

    @classmethod
    def build(cls, rule_type: RuleType) -> "RuleTypeRead":
        return cls(
            rule_type=rule_type.rule_type,
            label=rule_type.label,
            description=rule_type.description,
            priority=rule_type.priority,
            reads_history=rule_type.reads_history,
            needs_local_resolution=rule_type.needs_local_resolution,
            is_single=rule_type.is_single,
            params=[RuleParamRead.build(param) for param in rule_type.params],
        )


class SpaceRuleRead(BaseModel):
    """One configured ``space_rules`` row, as members of the Space see it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    rule_type: str
    params: dict
    applies_to: Optional[dict]
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, rule: SpaceRule) -> "SpaceRuleRead":
        return cls.model_validate(rule)


class SpaceRuleCreate(BaseModel):
    """The body of ``POST /spaces/{public_id}/rules``.

    ``rule_type`` and ``params`` are validated against ``rules.REGISTRY`` in
    ``app.identity.service``, not here — only the service layer knows the
    per-type schema (and this module already keeps its own shape checks,
    like ``applies_to``, separate from registry-dependent ones for the same
    reason ``SpaceUpdate``'s timezone check stays here while ``space_rules``
    row-level checks live in ``service.py``).

    Creating a second instance of an ``is_single`` type is not refused here:
    ``is_single`` is advisory only (``rules/rules/registry.py``) and a
    warning task 6.8's form surfaces, never a constraint this API enforces.
    """

    rule_type: str = Field(min_length=1, max_length=_RULE_TYPE_MAX)
    params: dict = Field(default_factory=dict)
    applies_to: Optional[dict] = None
    enabled: bool = True

    @field_validator("applies_to")
    @classmethod
    def _check_applies_to(cls, value: Optional[dict]) -> Optional[dict]:
        return _validate_applies_to(value)


class SpaceRuleUpdate(BaseModel):
    """The body of ``PATCH /spaces/{public_id}/rules/{id}``.

    All fields optional; omitted leaves the field alone, following the same
    convention as ``SpaceUpdate``. ``rule_type`` is deliberately not a field
    here at all — there is no sane way to reinterpret one type's stored
    ``params`` as another's, so changing it is a delete-and-recreate, not an
    edit.

    ``params``, if present, is a **wholesale replacement**, re-validated
    against the row's own (unchanged) ``rule_type`` schema — not merged with
    what is already stored. An explicit null is rejected the same way a null
    ``name`` is on ``SpaceUpdate``: a rule row cannot hold no params at all.

    ``applies_to`` follows the omit-leaves-alone / explicit-null-clears
    convention: omitted leaves the row's scoping untouched, ``null`` clears
    it back to "always".

    ``enabled`` follows the same omit/explicit-null split as ``applies_to``
    for *presence*, but the column itself is not nullable, so an explicit
    null is rejected rather than accepted as "clear it" — there is no
    "neither enabled nor disabled" state to clear it to.
    """

    params: Optional[dict] = None
    applies_to: Optional[dict] = None
    enabled: Optional[bool] = None

    @field_validator("applies_to")
    @classmethod
    def _check_applies_to(cls, value: Optional[dict]) -> Optional[dict]:
        return _validate_applies_to(value)

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> "SpaceRuleUpdate":
        if "params" in self.model_fields_set and self.params is None:
            raise ValueError("params may not be null; omit it to leave it unchanged")
        if "enabled" in self.model_fields_set and self.enabled is None:
            raise ValueError("enabled may not be null; omit it to leave it unchanged")
        return self


# --- The shape projection (task 10.3). ---------------------------------------------------------
#
# `GET /spaces/{public_id}/calendar` serves `shape.projection.project_day`'s own output, field for
# field, in the package's own vocabulary — minutes from local midnight throughout, never an
# `HH:MM:SS` wall-clock string (`.claude/rules/calendar-shape.md`, "the wire says the same"). The
# grid needs minutes to lay out slots, and converting from minutes to a wall clock and back is two
# chances to disagree with the gate that enforces the identical table.


class OfferedStartRead(BaseModel):
    """One grid-aligned local minute a booking may begin at, and every duration offered there.

    Mirrors ``shape.projection.OfferedStart``. This is the authoritative table a booking is
    judged against — the availability gate (task 10.3, ``app.routers.resource_bookings``) answers
    ``shape.permits`` from the identical table, so the calendar can never draw a start the gate
    would then refuse, or grey out one the gate would allow.
    """

    start_minutes: int
    durations_mins: list[int]

    @classmethod
    def build(cls, offered: OfferedStart) -> "OfferedStartRead":
        return cls(
            start_minutes=offered.start_minutes,
            durations_mins=list(offered.durations_mins),
        )


class OperatingIntervalRead(BaseModel):
    """One merged region of operating time in force on a date. Mirrors
    ``shape.projection.OperatingInterval`` — a structural summary of what is nominally offered
    across the merged region, not itself grid- or blackout-aware; ``offered_starts`` is the
    exact, per-minute answer.
    """

    start_minutes: int
    end_minutes: int
    allowed_durations_mins: list[int]

    @classmethod
    def build(cls, interval: OperatingInterval) -> "OperatingIntervalRead":
        return cls(
            start_minutes=interval.start_minutes,
            end_minutes=interval.end_minutes,
            allowed_durations_mins=list(interval.allowed_durations_mins),
        )


class BlackoutIntervalRead(BaseModel):
    """One blackout window in force on a date, with its own reason. Mirrors
    ``shape.projection.BlackoutInterval`` — ``reason`` is the one piece of admin-authored text
    this package ever serves verbatim; see that dataclass for why it is safe to.
    """

    start_minutes: int
    end_minutes: int
    reason: str

    @classmethod
    def build(cls, interval: BlackoutInterval) -> "BlackoutIntervalRead":
        return cls(
            start_minutes=interval.start_minutes,
            end_minutes=interval.end_minutes,
            reason=interval.reason,
        )


class DayProjectionRead(BaseModel):
    """One local date's shape projection, as ``GET /spaces/{public_id}/calendar`` serves it.

    Mirrors ``shape.projection.DayProjection`` field for field — see that class and
    ``.claude/rules/calendar-shape.md`` for what each field means and the fail-closed guarantee
    behind ``offered_starts``. ``bookable`` is ``bool(offered_starts)``, reported directly rather
    than left for the client to derive, matching this whole endpoint's "the server projects, the
    client never does" rule.
    """

    date: date
    operating_intervals: list[OperatingIntervalRead]
    blackout_intervals: list[BlackoutIntervalRead]
    offered_starts: list[OfferedStartRead]
    bookable: bool

    @classmethod
    def build(cls, projection: DayProjection) -> "DayProjectionRead":
        return cls(
            date=projection.on_date,
            operating_intervals=[
                OperatingIntervalRead.build(interval) for interval in projection.operating_intervals
            ],
            blackout_intervals=[
                BlackoutIntervalRead.build(interval) for interval in projection.blackout_intervals
            ],
            offered_starts=[
                OfferedStartRead.build(offered) for offered in projection.offered_starts
            ],
            bookable=projection.bookable,
        )


# The longest rule-generation prompt the API accepts, capped here at the wire boundary rather
# than inside the runner. **A cap, not a filter.** The text becomes part of a model prompt and
# nothing anywhere sanitises, strips or escapes it, deliberately: the AST validator over the
# *generated source* is the boundary (``rules.safety.validate_source``, run at write time and
# again at every load). A rule that survives the validator, the adversarial suite and the sandbox
# is safe whatever its description said; a rule that does not never reaches the catalog however
# innocent its description read. This number exists because an unbounded string costs tokens and
# a Text column, not because a shorter one is a safer one.
_RULE_PROMPT_MAX = 500


class RuleDraftCreate(BaseModel):
    """The body of ``POST /spaces/{public_id}/rule-drafts``: one sentence of English."""

    prompt: str = Field(min_length=1, max_length=_RULE_PROMPT_MAX)


class RuleDraftRead(BaseModel):
    """One generation job, as the admin polling it sees it.

    ``generated_rule_type`` is the ``rule_type`` string rather than the row's integer id: it is
    what a ``space_rules`` row names, so it is the value the page needs in order to offer "add an
    instance of this" next, and the integer id would be one more thing to look up.

    ``attempts`` crosses the wire as stored — number, outcome and capped failure text per attempt.
    The prompts and completions behind them are **not** exposed by this API: they are an operator
    and prompt-tuning artifact (``rule_generation_exchanges``), read from the database, not
    something the admin who typed one sentence is owed a model's full reasoning transcript for.

    ``human_code`` is the verified rule's own source — present only once a job has succeeded, and
    absent (``None``) at every other status. Unlike the exchanges, this one *is* meant for the
    admin: they are about to enforce it on their members, and a disclosure they never have to open
    is better than a feature that runs code on their bookings and shows them nothing.
    """

    id: int
    prompt: str
    status: RuleGenerationJobStatus
    attempts: list
    error: Optional[str]
    generated_rule_type: Optional[str]
    human_code: Optional[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(
        cls,
        job: RuleGenerationJob,
        *,
        rule_type: Optional[str] = None,
        human_code: Optional[str] = None,
    ) -> "RuleDraftRead":
        return cls(
            id=job.id,
            prompt=job.prompt,
            status=job.status,
            attempts=list(job.attempts or []),
            error=job.error,
            generated_rule_type=rule_type,
            human_code=human_code,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


class ShapeConversationCreate(BaseModel):
    """Open a shape conversation.  The empty body is intentional: it starts from the live shape."""


class ShapeConversationTurnCreate(BaseModel):
    """One admin-authored message in a shape conversation."""

    message: str = Field(min_length=1, max_length=4000)


class CalendarShapePublish(BaseModel):
    """An explicit acknowledgement is required before publishing an unbookable shape."""

    allow_unbookable: bool = False


class ShapeVersionRead(BaseModel):
    """A stored shape version, with raw JSON preserved for the studio preview."""

    id: int
    document: dict
    status: str
    created_at: datetime
    source_conversation_id: Optional[int]

    @classmethod
    def build(cls, row: SpaceCalendarShape) -> "ShapeVersionRead":
        return cls(
            id=row.id,
            document=row.document,
            status=row.status.value,
            created_at=row.created_at,
            source_conversation_id=row.source_conversation_id,
        )


class ShapeMessageRead(BaseModel):
    """One persisted visible transcript message."""

    ordinal: int
    role: ShapeMessageRole
    content: str
    resulting_shape_version_id: Optional[int]
    created_at: datetime

    @classmethod
    def build(cls, message: SpaceShapeMessage) -> "ShapeMessageRead":
        return cls(
            ordinal=message.ordinal,
            role=message.role,
            content=message.content,
            resulting_shape_version_id=message.resulting_shape_version_id,
            created_at=message.created_at,
        )


class ShapeConversationRead(BaseModel):
    """The reload-safe conversation transcript and the Space's current draft, if any."""

    id: int
    status: ShapeConversationStatus
    created_at: datetime
    closed_at: Optional[datetime]
    messages: list[ShapeMessageRead]
    draft: Optional[ShapeVersionRead]

    @classmethod
    def build(
        cls,
        conversation: SpaceShapeConversation,
        messages: list[SpaceShapeMessage],
        draft: Optional[SpaceCalendarShape],
    ) -> "ShapeConversationRead":
        return cls(
            id=conversation.id,
            status=conversation.status,
            created_at=conversation.created_at,
            closed_at=conversation.closed_at,
            messages=[ShapeMessageRead.build(message) for message in messages],
            draft=ShapeVersionRead.build(draft) if draft is not None else None,
        )


class ShapeConversationTurnRead(BaseModel):
    """The immediate result of one synchronous model turn."""

    summary: str
    question: Optional[str]
    draft: ShapeVersionRead
