"""The rule engine's controller: run a canon of rules against one booking request.

``evaluate_request`` is the single entry point the backend calls. It has three jobs, in order:

1. **Cross-check the request against the context.** ``Context`` cannot fully do this itself — the
   request is not visible when a context is built — so this is the first place both are in scope.
   Two things are checked here: that ``context.user`` is the request's own user, and that
   ``context.local`` and ``context.run`` were both resolved for this request rather than some other
   booking. (A third thing — that every entry in ``context.history`` belongs to the same user as
   ``context.user`` — needs no request at all and is enforced earlier, at ``Context`` construction
   itself; see ``interfaces.Context.__post_init__``. Together the two checks still guarantee history
   belongs to the requester, they just no longer need to be the same check.)
2. **Run the canon in order, fail-fast.** The first rule that denies wins and nothing after it runs.
3. **Contain a buggy rule.** A rule that raises becomes a denial with generic copy, logged with the
   real exception. A bug in one rule must never 500 the booking endpoint, and must never leak a
   traceback into text the UI shows verbatim.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .interfaces import BaseRule, BookingRequest, Context, RuleResult

__all__ = [
    "evaluate_request",
    "ContextMismatchError",
    "RULE_ERROR_MESSAGE",
]

logger = logging.getLogger(__name__)

#: Shown verbatim to the user when a rule raises. Deliberately says nothing about *which* rule or
#: *why* — ``fail_reason`` is user-facing copy, and the diagnostics belong in the log instead.
RULE_ERROR_MESSAGE = "We couldn't check this booking right now. Please try again in a moment."


class ContextMismatchError(ValueError):
    """The context does not describe the request it was passed with.

    A caller bug, not a user error, so it is raised rather than turned into a denial: a context
    holding another user's bookings would silently count them toward this user's limits, and
    answering "denied" would hide that. Fail loudly and let it reach the error tracker.
    """


def _check_context_matches_request(request: BookingRequest, context: Context) -> None:
    """Raise ``ContextMismatchError`` unless ``context`` genuinely describes ``request``.

    ``context.user`` must be the request's own user: ``Context`` cannot check this itself, since it
    has no visibility into ``request`` at construction time. Once this holds, and combined with the
    guarantee ``Context.__post_init__`` already enforces — that every booking in ``context.history``
    belongs to ``context.user`` — history transitively belongs to the requester too, without this
    function needing to walk it a second time. History stays filtered to the **user**, never to the
    requested resource: a caller may legitimately hand the engine one user's bookings drawn from
    several resources (an application scoping a frequency cap to every Resource in a Space rather
    than to the one being booked); the engine has no opinion on how wide that scope is, only on
    whose bookings it may be.

    ``context.local`` is checked the same way and for the same reason. A frame resolved for the
    wrong date is a frame whose day, week and month bounds all describe a different stretch of the
    calendar than the booking sits in, so a rule counting "bookings in this local day" would quietly
    count another day's. Only ``start_at`` is required to land inside ``[day_start, day_end)``:
    ``end_at`` may legitimately fall past the local day, which is exactly what an ``end_minutes``
    above 1440 means.

    ``context.run`` is checked the same way too: a run that does not contain its own request
    describes a different stretch of the calendar than the booking sits in, so a rule reading
    ``run.duration`` or ``run.booking_count`` would silently judge someone else's session — the
    adapter resolved a run for the wrong booking, or resolved it for a request it was never handed
    at all. Both bounds are required: ``run.start_at`` may run earlier than the request (a run this
    booking extends) and ``run.end_at`` may run later (a run this booking is merely part of), but
    the request's own span must fall entirely inside it either way.

    A future ``Context`` may carry the Space itself, so a rule can decide its own scope rather than
    relying on how the caller pre-filtered history; that is not this check's job today.
    """
    if context.user.user_id != request.user_id:
        raise ContextMismatchError(
            f"Context is for user {context.user.user_id!r} but the request is from "
            f"{request.user_id!r}. The caller built the context for the wrong user."
        )

    local = context.local
    if not local.day_start <= request.start_at < local.day_end:
        raise ContextMismatchError(
            f"Context.local describes the local day [{local.day_start}, {local.day_end}), but the "
            f"request starts at {request.start_at}. The caller resolved the frame for the wrong "
            "date, so every local bound in it belongs to another day."
        )

    run = context.run
    if not (run.start_at <= request.start_at and request.end_at <= run.end_at):
        raise ContextMismatchError(
            f"Context.run describes the span [{run.start_at}, {run.end_at}], but the request spans "
            f"[{request.start_at}, {request.end_at}). The caller resolved a run that does not "
            "contain its own request, so a rule reading it would judge someone else's session."
        )


def evaluate_request(
    request: BookingRequest,
    context: Context,
    canon: Iterable[BaseRule],
) -> RuleResult:
    """Evaluate ``request`` against every rule in ``canon``, in order, stopping at the first denial.

    Returns the denying rule's own ``RuleResult`` — its ``fail_reason`` is the copy the user sees —
    or ``RuleResult.allow()`` if every rule passed. An empty canon passes: no rules means no
    constraints, not an implicit denial.

    Raises ``ContextMismatchError`` if ``context`` does not describe ``request``. Every other
    exception raised by a rule is contained and converted into a denial.
    """
    _check_context_matches_request(request, context)

    for rule in canon:
        try:
            result = rule.evaluate(request, context)
        except Exception:
            # The rule name goes to the log, never to ``fail_reason``.
            logger.exception(
                "Rule %s raised while evaluating a booking request for user %s on resource %s; "
                "denying the request.",
                _rule_name(rule),
                request.user_id,
                request.resource_id,
            )
            return RuleResult.deny(RULE_ERROR_MESSAGE)

        if not isinstance(result, RuleResult):
            logger.error(
                "Rule %s returned %s, not a RuleResult; denying the request.",
                _rule_name(rule),
                type(result).__name__,
            )
            return RuleResult.deny(RULE_ERROR_MESSAGE)

        if not result.passed:
            return result

    return RuleResult.allow()


def _rule_name(rule: object) -> str:
    """Best-effort identifier for logs. Must not raise — it is used on the error path."""
    try:
        name = getattr(rule, "name", None)
        if isinstance(name, str) and name:
            return name
    except Exception:  # pragma: no cover - a property that raises is pathological
        pass
    return type(rule).__name__
