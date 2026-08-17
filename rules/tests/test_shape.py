"""The calendar shape's document, validator and projection -- the requirements file's own three
worked examples are the acceptance criteria (`ops/plans/stream-10/10.1-shape-document-and-
projection.md`, "Tests"), plus the seasonal/one-off cases, the past-midnight representation, the
empty document, the overlapping-blocks union, and one rejection per invalid construct.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from shape import (
    DEFAULT_SHAPE,
    InvalidBookingRequestError,
    InvalidShapeError,
    permits,
    project_day,
    validate_shape,
)


def _doc(**overrides):
    document = {"version": 1, "operating_blocks": [], "blackout_windows": []}
    document.update(overrides)
    return document


def _block(**overrides):
    block = {
        "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        "start_time": "09:00",
        "end_time": "17:00",
        "allowed_durations_mins": [60],
    }
    block.update(overrides)
    return block


def _blackout(**overrides):
    blackout = {"start_time": "10:00", "end_time": "10:20", "reason": "Break"}
    blackout.update(overrides)
    return blackout


# --- the three worked examples --------------------------------------------------------------


class TestTheTeacher:
    """The teacher: "20 minute slots from 18 to 20, a break for 10 min at 1930" -- the requirements
    file's own worked example, reproduced in ``shape.projection``'s own module docstring."""

    SHAPE = validate_shape(
        _doc(
            operating_blocks=[
                _block(
                    days=["MON"],
                    start_time="18:00",
                    end_time="20:00",
                    allowed_durations_mins=[20],
                )
            ],
            blackout_windows=[_blackout(start_time="19:30", end_time="19:40", reason="Break")],
        )
    )
    ON_DATE = date(2026, 8, 17)  # a Monday

    def test_offered_starts_are_the_20_minute_grid_minus_the_straddling_slot(self):
        projection = project_day(self.SHAPE, self.ON_DATE)
        starts = [s.start_minutes for s in projection.offered_starts]
        # 18:00, 18:20, 18:40, 19:00, 19:20, 19:40 is the raw grid; 19:20 would run to 19:40 and
        # straddle the 19:30-19:40 break, so it alone is missing.
        assert starts == [18 * 60, 18 * 60 + 20, 18 * 60 + 40, 19 * 60, 19 * 60 + 40]
        assert 19 * 60 + 20 not in starts

    def test_each_offered_start_grants_the_20_minute_duration(self):
        projection = project_day(self.SHAPE, self.ON_DATE)
        for offered in projection.offered_starts:
            assert offered.durations_mins == (20,)

    def test_permits_the_offered_starts_and_refuses_the_straddling_one(self):
        assert permits(self.SHAPE, self.ON_DATE, 18 * 60, 18 * 60 + 20).allowed
        assert permits(self.SHAPE, self.ON_DATE, 19 * 60 + 40, 20 * 60).allowed

        verdict = permits(self.SHAPE, self.ON_DATE, 19 * 60 + 20, 19 * 60 + 40)
        assert not verdict.allowed
        assert "Break" in verdict.reason

    def test_a_day_the_block_does_not_govern_is_unbookable(self):
        projection = project_day(self.SHAPE, date(2026, 8, 18))  # a Tuesday
        assert projection.bookable is False
        assert projection.offered_starts == ()


class TestTheLab:
    """The lab: "8 to 5pm, three 20 min cooldowns at 10, 13 and 15, 30 min slots"."""

    SHAPE = validate_shape(
        _doc(
            operating_blocks=[
                _block(
                    days=["WED"],
                    start_time="08:00",
                    end_time="17:00",
                    allowed_durations_mins=[30],
                )
            ],
            blackout_windows=[
                _blackout(start_time="10:00", end_time="10:20", reason="Cooldown"),
                _blackout(start_time="13:00", end_time="13:20", reason="Cooldown"),
                _blackout(start_time="15:00", end_time="15:20", reason="Cooldown"),
            ],
        )
    )
    ON_DATE = date(2026, 8, 19)  # a Wednesday

    def test_each_cooldown_removes_only_the_slot_that_would_straddle_it(self):
        # The 08:00-anchored 30-minute grid lands exactly on 10:00, 13:00 and 15:00, so each
        # cooldown straddles precisely the slot beginning at its own start time — 10:00-10:30
        # overlaps the 10:00-10:20 cooldown, and neither 09:30-10:00 nor 10:30-11:00 does.
        projection = project_day(self.SHAPE, self.ON_DATE)
        starts = {s.start_minutes for s in projection.offered_starts}

        assert 10 * 60 not in starts
        assert 9 * 60 + 30 in starts
        assert 10 * 60 + 30 in starts

        assert 13 * 60 not in starts
        assert 12 * 60 + 30 in starts
        assert 13 * 60 + 30 in starts

        assert 15 * 60 not in starts
        assert 14 * 60 + 30 in starts
        assert 15 * 60 + 30 in starts

    def test_the_residual_ten_minutes_after_a_cooldown_is_never_offered_alone(self):
        projection = project_day(self.SHAPE, self.ON_DATE)
        # 10:20-10:30 is not on the 08:00-anchored 30-minute grid at all, so it never appears as
        # its own candidate regardless of duration.
        assert all(s.start_minutes != 10 * 60 + 20 for s in projection.offered_starts)


class TestTheMusicRoom:
    """The music room: "1 hour sessions in the morning until 1400, one or two hour sessions in the
    evening until 2200" -- two operating blocks sharing a boundary, never merged
    (`OperatingInterval`'s own docstring), each offering an independent grid."""

    SHAPE = validate_shape(
        _doc(
            operating_blocks=[
                _block(
                    days=["THU"],
                    start_time="08:00",
                    end_time="14:00",
                    allowed_durations_mins=[60],
                ),
                _block(
                    days=["THU"],
                    start_time="14:00",
                    end_time="22:00",
                    allowed_durations_mins=[60, 120],
                ),
            ],
        )
    )
    ON_DATE = date(2026, 8, 20)  # a Thursday

    def test_the_two_blocks_stay_separate_operating_intervals(self):
        projection = project_day(self.SHAPE, self.ON_DATE)
        assert len(projection.operating_intervals) == 2

    def test_a_two_hour_booking_at_1pm_is_refused(self):
        # 13:00-15:00 is not reachable from the morning block (only 1h, and it would run past
        # 14:00) nor from the evening block (it does not start on that block's own 14:00 grid).
        verdict = permits(self.SHAPE, self.ON_DATE, 13 * 60, 15 * 60)
        assert not verdict.allowed

    def test_a_two_hour_booking_at_3pm_is_offered(self):
        verdict = permits(self.SHAPE, self.ON_DATE, 15 * 60, 17 * 60)
        assert verdict.allowed

    def test_a_one_hour_booking_in_the_morning_is_offered_but_two_hours_is_not(self):
        assert permits(self.SHAPE, self.ON_DATE, 9 * 60, 10 * 60).allowed
        assert not permits(self.SHAPE, self.ON_DATE, 9 * 60, 11 * 60).allowed


# --- overlapping blocks: the union, not the AND ---------------------------------------------


def test_overlapping_blocks_union_their_durations_in_the_merged_interval():
    shape = validate_shape(
        _doc(
            operating_blocks=[
                _block(
                    days=["FRI"],
                    start_time="08:00",
                    end_time="12:00",
                    allowed_durations_mins=[60],
                ),
                _block(
                    days=["FRI"],
                    start_time="10:00",
                    end_time="14:00",
                    allowed_durations_mins=[90],
                ),
            ]
        )
    )
    on_date = date(2026, 8, 21)  # a Friday
    projection = project_day(shape, on_date)

    assert len(projection.operating_intervals) == 1
    merged = projection.operating_intervals[0]
    assert merged.start_minutes == 8 * 60
    assert merged.end_minutes == 14 * 60
    assert merged.allowed_durations_mins == (60, 90)

    # Each block still grants only its own duration on its own grid -- the union describes the
    # structural interval, not that every minute now offers every duration.
    assert permits(shape, on_date, 8 * 60, 9 * 60).allowed  # 60 mins, morning block's own grid
    assert not permits(shape, on_date, 8 * 60, 8 * 60 + 90).allowed  # 90 mins off the 08:00 grid
    assert permits(shape, on_date, 10 * 60, 10 * 60 + 90).allowed  # 90 mins, evening block's grid


# --- seasonal and one-off cases ---------------------------------------------------------------


def test_a_block_scoped_to_a_season_does_not_apply_outside_it():
    shape = validate_shape(
        _doc(
            operating_blocks=[
                _block(
                    days=["MON"],
                    start_time="08:00",
                    end_time="20:00",
                    effective_from="2026-01-01",
                    effective_to="2026-05-31",
                )
            ]
        )
    )
    assert project_day(shape, date(2026, 3, 2)).bookable is True  # a Monday, inside the range
    assert project_day(shape, date(2026, 6, 1)).bookable is False  # a Monday, just past it


def test_effective_to_is_inclusive_of_its_own_date():
    shape = validate_shape(
        _doc(
            operating_blocks=[
                _block(
                    days=["SUN"],
                    start_time="08:00",
                    end_time="20:00",
                    effective_from="2026-01-01",
                    effective_to="2026-05-31",
                )
            ]
        )
    )
    # 2026-05-31 is a Sunday and is the last day the range names -- it must still be bookable.
    assert project_day(shape, date(2026, 5, 31)).bookable is True


def test_a_one_off_blackout_applies_only_on_its_own_date():
    shape = validate_shape(
        _doc(
            operating_blocks=[_block(days=["FRI"], start_time="08:00", end_time="20:00")],
            blackout_windows=[
                _blackout(
                    date="2026-04-17",
                    start_time="00:00",
                    end_time="23:59",
                    reason="Public Holiday",
                )
            ],
        )
    )
    assert project_day(shape, date(2026, 4, 17)).bookable is False  # the named Friday
    assert project_day(shape, date(2026, 4, 24)).bookable is True  # an ordinary Friday


def test_a_recurring_blackout_applies_every_matching_weekday():
    shape = validate_shape(
        _doc(
            operating_blocks=[_block(days=["MON", "WED"], start_time="08:00", end_time="20:00")],
            blackout_windows=[_blackout(days=["MON"], start_time="08:00", end_time="20:00")],
        )
    )
    assert project_day(shape, date(2026, 8, 17)).bookable is False  # a Monday
    assert project_day(shape, date(2026, 8, 19)).bookable is True  # a Wednesday


def test_a_blackout_naming_neither_days_nor_date_applies_every_day_in_its_range():
    shape = validate_shape(
        _doc(
            operating_blocks=[_block(days=["MON", "TUE"], start_time="08:00", end_time="20:00")],
            blackout_windows=[
                _blackout(
                    start_time="08:00",
                    end_time="20:00",
                    effective_from="2026-08-17",
                    effective_to="2026-08-18",
                )
            ],
        )
    )
    assert project_day(shape, date(2026, 8, 17)).bookable is False  # Monday, inside the range
    assert project_day(shape, date(2026, 8, 18)).bookable is False  # Tuesday, inside the range


# --- end_time past local midnight --------------------------------------------------------------


def test_end_time_past_midnight_represents_a_venue_open_past_local_midnight():
    shape = validate_shape(
        _doc(
            operating_blocks=[
                _block(
                    days=["SAT"],
                    start_time="18:00",
                    end_time="26:00",  # 02:00 the next local day
                    allowed_durations_mins=[60],
                )
            ]
        )
    )
    on_date = date(2026, 8, 22)  # a Saturday
    projection = project_day(shape, on_date)
    starts = [s.start_minutes for s in projection.offered_starts]
    assert starts[0] == 18 * 60
    assert starts[-1] == 25 * 60  # the last full hour before the 26:00 close
    assert permits(shape, on_date, 25 * 60, 26 * 60).allowed


def test_end_time_more_than_24h_past_start_time_is_rejected():
    with pytest.raises(InvalidShapeError, match="at most 24h after start_time"):
        validate_shape(
            _doc(
                operating_blocks=[
                    _block(start_time="18:00", end_time="42:01", allowed_durations_mins=[60])
                ]
            )
        )


# --- the empty document -------------------------------------------------------------------------


def test_the_empty_document_is_valid_and_unbookable_every_day():
    shape = validate_shape(_doc())
    assert shape.operating_blocks == ()
    assert shape.blackout_windows == ()
    projection = project_day(shape, date(2026, 8, 17))
    assert projection.bookable is False
    assert projection.offered_starts == ()


def test_the_empty_document_refuses_every_booking_with_reason():
    shape = validate_shape(_doc())
    verdict = permits(shape, date(2026, 8, 17), 9 * 60, 10 * 60)
    assert not verdict.allowed
    assert verdict.reason


# --- validation: one rejection per invalid construct --------------------------------------------


def test_missing_version_is_rejected():
    document = _doc()
    del document["version"]
    with pytest.raises(InvalidShapeError, match="version: is required"):
        validate_shape(document)


def test_non_integer_version_is_rejected():
    with pytest.raises(InvalidShapeError, match="version: must be an int"):
        validate_shape(_doc(version="1"))


def test_version_below_one_is_rejected():
    with pytest.raises(InvalidShapeError, match="version: must be at least 1"):
        validate_shape(_doc(version=0))


def test_missing_operating_blocks_is_rejected():
    document = _doc()
    del document["operating_blocks"]
    with pytest.raises(InvalidShapeError, match=r"operating_blocks: is required"):
        validate_shape(document)


def test_a_non_object_document_is_rejected():
    with pytest.raises(InvalidShapeError, match="document: must be an object"):
        validate_shape(["not", "a", "document"])


def test_a_block_missing_days_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"operating_blocks\[0\]\.days: is required"):
        validate_shape(_doc(operating_blocks=[{k: v for k, v in _block().items() if k != "days"}]))


def test_a_block_with_an_unknown_day_code_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"operating_blocks\[0\]\.days\[0\]"):
        validate_shape(_doc(operating_blocks=[_block(days=["FUNDAY"])]))


def test_a_block_with_a_duplicate_day_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"operating_blocks\[0\]\.days: duplicate"):
        validate_shape(_doc(operating_blocks=[_block(days=["MON", "MON"])]))


def test_a_block_with_an_empty_days_list_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"operating_blocks\[0\]\.days: must not be empty"):
        validate_shape(_doc(operating_blocks=[_block(days=[])]))


def test_a_malformed_time_string_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"operating_blocks\[0\]\.start_time"):
        validate_shape(_doc(operating_blocks=[_block(start_time="6pm")]))


def test_an_inverted_time_pair_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"operating_blocks\[0\]\.end_time"):
        validate_shape(_doc(operating_blocks=[_block(start_time="10:00", end_time="09:00")]))


def test_a_block_missing_allowed_durations_is_rejected():
    document = _doc(
        operating_blocks=[{k: v for k, v in _block().items() if k != "allowed_durations_mins"}]
    )
    with pytest.raises(InvalidShapeError, match=r"allowed_durations_mins: is required"):
        validate_shape(document)


def test_a_block_with_empty_allowed_durations_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"allowed_durations_mins: must not be empty"):
        validate_shape(_doc(operating_blocks=[_block(allowed_durations_mins=[])]))


def test_a_block_with_duplicate_durations_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"allowed_durations_mins: must not contain dup"):
        validate_shape(_doc(operating_blocks=[_block(allowed_durations_mins=[60, 60])]))


def test_a_block_with_unsorted_durations_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"allowed_durations_mins: must be in ascending"):
        validate_shape(_doc(operating_blocks=[_block(allowed_durations_mins=[120, 60])]))


def test_a_block_with_a_non_positive_duration_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"allowed_durations_mins\[0\]: must be at least 1"):
        validate_shape(_doc(operating_blocks=[_block(allowed_durations_mins=[0])]))


def test_effective_to_before_effective_from_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"effective_to: must not be before effective_from"):
        validate_shape(
            _doc(operating_blocks=[_block(effective_from="2026-06-01", effective_to="2026-01-01")])
        )


def test_a_malformed_date_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"effective_from"):
        validate_shape(_doc(operating_blocks=[_block(effective_from="not-a-date")]))


def test_a_blackout_missing_reason_is_rejected():
    document = _doc(blackout_windows=[{k: v for k, v in _blackout().items() if k != "reason"}])
    with pytest.raises(InvalidShapeError, match=r"blackout_windows\[0\]\.reason: is required"):
        validate_shape(document)


def test_a_blackout_with_a_blank_reason_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"reason: must not be empty"):
        validate_shape(_doc(blackout_windows=[_blackout(reason="   ")]))


def test_a_blackout_naming_both_days_and_date_is_rejected():
    with pytest.raises(InvalidShapeError, match="must not carry both 'days' and 'date'"):
        validate_shape(_doc(blackout_windows=[_blackout(days=["MON"], date="2026-04-15")]))


def test_a_blackout_with_an_empty_days_list_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"blackout_windows\[0\]\.days: must not be empty"):
        validate_shape(_doc(blackout_windows=[_blackout(days=[])]))


def test_a_non_object_block_is_rejected():
    with pytest.raises(InvalidShapeError, match=r"operating_blocks\[0\]: must be an object"):
        validate_shape(_doc(operating_blocks=["not-a-block"]))


def test_the_message_names_the_field_and_never_paraphrases():
    document = _doc(operating_blocks=[_block(allowed_durations_mins=[])])
    with pytest.raises(InvalidShapeError) as excinfo:
        validate_shape(document)
    assert str(excinfo.value) == "operating_blocks[0].allowed_durations_mins: must not be empty"


# --- permits: caller mistakes raise, ordinary refusals do not -----------------------------------


def test_permits_raises_for_a_datetime_where_a_date_is_expected():
    shape = validate_shape(_doc())
    with pytest.raises(InvalidBookingRequestError, match="not a datetime"):
        permits(shape, datetime(2026, 8, 17, 9, 0), 9 * 60, 10 * 60)


def test_permits_raises_for_an_inverted_interval():
    shape = validate_shape(_doc())
    with pytest.raises(InvalidBookingRequestError, match="strictly after"):
        permits(shape, date(2026, 8, 17), 10 * 60, 9 * 60)


def test_permits_never_raises_for_an_ordinary_refusal():
    shape = validate_shape(_doc(operating_blocks=[_block(days=["MON"])]))
    verdict = permits(shape, date(2026, 8, 18), 9 * 60, 10 * 60)  # a Tuesday: not governed
    assert verdict.allowed is False
    assert isinstance(verdict.reason, str) and verdict.reason


# --- DEFAULT_SHAPE: the value task 10.2 seeds every Space with ----------------------------------


class TestDefaultShape:
    """``DEFAULT_SHAPE`` (``ops/plans/stream-10/10.2-a-shape-per-space.md``) reproduces what a
    Space with no ``availability_hours`` and no ``session_length`` row renders as today: open
    every day, no blackouts, 60-minute bookings on the hour."""

    def test_validates(self):
        shape = validate_shape(DEFAULT_SHAPE)
        assert shape.version == 1
        assert len(shape.operating_blocks) == 1
        assert shape.blackout_windows == ()

    def test_every_day_is_bookable_at_every_hour(self):
        shape = validate_shape(DEFAULT_SHAPE)
        for on_date in (date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 23)):  # Mon, Tue, Sun
            projection = project_day(shape, on_date)
            assert projection.bookable is True
            starts = {
                offered.start_minutes: offered.durations_mins
                for offered in projection.offered_starts
            }
            assert starts[0] == (60,)
            assert starts[23 * 60] == (60,)  # the last on-the-hour start of the day

    def test_a_60_minute_booking_at_any_hour_is_permitted(self):
        shape = validate_shape(DEFAULT_SHAPE)
        verdict = permits(shape, date(2026, 8, 17), 13 * 60, 14 * 60)
        assert verdict.allowed is True

    def test_an_off_grid_start_is_refused(self):
        shape = validate_shape(DEFAULT_SHAPE)
        verdict = permits(shape, date(2026, 8, 17), 13 * 60 + 15, 14 * 60 + 15)
        assert verdict.allowed is False
