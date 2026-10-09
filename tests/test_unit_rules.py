from datetime import datetime, timezone
import pytest

from app.main import (
    compute_attendance_date,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    compute_half_day,
    truncate_to_seconds,
    from_epoch_ms,
    to_epoch_ms,
)

# UTC test datetimes
# IST is UTC+5:30.
# 2026-07-06 09:30 IST -> 2026-07-06 04:00:00 UTC


def test_truncate_to_seconds():
    dt = datetime(2026, 7, 6, 4, 0, 0, 900000, tzinfo=timezone.utc)
    res = truncate_to_seconds(dt)
    assert res.microsecond == 0
    assert res.second == 0


def test_epoch_ms_conversion():
    ms = 1778126400000  # epoch ms
    dt = from_epoch_ms(ms)
    assert dt.tzinfo == timezone.utc
    assert to_epoch_ms(dt) == ms


def test_rule_r1_attendance_date_standard_shift():
    # Standard shift 09:30 to 18:30 IST
    # 09:28 IST on 2026-07-06 -> 03:58:00 UTC
    dt1 = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    assert compute_attendance_date(dt1, "09:30", "18:30") == "2026-07-06"


def test_rule_r1_attendance_date_overnight_shift():
    # Overnight shift 22:00 to 06:00 IST
    # 21:55 IST on 2026-07-06 -> 16:25:00 UTC on 2026-07-06
    dt1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert compute_attendance_date(dt1, "22:00", "06:00") == "2026-07-06"

    # 02:00 IST on 2026-07-07 -> 20:30:00 UTC on 2026-07-06 (still early morning of 7th IST)
    dt2 = datetime(2026, 7, 6, 20, 30, 0, tzinfo=timezone.utc)
    assert compute_attendance_date(dt2, "22:00", "06:00") == "2026-07-06"

    # 06:00 IST on 2026-07-07 -> 00:30:00 UTC on 2026-07-07 (exactly shift end) -> belongs to 2026-07-06
    dt3 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)
    assert compute_attendance_date(dt3, "22:00", "06:00") == "2026-07-06"

    # 06:01 IST on 2026-07-07 -> 00:31:00 UTC on 2026-07-07 (after shift end) -> belongs to 2026-07-07
    dt4 = datetime(2026, 7, 7, 0, 31, 0, tzinfo=timezone.utc)
    assert compute_attendance_date(dt4, "22:00", "06:00") == "2026-07-07"


def test_rule_r2_late_minutes_boundary():
    # Shift start: 09:30 IST -> 04:00 UTC
    # Exactly 09:40:00 IST (10 min late) -> grace period -> 0 late minutes
    dt_10m = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_10m, "09:30", "2026-07-06") == 0

    # 09:40:01 IST (10 min 1 sec late) -> strictly > 10 min threshold -> 10 late minutes
    dt_10m1s = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_10m1s, "09:30", "2026-07-06") == 10

    # 10:05:00 IST (35 min late) -> 35 late minutes
    dt_35m = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_35m, "09:30", "2026-07-06") == 35


def test_rule_r3_overtime_minutes_boundary():
    # Shift end: 18:30 IST -> 13:00 UTC
    # 29 min overtime (18:59 IST / 13:29 UTC) -> < 30 min threshold -> 0 overtime
    dt_29m = datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_29m, "09:30", "18:30", "2026-07-06") == 0

    # Exactly 30 min overtime (19:00 IST / 13:30 UTC) -> 30 overtime minutes
    dt_30m = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_30m, "09:30", "18:30", "2026-07-06") == 30

    # 40 min overtime (19:10 IST / 13:40 UTC) -> 40 overtime minutes
    dt_40m = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_40m, "09:30", "18:30", "2026-07-06") == 40


def test_rule_r4_work_hours_half_up_rounding():
    in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    # 3.5 hours = 12600 seconds
    out_dt1 = datetime(2026, 7, 6, 7, 30, 0, tzinfo=timezone.utc)
    assert compute_work_hours(in_dt, out_dt1) == 3.5

    # 4.495 hours = 16182 seconds -> half-up rounds to 4.50
    out_dt_halfup = datetime(2026, 7, 6, 8, 29, 42, tzinfo=timezone.utc)  # 16182s / 3600 = 4.495
    assert compute_work_hours(in_dt, out_dt_halfup) == 4.50


def test_rule_r5_half_day():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(8.50) is False
