import pytest
from fastapi.testclient import TestClient
from app.main import app, db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_db():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


def test_employee_monthly_analytics():
    # Employee joined 2026-07-13 (mid-month)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0200",
            "name": "Mid Month Joiner",
            "email": "joiner@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-13",
        },
    )

    # July 2026 has 31 days. Weekdays from July 13 to July 31 = 15 working days.
    # Add a punch-in record on July 13 (Monday)
    punch_in_ms = 1783915080000  # 2026-07-13 03:58 UTC
    client.post("/attendance/punch-in", json={"emp_code": "EMP0200", "punched_at": punch_in_ms, "status": "PRESENT"})
    punch_out_ms = 1783947900000  # 2026-07-13 13:05 UTC
    client.post("/attendance/punch-out", json={"emp_code": "EMP0200", "punched_at": punch_out_ms})

    res = client.get("/analytics/employees/EMP0200/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0200"
    assert data["working_days"] == 15
    assert data["present_days"] == 1.0
    assert data["attendance_pct"] == 6.67  # (1/15)*100 = 6.6666... -> 6.67 half-up


def test_department_summary_and_headcount():
    # 2 Employees in Engineering: one with logs, one with ZERO logs
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0201",
            "name": "Eng One",
            "email": "eng1@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0202",
            "name": "Eng Two (No logs)",
            "email": "eng2@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    punch_in_ms = 1783310280000  # 2026-07-06 03:58 UTC
    client.post("/attendance/punch-in", json={"emp_code": "EMP0201", "punched_at": punch_in_ms, "status": "PRESENT"})
    punch_out_ms = 1783343100000  # 2026-07-06 13:05 UTC
    client.post("/attendance/punch-out", json={"emp_code": "EMP0201", "punched_at": punch_out_ms})

    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    summaries = res.json()
    assert len(summaries) == 1
    eng = summaries[0]
    assert eng["department"] == "Engineering"
    assert eng["headcount"] == 2  # Includes zero-log employee!
    assert eng["avg_work_hours"] == 9.12


def test_late_leaderboard_ties():
    # 3 Employees with late minutes
    for code, name, dept in [
        ("EMP0301", "Alice", "Sales"),
        ("EMP0302", "Bob", "Sales"),
        ("EMP0303", "Charlie", "Sales"),
    ]:
        client.post(
            "/employees",
            json={
                "emp_code": code,
                "name": name,
                "email": f"{code}@example.com",
                "department": dept,
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )

    # Insert late logs: Alice 35 mins, Bob 35 mins (TIE), Charlie 15 mins
    # 04:35 UTC = 10:05 IST (35 min late) -> 1783312500000 ms
    late_35_ms = 1783312500000
    # 04:15 UTC = 09:45 IST (15 min late) -> 1783311300000 ms
    late_15_ms = 1783311300000

    client.post("/attendance/punch-in", json={"emp_code": "EMP0301", "punched_at": late_35_ms, "status": "PRESENT"})
    client.post("/attendance/punch-in", json={"emp_code": "EMP0302", "punched_at": late_35_ms, "status": "PRESENT"})
    client.post("/attendance/punch-in", json={"emp_code": "EMP0303", "punched_at": late_15_ms, "status": "PRESENT"})

    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 3
    # Check standard competition ranking 1, 1, 3 (or tied rank 1 for both 35 mins, rank 3 for 15 mins)
    ranks = [item["rank"] for item in items]
    assert ranks == [1, 1, 3]


def test_department_trend():
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0401",
            "name": "Trend Employee",
            "email": "trend@example.com",
            "department": "Support",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        },
    )

    # Query trend for 5 days: 2026-07-01 to 2026-07-05
    res = client.get("/analytics/departments/Support/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 200
    trend = res.json()
    assert len(trend) == 5
    # 2026-07-04 and 2026-07-05 are Saturday and Sunday -> attendance_rate must be null
    assert trend[3]["date"] == "2026-07-04"
    assert trend[3]["attendance_rate"] is None
    assert trend[4]["date"] == "2026-07-05"
    assert trend[4]["attendance_rate"] is None
