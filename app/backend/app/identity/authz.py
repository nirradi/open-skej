"""``require_space_role`` — the per-Space authorization dependency.

This module answers one question for every Space-scoped route: *may this caller
do this here?* Two decisions in it are load-bearing and neither is arbitrary.

**A caller with no membership gets 404, never 403.**

Spaces are not discoverable. There is no endpoint that lists them, so the only
way to reach one is to be handed its ``public_id`` — the link *is* the
capability. A 403 would confirm that a Space with that ``public_id`` exists,
which turns every capability URL into an oracle: an attacker with a candidate id
could ask, and the status code would answer. Even at 128 bits of entropy that is
the wrong shape of API to expose, and it leaks far more in the cases that
actually happen — a link forwarded to the wrong person, or an id lifted from a
browser history or a proxy log, where the question is not "can this be guessed?"
but "is this id still live?"

So a non-member must be unable to distinguish *this Space does not exist* from
*this Space exists and I am not in it*. Both paths raise the identical exception
with the identical body, and the single query below means they also take the
same route through the code.

A caller who **is** a member but lacks the required role gets a genuine **403**.
At that point they already know the Space exists — they are in it — so there is
nothing left to conceal, and 404 would only be confusing.

**Role ordering is an explicit table.** ``MembershipRole`` is a ``str`` enum, so
comparing two roles directly compares their *strings*, under which
``"admin" < "member" < "owner"`` — alphabetical order, which puts ``member``
above ``admin`` and would quietly grant every member admin authority. Declaration
order is no safer: it is invisible at the comparison site and one reordered line
away from the same bug. ``_ROLE_RANK`` states the hierarchy once, where it can be
read and tested.
"""

from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.session import get_session
from app.identity.models import MembershipRole, Space, SpaceMembership, User

# Higher rank means more authority. Gaps between the values leave room for a role
# to be slotted in later without renumbering the others.
_ROLE_RANK: dict[MembershipRole, int] = {
    MembershipRole.MEMBER: 10,
    MembershipRole.ADMIN: 20,
    MembershipRole.OWNER: 30,
}

# One message for both "no such Space" and "not your Space". If these differed,
# the distinction the 404 exists to hide would leak straight back out in the body.
SPACE_NOT_FOUND_DETAIL = "Space not found"


def role_rank(role: MembershipRole) -> int:
    """This role's authority as a comparable integer."""
    return _ROLE_RANK[role]


def role_at_least(role: MembershipRole, minimum: MembershipRole) -> bool:
    """Does ``role`` carry at least the authority of ``minimum``?"""
    return role_rank(role) >= role_rank(minimum)


def space_not_found() -> HTTPException:
    """The response a caller outside a Space gets, whatever the real reason."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=SPACE_NOT_FOUND_DETAIL)


@dataclass(frozen=True)
class SpaceContext:
    """The resolved answer to "who is calling, about which Space, as what?".

    Handlers take this instead of re-querying: the dependency has already loaded
    the Space and proven the caller's role, and a second lookup in the handler
    would be both a wasted round trip and a chance for the two to disagree.
    """

    space: Space
    membership: SpaceMembership
    user: User

    @property
    def role(self) -> MembershipRole:
        return self.membership.role


def lookup_space(session: Session, public_id: str) -> Space | None:
    """The Space bearing this ``public_id``, or ``None``.

    Used by ``/preview``, the one route reachable without a membership.
    """
    return session.execute(select(Space).where(Space.public_id == public_id)).scalar_one_or_none()


def require_space_role(minimum: MembershipRole) -> Callable[..., SpaceContext]:
    """A dependency resolving the caller's membership in the addressed Space.

    Returns a :class:`SpaceContext` when the caller is a member of at least
    ``minimum``; otherwise raises 404 (not a member at all) or 403 (a member,
    but not senior enough). See the module docstring for why those two are not
    the same status code.

    Written as a factory rather than one dependency taking a role argument
    because FastAPI resolves dependency parameters from the request — a plain
    argument would become a query parameter, and the caller would get to choose
    which role to require.
    """

    def dependency(
        public_id: str,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> SpaceContext:
        # One outer-joined query rather than "load the Space, then load the
        # membership". Two queries would return early on a missing Space and so
        # take measurably less time than the not-a-member path, which is the same
        # oracle the shared 404 exists to close, just measured with a stopwatch.
        row = session.execute(
            select(Space, SpaceMembership)
            .outerjoin(
                SpaceMembership,
                and_(
                    SpaceMembership.space_id == Space.id,
                    SpaceMembership.user_id == user.id,
                ),
            )
            .where(Space.public_id == public_id)
        ).one_or_none()

        if row is None:
            raise space_not_found()

        space, membership = row
        if membership is None:
            # The Space exists, but saying so would confirm the id. 404.
            raise space_not_found()

        if not role_at_least(membership.role, minimum):
            # They are inside the Space and know it exists, so 403 conceals
            # nothing and is the honest answer.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the {minimum.value} role in this Space",
            )

        return SpaceContext(space=space, membership=membership, user=user)

    return dependency
