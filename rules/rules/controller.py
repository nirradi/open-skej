"""The rule engine's controller: run a canon of rules against one booking request.

``evaluate_request`` is the single entry point the backend calls. It has three jobs, in order:

1. **Cross-check the request against the context.** ``Context`` cannot do this itself — the request
   is not visible when a context is built — so this is the first place both are in scope.
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

    History is filtered to the requesting **user** — never to the requested resource. A caller may
    legitimately hand the engine one user's bookings drawn from several resources (an application
    scoping a frequency cap to every Resource in a Space rather than to the one being booked); the
    engine has no opinion on how wide that scope is, only on whose bookings it may be. The real
    invariant, and the only one checked here, is that every booking in ``context.history`` belongs
    to the user in ``request`` — mixing in another user's history would silently count it toward
    this user's limits, which is exactly what ``ContextMismatchError`` exists to catch instead of
    hiding it.

    A future ``Context`` may carry the Space itself, so a rule can decide its own scope rather than
    relying on how the caller pre-filtered history; that is not this check's job today.
    """
    if context.user.user_id != request.user_id:
        raise ContextMismatchError(
            f"Context is for user {context.user.user_id!r} but the request is from "
            f"{request.user_id!r}. The caller built the context for the wrong user."
        )

    for index, booking in enumerate(context.history.bookings):
        if booking.user_id != request.user_id:
            raise ContextMismatchError(
                f"Context.history.bookings[{index}] belongs to user {booking.user_id!r}, not the "
                f"requesting user {request.user_id!r}. History must be filtered to the requesting "
                "user before the context is built."
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
