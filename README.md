# Employee Attendance & Analytics API

A FastAPI + MongoDB service for managing employee attendance logs, manual regularizations, department analytics, and late leaderboards.

## Local Setup & Running

1. **Environment Setup**:
   Create a `.env` file from `.env.example`:
   ```bash
   cp .env.example .env
   ```
   Set `MONGO_URI` (default: `mongodb://localhost:27017`) and `MONGO_DB` (default: `attendance_db`).

2. **Virtual Environment & Dependencies**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .\.venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Seed Sample Data**:
   ```bash
   python sample_seed.py
   ```

4. **Start Application**:
   ```bash
   uvicorn app.main:app --port 8000
   ```
   The API will start and create all database indexes automatically on startup.

## Testing

Run the full automated test suite (unit tests, API integration tests, concurrency tests, and 100k-record performance/explain tests):
```bash
pytest
```

To run performance & explain tests with execution timing output:
```bash
pytest -s tests/test_perf_explain.py
```

## Implementation Highlights

- **Single File Core**: All API code, Pydantic models, helper functions, and aggregation pipelines are self-contained in `app/main.py`.
- **IST Timezone & Overnight Shifts**: Correct handling of IST timezone conversions and overnight shifts spanning midnight (`shift_end <= shift_start`).
- **Atomic Operations & Optimistic Concurrency**: Race conditions on punch-in, punch-out, and regularization patches are handled atomically via MongoDB unique indexes and `find_one_and_update`.
- **Decimal Rounding**: All work hours, percentages, and rates use `Decimal.quantize(..., rounding=ROUND_HALF_UP)` for half-up rounding precision.
- **Index-Backed Analytics**: Aggregation pipelines use `IXSCAN` index strategies with zero full collection scans (`COLLSCAN`).
