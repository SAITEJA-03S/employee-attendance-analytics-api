"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import calendar
import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Path, Body, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Priority for environment variables: real env vars win over local .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Database Initialization & Index Creation at Startup
# --------------------------------------------------------------------------- #
def create_indexes():
    """Create all required indexes idempotently on app startup."""
    try:
        # employees collection indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)])

        # attendance_logs collection indexes
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)])
    except Exception as e:
        print(f"Warning during index creation: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int = 2) -> float:
    """Perform half-up rounding (not banker's rounding)."""
    d = Decimal(str(val))
    if decimals == 0:
        return float(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    fmt = "1." + "0" * decimals
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def dt_to_epoch(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime (UTC/IST) to epoch milliseconds int."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punched_at_ms: int, shift_start: str, shift_end: str) -> str:
    """
    Determine IST attendance date (R1).
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time is
    strictly earlier than shift_end belongs to the shift starting the previous day.
    """
    sec = punched_at_ms // 1000
    dt_ist = datetime.fromtimestamp(sec, tz=IST)
    punched_time_str = dt_ist.strftime("%H:%M")
    
    is_overnight = shift_end <= shift_start
    if is_overnight and punched_time_str < shift_end:
        attendance_date = (dt_ist.date() - timedelta(days=1)).isoformat()
    else:
        attendance_date = dt_ist.date().isoformat()
    return attendance_date


def get_shift_instants(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """
    Return shift_start and shift_end IST datetime objects for a given attendance date.
    For overnight shifts, shift_end is on the next calendar day.
    """
    year, month, day = map(int, date_str.split("-"))
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    
    start_dt = datetime(year, month, day, sh, sm, tzinfo=IST)
    if shift_end <= shift_start:
        end_dt = datetime(year, month, day, eh, em, tzinfo=IST) + timedelta(days=1)
    else:
        end_dt = datetime(year, month, day, eh, em, tzinfo=IST)
    return start_dt, end_dt


def compute_late_minutes(punch_in_dt: datetime, shift_start_dt: datetime) -> int:
    """
    Compute late minutes per R2.
    Grace period 10 minutes. Late only if strictly > 10:00 after shift_start.
    late_minutes = floor((punch_in - shift_start) in sec / 60).
    """
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in_dt.astimezone(IST)
    
    elapsed_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_sec > 600:
        return math.floor(elapsed_sec / 60.0)
    return 0


def compute_overtime(punch_out_dt: datetime, shift_end_dt: datetime) -> int:
    """
    Compute overtime minutes per R3.
    Whole minutes (floor) between shift_end and punch_out, counted only if >= 30 mins.
    """
    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=timezone.utc)
    punch_out_ist = punch_out_dt.astimezone(IST)
    
    elapsed_sec = (punch_out_ist - shift_end_dt).total_seconds()
    if elapsed_sec >= 1800:
        return math.floor(elapsed_sec / 60.0)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    Compute work hours per R4.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    Instants are assumed pre-truncated to whole seconds.
    """
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=timezone.utc)
        
    sec = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(sec / 3600.0, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """Compute half_day per R5: work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.50


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance document to OpenAPI AttendanceRecord response."""
    history = []
    for h in doc.get("history", []):
        at_ms = dt_to_epoch(h.get("at"))
        formatted_changes = {}
        for key, val in h.get("changes", {}).items():
            from_val = val.get("from")
            to_val = val.get("to")
            if isinstance(from_val, datetime):
                from_val = dt_to_epoch(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_epoch(to_val)
            formatted_changes[key] = {"from": from_val, "to": to_val}
            
        history.append({
            "at": at_ms,
            "by": h.get("by", ""),
            "reason": h.get("reason", ""),
            "changes": formatted_changes
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch(doc.get("punch_in")),
        "punch_out": dt_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Pydantic Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def check_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end cannot be equal")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Endpoints Implementation
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB connection failed")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch(doc["created_at"]),
    }


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = []
    for doc in cursor:
        items.append({
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "email": doc["email"],
            "department": doc["department"],
            "shift_start": doc["shift_start"],
            "shift_end": doc["shift_end"],
            "joined_on": doc["joined_on"],
            "created_at": dt_to_epoch(doc["created_at"]),
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punched_at_ms = body.punched_at
    else:
        punched_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # R1: Truncate to whole seconds
    punched_at_sec = punched_at_ms // 1000
    punched_at_ms_trunc = punched_at_sec * 1000
    punch_in_dt = datetime.fromtimestamp(punched_at_sec, tz=timezone.utc)

    # Determine attendance date
    date_str = get_attendance_date(punched_at_ms_trunc, emp["shift_start"], emp["shift_end"])

    # Compute late minutes (R2)
    start_dt, _ = get_shift_instants(date_str, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
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
        raise HTTPException(409, "Attendance record already exists for this date")

    return format_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punched_at_ms = body.punched_at
    else:
        punched_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    punched_at_sec = punched_at_ms // 1000
    punch_out_dt = datetime.fromtimestamp(punched_at_sec, tz=timezone.utc)

    # Closes most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(404, "No punch-in record found for punch-out")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    punch_in_ms = int(punch_in_dt.timestamp() * 1000)
    punch_out_ms = punched_at_sec * 1000

    if punch_out_ms <= punch_in_ms:
        raise HTTPException(422, "punched_at must be strictly after punch_in")

    if (punch_out_ms - punch_in_ms) > 24 * 3600 * 1000:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    _, end_dt = get_shift_instants(record["date"], emp["shift_start"], emp["shift_end"])
    ot_mins = compute_overtime(punch_out_dt, end_dt)
    h_day = compute_half_day(work_hrs)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": h_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(409, "Record is already punched out")

    return format_attendance_doc(updated_doc)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found for date")

    new_status = body.status if body.status is not None else record["status"]

    if body.punch_in is not None:
        in_sec = body.punch_in // 1000
        new_punch_in_dt = datetime.fromtimestamp(in_sec, tz=timezone.utc)
    else:
        new_punch_in_dt = record.get("punch_in")

    if body.punch_out is not None:
        out_sec = body.punch_out // 1000
        new_punch_out_dt = datetime.fromtimestamp(out_sec, tz=timezone.utc)
    else:
        new_punch_out_dt = record.get("punch_out")

    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch_in and punch_out cannot be set for ABSENT or LEAVE status")
        new_punch_in_dt = None
        new_punch_out_dt = None
    else:
        if new_punch_in_dt is None:
            raise HTTPException(422, "Presence status requires punch_in")

        in_ms = int(new_punch_in_dt.timestamp() * 1000)
        calc_date = get_attendance_date(in_ms, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(422, f"punch_in falls on attendance date {calc_date}, expected {date}")

    if new_punch_out_dt is not None:
        if new_punch_in_dt is None:
            raise HTTPException(422, "punch_out supplied without punch_in")
        if new_punch_out_dt <= new_punch_in_dt:
            raise HTTPException(422, "punch_out must be strictly after punch_in")
        if (new_punch_out_dt - new_punch_in_dt).total_seconds() > 86400:
            raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    if new_status in ["ABSENT", "LEAVE"]:
        new_late_mins = 0
        new_work_hrs = None
        new_ot_mins = 0
        new_h_day = False
    else:
        start_dt, end_dt = get_shift_instants(date, emp["shift_start"], emp["shift_end"])
        new_late_mins = compute_late_minutes(new_punch_in_dt, start_dt)
        if new_punch_out_dt is not None:
            new_work_hrs = compute_work_hours(new_punch_in_dt, new_punch_out_dt)
            new_ot_mins = compute_overtime(new_punch_out_dt, end_dt)
            new_h_day = compute_half_day(new_work_hrs)
        else:
            new_work_hrs = None
            new_ot_mins = 0
            new_h_day = False

    changes = {}
    if record["status"] != new_status:
        changes["status"] = {"from": record["status"], "to": new_status}

    old_in_epoch = dt_to_epoch(record.get("punch_in"))
    new_in_epoch = dt_to_epoch(new_punch_in_dt)
    if old_in_epoch != new_in_epoch:
        changes["punch_in"] = {"from": record.get("punch_in"), "to": new_punch_in_dt}

    old_out_epoch = dt_to_epoch(record.get("punch_out"))
    new_out_epoch = dt_to_epoch(new_punch_out_dt)
    if old_out_epoch != new_out_epoch:
        changes["punch_out"] = {"from": record.get("punch_out"), "to": new_punch_out_dt}

    if record.get("work_hours") != new_work_hrs:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hrs}

    if record.get("late_minutes", 0) != new_late_mins:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_mins}

    if record.get("overtime_minutes", 0) != new_ot_mins:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_ot_mins}

    if record.get("half_day", False) != new_h_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_h_day}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    now_utc = datetime.now(timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = record.get("history", [])
    current_history_len = len(current_history)

    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": current_history_len}
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hrs,
                "late_minutes": new_late_mins,
                "overtime_minutes": new_ot_mins,
                "half_day": new_h_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(409, "Concurrent regularization conflict")

    return format_attendance_doc(updated_doc)


@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    joined_on = emp["joined_on"]
    effective_start = max(start_date, joined_on)

    working_days = 0
    if effective_start <= end_date:
        cur = datetime.strptime(effective_start, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        while cur <= end_dt:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$addFields": {
                "dt": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "present_val": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$gte": [{"$dayOfWeek": "$dt"}, 2]},
                                {"$lte": [{"$dayOfWeek": "$dt"}, 6]}
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_val"},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
            }
        }
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        stats = res[0]
        p_days = stats["present_days"]
        l_days = stats["leave_days"]
        l_count = stats["late_count"]
        tot_late = stats["total_late_minutes"]
        tot_ot = stats["total_overtime_minutes"]
    else:
        p_days = 0.0
        l_days = 0
        l_count = 0
        tot_late = 0
        tot_ot = 0

    if working_days > 0:
        att_pct = round_half_up((p_days / working_days) * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": p_days,
        "leave_days": l_days,
        "late_count": l_count,
        "total_late_minutes": tot_late,
        "total_overtime_minutes": tot_ot,
        "attendance_pct": att_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    emp_match = {"joined_on": {"$lte": end_date}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
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
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "log_date_dt": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d"}},
                        None
                    ]
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "has_log": {"$cond": [{"$ifNull": ["$logs.date", False]}, 1, 0]},
                "status": "$logs.status",
                "late_minutes": "$logs.late_minutes",
                "work_hours": "$logs.work_hours",
                "half_day": "$logs.half_day",
                "is_present": {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_mon_fri": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {
                            "$and": [
                                {"$gte": [{"$dayOfWeek": "$log_date_dt"}, 2]},
                                {"$lte": [{"$dayOfWeek": "$log_date_dt"}, 6]}
                            ]
                        },
                        False
                    ]
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "has_log": 1,
                "present_val": {
                    "$cond": [
                        {"$and": ["$is_present", "$is_mon_fri"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                },
                "work_hours_val": {
                    "$cond": [
                        {"$and": ["$is_present", {"$ne": ["$work_hours", None]}]},
                        "$work_hours",
                        "$$REMOVE"
                    ]
                },
                "late_count_val": {
                    "$cond": [
                        {"$and": ["$has_log", {"$gt": ["$late_minutes", 0]}]},
                        1,
                        0
                    ]
                },
                "late_mins_val": {
                    "$cond": [
                        {"$and": ["$has_log", {"$gt": ["$late_minutes", 0]}]},
                        "$late_minutes",
                        0
                    ]
                },
                "leave_val": {
                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                },
                "on_duty_val": {
                    "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": "$present_val"},
                "work_hours_list": {"$push": "$work_hours_val"},
                "late_count": {"$sum": "$late_count_val"},
                "total_late_minutes": {"$sum": "$late_mins_val"},
                "leave_count": {"$sum": "$leave_val"},
                "on_duty_count": {"$sum": "$on_duty_val"}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$employees_set"},
                "present_days": 1,
                "avg_work_hours": {"$avg": "$work_hours_list"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]
    return pipeline


@app.get("/analytics/departments/summary")
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None)
):
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        avg_wh = doc.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
            
        items.append({
            "department": doc["department"],
            "headcount": doc["headcount"],
            "present_days": doc["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": doc["late_count"],
            "total_late_minutes": doc["total_late_minutes"],
            "leave_count": doc["leave_count"],
            "on_duty_count": doc["on_duty_count"],
        })
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)
    items = list(cursor)
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    from_dt = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    to_dt_plus_1 = to_dt + timedelta(days=1)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "date_str": {"$literal": from_date}}},
        {"$addFields": {"date_dt": {"$dateFromString": {"dateString": "$date_str", "format": "%Y-%m-%d"}}}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1]
                }
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_dt"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_dt"}, 6]}
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d_str"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc_info"
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$hc_info.count", 0]}, 0]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$d_str"]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp"
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "emp.department": department
                        }
                    }
                ],
                "as": "day_logs"
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$reduce": {
                        "input": "$day_logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": ["$$this.half_day", 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            ]
                        }
                    }
                },
                "late_count": {
                    "$reduce": {
                        "input": "$day_logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]},
                                        1,
                                        0
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$divide": ["$present_count", "$headcount"]
                        },
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        },
        {"$sort": {"date": 1}}
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend")
def get_department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
):
    if to_date < from_date:
        raise HTTPException(422, "to date cannot be earlier than from date")

    dt_from = datetime.strptime(from_date, "%Y-%m-%d")
    dt_to = datetime.strptime(to_date, "%Y-%m-%d")
    if (dt_to - dt_from).days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(404, "Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        rate = doc.get("attendance_rate")
        if rate is not None:
            rate = round_half_up(rate, 4)

        m_avg = doc.get("moving_avg_7d")
        if m_avg is not None:
            m_avg = round_half_up(m_avg, 4)

        items.append({
            "date": doc["date"],
            "is_working_day": doc["is_working_day"],
            "headcount": doc["headcount"],
            "present_count": doc["present_count"],
            "late_count": doc["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1),
    page_size: Optional[int] = Query(20),
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
        skip = ((page or 1) - 1) * (page_size or 20)
        explain_res = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size or 20
            },
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}}
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    else:
        raise HTTPException(422, f"Unknown endpoint: {endpoint}")
