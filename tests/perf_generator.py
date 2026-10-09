import os
import time
from datetime import datetime, date, timedelta, timezone
from pymongo import MongoClient

PERF_DB_NAME = "attendance_perf"


def seed_perf_database(mongo_uri="mongodb://localhost:27017"):
    client = MongoClient(mongo_uri, tz_aware=True)
    db = client[PERF_DB_NAME]

    print(f"Clearing database '{PERF_DB_NAME}'...")
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    # Initialize indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1), ("status", 1)])


    departments = ["Engineering", "Sales", "Support", "HR", "Finance"]
    num_employees = 2000
    days_to_seed = 50

    print(f"Generating {num_employees} employees...")
    employees = []
    start_joined = date(2025, 1, 1)
    for i in range(1, num_employees + 1):
        emp_code = f"EMP{i:04d}"
        dept = departments[i % len(departments)]
        joined = start_joined + timedelta(days=i % 300)
        employees.append(
            {
                "emp_code": emp_code,
                "name": f"Employee {i}",
                "email": f"emp{i}@example.com",
                "department": dept,
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": joined.isoformat(),
                "created_at": datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            }
        )
    db.employees.insert_many(employees)
    print(f"Inserted {len(employees)} employees.")

    print(f"Generating ~100,000 attendance logs across {days_to_seed} days...")
    start_date = date(2026, 6, 1)
    logs = []
    batch_size = 10000

    count = 0
    t0 = time.time()

    for d_idx in range(days_to_seed):
        cur_date = start_date + timedelta(days=d_idx)
        cur_date_str = cur_date.isoformat()

        for emp in employees:
            status = "PRESENT" if d_idx % 7 < 5 else "ABSENT"
            if status == "PRESENT":
                # 09:28 IST punch in
                p_in = datetime(cur_date.year, cur_date.month, cur_date.day, 3, 58, 0, tzinfo=timezone.utc)
                p_out = datetime(cur_date.year, cur_date.month, cur_date.day, 13, 5, 0, tzinfo=timezone.utc)
                log = {
                    "emp_code": emp["emp_code"],
                    "date": cur_date_str,
                    "status": "PRESENT",
                    "punch_in": p_in,
                    "punch_out": p_out,
                    "work_hours": 9.12,
                    "late_minutes": 0 if d_idx % 10 != 0 else 20,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                }
            else:
                log = {
                    "emp_code": emp["emp_code"],
                    "date": cur_date_str,
                    "status": "ABSENT",
                    "punch_in": None,
                    "punch_out": None,
                    "work_hours": None,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                }

            logs.append(log)
            count += 1

            if len(logs) >= batch_size:
                db.attendance_logs.insert_many(logs)
                logs = []

    if logs:
        db.attendance_logs.insert_many(logs)

    t1 = time.time()
    print(f"Successfully seeded {count} attendance logs in {t1 - t0:.2f} seconds.")


if __name__ == "__main__":
    seed_perf_database()
