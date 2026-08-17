"""Parses and validates a calendar shape document from untrusted JSON.

``validate_shape`` is the boundary between "text a chat turn produced" and the typed :class:`Shape`
everything else in this package reads. It returns the parsed value and there is no boolean for a
caller to forget to check -- the same shape ``rules.safety.validate_source`` already has, and for
the same reason: either the call returns a usable value, or it raised.

**Every message names the field it is unhappy with and the problem, and never paraphrases.** Task
10.6 feeds this message back to the model verbatim as the retry turn, exactly as
``rules.safety.UnsafeRuleError``'s own message is fed back to the generation loop -- so a message
here says ``operating_blocks[1].allowed_durations_mins: must not be empty``, not "your rule has an
error" or some friendlier gloss that costs the model the one detail that lets it fix the candidate.
The convention is mechanical: every path-qualified check raises with the field's own dotted/indexed
path, a colon, and the problem in plain words.

An **empty** document -- ``{"version": 1, "operating_blocks": [], "blackout_windows": []}`` -- is
**valid**, not an error. It is what a Space that has been told "closed until further notice" holds,
and :func:`shape.projection.project_day` reports every date of it as unbookable rather than this
module ever refusing it.

This module performs every check itself, with full path context, before constructing a single
:mod:`shape.types` dataclass -- the dataclasses' own ``__post_init__`` checks exist for a caller
that builds one directly, not because this module relies on catching them; a document that passes
every check here should never trip one of them, and if it does that is a bug in this module, not a
bad document.
"""

from __future__ import annotations

from datetime import date
from re import compile as re_compile
from typing import Any

from .types import DAY_CODES, BlackoutWindow, OperatingBlock, Shape

__all__ = ["InvalidShapeError", "validate_shape"]

_TIME_PATTERN = re_compile(r"^(\d{1,3}):([0-5]\d)$")
_MAX_START_MINUTES = 1439
_DAY_MINUTES = 1440

#: A sentinel distinct from ``None`` -- a document can legitimately write ``"date": null``, and
#: that must read as "not present" exactly like the key being absent, never as a type error.
_MISSING = object()


class InvalidShapeError(ValueError):
    """A shape document is not valid. The message names the field and the problem -- see the module
    docstring: this is retried verbatim against the model that produced the document."""


def _type_name(value: object) -> str:
    return type(value).__name__


def _field(document: dict, key: str) -> Any:
    """Return ``document[key]``, or ``_MISSING`` if absent or explicitly ``null``."""
    value = document.get(key, _MISSING)
    if value is None:
        return _MISSING
    return value


def _require_dict(value: object, path: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidShapeError(f"{path}: must be an object, got {_type_name(value)}")
    return value


def _require_list(document: dict, key: str, path: str) -> list:
    value = _field(document, key)
    if value is _MISSING:
        raise InvalidShapeError(f"{path}: is required")
    if not isinstance(value, list):
        raise InvalidShapeError(f"{path}: must be a list, got {_type_name(value)}")
    return value


def _parse_version(document: dict) -> int:
    value = _field(document, "version")
    if value is _MISSING:
        raise InvalidShapeError("version: is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidShapeError(f"version: must be an int, got {_type_name(value)}")
    if value < 1:
        raise InvalidShapeError(f"version: must be at least 1; got {value}")
    return value


def _parse_time(document: dict, key: str, path: str, *, required: bool) -> int | None:
    """Parse an ``"HH:MM"`` field into minutes past local midnight, or ``None`` if optional and
    absent. Does not enforce the pair's own ordering -- the caller does that once both bounds of a
    window are in hand, so the message can compare them rather than judge each in isolation."""
    field_path = f"{path}.{key}"
    value = _field(document, key)
    if value is _MISSING:
        if required:
            raise InvalidShapeError(f"{field_path}: is required")
        return None
    if not isinstance(value, str):
        raise InvalidShapeError(f"{field_path}: must be a string 'HH:MM', got {_type_name(value)}")
    match = _TIME_PATTERN.match(value)
    if match is None:
        raise InvalidShapeError(f"{field_path}: must be 'HH:MM' (24-hour); got {value!r}")
    hours, minutes = int(match.group(1)), int(match.group(2))
    return hours * 60 + minutes


def _check_start_time(minutes: int, field_path: str) -> None:
    if not (0 <= minutes <= _MAX_START_MINUTES):
        raise InvalidShapeError(
            f"{field_path}: must be within a single local day (00:00 up to but not including "
            f"24:00); got {minutes} minutes"
        )


def _check_time_pair(start: int, end: int, path: str) -> None:
    if end <= start:
        raise InvalidShapeError(
            f"{path}.end_time: must be after start_time; got end_time <= start_time "
            f"({end} <= {start} minutes)"
        )
    if end > start + _DAY_MINUTES:
        raise InvalidShapeError(
            f"{path}.end_time: must be at most 24h after start_time; got {end} minutes, "
            f"more than {start + _DAY_MINUTES} (start_time + 24h)"
        )


def _parse_days(document: dict, path: str, *, required: bool) -> frozenset[int] | None:
    field_path = f"{path}.days"
    value = _field(document, "days")
    if value is _MISSING:
        if required:
            raise InvalidShapeError(f"{field_path}: is required")
        return None
    if not isinstance(value, list):
        raise InvalidShapeError(
            f"{field_path}: must be a list of day codes, got {_type_name(value)}"
        )
    if not value:
        raise InvalidShapeError(f"{field_path}: must not be empty")
    seen: dict[int, str] = {}
    for index, raw_code in enumerate(value):
        if not isinstance(raw_code, str) or raw_code.upper() not in DAY_CODES:
            raise InvalidShapeError(
                f"{field_path}[{index}]: must be one of {sorted(DAY_CODES)}, got {raw_code!r}"
            )
        code = raw_code.upper()
        weekday = DAY_CODES[code]
        if weekday in seen:
            raise InvalidShapeError(f"{field_path}: duplicate day {code!r}")
        seen[weekday] = code
    return frozenset(seen)


def _parse_durations(document: dict, path: str) -> tuple[int, ...]:
    field_path = f"{path}.allowed_durations_mins"
    value = _field(document, "allowed_durations_mins")
    if value is _MISSING:
        raise InvalidShapeError(f"{field_path}: is required")
    if not isinstance(value, list):
        raise InvalidShapeError(
            f"{field_path}: must be a list of integers, got {_type_name(value)}"
        )
    if not value:
        raise InvalidShapeError(f"{field_path}: must not be empty")
    durations: list[int] = []
    for index, raw in enumerate(value):
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InvalidShapeError(f"{field_path}[{index}]: must be an int, got {_type_name(raw)}")
        if raw < 1:
            raise InvalidShapeError(f"{field_path}[{index}]: must be at least 1 minute; got {raw}")
        durations.append(raw)
    if len(set(durations)) != len(durations):
        raise InvalidShapeError(f"{field_path}: must not contain duplicates; got {value!r}")
    if durations != sorted(durations):
        raise InvalidShapeError(f"{field_path}: must be in ascending order; got {value!r}")
    return tuple(durations)


def _parse_date(document: dict, key: str, path: str) -> date | None:
    field_path = f"{path}.{key}"
    value = _field(document, key)
    if value is _MISSING:
        return None
    if not isinstance(value, str):
        raise InvalidShapeError(
            f"{field_path}: must be a string 'YYYY-MM-DD', got {_type_name(value)}"
        )
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidShapeError(f"{field_path}: must be 'YYYY-MM-DD'; got {value!r}") from None


def _check_effective_range(
    effective_from: date | None, effective_to: date | None, path: str
) -> None:
    if effective_from is not None and effective_to is not None and effective_from > effective_to:
        raise InvalidShapeError(
            f"{path}.effective_to: must not be before effective_from; got "
            f"effective_from={effective_from.isoformat()}, effective_to={effective_to.isoformat()}"
        )


def _validate_operating_block(raw: object, path: str) -> OperatingBlock:
    item = _require_dict(raw, path)
    days = _parse_days(item, path, required=True)
    start = _parse_time(item, "start_time", path, required=True)
    end = _parse_time(item, "end_time", path, required=True)
    _check_start_time(start, f"{path}.start_time")
    _check_time_pair(start, end, path)
    durations = _parse_durations(item, path)
    effective_from = _parse_date(item, "effective_from", path)
    effective_to = _parse_date(item, "effective_to", path)
    _check_effective_range(effective_from, effective_to, path)
    return OperatingBlock(
        days=days,
        start_time=start,
        end_time=end,
        allowed_durations_mins=durations,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _validate_blackout_window(raw: object, path: str) -> BlackoutWindow:
    item = _require_dict(raw, path)
    start = _parse_time(item, "start_time", path, required=True)
    end = _parse_time(item, "end_time", path, required=True)
    _check_start_time(start, f"{path}.start_time")
    _check_time_pair(start, end, path)

    reason = _field(item, "reason")
    if reason is _MISSING:
        raise InvalidShapeError(f"{path}.reason: is required")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidShapeError(f"{path}.reason: must not be empty")

    days = _parse_days(item, path, required=False)
    one_off_date = _parse_date(item, "date", path)
    if days is not None and one_off_date is not None:
        raise InvalidShapeError(
            f"{path}: must not carry both 'days' and 'date' -- a blackout is either recurring "
            "(days) or one-off (date), never both"
        )

    effective_from = _parse_date(item, "effective_from", path)
    effective_to = _parse_date(item, "effective_to", path)
    _check_effective_range(effective_from, effective_to, path)

    return BlackoutWindow(
        start_time=start,
        end_time=end,
        reason=reason,
        days=days,
        date=one_off_date,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def validate_shape(document: object) -> Shape:
    """Validate ``document`` (already-decoded JSON: dicts, lists, strings, ints, ``None``) and
    return the parsed :class:`~shape.types.Shape`.

    Raises :class:`InvalidShapeError` naming the first field that fails, in document order --
    ``operating_blocks`` before ``blackout_windows``, each in list order. There is no attempt to
    collect every problem in one pass: the message is fed back as a single retry turn (task 10.6),
    and a model handed five defects at once fixes none of them reliably. One defect, one retry,
    matching the generation loop's own ``UnsafeRuleError`` contract.
    """
    if not isinstance(document, dict):
        raise InvalidShapeError(f"document: must be an object, got {_type_name(document)}")

    version = _parse_version(document)

    raw_blocks = _require_list(document, "operating_blocks", "operating_blocks")
    blocks = tuple(
        _validate_operating_block(item, f"operating_blocks[{index}]")
        for index, item in enumerate(raw_blocks)
    )

    raw_blackouts = _require_list(document, "blackout_windows", "blackout_windows")
    blackouts = tuple(
        _validate_blackout_window(item, f"blackout_windows[{index}]")
        for index, item in enumerate(raw_blackouts)
    )

    return Shape(version=version, operating_blocks=blocks, blackout_windows=blackouts)
