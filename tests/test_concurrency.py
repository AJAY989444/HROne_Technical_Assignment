import asyncio
import os
import time
import pytest
import httpx
from pymongo import MongoClient

from app.main import db

# Concurrency test using httpx async client against running test app
from app.main import app


@pytest.fixture(autouse=True)
def setup_db():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


@pytest.mark.anyio
async def test_concurrent_punch_in_race_condition():
    # Setup employee
    db.employees.insert_one(
        {
            "emp_code": "EMP0500",
            "name": "Race Tester",
            "email": "race@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
    )

    punch_in_ms = 1783310280000  # 2026-07-06 03:58 UTC

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        tasks = [
            ac.post("/attendance/punch-in", json={"emp_code": "EMP0500", "punched_at": punch_in_ms, "status": "PRESENT"})
            for _ in range(25)
        ]
        responses = await asyncio.gather(*tasks)

    status_codes = [r.status_code for r in responses]
    assert status_codes.count(201) == 1
    assert status_codes.count(409) == 24


@pytest.mark.anyio
async def test_concurrent_punch_out_race_condition():
    db.employees.insert_one(
        {
            "emp_code": "EMP0501",
            "name": "PunchOut Race Tester",
            "email": "raceout@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
    )

    punch_in_ms = 1783310280000  # 2026-07-06 03:58 UTC
    punch_out_ms = 1783343100000  # 2026-07-06 13:05 UTC

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # First punch in
        await ac.post("/attendance/punch-in", json={"emp_code": "EMP0501", "punched_at": punch_in_ms, "status": "PRESENT"})

        # Send 25 concurrent punch out requests
        tasks = [ac.post("/attendance/punch-out", json={"emp_code": "EMP0501", "punched_at": punch_out_ms}) for _ in range(25)]
        responses = await asyncio.gather(*tasks)

    status_codes = [r.status_code for r in responses]
    assert status_codes.count(200) == 1
    assert status_codes.count(409) == 24
