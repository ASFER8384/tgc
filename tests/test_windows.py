"""Working hours arithmetic, which is where an off by one hour is a real cost."""

from datetime import UTC, datetime

from sca.scheduling.windows import (
    WorkingHours,
    hours_until_open,
    is_open,
    next_open,
    overlap_hours,
)

GUANGZHOU = WorkingHours("Asia/Shanghai", (1, 2, 3, 4, 5, 6), 8, 18)
RIYADH = WorkingHours("Asia/Riyadh", (7, 1, 2, 3, 4), 9, 17)
ISTANBUL = WorkingHours("Europe/Istanbul", (1, 2, 3, 4, 5), 9, 18)


def test_open_during_local_working_hours():
    # Monday 10:00 in Guangzhou is Monday 02:00 UTC.
    assert is_open(GUANGZHOU, datetime(2026, 8, 10, 2, 0, tzinfo=UTC))


def test_closed_overnight_in_local_time():
    # Monday 22:00 Riyadh, well past close, even though it is a working day.
    assert not is_open(RIYADH, datetime(2026, 8, 10, 19, 0, tzinfo=UTC))


def test_gulf_weekend_is_friday_and_saturday():
    # 2026-08-14 is a Friday. Riyadh rests, Guangzhou works.
    friday_morning = datetime(2026, 8, 14, 7, 0, tzinfo=UTC)
    assert not is_open(RIYADH, friday_morning)
    assert is_open(GUANGZHOU, friday_morning)


def test_next_open_returns_now_when_already_open():
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    assert next_open(GUANGZHOU, now) == now


def test_next_open_skips_to_the_following_working_morning():
    # Friday 20:00 Istanbul, closed until Monday 09:00 local.
    friday_evening = datetime(2026, 8, 14, 17, 0, tzinfo=UTC)
    opens = next_open(ISTANBUL, friday_evening)
    local = opens.astimezone(ISTANBUL.zone)
    assert local.isoweekday() == 1
    assert local.hour == 9


def test_next_open_same_day_before_start():
    # 05:00 in Guangzhou, three hours before the shift starts.
    early = datetime(2026, 8, 10, 21, 0, tzinfo=UTC)  # Monday 05:00 CST on the 11th
    opens = next_open(GUANGZHOU, early).astimezone(GUANGZHOU.zone)
    assert opens.hour == 8
    assert hours_until_open(GUANGZHOU, early) == 3


def test_overlap_between_riyadh_and_guangzhou_is_small_but_real():
    # Riyadh 09:00 to 17:00 against Guangzhou 08:00 to 18:00, five hours apart.
    monday = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    shared = overlap_hours(RIYADH, GUANGZHOU, monday)
    assert 4.0 <= shared <= 5.0


def test_no_overlap_when_one_side_is_on_weekend():
    friday = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    assert overlap_hours(RIYADH, GUANGZHOU, friday) == 0.0


def test_from_supplier_reads_the_stored_columns():
    class FakeSupplier:
        timezone = "Asia/Kolkata"
        working_days = "1,2,3,4,5,6"
        work_start_hour = 10
        work_end_hour = 19

    hours = WorkingHours.from_supplier(FakeSupplier())
    assert hours.working_days == (1, 2, 3, 4, 5, 6)
    assert hours.start_hour == 10
