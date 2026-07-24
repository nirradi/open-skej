"""Deterministic sandbox seed: the INTERESTING states, not a lone happy user.

Playwright (task 4.9) and manual QA both need a sandbox that exercises the
things a single "owner books a slot" flow never touches: another Space the
caller is *not* in, a Resource with different hours than its sibling, a
pending access request and a pending invitation sitting in an admin's queue,
and a Space that is already finished. This module plants exactly that,
built through ``app.identity.service`` wherever a service function exists so
the same invariants a real request would enforce also hold in the sandbox
(an owner membership is never skipped, an archived Space takes no new rows).

Run it with ``python -m app.sandbox_seed`` against a migrated
``DATABASE_URL``, the same convention ``app.db.bootstrap`` established for
the default booking target.

## The deterministic identities

Four users, each with a stable ``auth0_sub`` — the identity
``mint_sandbox_token`` (``app.auth.sandbox``) mints a token for, so 4.9 and a
human both authenticate *as* one of these rather than discovering an id at
run time:

* ``OWNER_AUTH0_SUB`` / ``OWNER_EMAIL`` — owns Space A and Space B and the
  archived Space.
* ``ADMIN_AUTH0_SUB`` / ``ADMIN_EMAIL`` — admin of Space A only.
* ``MEMBER_AUTH0_SUB`` / ``MEMBER_EMAIL`` — member of Space A only; absent
  from Space B, which is what makes cross-tenant isolation observable.
* ``STRANGER_AUTH0_SUB`` / ``STRANGER_EMAIL`` — a cold link-holder, in no
  Space at all, who holds a pending access request against Space A.

## The two Spaces

* ``SPACE_A_NAME`` — zone ``SPACE_A_TIMEZONE`` (``Europe/Berlin``, chosen
  because it is not UTC: Playwright pins the browser clock to UTC, which is
  exactly the setting that would hide a timezone bug if every Space in the
  sandbox were UTC too). Owner + admin + member. Two Resources with
  different ``opens_at``/``closes_at``/``slot_minutes`` — the canon is still
  module-level literals until 4.13, so the two differ in hours and slot
  length only, never in rule parameters.
* ``SPACE_B_NAME`` — zone ``SPACE_B_TIMEZONE`` (UTC), one Resource, and
  neither the member nor the stranger has a membership row in it. A member
  of Space A must get 404 here, never 403.

## Reset, not accumulate

Re-running this module must yield the same row counts, not doubled ones.
``_reset`` deletes every application row in FK-safe order before anything is
(re)inserted — this is a disposable sandbox database, so unlike the
production "nothing is deleted" invariant (see ``CLAUDE.md``), a seed
script wiping its own fixtures is exactly the point. The default booking
target (``DEFAULT_USER_ID`` / ``DEFAULT_RESOURCE_ID``, both id ``1``) is
then replanted by ``ensure_booking_defaults`` before anything else, so the
still-unscoped ``POST /bookings`` always has a row to point at, in this
seed's output as much as in ``app.db.bootstrap``'s.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from app.db.bootstrap import ensure_booking_defaults
from app.db.models import Booking, BookingStatus
from app.identity import service
from app.identity.models import (
    MembershipRole,
    Resource,
    Space,
    SpaceAccessRequest,
    SpaceInvitation,
    SpaceMembership,
    User,
)
from app.identity.schemas import ResourceUpdate

# --- Deterministic identities -------------------------------------------------
# The ``sandbox|`` prefix mirrors ``app.db.bootstrap``'s ``bootstrap|`` one: a
# namespace no real Auth0 ``sub`` can collide with (a real one is always
# ``auth0|...`` or a social provider's own prefix), and the ``@sandbox.
# open-skej.local`` domain matches the host ``app.auth.sandbox.SANDBOX_ISSUER``
# already uses, so nothing here can be mistaken for a real account.

OWNER_AUTH0_SUB = "sandbox|owner"
OWNER_EMAIL = "owner@sandbox.open-skej.local"

ADMIN_AUTH0_SUB = "sandbox|admin"
ADMIN_EMAIL = "admin@sandbox.open-skej.local"

MEMBER_AUTH0_SUB = "sandbox|member"
MEMBER_EMAIL = "member@sandbox.open-skej.local"

# Holds a link (Space A's ``public_id``, read from the running sandbox) but no
# membership anywhere — the cold link-holder path, and the identity a pending
# access request is filed under.
STRANGER_AUTH0_SUB = "sandbox|stranger"
STRANGER_EMAIL = "stranger@sandbox.open-skej.local"

# An address with no ``users`` row at all, invited but never having logged in
# — the pending-invitation state. Deliberately not one of the four subs above:
# an invitation is keyed on email precisely because the invitee may not exist
# yet, and giving this one a login identity would collapse that distinction.
PENDING_INVITEE_EMAIL = "invitee@sandbox.open-skej.local"

# --- Space A: non-UTC, two differently configured Resources ------------------

SPACE_A_NAME = "Sandbox Space A (Berlin)"
SPACE_A_DESCRIPTION = "Owner + admin + member; two Resources with different hours."
SPACE_A_TIMEZONE = "Europe/Berlin"

RESOURCE_A1_NAME = "Court 1 (long hours)"
RESOURCE_A1_OPENS_AT = time(6, 0)
RESOURCE_A1_CLOSES_AT = time(22, 0)
RESOURCE_A1_SLOT_MINUTES = 60

RESOURCE_A2_NAME = "Court 2 (business hours)"
RESOURCE_A2_OPENS_AT = time(9, 0)
RESOURCE_A2_CLOSES_AT = time(17, 0)
RESOURCE_A2_SLOT_MINUTES = 30

# --- Space B: a different tenant, unreachable by the member or the stranger --

SPACE_B_NAME = "Sandbox Space B (UTC)"
SPACE_B_DESCRIPTION = "Owned by the same owner as Space A; nobody else is in it."
SPACE_B_TIMEZONE = "UTC"
# Its one Resource is the auto-created first Resource `create_space` always
# gives a fresh Space (`service.FIRST_RESOURCE_NAME`) — Space B needs nothing
# more than that to demonstrate cross-tenant isolation, so nothing here adds a
# second one.

# --- The archived Space -------------------------------------------------------

ARCHIVED_SPACE_NAME = "Sandbox Archived Space"
ARCHIVED_SPACE_DESCRIPTION = "Created and immediately archived; reads work, writes 409."


def _reset(session: Session) -> None:
    """Delete every application row, in FK-safe order.

    Children before parents: bookings before the Resources and users they
    reference, the three per-Space queues before the Spaces and users they
    reference, Resources before Spaces, Spaces before users (``created_by_
    user_id``), users last. One transaction, so a second run never observes a
    half-wiped database. This is a disposable sandbox, not the production
    schema this repository otherwise never deletes from — see the module
    docstring.
    """
    session.execute(delete(Booking))
    session.execute(delete(SpaceAccessRequest))
    session.execute(delete(SpaceInvitation))
    session.execute(delete(SpaceMembership))
    session.execute(delete(Resource))
    session.execute(delete(Space))
    session.execute(delete(User))
    session.commit()


def _sync_sequence_past_explicit_defaults(session: Session) -> None:
    """Advance ``users``/``spaces``/``resources``' id sequences past 1.

    ``ensure_booking_defaults`` plants each of those three rows with an
    *explicit* primary key of 1, and a plain ``nextval()``-backed sequence has
    no idea that happened — it still hands out 1 the first time something
    else in the same table asks for a fresh id, colliding with the row that is
    already there. Every other Postgres-only fixture in this repository
    sidesteps the collision by simply never mixing an explicit id with an
    auto-generated one in the same table; this seed does both (the default
    row, then the sandbox's own auto-id users and Spaces), so it has to fix
    the sequence up itself rather than inherit that luck.
    """
    for table in ("users", "spaces", "resources"):
        session.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
            )
        )
    session.commit()


def _seed_user(session: Session, *, auth0_sub: str, email: str, name: str) -> User:
    """A ``users`` row inserted directly, matching ``ensure_booking_defaults``.

    There is no ``service.create_user`` — in production a user is only ever
    provisioned just-in-time from a verified JWT (``app.auth.dependencies.
    _upsert_user``), which this seed has no token to run through. Constructing
    the row directly is the same shortcut ``app.db.bootstrap`` already takes
    for the default user.
    """
    user = User(auth0_sub=auth0_sub, email=email, name=name)
    session.add(user)
    session.flush()
    return user


def _add_membership(session: Session, space: Space, user: User, role: MembershipRole) -> None:
    """Join a user to a Space at a role, bypassing the invite/request flow.

    There is no service function for "add this membership directly" — every
    production path to one goes through an accepted invitation or an approved
    access request, both of which need a second identity or a login this seed
    is not simulating. This mirrors how the existing Postgres test suite
    (``tests/test_spaces_api.py``) builds its own membership fixtures: a plain
    row, since none of :mod:`app.identity.service`'s invariants (an owner
    always exists, a Space accepts no new member once archived) are at risk
    for a Space that is freshly created and already has its owner.
    """
    session.add(SpaceMembership(space_id=space.id, user_id=user.id, role=role))
    session.commit()


def _seed_future_bookings(session: Session, resource: Resource, member: User) -> None:
    """Two confirmed bookings inside Resource A1's hours, so the calendar a
    manual QA session opens is not empty.

    Anchored on "tomorrow" and "the day after" rather than a fixed date so the
    seed stays useful indefinitely; computed in Space A's zone and converted
    to UTC at the boundary like everything else here, so both actually land
    inside ``RESOURCE_A1_OPENS_AT``/``CLOSES_AT`` however DST falls on the day
    the seed happens to run.
    """
    tz = ZoneInfo(SPACE_A_TIMEZONE)
    today = datetime.now(tz).date()

    def _slot(on: date, start_local: time, end_local: time) -> Booking:
        start_utc = datetime.combine(on, start_local, tzinfo=tz).astimezone(timezone.utc)
        end_utc = datetime.combine(on, end_local, tzinfo=tz).astimezone(timezone.utc)
        return Booking(
            resource_id=resource.id,
            user_id=member.id,
            start_at=start_utc,
            end_at=end_utc,
            status=BookingStatus.CONFIRMED,
        )

    session.add_all(
        [
            _slot(today + timedelta(days=1), time(10, 0), time(11, 0)),
            _slot(today + timedelta(days=2), time(14, 0), time(15, 0)),
        ]
    )
    session.commit()


def run(session: Session) -> None:
    """Reset the sandbox and (re)plant every interesting state, once.

    Order matters only where a later step reads an earlier one's output
    (Space A must exist before a Resource is added to it); it is otherwise
    the order the module docstring lists the states in.
    """
    _reset(session)
    ensure_booking_defaults(session)
    _sync_sequence_past_explicit_defaults(session)

    owner = _seed_user(session, auth0_sub=OWNER_AUTH0_SUB, email=OWNER_EMAIL, name="Sandbox Owner")
    admin = _seed_user(session, auth0_sub=ADMIN_AUTH0_SUB, email=ADMIN_EMAIL, name="Sandbox Admin")
    member = _seed_user(
        session, auth0_sub=MEMBER_AUTH0_SUB, email=MEMBER_EMAIL, name="Sandbox Member"
    )
    stranger = _seed_user(
        session, auth0_sub=STRANGER_AUTH0_SUB, email=STRANGER_EMAIL, name="Sandbox Stranger"
    )
    session.commit()

    # Space A: non-UTC, owner + admin + member, two differently configured
    # Resources. `create_space` takes no timezone argument (the config UI is
    # task 4.12), so the zone is set directly on the row it returns.
    space_a = service.create_space(
        session, owner, name=SPACE_A_NAME, description=SPACE_A_DESCRIPTION
    )
    space_a.timezone = SPACE_A_TIMEZONE
    session.commit()

    _add_membership(session, space_a, admin, MembershipRole.ADMIN)
    _add_membership(session, space_a, member, MembershipRole.MEMBER)

    # `create_space` auto-creates one Resource ("Main"); configure it as A1
    # rather than leaving it unconfigured, so both of Space A's Resources
    # carry hours and the difference between them is visible.
    (resource_a1,) = service.list_resources(session, space_a, include_archived=False)
    service.update_resource(
        session,
        space_a,
        resource_id=resource_a1.id,
        payload=ResourceUpdate(
            name=RESOURCE_A1_NAME,
            opens_at=RESOURCE_A1_OPENS_AT,
            closes_at=RESOURCE_A1_CLOSES_AT,
            slot_minutes=RESOURCE_A1_SLOT_MINUTES,
        ),
    )
    service.create_resource(
        session,
        space_a,
        name=RESOURCE_A2_NAME,
        opens_at=RESOURCE_A2_OPENS_AT,
        closes_at=RESOURCE_A2_CLOSES_AT,
        slot_minutes=RESOURCE_A2_SLOT_MINUTES,
    )

    # Space B: a second tenant the member and stranger have no row in at all,
    # so a 404 (never 403) on any of its routes is observable from either.
    space_b = service.create_space(
        session, owner, name=SPACE_B_NAME, description=SPACE_B_DESCRIPTION
    )
    space_b.timezone = SPACE_B_TIMEZONE
    session.commit()

    # A pending access request: the stranger asks to get into Space A.
    service.request_access(
        session, space_a, stranger, message="Found the link, would like to join."
    )

    # A pending invitation: an address with no `users` row yet, pre-approved
    # for Space A by its owner.
    service.create_invitation(
        session,
        space_a,
        owner,
        email=PENDING_INVITEE_EMAIL,
        role=MembershipRole.MEMBER,
        inviter_role=MembershipRole.OWNER,
    )

    # An archived Space: created, then immediately ended. Reads still work;
    # every mutation on it is refused with 409.
    archived_space = service.create_space(
        session, owner, name=ARCHIVED_SPACE_NAME, description=ARCHIVED_SPACE_DESCRIPTION
    )
    service.archive_space(session, archived_space)

    # A couple of future bookings on Space A's first Resource, so a manual QA
    # calendar is not empty on first look.
    _seed_future_bookings(session, resource_a1, member)


def main() -> None:
    """Run the seed against ``DATABASE_URL`` — the entry point for ``-m``."""
    from app.db.session import get_session_factory

    with get_session_factory()() as session:
        run(session)


if __name__ == "__main__":
    main()
