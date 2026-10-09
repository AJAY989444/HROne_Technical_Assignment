import os
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_db():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    # Run indexes initialization
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_employee():
    payload = {
        "emp_code": "EMP0100",
        "name": "Alice Smith",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    response = client.post("/employees", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["emp_code"] == "EMP0100"
    assert "_id" not in data
    assert "id" not in data
    assert isinstance(data["created_at"], int)

    # Duplicate create -> 409 Conflict
    response_dup = client.post("/employees", json=payload)
    assert response_dup.status_code == 409


def test_create_employee_validation_errors():
    # Bad email
    payload = {
        "emp_code": "EMP0101",
        "name": "Bob",
        "email": "invalid-email",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    assert client.post("/employees", json=payload).status_code == 422

    # Equal shifts
    payload["email"] = "bob@example.com"
    payload["shift_start"] = "09:30"
    payload["shift_end"] = "09:30"
    assert client.post("/employees", json=payload).status_code == 422


def test_punch_in_and_punch_out_flow():
    # 1. Create employee
    emp_payload = {
        "emp_code": "EMP0102",
        "name": "Charlie",
        "email": "charlie@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    client.post("/employees", json=emp_payload)

    # 2. Punch in at 09:28 IST (03:58 UTC on 2026-07-06)
    # 2026-07-06 03:58:00 UTC = 1783310280000 ms
    punch_in_ms = 1783310280000
    punch_in_res = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0102", "punched_at": punch_in_ms, "status": "PRESENT"},
    )
    assert punch_in_res.status_code == 201
    in_data = punch_in_res.json()
    assert in_data["emp_code"] == "EMP0102"
    assert in_data["date"] == "2026-07-06"
    assert in_data["late_minutes"] == 0
    assert in_data["punch_out"] is None

    # Duplicate punch in for same day -> 409
    dup_in = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0102", "punched_at": punch_in_ms + 1000, "status": "PRESENT"},
    )
    assert dup_in.status_code == 409

    # 3. Punch out at 18:35 IST (13:05 UTC on 2026-07-06) -> 1783343100000 ms
    punch_out_ms = 1783343100000
    punch_out_res = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0102", "punched_at": punch_out_ms},
    )
    assert punch_out_res.status_code == 200
    out_data = punch_out_res.json()
    assert out_data["punch_out"] == punch_out_ms
    assert out_data["work_hours"] == 9.12
    assert out_data["half_day"] is False

    # Second punch out for same record -> 409 Conflict
    dup_out = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0102", "punched_at": punch_out_ms + 1000},
    )
    assert dup_out.status_code == 409


def test_patch_attendance():
    # Setup employee and record
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0103",
            "name": "David",
            "email": "david@example.com",
            "department": "Support",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    punch_in_ms = 1783310280000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0103", "punched_at": punch_in_ms, "status": "PRESENT"})

    # Patch regularization
    patch_res = client.patch(
        "/attendance/EMP0103/2026-07-06",
        json={
            "status": "WFH",
            "by": "hr.admin",
            "reason": "Approved WFH request",
        },
    )
    assert patch_res.status_code == 200
    patch_data = patch_res.json()
    assert patch_data["status"] == "WFH"
    assert len(patch_data["history"]) == 1
    assert patch_data["history"][0]["by"] == "hr.admin"

    # No-op patch -> 422
    noop_res = client.patch(
        "/attendance/EMP0103/2026-07-06",
        json={
            "status": "WFH",
            "by": "hr.admin",
            "reason": "Duplicate request",
        },
    )
    assert noop_res.status_code == 422

