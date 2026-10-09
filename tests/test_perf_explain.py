import os
import time
import pytest
from fastapi.testclient import TestClient

from app.main import app, db
from tests.perf_generator import seed_perf_database, PERF_DB_NAME

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def setup_perf_dataset():
    # Save original DB name
    orig_db = os.environ.get("MONGO_DB", "attendance_db")

    # Seed 100,000 record dataset into attendance_perf
    seed_perf_database()

    # Switch app's db reference to attendance_perf
    from app.main import client as mongo_client

    import app.main

    app.main.db = mongo_client[PERF_DB_NAME]

    yield

    # Restore original DB
    app.main.db = mongo_client[orig_db]


def is_index_scan(explain_dict: dict) -> bool:
    """Recursively search explain output for IXSCAN stage and ensure COLLSCAN is absent."""
    explain_str = str(explain_dict)
    has_ixscan = "IXSCAN" in explain_str
    has_collscan = "COLLSCAN" in explain_str
    return has_ixscan and not has_collscan


def test_explain_attendance_list():
    t0 = time.time()
    res = client.get("/admin/explain/attendance_list?emp_code=EMP0001&page=1&page_size=20")
    t1 = time.time()
    assert res.status_code == 200
    data = res.json()
    assert is_index_scan(data["explain"])
    print(f"\n[PERF] attendance_list explain time: {(t1 - t0)*1000:.2f} ms")


def test_explain_employee_monthly():
    t0 = time.time()
    res = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-06")
    t1 = time.time()
    assert res.status_code == 200
    data = res.json()
    assert is_index_scan(data["explain"])
    print(f"\n[PERF] employee_monthly explain time: {(t1 - t0)*1000:.2f} ms")


def test_explain_department_summary():
    t0 = time.time()
    res = client.get("/admin/explain/department_summary?month=2026-06")
    t1 = time.time()
    assert res.status_code == 200
    data = res.json()
    assert is_index_scan(data["explain"])
    print(f"\n[PERF] department_summary explain time: {(t1 - t0)*1000:.2f} ms")


def test_explain_late_leaderboard():
    t0 = time.time()
    res = client.get("/admin/explain/late_leaderboard?month=2026-06&limit=10")
    t1 = time.time()
    assert res.status_code == 200
    data = res.json()
    assert is_index_scan(data["explain"])
    print(f"\n[PERF] late_leaderboard explain time: {(t1 - t0)*1000:.2f} ms")


def test_explain_department_trend():
    t0 = time.time()
    res = client.get("/admin/explain/department_trend?department=Engineering&from=2026-06-01&to=2026-06-15")
    t1 = time.time()
    assert res.status_code == 200
    data = res.json()
    assert is_index_scan(data["explain"])
    print(f"\n[PERF] department_trend explain time: {(t1 - t0)*1000:.2f} ms")
