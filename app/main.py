"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import calendar
import json
from datetime import date
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Query
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager
from pymongo import MongoClient, ASCENDING, DESCENDING
from typing import Literal
from pydantic import BaseModel, StrictInt
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP
from pymongo import ReturnDocument

from pydantic import Field
from bson import json_util
load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "candidateKit")]
def create_indexes():
    # Employee codes must be unique.
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employees_emp_code",
    )

    # An employee can have at most one attendance record per attendance date.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )

    # Supports attendance filters by date, with newest dates first.
    db.attendance_logs.create_index(
        [("date", DESCENDING)],
        name="ix_attendance_date_desc",
    )

    # Supports employee-specific attendance date ranges and newest-first sorting.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_emp_date_desc",
    )

    # Supports status/date filtering and date sorting.
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_status_date_desc",
    )

    # Supports department filtering and employee listing order.
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employees_department_emp_code",
    )

    # Supports monthly headcount filtering by employee join date.
    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employees_joined_on",
    )

IST = timezone(timedelta(hours=5, minutes=30))

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    # Epoch milliseconds -> UTC instant; truncate to whole seconds.
    if body.punched_at is None:
        ts_utc = datetime.now(timezone.utc)
    else:
        ts_utc = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        )

    ts_utc = ts_utc.replace(microsecond=0)
    ts_ist = ts_utc.astimezone(IST)

    shift_start_hour, shift_start_minute = map(
        int, emp["shift_start"].split(":")
    )
    shift_end_hour, shift_end_minute = map(
        int, emp["shift_end"].split(":")
    )

    shift_start_minutes = shift_start_hour * 60 + shift_start_minute
    shift_end_minutes = shift_end_hour * 60 + shift_end_minute
    punch_minutes = ts_ist.hour * 60 + ts_ist.minute

    # For an overnight shift, an early-morning punch belongs to yesterday's shift.
    attendance_date = ts_ist.date()
    if shift_end_minutes <= shift_start_minutes and punch_minutes < shift_end_minutes:
        from datetime import timedelta as dt_timedelta
        attendance_date -= dt_timedelta(days=1)

    date_str = attendance_date.isoformat()

    # Build shift start as an IST instant. For an overnight punch assigned
    # to the previous day, the shift started on attendance_date.
    shift_start_ist = datetime(
        attendance_date.year,
        attendance_date.month,
        attendance_date.day,
        shift_start_hour,
        shift_start_minute,
        tzinfo=IST,
    )
    shift_start_utc = shift_start_ist.astimezone(timezone.utc)

    # Strictly more than 10 minutes late; measure whole minutes from shift start.
    seconds_late = (ts_utc - shift_start_utc).total_seconds()
    late_minutes = int(seconds_late // 60) if seconds_late > 600 else 0

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": int(ts_utc.timestamp() * 1000),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
def to_utc_datetime(epoch_ms: int) -> datetime:
    # Integer division drops any sub-second part of the supplied milliseconds.
    seconds = epoch_ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def round_hours_half_up(seconds: int) -> float:
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def get_shift_end_utc(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    year, month, day = map(int, attendance_date.split("-"))
    end_date = datetime(year, month, day)

    # An overnight shift ends on the day after the attendance date.
    if end_minutes <= start_minutes:
        end_date += timedelta(days=1)

    end_ist = end_date.replace(
        hour=end_h,
        minute=end_m,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )
    return end_ist.astimezone(timezone.utc)

def as_utc(value: datetime) -> datetime:
    # PyMongo returns naive datetimes by default; those represent UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def round_work_hours_half_up(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def attendance_record_response(doc: dict) -> dict:
    history = []

    for entry in doc.get("history", []):
        changes = {}
        for field, pair in entry.get("changes", {}).items():
            old_value = pair.get("from")
            new_value = pair.get("to")

            if field in {"punch_in", "punch_out"}:
                old_value = epoch_ms(old_value) if old_value is not None else None
                new_value = epoch_ms(new_value) if new_value is not None else None

            changes[field] = {"from": old_value, "to": new_value}

        history.append({
            "at": epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": epoch_ms(doc.get("punch_in")),
        "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    if body.punched_at is None:
        punched_out = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        punched_out = to_utc_datetime(body.punched_at)

    # Match records whose punch-in is no later than this punch-out.
    eligible = list(
        db.attendance_logs.find(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": punched_out},
            },
            {"_id": 1, "date": 1, "punch_in": 1, "punch_out": 1},
        )
        .sort("punch_in", DESCENDING)
        .limit(1)
    )

    if not eligible:
        raise HTTPException(status_code=404, detail="no punch-in found")

    record = eligible[0]

    # PyMongo may return a naive datetime, which represents UTC by default.
    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    elapsed_seconds = int((punched_out - punch_in_utc).total_seconds())
    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and no more than 24 hours later",
        )

    work_hours = round_hours_half_up(elapsed_seconds)

    shift_end_utc = get_shift_end_utc(
        record["date"],
        emp["shift_start"],
        emp["shift_end"],
    )
    overtime_seconds = int((punched_out - shift_end_utc).total_seconds())
    overtime_minutes = overtime_seconds // 60 if overtime_seconds >= 30 * 60 else 0
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_out,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        # Someone else may have punched out this record between our read and update.
        raise HTTPException(status_code=409, detail="record already punched out")

    def epoch_ms(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)

    return {
        "emp_code": updated["emp_code"],
        "date": updated["date"],
        "status": updated["status"],
        "punch_in": epoch_ms(updated.get("punch_in")),
        "punch_out": epoch_ms(updated.get("punch_out")),
        "work_hours": updated.get("work_hours"),
        "late_minutes": updated.get("late_minutes", 0),
        "overtime_minutes": updated.get("overtime_minutes", 0),
        "half_day": updated.get("half_day", False),
        "history": updated.get("history", []),
    }

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    # Validate the attendance-date path parameter.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    original = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })
    if original is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    old_in = as_utc(original["punch_in"]) if original.get("punch_in") else None
    old_out = as_utc(original["punch_out"]) if original.get("punch_out") else None

    supplied = body.model_fields_set
    final_status = body.status if "status" in supplied else original["status"]
    final_in = (
        to_utc_datetime(body.punch_in)
        if "punch_in" in supplied and body.punch_in is not None
        else old_in
    )
    final_out = (
        to_utc_datetime(body.punch_out)
        if "punch_out" in supplied and body.punch_out is not None
        else old_out
    )

    # ABSENT and LEAVE clear punches. Supplying punch fields with either is invalid.
    if final_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="cannot provide punch times with ABSENT or LEAVE",
            )
        final_in = None
        final_out = None
    elif final_in is None:
        raise HTTPException(
            status_code=422,
            detail="presence status requires punch_in",
        )

    # A punch-in must belong to this attendance date under R1.
    if final_in is not None:
        local_in = final_in.astimezone(IST)
        start_h, start_m = map(int, employee["shift_start"].split(":"))
        end_h, end_m = map(int, employee["shift_end"].split(":"))
        shift_start_mins = start_h * 60 + start_m
        shift_end_mins = end_h * 60 + end_m

        punch_date = local_in.date()
        if shift_end_mins <= shift_start_mins:
            punch_mins = local_in.hour * 60 + local_in.minute
            if punch_mins < shift_end_mins:
                punch_date -= timedelta(days=1)

        if punch_date.isoformat() != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

    if final_out is not None:
        if final_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")

        elapsed_seconds = (final_out - final_in).total_seconds()
        if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    # Recompute all stored derived fields from the final record values.
    if final_status in {"ABSENT", "LEAVE"}:
        derived = {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        derived = {
            "late_minutes": compute_late_minutes(final_in, employee["shift_start"]),
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

        if final_out is not None:
            elapsed_seconds = int((final_out - final_in).total_seconds())
            work_hours = round_work_hours_half_up(final_in, final_out)

            shift_end_utc = get_shift_end_utc(
                date,
                employee["shift_start"],
                employee["shift_end"],
            )
            overtime_seconds = int((final_out - shift_end_utc).total_seconds())

            derived.update({
                "work_hours": work_hours,
                "overtime_minutes": (
                    overtime_seconds // 60
                    if overtime_seconds >= 30 * 60
                    else 0
                ),
                "half_day": work_hours < 4.50,
            })

    final_values = {
        "status": final_status,
        "punch_in": final_in,
        "punch_out": final_out,
        **derived,
    }

    old_values = {
        "status": original["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "late_minutes": original.get("late_minutes", 0),
        "work_hours": original.get("work_hours"),
        "overtime_minutes": original.get("overtime_minutes", 0),
        "half_day": original.get("half_day", False),
    }

    changes = {}
    for field, new_value in final_values.items():
        if old_values.get(field) != new_value:
            before = old_values.get(field)
            after = new_value

            if field in {"punch_in", "punch_out"}:
                before = as_utc(before) if before is not None else None
                after = as_utc(after) if after is not None else None

            changes[field] = {"from": before, "to": after}

    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    audit_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: update only if the record still matches what we read.
    concurrency_filter = {
        "emp_code": emp_code,
        "date": date,
        "status": original["status"],
        "punch_in": original.get("punch_in"),
        "punch_out": original.get("punch_out"),
        "late_minutes": original.get("late_minutes", 0),
        "work_hours": original.get("work_hours"),
        "overtime_minutes": original.get("overtime_minutes", 0),
        "half_day": original.get("half_day", False),
        "history": original.get("history", []),
    }

    updated = db.attendance_logs.find_one_and_update(
        concurrency_filter,
        {
            "$set": final_values,
            "$push": {"history": audit_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="record changed concurrently; retry",
        )

    return attendance_record_response(updated)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = _attendance_list_query(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)
    items = list(_attendance_list_find(q, page, page_size))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    try:
        year, month_num = map(int, month.split("-"))
        month_start = date(year, month_num, 1)
    except ValueError:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")

    last_day = calendar.monthrange(year, month_num)[1]
    month_end = date(year, month_num, last_day)
    joined_on = date.fromisoformat(employee["joined_on"])

    # Count Monday-Friday from the later of joined_on and month_start through month_end.
    first_work_date = max(joined_on, month_start)
    working_days = 0
    if first_work_date <= month_end:
        current = first_work_date
        while current <= month_end:
            if current.weekday() < 5:
                working_days += 1
            current = date.fromordinal(current.toordinal() + 1)

    logs = list(
        db.attendance_logs.aggregate(_employee_monthly_pipeline(emp_code, month))
    )

    totals = logs[0] if logs else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    present_days = totals["present_days"]
    if working_days == 0:
        attendance_pct = None
    else:
        raw_pct = present_days * 100 / working_days
        # Half-up rounding to 2 decimals: floor(raw*100 + 0.5)/100.
        attendance_pct = (int(raw_pct * 100 + 0.5)) / 100

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals["leave_days"],
        "late_count": totals["late_count"],
        "total_late_minutes": totals["total_late_minutes"],
        "total_overtime_minutes": totals["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


def _department_summary_pipeline(month, department=None):
    first_day, final_day = _month_bounds(month)
    employee_match = {"joined_on": {"$lte": final_day.isoformat()}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                            "date": {
                                "$gte": first_day.isoformat(),
                                "$lte": final_day.isoformat(),
                            },
                        }
                    },
                    {
                        "$addFields": {
                            "_is_weekday": {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$date",
                                                "format": "%Y-%m-%d",
                                            }
                                        }
                                    },
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                            "_present_value": {
                                "$cond": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$half_day", True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs._is_weekday", True]},
                                ]
                            },
                            "$logs._present_value",
                            0,
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None,
):
    _month_bounds(month)
    items = list(
        db.employees.aggregate(_department_summary_pipeline(month, department))
    )
    # Average over records; round displayed average to 2 decimal places.
    # Note: MongoDB $round uses ties-to-even, so apply the assignment's
    # half-up rounding rule explicitly if exact midpoint cases are tested.
    for item in items:
        value = item["avg_work_hours"]
        if value is not None:
            item["avg_work_hours"] = (int(value * 100 + 0.5)) / 100

    return {"month": month, "items": items}
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    _month_bounds(month)
    pipeline = _late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}

def _valid_iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise HTTPException(status_code=422, detail="dates must be YYYY-MM-DD")


def _trend_pipeline(department: str, start: date, end: date) -> list:
    # $densify uses an exclusive upper bound, so add one day.
    upper_exclusive = end + timedelta(days=1)

    return [
        {
            "$match": {"department": department}
        },
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$literal": datetime.combine(start, datetime.min.time())
                },
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start, datetime.min.time()),
                        datetime.combine(upper_exclusive, datetime.min.time()),
                    ],
                },
            }
        },
        {
            "$set": {
                "date_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                        "timezone": "UTC",
                    }
                },
                "iso_weekday": {"$isoDayOfWeek": "$date"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date_string"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day"]},
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date_string"},
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                            "$expr": {"$eq": ["$date", "$$day"]},
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]
                },
                "late_count": {
                    "$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]
                },
                "is_working_day": {"$lte": ["$iso_weekday", 5]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$present_count",
                                                            "$headcount",
                                                        ]
                                                    },
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$moving_avg_7d", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = _valid_iso_date(from_date)
    end = _valid_iso_date(to_date)

    days = (end - start).days + 1
    if days <= 0 or days > 92:
        raise HTTPException(
            status_code=422,
            detail="to must be on or after from and range must not exceed 92 days",
        )

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="unknown department")

    pipeline = _trend_pipeline(department, start, end)
    items = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "items": items,
    }


# Builders shared by the API routes and /admin/explain.
def _attendance_list_query(
    emp_code=None,
    date_from=None,
    date_to=None,
    status=None,
):
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


def _attendance_list_find(
    query,
    page=1,
    page_size=20,
):
    return (
        db.attendance_logs.find(query, {"_id": 0})
        .sort(_attendance_list_sort())
        .skip((page - 1) * page_size)
        .limit(page_size)
    )


def _attendance_list_sort():
    return {"date": DESCENDING, "emp_code": ASCENDING}


def _explain_find(collection_name, query, page=1, page_size=20,
                  sort=None, projection=None):
    spec = {"find": collection_name, "filter": query}
    if projection is not None:
        spec["projection"] = projection
    if sort is not None:
        spec["sort"] = sort
    spec["skip"] = (page - 1) * page_size
    spec["limit"] = page_size
    return db.command({
        "explain": spec,
        "verbosity": "executionStats",
    })


def _explain_aggregate(collection_name, pipeline):
    return db.command({
        "explain": {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        },
        "verbosity": "executionStats",
    })


def _month_bounds(month):
    try:
        year, month_num = map(int, month.split("-"))
        start = date(year, month_num, 1)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    end = date(year, month_num, calendar.monthrange(year, month_num)[1])
    return start, end


def _employee_monthly_pipeline(emp_code, month):
    month_start, month_end = _month_bounds(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "is_weekday": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "present_value": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": ["$is_weekday", "$present_value", 0]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


def _late_leaderboard_pipeline(month, limit, department=None):
    month_start, month_end = _month_bounds(month)
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.append({"$match": {"total_late_minutes": {"$gt": 0}}})
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "employee.emp_code": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$employee.emp_code",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(
        default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail="unknown endpoint")

    if endpoint == "attendance_list":
        query = _attendance_list_query(emp_code, date_from, date_to, status)
        explain = _explain_find(
            "attendance_logs",
            query,
            page,
            page_size,
            sort=_attendance_list_sort(),
            projection={"_id": 0},
        )
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )
        explain = _explain_aggregate(
            "attendance_logs",
            _employee_monthly_pipeline(emp_code, month),
        )
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = _department_summary_pipeline(month, department)
        explain = _explain_aggregate("employees", pipeline)
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        explain = _explain_aggregate(
            "attendance_logs",
            _late_leaderboard_pipeline(month, limit, department),
        )
        collection = "attendance_logs"

    else:
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required",
            )
        start = _valid_iso_date(from_date)
        end = _valid_iso_date(to_date)
        days = (end - start).days + 1
        if days <= 0 or days > 92:
            raise HTTPException(
                status_code=422,
                detail="to must be on or after from and range must not exceed 92 days",
            )
        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": _trend_pipeline(department, start, end),
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        collection = "employees"

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(explain)),
    }
