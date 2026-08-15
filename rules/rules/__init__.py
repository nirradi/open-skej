"""Open-Skej rule engine."""

from .canon import (
    DEFAULT_CANON,
    AvailabilityHoursRule,
    BookingHorizonRule,
    MaxConsecutiveDurationRule,
    MaxDurationRule,
    NotInThePastRule,
    SessionLengthRule,
    default_canon,
)
from .controller import RULE_ERROR_MESSAGE, ContextMismatchError, evaluate_request
from .frequency import (
    MaxBookingsPerDayRule,
    MaxBookingsPerMonthRule,
    MaxBookingsPerWeekRule,
    MaxDurationPerDayRule,
)
from .interfaces import (
    HISTORY_ROLLING_WINDOW,
    BaseRule,
    BookingRecord,
    BookingRequest,
    CalendarContext,
    Context,
    HistoryContext,
    LocalFrame,
    RuleResult,
    RunContext,
    UserContext,
    Weekday,
    history_window,
)
from .registry import REGISTRY, ParamKind, RuleParam, RuleType, rule_types
from .safety import UnsafeRuleError, validate_source
from .spans import MergedSpan, merge_adjoining_spans

__all__ = [
    "BaseRule",
    "ContextMismatchError",
    "evaluate_request",
    "RULE_ERROR_MESSAGE",
    "BookingRecord",
    "BookingRequest",
    "CalendarContext",
    "Context",
    "HistoryContext",
    "LocalFrame",
    "RuleResult",
    "RunContext",
    "UserContext",
    "Weekday",
    "history_window",
    "HISTORY_ROLLING_WINDOW",
    "validate_source",
    "UnsafeRuleError",
    "NotInThePastRule",
    "BookingHorizonRule",
    "MaxDurationRule",
    "MaxConsecutiveDurationRule",
    "SessionLengthRule",
    "AvailabilityHoursRule",
    "default_canon",
    "DEFAULT_CANON",
    # Exported but deliberately not in DEFAULT_CANON — see frequency.py.
    "MaxBookingsPerDayRule",
    "MaxBookingsPerWeekRule",
    "MaxBookingsPerMonthRule",
    "MaxDurationPerDayRule",
    # The rule type registry — a separate, additive description of the same rule classes.
    "REGISTRY",
    "ParamKind",
    "RuleParam",
    "RuleType",
    "rule_types",
    # Interval-merging math shared by the run resolver and the frequency rules (task 8.6).
    "MergedSpan",
    "merge_adjoining_spans",
]
