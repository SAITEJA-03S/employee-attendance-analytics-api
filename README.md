# Employee Attendance & Analytics API

FastAPI & MongoDB production-grade backend service for managing employee attendance, punch-in/punch-out workflows, manual regularizations with full audit history, and real-time department analytics powered by MongoDB aggregation pipelines.

## Tech Stack & Architecture

- **Python**: 3.11+
- **Framework**: FastAPI (Pydantic v2)
- **Database**: MongoDB 6.0+ (PyMongo)
- **Timezone**: IST (Asia/Kolkata, UTC+05:30)

## Quick Start

### 1. Installation

```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Environment Setup

Configure environment variables in a `.env` file or export them directly:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 3. Load Sample Data (Optional)

```bash
python sample_seed.py
```

### 4. Run Application

```bash
uvicorn app.main:app --port 8000 --reload
```

The application automatically initializes all required unique and compound database indexes at startup.

## API Documentation & OpenAPI Spec

Once running, interactive API docs are available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Features & Implementation Summary

- **Health Probe (`GET /health`)**: Database readiness check returning 200 OK or 503.
- **Employee Management (`POST /employees`, `GET /employees`)**: Unique `emp_code` enforcement, 1-based pagination, department filtering.
- **Punch-In (`POST /attendance/punch-in`)**: Overnight shift attendance date determination (R1), grace period & late calculation (R2), concurrency safety via unique compound index `{emp_code: 1, date: 1}`.
- **Punch-Out (`POST /attendance/punch-out`)**: Work hours half-up rounding (R4), overtime calculation (R3), half day flag (R5), atomic updates via `find_one_and_update`.
- **Attendance Listing (`GET /attendance`)**: Sorted by date DESC and emp_code ASC, date range filtering, memory-efficient cursor pagination.
- **Regularization (`PATCH /attendance/{emp_code}/{date}`)**: Audit trail history logging with field diffs, full derived field recomputation, optimistic concurrency control.
- **Employee Monthly Summary (`GET /analytics/employees/{emp_code}/monthly`)**: Aggregation pipeline for working days, present days, leave days, late count, and attendance percentage.
- **Department Summary (`GET /analytics/departments/summary`)**: Cross-collection aggregation including zero-log employees (R9), present days, mean work hours, and late statistics.
- **Late Leaderboard (`GET /analytics/leaderboard/late`)**: MongoDB `$setWindowFields` with `$rank` standard competition ranking, post-ranking limit filtering, tie handling.
- **Department Trend (`GET /analytics/departments/{department}/trend`)**: Daily series aggregation with `$documents` gap filling, working day rates, and 7-day moving averages (`$setWindowFields`).
- **Query Explain (`GET /admin/explain/{endpoint}`)**: Returns raw MongoDB `executionStats` query plans verifying index scans (`IXSCAN`) and absence of collection scans (`COLLSCAN`).

## File Structure

- `app/main.py` - Single-file FastAPI application containing models, helpers, database logic, and endpoints.
- `requirements.txt` - Python package dependencies.
- `REVIEW.md` - Technical review detailing defects found in the starter code and their fixes.
- `DECISIONS.md` - Technical design decisions covering indexing, concurrency, ranking, headcount, and scaling.
- `openapi.yaml` - OpenAPI contract specification.
- `DATA_MODEL.md` - MongoDB collection schemas and sample document descriptions.
- `sample_seed.py` - Script to seed sample documents into MongoDB.
- `sample_data/` - Extended JSON sample datasets.
