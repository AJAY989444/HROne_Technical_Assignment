# STUDY_NOTES.md: FastAPI + PyMongo Attendance System Guide

> **For Java / Spring Boot Engineers**: This guide bridges concepts from Spring Boot, JPA/Hibernate, and relational SQL databases to FastAPI, Pydantic, PyMongo, and MongoDB Aggregation Pipelines.

---

## 1. Spring Boot vs. FastAPI Core Concepts

| Spring Boot / Java Concept | FastAPI / Python Equivalent | Notes |
|---|---|---|
| `@RestController` / `@GetMapping` | `@app.get()`, `@app.post()` | FastAPI routes are declared using decorator annotations directly on functions. |
| `@Valid` / DTO Classes | Pydantic `BaseModel` | Input schemas subclass `BaseModel`. Validation rules run automatically before endpoint code executes. |
| `ResponseEntity<T>` / Exceptions | Return `dict` / `raise HTTPException` | Returning a Python dictionary auto-serializes to JSON. Raising `HTTPException(status, detail)` sends FastAPI error JSON format `{"detail": "..."}`. |
| `MongoRepository` / JPA | `pymongo.MongoClient` & `db.collection` | PyMongo is the standard MongoDB driver. Documents are native Python dictionaries (`dict`). |
| Spring `@Transactional` | Atomic updates (`find_one_and_update`) | Multi-document transactions exist in Mongo, but single-document operations are atomic by default in MongoDB and much faster. |
| `application.properties` | `python-dotenv` & `os.getenv` | Reads environment variables from system or `.env`. |

---

## 2. Helper Functions Deep Dive (`app/main.py`)

### `truncate_to_seconds(dt)`
- **Purpose**: Drops microseconds (`microsecond=0`) from datetime objects.
- **Why**: Prevents floating-point sub-second discrepancies when comparing timestamps between Python and MongoDB BSON dates.

### `to_epoch_ms(dt)` & `from_epoch_ms(ms)`
- **Purpose**: Converts UTC datetimes to/from epoch milliseconds integers (`1783310280000`).
- **Why**: MongoDB stores BSON UTC dates internally, but the HTTP API contract specifies epoch milliseconds integers at the external boundary.

### `compute_attendance_date(punch_in_utc, shift_start_str, shift_end_str)` (Rule R1)
- **Purpose**: Determines the IST calendar date for a punch.
- **Logic**:
  - Converts UTC `punch_in` to IST (`Asia/Kolkata`, UTC+5:30).
  - Standard shift (`shift_end > shift_start`, e.g., 09:30 - 18:30): Date is simply the IST calendar date.
  - Overnight shift (`shift_end <= shift_start`, e.g., 22:00 - 06:00): A punch-in between 00:00 and 06:00 IST early morning belongs to the shift starting on the *previous calendar day* (`date - 1 day`).

### `compute_late_minutes(punch_in_utc, shift_start_str, attendance_date_str)` (Rule R2)
- **Purpose**: Calculates late arrival minutes.
- **Grace Period Rule**: Strictly > 10 minutes (600 seconds) returns total minutes late. Punching in <= 10 minutes late returns `0`.

### `compute_overtime_minutes(punch_out_utc, shift_start_str, shift_end_str, attendance_date_str)` (Rule R3)
- **Purpose**: Calculates overtime minutes at punch-out.
- **Threshold Rule**: Overtime < 30 minutes returns `0`. Overtime >= 30 minutes returns full total overtime minutes. For overnight shifts, `shift_end` is evaluated on `attendance_date + 1 day`.

### `compute_work_hours(punch_in_utc, punch_out_utc)` & `compute_half_day(work_hours)` (Rules R4 & R5)
- **Purpose**: Calculates total working hours rounded half-up to 2 decimal places using `decimal.Decimal(..., rounding=ROUND_HALF_UP)`.
- **Half Day Rule**: `half_day = True` if rounded `work_hours < 4.50`.

---

## 3. Prevented Race Conditions

### 1. Punch-in Race (Duplicate Day Punch)
- **Scenario**: User double-clicks "Punch In" simultaneously.
- **Prevention**: A unique compound index on `(emp_code, date)` in MongoDB. The first request inserts successfully (201). The second request throws `DuplicateKeyError`, which is caught and returned as HTTP `409 Conflict`. No check-then-insert vulnerability.

### 2. Punch-out Race (Double Punch Out)
- **Scenario**: Concurrent punch-out requests for the same open record.
- **Prevention**: Atomic `db.attendance_logs.find_one_and_update` matching `{"_id": doc_id, "punch_out": None}`. Only the first request finds `punch_out == None` and updates it. The second concurrent request finds no matching document and receives HTTP `409 Conflict`.

### 3. PATCH Concurrency (Lost Audit Log Entries)
- **Scenario**: Two HR admins update an attendance log simultaneously.
- **Prevention**: Optimistic concurrency control. `find_one_and_update` includes `{"history": current_history}` in the filter query. If history was modified by another thread, the query matches 0 documents and returns HTTP `409 Conflict`, preventing lost history entries.

---

## 4. Aggregation Pipelines Explained

### Monthly Analytics (`/analytics/employees/{emp_code}/monthly`)
- **Stage 1**: `$match` by `emp_code` and date range (`month_start` to `month_end`). Uses index `(emp_code, date)`.
- **Computation**: Sums working days (Mon-Fri from `max(joined_on, month_start)`), present days (0.5 for half day, 1.0 for full day), leave days, late count, and overtime minutes. Calculates `attendance_pct` rounded to 2 decimal places.

### Department Summary (`/analytics/departments/summary`)
- **Stage 1**: `$match` on `employees` where `joined_on <= month_end`. Uses index `(joined_on, 1)` (IXSCAN).
- **Stage 2**: `$lookup` joins `attendance_logs` for that month.
- **Headcount Calculation**: Includes employees with 0 attendance logs in total department headcount. Computes average work hours across present logs.

### Late Leaderboard (`/analytics/leaderboard/late`)
- **Stage 1**: `$match` `attendance_logs` where `late_minutes > 0` in month range.
- **Stage 2**: `$group` by `emp_code` summing `total_late_minutes`.
- **Stage 3**: `$lookup` join `employees` (unwinds to drop deleted employees).
- **Stage 4**: Optional `$match` on department.
- **Stage 5**: `$setWindowFields` with `$rank` for standard competition ranking (1, 2, 2, 4).
- **Stage 6**: `$match` `rank <= limit` and `$sort` by `total_late_minutes desc, emp_code asc`.

### Department Trend (`/analytics/departments/{department}/trend`)
- **Gap-Filling**: Generates daily entries for all calendar days from `date_from` to `date_to` (up to 92 days max).
- **7-Day Moving Average**: Computes rolling moving average over window `[-6, 0]` days for valid non-null weekday rates using `ROUND_HALF_UP` 4 dp.

---

## 5. 15 Likely Live-Interview Questions & Answers

1. **Q: Why use `Decimal` with `ROUND_HALF_UP` instead of Python's built-in `round()`?**  
   *A*: Python's built-in `round()` uses round-half-to-even (banker's rounding), which rounds `4.495` to `4.49`. Financial and HR compliance rules require round-half-up, which rounds `4.495` to `4.50`.

2. **Q: How do you handle overnight shifts across midnight?**  
   *A*: In `compute_attendance_date`, if `shift_end <= shift_start`, punches between 00:00 and `shift_end` IST belong to the previous day's shift date. Overtime evaluates `shift_end` on `date + 1 day`.

3. **Q: How does `create_employee` prevent duplicate employee codes under heavy load?**  
   *A*: It relies on a unique MongoDB index on `emp_code`. If a duplicate code is inserted, PyMongo raises `DuplicateKeyError`, which FastAPI converts to a 409 Conflict response.

4. **Q: How do you ensure index scans (IXSCAN) instead of collection scans (COLLSCAN) for explain queries?**  
   *A*: We create compound indexes matching query patterns (`(emp_code, date)`, `(joined_on)`, `(department, joined_on)`). The admin explain endpoint runs `db.command("explain", ...)` and verifies `IXSCAN` is present and `COLLSCAN` is absent.

5. **Q: What happens if an employee has zero logs in a given month?**  
   *A*: Department summary starts from the `employees` collection and `$lookup` joins logs. The employee is correctly counted in department headcount, and `avg_work_hours` returns `null` if no present logs exist.

6. **Q: How is pagination implemented safely?**  
   *A*: `page` must be >= 1 and `page_size` between 1 and 100. Database queries use `.skip((page - 1) * page_size).limit(page_size)` with a deterministic sort.

7. **Q: What is the threshold for late minutes?**  
   *A*: Up to 10 minutes late (<= 600 seconds) is covered by grace period (0 late minutes). Strictly > 10 minutes returns full late minutes.

8. **Q: What is the threshold for overtime?**  
   *A*: Overtime < 30 minutes returns 0. Overtime >= 30 minutes returns the exact overtime duration in minutes.

9. **Q: How are manual regularizations tracked in history?**  
   *A*: A `PATCH` request computes field changes (`from` and `to`), appends an audit record to `history` via `$push`, and updates derived fields atomically.

10. **Q: How do you prevent lost updates during concurrent PATCH edits?**  
    *A*: Optimistic concurrency locking matching `history` array length or content during `find_one_and_update`.

11. **Q: Live Change Test: How would you change the late grace period from 10 to 15 minutes on the spot?**  
    *A*: In `compute_late_minutes`, change `diff_sec > 600` to `diff_sec > 900` (15 minutes * 60 seconds).

12. **Q: Live Change Test: How would you add a minimum work hours filter to `list_attendance`?**  
    *A*: Add `min_work_hours: Optional[float] = None` query param to `list_attendance` and append `q["work_hours"] = {"$gte": min_work_hours}` to the query dictionary.

13. **Q: Live Change Test: How would you export attendance logs to CSV?**  
    *A*: Add a GET endpoint returning `StreamingResponse(csv_generator(), media_type="text/csv")`.

14. **Q: How are legacy documents missing `history` or `half_day` handled?**  
    *A*: Response formatting helpers use `.setdefault("history", [])` and `.setdefault("half_day", False)` so missing fields never raise `KeyError`.

15. **Q: Why are shift times stored as `"HH:MM"` strings instead of datetimes?**  
    *A*: A shift time is a recurring time of day, not a point in time. Storing as plain strings avoids time zone binding until evaluated against a specific date.
