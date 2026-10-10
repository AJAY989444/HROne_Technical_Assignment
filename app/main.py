"""
Employee Attendance & Analytics API - Main Application
"""
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from datetime import datetime, date, time, timedelta, timezone
from typing import Optional, List, Any, Dict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status as http_status
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))

mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
mongo_db_name = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(mongo_uri, tz_aware=True)
db = client[mongo_db_name]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Indexes Init on Startup
# --------------------------------------------------------------------------- #
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.employees.create_index([("joined_on", 1)])

        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1), ("status", 1)])
    except Exception as e:
        print(f"Error initializing indexes: {e}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate microsecond part of a datetime."""
    return dt.replace(microsecond=0)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC datetime to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """Convert epoch milliseconds integer to timezone-aware UTC datetime truncated to seconds."""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return truncate_to_seconds(dt)


def parse_time_str(time_str: str) -> time:
    """Parse 'HH:MM' string to datetime.time."""
    h, m = map(int, time_str.split(":"))
    return time(hour=h, minute=m)


def compute_attendance_date(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str) -> str:
    """
    Rule R1: Attendance Date Calculation in IST.
    For standard shifts (shift_end > shift_start): calendar date of punch_in in IST.
    For overnight shifts (shift_end <= shift_start):
      If punch_in (IST time) <= shift_end_time, it belongs to the shift starting on the previous day.
      Otherwise, it belongs to current IST date.
    """
    dt_ist = punch_in_utc.astimezone(IST)
    start_t = parse_time_str(shift_start_str)
    end_t = parse_time_str(shift_end_str)

    if end_t > start_t:
        return dt_ist.date().isoformat()
    else:
        t = dt_ist.time()
        if t <= end_t:
            return (dt_ist.date() - timedelta(days=1)).isoformat()
        else:
            return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start_str: str, attendance_date_str: str) -> int:
    """
    Rule R2: Late Minutes relative to shift_start in IST.
    Grace period <= 10 minutes -> 0. Strictly > 10 minutes -> total late minutes.
    """
    punch_in_ist = truncate_to_seconds(punch_in_utc.astimezone(IST))
    sh_h, sh_m = map(int, shift_start_str.split(":"))
    att_date = date.fromisoformat(attendance_date_str)
    shift_start_dt = datetime.combine(att_date, time(sh_h, sh_m), tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_sec <= 0:
        return 0
    diff_min = int(diff_sec // 60)
    if diff_sec > 600:  # strictly > 10 minutes
        return diff_min
    return 0


def compute_overtime_minutes(punch_out_utc: datetime, shift_start_str: str, shift_end_str: str, attendance_date_str: str) -> int:
    """
    Rule R3: Overtime Minutes relative to shift_end in IST.
    Minimum threshold >= 30 minutes -> total overtime minutes. Otherwise 0.
    """
    punch_out_ist = truncate_to_seconds(punch_out_utc.astimezone(IST))
    start_t = parse_time_str(shift_start_str)
    end_t = parse_time_str(shift_end_str)
    att_date = date.fromisoformat(attendance_date_str)

    if end_t > start_t:
        shift_end_dt = datetime.combine(att_date, end_t, tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(att_date + timedelta(days=1), end_t, tzinfo=IST)

    ot_sec = (punch_out_ist - shift_end_dt).total_seconds()
    if ot_sec < 1800:  # < 30 minutes
        return 0
    return int(ot_sec // 60)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    Rule R4: Work Hours computed as duration in hours, rounded half-up to 2 dp.
    """
    in_sec = truncate_to_seconds(punch_in_utc)
    out_sec = truncate_to_seconds(punch_out_utc)
    seconds = (out_sec - in_sec).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: float) -> bool:
    """
    Rule R5: half_day is true if rounded work_hours < 4.50.
    """
    return Decimal(str(work_hours)) < Decimal("4.50")


def round_half_up_2dp(val: float) -> float:
    """Round a float to 2 decimal places using HALF_UP."""
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4dp(val: float) -> float:
    """Round a float to 4 decimal places using HALF_UP."""
    return float(Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Response Document Formatters
# --------------------------------------------------------------------------- #
def format_employee_doc(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)
    if "created_at" in d and isinstance(d["created_at"], datetime):
        d["created_at"] = to_epoch_ms(d["created_at"])
    return d


def format_attendance_doc(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)
    d["punch_in"] = to_epoch_ms(d.get("punch_in"))
    d["punch_out"] = to_epoch_ms(d.get("punch_out"))
    d.setdefault("history", [])
    d.setdefault("half_day", False)

    formatted_history = []
    for h in d.get("history", []):
        h_copy = dict(h)
        if "at" in h_copy and isinstance(h_copy["at"], datetime):
            h_copy["at"] = to_epoch_ms(h_copy["at"])
        if "changes" in h_copy and isinstance(h_copy["changes"], dict):
            changes_copy = {}
            for field, ch in h_copy["changes"].items():
                ch_copy = dict(ch)
                if field in ("punch_in", "punch_out"):
                    if "from" in ch_copy and isinstance(ch_copy["from"], datetime):
                        ch_copy["from"] = to_epoch_ms(ch_copy["from"])
                    if "to" in ch_copy and isinstance(ch_copy["to"], datetime):
                        ch_copy["to"] = to_epoch_ms(ch_copy["to"])
                changes_copy[field] = ch_copy
            h_copy["changes"] = changes_copy
        formatted_history.append(h_copy)
    d["history"] = formatted_history
    return d


# --------------------------------------------------------------------------- #
# Pydantic Input Models
# --------------------------------------------------------------------------- #
def validate_epoch_ms_int(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Timestamp must be a strict integer")
    if not (100000000000 <= v <= 4102444800000):
        raise ValueError("Timestamp out of valid epoch millisecond range")
    return v


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)
    department: str = Field(..., min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Invalid email format")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("Time must be HH:MM in 24-hour format")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v


class PunchInIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: int
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_punched_at(cls, v: Any) -> int:
        return validate_epoch_ms_int(v)

    @field_validator("status")
    @classmethod
    def val_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status for punch-in must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: int

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_punched_at(cls, v: Any) -> int:
        return validate_epoch_ms_int(v)


class AttendancePatchIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    by: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def val_optional_timestamps(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_epoch_ms_int(v)

    @field_validator("status")
    @classmethod
    def val_patch_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("Invalid attendance status")
        return v


# --------------------------------------------------------------------------- #
# Endpoints Implementation
# --------------------------------------------------------------------------- #

@app.get("/")
def root():
    return {
        "title": "Employee Attendance & Analytics API",
        "docs": "/docs",
        "health": "/health",
        "employees": "/employees",
        "attendance": "/attendance"
    }


@app.get("/health")
def health():

    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unreachable: {e}")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start and shift_end cannot be identical")

    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(timezone.utc))

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    docs = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    items = [format_employee_doc(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    punch_in_dt = from_epoch_ms(body.punched_at)
    att_date = compute_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    punched_at_dt = from_epoch_ms(body.punched_at)

    # Find latest record where punch_in <= punched_at
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at_dt}},
        sort=[("punch_in", -1)],
    )

    if not doc:
        raise HTTPException(404, detail="No matching punch-in record found for punch-out")

    punch_in_dt = doc["punch_in"]
    if punched_at_dt <= punch_in_dt:
        raise HTTPException(422, detail="punch_out time must be strictly after punch_in time")

    if (punched_at_dt - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, detail="punch_out time cannot be more than 24 hours after punch_in time")

    work_hrs = compute_work_hours(punch_in_dt, punched_at_dt)
    ot_mins = compute_overtime_minutes(punched_at_dt, emp["shift_start"], emp["shift_end"], doc["date"])
    is_half = compute_half_day(work_hrs)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": is_half,
            }
        },
        return_document=True,
    )

    if not updated_doc:
        raise HTTPException(409, detail="Record has already been punched out")

    return format_attendance_doc(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(422, detail="date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            try:
                date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(422, detail="Invalid date_from format")
            q["date"]["$gte"] = date_from
        if date_to:
            try:
                date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(422, detail="Invalid date_to format")
            q["date"]["$lte"] = date_to

    if status:
        if status not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise HTTPException(422, detail="Invalid status filter")
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{att_date}")
def patch_attendance(
    emp_code: str = Path(...),
    att_date: str = Path(...),
    body: AttendancePatchIn = ...,
):
    try:
        date.fromisoformat(att_date)
    except ValueError:
        raise HTTPException(422, detail="att_date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if not doc:
        raise HTTPException(404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else doc["status"]

    # Check if punch_in/punch_out are explicitly provided or kept
    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = from_epoch_ms(body.punch_in) if body.punch_in is not None else doc.get("punch_in")
        new_punch_out = from_epoch_ms(body.punch_out) if body.punch_out is not None else doc.get("punch_out")

    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        raise HTTPException(422, detail="Presence status requires a punch_in timestamp")

    # Validate R1 consistency for punch_in
    if new_punch_in is not None:
        calc_date = compute_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != att_date:
            raise HTTPException(422, detail=f"punch_in timestamp maps to attendance date {calc_date}, not record date {att_date}")

    if new_punch_in is not None and new_punch_out is not None:
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, detail="punch_out must be strictly after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 86400:
            raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Compute new derived values
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], att_date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_minutes(new_punch_out, emp["shift_start"], emp["shift_end"], att_date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compare changes
    changes = {}
    field_mappings = [
        ("status", doc.get("status"), new_status),
        ("punch_in", doc.get("punch_in"), new_punch_in),
        ("punch_out", doc.get("punch_out"), new_punch_out),
        ("work_hours", doc.get("work_hours"), new_work_hours),
        ("late_minutes", doc.get("late_minutes", 0), new_late_minutes),
        ("overtime_minutes", doc.get("overtime_minutes", 0), new_overtime_minutes),
        ("half_day", doc.get("half_day", False), new_half_day),
    ]

    for fname, old_val, new_val in field_mappings:
        if old_val != new_val:
            changes[fname] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(422, detail="No changes detected in attendance patch request")

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now_utc,
        "by": body.by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = doc.get("history", [])

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "history": current_history},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if not updated_doc:
        raise HTTPException(409, detail="Concurrent edit detected. Please retry.")

    return format_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Endpoints Implementation
# --------------------------------------------------------------------------- #
def get_month_range(month_str: str):
    if not re.match(r"^\d{4}-(?:0[1-9]|1[0-2])$", month_str):
        raise HTTPException(422, detail="month must be YYYY-MM")
    y, m = map(int, month_str.split("-"))
    start_d = date(y, m, 1)
    if m == 12:
        end_d = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        end_d = date(y, m + 1, 1) - timedelta(days=1)
    return start_d, end_d


def get_working_days_count(joined_on_str: str, month_start: date, month_end: date) -> int:
    joined_d = date.fromisoformat(joined_on_str)
    eff_start = max(joined_d, month_start)
    if eff_start > month_end:
        return 0
    cnt = 0
    cur = eff_start
    while cur <= month_end:
        if cur.weekday() < 5:  # Mon-Fri
            cnt += 1
        cur += timedelta(days=1)
    return cnt


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str = Path(...), month: str = Query(...)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    start_d, end_d = get_month_range(month)
    working_days = get_working_days_count(emp["joined_on"], start_d, end_d)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_d.isoformat(), "$lte": end_d.isoformat()}}},
        {
            "$project": {
                "status": 1,
                "date": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "work_hours": "$work_hours",
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }
        },
    ]

    docs = list(db.attendance_logs.aggregate(pipeline))

    present_days = 0.0
    leave_days = 0.0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    total_work_hours = 0.0

    for d in docs:
        dt_obj = date.fromisoformat(d["date"])
        is_weekday = dt_obj.weekday() < 5

        late_m = d.get("late_minutes", 0)
        ot_m = d.get("overtime_minutes", 0)
        wh = d.get("work_hours")

        total_late_minutes += late_m
        if late_m > 0:
            late_count += 1
        total_overtime_minutes += ot_m
        if wh is not None:
            total_work_hours += wh

        if is_weekday:
            if d.get("is_present_status"):
                present_days += 0.5 if d.get("half_day") else 1.0
            elif d.get("status") == "LEAVE":
                leave_days += 1.0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up_2dp((present_days / working_days) * 100.0)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up_2dp(present_days),
        "leave_days": round_half_up_2dp(leave_days),
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "total_work_hours": round_half_up_2dp(total_work_hours),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary_analytics(month: str = Query(...)):
    start_d, end_d = get_month_range(month)

    pipeline = [
        {"$match": {"joined_on": {"$lte": end_d.isoformat()}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_d.isoformat()]},
                                    {"$lte": ["$date", end_d.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
    ]

    emp_docs = list(db.employees.aggregate(pipeline))

    dept_stats: Dict[str, dict] = {}

    for emp in emp_docs:
        dept = emp["department"]
        if dept not in dept_stats:
            dept_stats[dept] = {
                "headcount": 0,
                "total_present_days": 0.0,
                "total_working_days": 0,
                "total_late_minutes": 0,
                "total_overtime_minutes": 0,
                "work_hours_list": [],
            }

        w_days = get_working_days_count(emp["joined_on"], start_d, end_d)
        dept_stats[dept]["headcount"] += 1
        dept_stats[dept]["total_working_days"] += w_days

        for log in emp.get("logs", []):
            dt_obj = date.fromisoformat(log["date"])
            is_weekday = dt_obj.weekday() < 5
            st = log.get("status")

            late_m = log.get("late_minutes", 0)
            ot_m = log.get("overtime_minutes", 0)
            wh = log.get("work_hours")

            dept_stats[dept]["total_late_minutes"] += late_m
            dept_stats[dept]["total_overtime_minutes"] += ot_m

            if st in ("PRESENT", "WFH", "ON_DUTY"):
                if is_weekday:
                    dept_stats[dept]["total_present_days"] += 0.5 if log.get("half_day") else 1.0
                if wh is not None:
                    dept_stats[dept]["work_hours_list"].append(wh)

    res = []
    for dept in sorted(dept_stats.keys()):
        data = dept_stats[dept]
        if data["headcount"] == 0:
            continue

        wh_list = data["work_hours_list"]
        avg_wh = round_half_up_2dp(sum(wh_list) / len(wh_list)) if wh_list else None

        tot_w_days = data["total_working_days"]
        if tot_w_days == 0:
            avg_att_pct = None
        else:
            avg_att_pct = round_half_up_2dp((data["total_present_days"] / tot_w_days) * 100.0)

        res.append(
            {
                "department": dept,
                "headcount": data["headcount"],
                "avg_work_hours": avg_wh,
                "total_late_minutes": data["total_late_minutes"],
                "total_overtime_minutes": data["total_overtime_minutes"],
                "avg_attendance_pct": avg_att_pct,
            }
        )

    return res


@app.get("/analytics/leaderboard/late")
def late_leaderboard_analytics(
    month: str = Query(...),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
):
    start_d, end_d = get_month_range(month)

    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": start_d.isoformat(), "$lte": end_d.isoformat()}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
    )

    docs = list(db.attendance_logs.aggregate(pipeline))

    res = []
    for d in docs:
        res.append(
            {
                "rank": d["rank"],
                "emp_code": d["_id"],
                "name": d["emp"]["name"],
                "department": d["emp"]["department"],
                "total_late_minutes": d["total_late_minutes"],
                "late_count": d["late_count"],
            }
        )

    return res


@app.get("/analytics/departments/{department}/trend")
def department_trend_analytics(
    department: str = Path(...),
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    try:
        start_d = date.fromisoformat(date_from)
        end_d = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(422, detail="from and to must be YYYY-MM-DD")

    if end_d < start_d:
        raise HTTPException(422, detail="to cannot be before from")

    num_days = (end_d - start_d).days + 1
    if num_days > 92:
        raise HTTPException(422, detail="Date range cannot exceed 92 days")

    # Check department existence
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(404, detail="Department not found")

    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_d.isoformat()]},
                                    {"$lte": ["$date", end_d.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
    ]

    dept_emps = list(db.employees.aggregate(pipeline))

    # Daily aggregation gap-fill across requested range
    days_data = []
    cur = start_d
    while cur <= end_d:
        d_str = cur.isoformat()
        is_weekday = cur.weekday() < 5

        headcount = 0
        present_count = 0.0
        late_count = 0

        for emp in dept_emps:
            joined_d = date.fromisoformat(emp["joined_on"])
            if joined_d <= cur:
                headcount += 1

            for log in emp.get("logs", []):
                if log["date"] == d_str:
                    st = log.get("status")
                    if st in ("PRESENT", "WFH", "ON_DUTY"):
                        present_count += 0.5 if log.get("half_day") else 1.0
                    if log.get("late_minutes", 0) > 0:
                        late_count += 1

        if not is_weekday or headcount == 0:
            rate = None
        else:
            rate = round_half_up_4dp(present_count / headcount)

        days_data.append(
            {
                "date": d_str,
                "headcount": headcount,
                "present_count": round_half_up_2dp(present_count),
                "late_count": late_count,
                "attendance_rate": rate,
            }
        )
        cur += timedelta(days=1)

    # 7-day moving average calculation over window [-6, 0]
    res = []
    for i, day_info in enumerate(days_data):
        window = days_data[max(0, i - 6) : i + 1]
        rates = [w["attendance_rate"] for w in window if w["attendance_rate"] is not None]
        if rates:
            moving_avg = round_half_up_4dp(sum(rates) / len(rates))
        else:
            moving_avg = None

        day_info["moving_avg_7d"] = moving_avg
        res.append(day_info)

    return res


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        explain_res = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        start_d, end_d = get_month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_d.isoformat(), "$lte": end_d.isoformat()}}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        start_d, end_d = get_month_range(month)
        pipeline = [
            {"$match": {"joined_on": {"$lte": end_d.isoformat()}}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "logs",
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        start_d, end_d = get_month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": start_d.isoformat(), "$lte": end_d.isoformat()}, "late_minutes": {"$gt": 0}}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_trend":
        if not department or not date_from or not date_to:
            raise HTTPException(422, detail="department, from, and to are required")
        pipeline = [
            {"$match": {"department": department}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    else:
        raise HTTPException(404, detail=f"Unknown explain endpoint: {endpoint}")
