import math
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

from app.main import (
    round_half_up,
    dt_to_epoch,
    get_attendance_date,
    get_shift_instants,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    EmployeeCreate,
    PunchInRequest,
    PunchOutRequest,
    RegularizeRequest,
    IST,
)

def test_round_half_up():
    assert round_half_up(2.5, 0) == 3.0
    assert round_half_up(3.5, 0) == 4.0
    assert round_half_up(9.125, 2) == 9.13
    assert round_half_up(9.124, 2) == 9.12
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234

def test_late_minutes_rule_r2():
    # Shift 09:30
    shift_start_dt, _ = get_shift_instants("2026-07-06", "09:30", "18:30")
    
    # 09:40:00 IST -> 10m 00s -> 0 late minutes
    in_094000 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(in_094000, shift_start_dt) == 0

    # 09:40:01 IST -> 10m 01s -> strictly > 10m -> 10 late minutes
    in_094001 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(in_094001, shift_start_dt) == 10

    # 10:15:59 IST -> 45m 59s -> 45 late minutes
    in_101559 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(in_101559, shift_start_dt) == 45

def test_overtime_rule_r3():
    # Shift 09:30 - 18:30
    _, shift_end_dt = get_shift_instants("2026-07-06", "09:30", "18:30")
    
    # 18:59:59 IST -> 29m 59s -> under 30 mins -> 0 overtime
    out_1859 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
    assert compute_overtime(out_1859, shift_end_dt) == 0

    # 19:00:00 IST -> 30m 00s -> 30 overtime minutes
    out_1900 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert compute_overtime(out_1900, shift_end_dt) == 30

    # 19:10:45 IST -> 40m 45s -> 40 overtime minutes
    out_1910 = datetime(2026, 7, 6, 19, 10, 45, tzinfo=IST)
    assert compute_overtime(out_1910, shift_end_dt) == 40

def test_overnight_shift_r1():
    # Overnight shift 22:00 to 06:00
    # Punch in at 21:55 IST on 2026-07-06 -> Date is 2026-07-06
    dt_2155 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    ms_2155 = int(dt_2155.timestamp() * 1000)
    assert get_attendance_date(ms_2155, "22:00", "06:00") == "2026-07-06"

    # Punch in at 02:00 IST on 2026-07-07 -> Date is 2026-07-06 (belongs to shift starting previous day)
    dt_0200 = datetime(2026, 7, 7, 2, 0, 0, tzinfo=IST)
    ms_0200 = int(dt_0200.timestamp() * 1000)
    assert get_attendance_date(ms_0200, "22:00", "06:00") == "2026-07-06"

    # Shift instants for overnight shift starting 2026-07-06
    start_dt, end_dt = get_shift_instants("2026-07-06", "22:00", "06:00")
    assert start_dt == datetime(2026, 7, 6, 22, 0, 0, tzinfo=IST)
    assert end_dt == datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)

def test_work_hours_and_half_day():
    in_dt = datetime(2026, 7, 6, 9, 0, 0, tzinfo=IST)
    out_dt = datetime(2026, 7, 6, 12, 30, 0, tzinfo=IST)
    wh = compute_work_hours(in_dt, out_dt)
    assert wh == 3.50
    assert compute_half_day(wh) is True

    out_full = datetime(2026, 7, 6, 18, 0, 0, tzinfo=IST)
    wh_full = compute_work_hours(in_dt, out_full)
    assert wh_full == 9.00
    assert compute_half_day(wh_full) is False

def test_models_validation():
    # Valid EmployeeCreate
    emp = EmployeeCreate(
        emp_code="EMP0001",
        name="Asha Rao",
        email="asha@example.com",
        department="Engineering",
        joined_on="2026-01-05"
    )
    assert emp.emp_code == "EMP0001"

    # Invalid equal shift start and end
    with pytest.raises(ValueError):
        EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="09:30",
            joined_on="2026-01-05"
        )

    # Invalid timestamp in PunchInRequest (seconds instead of ms)
    with pytest.raises(ValueError):
        PunchInRequest(emp_code="EMP0001", punched_at=17833125)
