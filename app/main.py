"""Employee Attendance & Analytics API (HROne assignment, contract v2.0).

Run from the repository root: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables (optional local .env).
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- validation ---------------------------------
def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "month must be YYYY-MM")
    start = date.fromisoformat(value + "-01")
    end = (date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1) - timedelta(days=1))
    return start, end


def millis_to_dt(value: int | None, *, default_now: bool = False) -> datetime:
    if value is None:
        if default_now:
            return truncate_seconds(datetime.now(UTC))
        raise HTTPException(422, "timestamp is required")
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        raise HTTPException(422, "timestamp must be an integer epoch-milliseconds value")
    return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC))


def truncate_seconds(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def to_millis(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    return int(value.timestamp() * 1000)


def local_dt(day: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def shift_start_for(attendance_day: date, emp: dict[str, Any]) -> datetime:
    return local_dt(attendance_day, emp["shift_start"])


def shift_end_for(attendance_day: date, emp: dict[str, Any]) -> datetime:
    start_hm, end_hm = emp["shift_start"], emp["shift_end"]
    end_day = attendance_day + timedelta(days=1) if end_hm <= start_hm else attendance_day
    return local_dt(end_day, end_hm)


def attendance_day_for(punch_in: datetime, emp: dict[str, Any]) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, emp: dict[str, Any], day: date) -> int:
    start = shift_start_for(day, emp)
    seconds_late = (punch_in - start).total_seconds()
    return int(seconds_late // 60) if seconds_late > 600 else 0


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime | None, punch_out: datetime | None) -> float | None:
    if punch_in is None or punch_out is None:
        return None
    return round_half_up(Decimal(str((punch_out - punch_in).total_seconds())) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime | None, emp: dict[str, Any], day: date) -> int:
    if punch_out is None:
        return 0
    extra = int((punch_out - shift_end_for(day, emp)).total_seconds() // 60)
    return extra if extra >= 30 else 0


def is_weekday(day_string: str) -> bool:
    return date.fromisoformat(day_string).weekday() < 5


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    history = []
    for entry in doc.get("history", []) or []:
        changes = {}
        for key, change in (entry.get("changes") or {}).items():
            changes[key] = {
                "from": to_millis(change.get("from")) if key in {"punch_in", "punch_out"} else change.get("from"),
                "to": to_millis(change.get("to")) if key in {"punch_in", "punch_out"} else change.get("to"),
            }
        history.append({"at": to_millis(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes})
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_millis(doc.get("punch_in")), "punch_out": to_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0), "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {"emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
            "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
            "joined_on": doc["joined_on"], "created_at": to_millis(doc["created_at"])}


def ensure_indexes() -> None:
    """Idempotent indexes; the unique indexes enforce correctness under concurrency."""
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employees_department_code")
    employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employees_joined_department")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punch_in")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING), ("status", ASCENDING)], name="ix_attendance_date_employee_status")
    attendance.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_late_employee")


@app.on_event("startup")
def startup() -> None:
    # Do not prevent the process from starting if MongoDB is temporarily down;
    # /health will report 503. Index creation is idempotent when MongoDB is up.
    try:
        ensure_indexes()
    except PyMongoError:
        pass


# --------------------------------- models ------------------------------------
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, v: str) -> str:
        parse_date(v, "joined_on")
        return v

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: StrictInt | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_time(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX:
            raise ValueError("punched_at must be epoch milliseconds")
        return v


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: StrictInt | None = None

    @field_validator("punched_at")
    @classmethod
    def valid_time(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX:
            raise ValueError("punched_at must be epoch milliseconds")
        return v


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_time(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX:
            raise ValueError("timestamp must be epoch milliseconds")
        return v


# ---------------------------------- routes -----------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = employees.count_documents(query)
    docs = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be epoch milliseconds when supplied")
    ts = millis_to_dt(body.punched_at, default_now=True)
    day = attendance_day_for(ts, emp)
    if day.isoformat() < emp["joined_on"]:
        raise HTTPException(422, "attendance date is before employee joined_on")
    doc = {"emp_code": body.emp_code, "date": day.isoformat(), "status": body.status,
           "punch_in": ts, "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(ts, emp, day), "overtime_minutes": 0,
           "half_day": False, "history": []}
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if status is not None and status not in STATUSES:
        raise HTTPException(422, "invalid status")
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query: dict[str, Any] = {}
    if emp_code is not None: query["emp_code"] = emp_code
    if status is not None: query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from: query["date"]["$gte"] = date_from
        if date_to: query["date"]["$lte"] = date_to
    total = attendance.count_documents(query)
    docs = attendance.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be epoch milliseconds when supplied")
    ts = millis_to_dt(body.punched_at, default_now=True)
    doc = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not doc:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    punch_in_dt = doc.get("punch_in")
    if punch_in_dt is None:
        raise HTTPException(404, "no punch-in found")
    if ts <= punch_in_dt or ts > punch_in_dt + timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and no more than 24 hours later")
    day = parse_date(doc["date"])
    hours = compute_work_hours(punch_in_dt, ts)
    update = {"punch_out": ts, "work_hours": hours, "overtime_minutes": compute_overtime(ts, emp, day), "half_day": bool(hours < 4.5)}
    updated = attendance.find_one_and_update({"_id": doc["_id"], "punch_out": None}, {"$set": update}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    day = parse_date(date)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = attendance.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if not old:
        raise HTTPException(404, "attendance record not found")
    if "punch_in" in body.model_fields_set and body.punch_in is None:
        raise HTTPException(422, "punch_in must be epoch milliseconds when supplied")
    if "punch_out" in body.model_fields_set and body.punch_out is None:
        raise HTTPException(422, "punch_out must be epoch milliseconds when supplied")
    if "status" in body.model_fields_set and body.status is None:
        raise HTTPException(422, "status must be a valid status when supplied")
    old_status = old["status"]
    status = body.status if body.status is not None else old_status
    old_in, old_out = old.get("punch_in"), old.get("punch_out")
    new_in = millis_to_dt(body.punch_in) if body.punch_in is not None else old_in
    new_out = millis_to_dt(body.punch_out) if body.punch_out is not None else old_out
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot be supplied with punch times")
        new_in, new_out = None, None
    else:
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_day_for(new_in, emp).isoformat() != day.isoformat():
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if new_out is not None and (new_out <= new_in or new_out > new_in + timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late = compute_late_minutes(new_in, emp, day) if status in PRESENCE and new_in else 0
    hours = compute_work_hours(new_in, new_out) if status in PRESENCE else None
    overtime = compute_overtime(new_out, emp, day) if status in PRESENCE else 0
    half_day = bool(hours is not None and hours < 4.5)
    new_values = {"status": status, "punch_in": new_in, "punch_out": new_out, "work_hours": hours,
                  "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day}
    changed = {}
    for key, new_value in new_values.items():
        old_value = old.get(key, False if key == "half_day" else 0 if key in {"late_minutes", "overtime_minutes"} else None)
        if key in {"punch_in", "punch_out"}:
            same = old_value == new_value
        else:
            same = old_value == new_value
        if not same:
            changed[key] = {"from": old_value, "to": new_value}
    # Compare-and-set the history field so concurrent corrections cannot overwrite each other.
    history_filter = {"history": old["history"]} if "history" in old else {"history": {"$exists": False}}
    if not changed:
        raise HTTPException(422, "request does not change the record")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changed}
    query = {"_id": old["_id"], **history_filter}
    updated = attendance.find_one_and_update(query, {"$set": new_values, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; reload and retry")
    return serialize_attendance(updated)


# ----------------------------- aggregation helpers ---------------------------
def month_match(month: str) -> tuple[str, str]:
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat()


def round_half_up_expr(expression: Any, places: int) -> dict[str, Any]:
    """Mongo expression for non-negative half-up rounding (Mongo $round is half-even)."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}}, scale]}


def working_days_expr(start_expr: Any, end_expr: Any) -> dict[str, Any]:
    # Count weekdays in [start_expr, end_expr], inclusive. The expression executes in MongoDB.
    return {"$size": {"$filter": {"input": {"$map": {"input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": start_expr, "endDate": end_expr, "unit": "day"}}, 1]}]},
        "as": "offset", "in": {"$add": [{"$dayOfWeek": {"$dateAdd": {"startDate": start_expr, "unit": "day", "amount": "$$offset"}}}, 0]}}},
        "as": "dow", "cond": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]}}}}


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start_s, end_s = month_match(month)
    start_dt = datetime.fromisoformat(start_s).replace(tzinfo=UTC)
    end_dt = datetime.fromisoformat(end_s).replace(tzinfo=UTC)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}},
            {"$set": {"_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}}], "as": "logs"}},
        {"$set": {"_period_start": {"$max": [start_dt, {"$dateFromString": {"dateString": "$joined_on"}}]}, "_period_end": end_dt}},
        {"$set": {"working_days": {"$cond": [{"$gt": ["$_period_start", "$_period_end"]}, 0, working_days_expr("$_period_start", "$_period_end")]}}},
        {"$set": {"present_days": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$and": [{"$in": ["$$l.status", list(PRESENCE)]}, {"$gte": ["$$l._dow", 2]}, {"$lte": ["$$l._dow", 6]}]}, {"$cond": ["$$l.half_day", 0.5, 1]}, 0]}}}},
                   "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
                   "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
                   "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},
                   "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.overtime_minutes", 0]}}}}}},
        {"$project": {"_id": 0, "emp_code": 1, "working_days": 1, "present_days": 1, "leave_days": 1, "late_count": 1,
                      "total_late_minutes": 1, "total_overtime_minutes": 1,
                      "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]}, round_half_up_expr({"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 4), None]}}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    parse_month(month)
    if employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")
    rows = list(employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    row = rows[0] if rows else {"emp_code": emp_code, "working_days": 0, "present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": None}
    row["month"] = month
    row["present_days"] = round_half_up(row.get("present_days", 0), 2)
    row["attendance_pct"] = round_half_up(row["attendance_pct"], 4) if row.get("attendance_pct") is not None else None
    return row


def department_summary_pipeline(month: str, department: str | None = None) -> list[dict[str, Any]]:
    start_s, end_s = month_match(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end_s}}
    if department is not None: match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}}], "as": "logs"}},
        {"$set": {"_present": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$and": [{"$in": ["$$l.status", list(PRESENCE)]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$l.date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$l.date"}}}, 6]}]}, {"$cond": ["$$l.half_day", 0.5, 1]}, 0]}}}},
                   "_hours": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$and": [{"$in": ["$$l.status", list(PRESENCE)]}, {"$ne": ["$$l.work_hours", None]}]}, 1, 0]}}}},
                   "_hours_sum": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$and": [{"$in": ["$$l.status", list(PRESENCE)]}, {"$ne": ["$$l.work_hours", None]}]}, "$l.work_hours", 0]}}}},
                   "_late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
                   "_late_sum": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},
                   "_leave": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
                   "_on_duty": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "ON_DUTY"]}}}}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$_present"},
                    "hours_sum": {"$sum": "$_hours_sum"}, "hours_count": {"$sum": "$_hours"}, "late_count": {"$sum": "$_late_count"},
                    "total_late_minutes": {"$sum": "$_late_sum"}, "leave_count": {"$sum": "$_leave"}, "on_duty_count": {"$sum": "$_on_duty"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": round_half_up_expr("$present_days", 2),
                      "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, round_half_up_expr({"$divide": ["$hours_sum", "$hours_count"]}, 2), None]},
                      "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None):
    parse_month(month)
    return {"month": month, "items": list(employees.aggregate(department_summary_pipeline(month, department)))}


def leaderboard_pipeline(month: str, limit: int, department: str | None = None) -> list[dict[str, Any]]:
    start_s, end_s = month_match(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1, "rank": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: str | None = None):
    parse_month(month)
    return {"month": month, "items": list(attendance.aggregate(leaderboard_pipeline(month, limit, department)))}


def trend_pipeline(department: str, start: date, end: date) -> list[dict[str, Any]]:
    """Generate the calendar inside MongoDB; no Python-side date loop or gap-filling."""
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    days_count = (end - start).days + 1
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$set": {"_days": {"$map": {"input": {"$range": [0, days_count]}, "as": "offset",
            "in": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$_days"},
        {"$set": {
            "date": "$_days",
            "date_string": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_days"}},
            "headcount": {"$size": {"$filter": {
                "input": "$employees", "as": "e",
                "cond": {"$lte": ["$$e.joined_on", {"$dateToString": {"format": "%Y-%m-%d", "date": "$_days"}}]}
            }}},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$_days"}, 2]},
                {"$lte": [{"$dayOfWeek": "$_days"}, 6]}
            ]}
        }},
        {"$lookup": {"from": "attendance_logs", "let": {"day_string": "$date_string"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day_string"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
            {"$project": {"_id": 0, "present_count": 1, "late_count": 1}}], "as": "_counts"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$_counts.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$_counts.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round_half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_string", "is_working_day": 1, "headcount": 1,
            "present_count": round_half_up_expr("$present_count", 2), "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, round_half_up_expr("$moving_avg_7d", 4)]}}},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": list(employees.aggregate(trend_pipeline(department, start, end)))}


# ------------------------------ explain endpoint -----------------------------
def explain_find(collection_name: str, query: dict[str, Any], sort: list[tuple[str, int]], page: int, page_size: int) -> dict[str, Any]:
    command: dict[str, Any] = {"find": collection_name, "filter": query, "sort": dict(sort), "skip": (page - 1) * page_size, "limit": page_size}
    return db.command("explain", command, verbosity="executionStats")


def explain_aggregate(collection_name: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return db.command("explain", {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: str | None = None, month: str | None = None,
                     department: str | None = None, limit: int = Query(10, ge=1, le=50),
                     date_from: str | None = None, date_to: str | None = None, status: str | None = None,
                     from_date: str | None = Query(None, alias="from"), to_date: str | None = Query(None, alias="to"),
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, f"endpoint must be one of {', '.join(sorted(allowed))}")
    try:
        if endpoint == "attendance_list":
            if date_from: parse_date(date_from, "date_from")
            if date_to: parse_date(date_to, "date_to")
            if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be on or before date_to")
            q: dict[str, Any] = {}
            if emp_code is not None: q["emp_code"] = emp_code
            if status is not None:
                if status not in STATUSES: raise HTTPException(422, "invalid status")
                q["status"] = status
            if date_from or date_to:
                q["date"] = {}
                if date_from: q["date"]["$gte"] = date_from
                if date_to: q["date"]["$lte"] = date_to
            output = explain_find("attendance_logs", q, [("date", -1), ("emp_code", 1)], page, page_size)
            coll = "attendance_logs"
        else:
            if not month and endpoint != "department_trend": raise HTTPException(422, "month is required")
            if endpoint == "employee_monthly":
                if not emp_code: raise HTTPException(422, "emp_code is required")
                parse_month(month)  # type: ignore[arg-type]
                output, coll = explain_aggregate("employees", employee_monthly_pipeline(emp_code, month)), "employees"  # type: ignore[arg-type]
            elif endpoint == "department_summary":
                parse_month(month)  # type: ignore[arg-type]
                output, coll = explain_aggregate("employees", department_summary_pipeline(month, department)), "employees"  # type: ignore[arg-type]
            elif endpoint == "late_leaderboard":
                parse_month(month)  # type: ignore[arg-type]
                output, coll = explain_aggregate("attendance_logs", leaderboard_pipeline(month, limit, department)), "attendance_logs"  # type: ignore[arg-type]
            else:
                if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
                start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
                if end < start or (end - start).days + 1 > 92: raise HTTPException(422, "invalid trend date range")
                output, coll = explain_aggregate("employees", trend_pipeline(department, start, end)), "employees"
        return {"endpoint": endpoint, "collection": coll, "explain": output}
    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(503, f"MongoDB explain failed: {exc.__class__.__name__}")
