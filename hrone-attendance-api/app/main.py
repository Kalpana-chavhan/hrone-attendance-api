"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is read for convenience; real environment variables win)

Layout of this file (everything lives here, as the assignment requires):
  1. Configuration and database handle
  2. Time, rounding and business-rule helpers (R1-R5, R7, R8)
  3. Validation types and request models
  4. Serialisation (MongoDB document -> API JSON)
  5. Index management (created at startup, idempotent)
  6. App, error handling, /health
  7. Employees
  8. Attendance (punch-in, punch-out, list, regularize)
  9. Analytics pipeline builders + endpoints
 10. Admin explain endpoint
"""
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration and database handle
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False -> real environment variables take priority over .env

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True makes PyMongo return timezone-aware (UTC) datetimes instead of naive ones.
# MongoClient is thread-safe and pools connections, so the sync endpoints below
# (run by FastAPI in a thread pool) can serve parallel requests safely.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

# --------------------------------------------------------------------------- #
# 2. Time, rounding and business-rule helpers
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000  # contract: reject seconds-looking / absurd values

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW_HUNDREDTHS = 450  # R5: rounded work_hours < 4.50
MAX_SHIFT = timedelta(hours=24)


def utc_now_s() -> datetime:
    """Current instant, truncated to whole seconds (R1)."""
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Make any datetime timezone-aware UTC (naive values are assumed to already be UTC)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def ms_to_dt(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Integer epoch milliseconds using integer arithmetic (no float rounding)."""
    if dt is None:
        return None
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def instant_from_ms(ms: Optional[int]) -> datetime:
    """Client instant -> UTC datetime truncated to whole seconds; None means 'now' (R1)."""
    return utc_now_s() if ms is None else ms_to_dt(ms).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def round_half_up(value: Any, places: int) -> float:
    """R8: half-up rounding (Python's round() is banker's rounding on floats - wrong here)."""
    quantum = Decimal(1).scaleb(-places)
    return float(to_decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift ends on the next calendar day. ("HH:MM" sorts correctly as text.)"""
    return shift_end <= shift_start


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: the IST calendar date of the punch-in, or the previous day for an early-morning overnight punch-in."""
    local = as_utc(punch_in).astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_bounds(att_date: date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Concrete IST start/end instants of the shift that belongs to `att_date`."""
    start = datetime.combine(att_date, parse_hhmm(shift_start), tzinfo=IST)
    end_day = att_date + timedelta(days=1) if is_overnight(shift_start, shift_end) else att_date
    end = datetime.combine(end_day, parse_hhmm(shift_end), tzinfo=IST)
    return start, end


def compute_late_minutes(punch_in: datetime, att_date: date, shift_start: str, shift_end: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then whole minutes since shift start."""
    start, _ = shift_bounds(att_date, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, att_date: date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, counted only when at least 30."""
    _, end = shift_bounds(att_date, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds()) // 60  # floor
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def work_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    """R4 in integer hundredths of an hour, rounded half-up: seconds / 3600 * 100 = seconds / 36."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (seconds + 18) // 36  # floor(x + 0.5) with x = seconds / 36


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  att_date: date, shift_start: str, shift_end: str) -> dict:
    """All server-computed fields for one record (matches the table in DATA_MODEL.md)."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, att_date, shift_start, shift_end)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hundredths = work_hundredths(punch_in, punch_out)
    return {
        "work_hours": hundredths / 100,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, att_date, shift_start, shift_end),
        "half_day": hundredths < HALF_DAY_BELOW_HUNDREDTHS,
    }


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    year, mon = int(month[:4]), int(month[5:7])
    return date(year, mon, 1).isoformat(), date(year, mon, calendar.monthrange(year, mon)[1]).isoformat()


def working_days(first: date, last: date, joined_on: date) -> int:
    """R7: Mon-Fri days in [first, last], counted from joined_on for a mid-month joiner."""
    start = max(first, joined_on)
    if start > last:
        return 0
    return sum(1 for i in range((last - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 3. Validation types and request models
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def _valid_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ValueError("must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a real calendar date")
    return value


def _valid_month(value: str) -> str:
    if not MONTH_RE.fullmatch(value) or int(value[:4]) < 1:
        raise ValueError("must be YYYY-MM")
    return value


DateStr = Annotated[str, AfterValidator(_valid_date)]
MonthStr = Annotated[str, AfterValidator(_valid_month)]
# strict=True: floats, strings and booleans are rejected instead of being coerced to int.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def unprocessable(loc: list, msg: str) -> HTTPException:
    """422 in FastAPI's own validation-error shape, for rules that need database state."""
    return HTTPException(422, detail=[{"loc": loc, "msg": msg, "type": "value_error"}])


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: DateStr

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # Omitted means "unchanged"; an explicit null is not a valid value for these fields.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


# --------------------------------------------------------------------------- #
# 4. Serialisation (MongoDB document -> API JSON)
# --------------------------------------------------------------------------- #
def ser_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _ser_change_value(field: str, value: Any) -> Any:
    # punch_in / punch_out are BSON dates inside history too, and must become epoch ms.
    return dt_to_ms(value) if field in ("punch_in", "punch_out") else value


def ser_history_entry(entry: dict) -> dict:
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _ser_change_value(field, c.get("from")), "to": _ser_change_value(field, c.get("to"))}
            for field, c in (entry.get("changes") or {}).items()
        },
    }


def ser_attendance(doc: dict) -> dict:
    """_id is never exposed; missing legacy fields get their documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [ser_history_entry(e) for e in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 5. Index management (idempotent; runs at startup)
# --------------------------------------------------------------------------- #
INDEXES: dict[str, list[tuple[list[tuple[str, int]], dict]]] = {
    "employees": [
        # emp_code identity + the duplicate guard for concurrent POST /employees
        ([("emp_code", 1)], {"name": "uniq_emp_code", "unique": True}),
        # GET /employees?department=  (filter + sort by emp_code without an in-memory sort)
        ([("department", 1), ("emp_code", 1)], {"name": "dept_emp_code"}),
        # headcount (R9) inside one department, and the trend / summary $match
        ([("department", 1), ("joined_on", 1)], {"name": "dept_joined_on"}),
        # headcount (R9) across all departments
        ([("joined_on", 1)], {"name": "joined_on"}),
    ],
    "attendance_logs": [
        # one record per (emp_code, date): the race guard for punch-in, and the index behind the
        # employee monthly summary, the per-employee $lookup joins and ?emp_code= listings
        ([("emp_code", 1), ("date", 1)], {"name": "uniq_emp_date", "unique": True}),
        # GET /attendance default order (date desc, emp_code asc), date ranges, leaderboard month scan
        ([("date", -1), ("emp_code", 1)], {"name": "date_desc_emp"}),
        # GET /attendance?status=...
        ([("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp"}),
        # punch-out: latest record of an employee with punch_in <= punched_at
        ([("emp_code", 1), ("punch_in", -1)], {"name": "emp_punch_in_desc"}),
    ],
}

_index_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes() -> None:
    """create_index is a no-op when the same index already exists, so this is safe to call repeatedly."""
    global _indexes_ready
    with _index_lock:
        if _indexes_ready:
            return
        failed = False
        for collection, specs in INDEXES.items():
            for keys, options in specs:
                try:
                    db[collection].create_index(keys, **options)
                except ConnectionFailure:
                    # MongoDB is unreachable: stop here instead of waiting out a timeout per index.
                    # /health keeps retrying, so the indexes still get created once the database is up.
                    log.warning("MongoDB unreachable, skipping index creation for now")
                    return
                except PyMongoError:
                    failed = True
                    log.exception("could not create index %s on %s", options["name"], collection)
        _indexes_ready = not failed


# --------------------------------------------------------------------------- #
# 6. App, error handling, /health
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await run_in_threadpool(ensure_indexes)
    except Exception:  # never stop the app from booting; /health retries and reports DB trouble
        log.exception("index creation at startup failed")
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("database error: %r", exc)
    return JSONResponse(status_code=503, content={"detail": "database error"})


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:
        ensure_indexes()  # DB was down at boot, or an earlier attempt failed: try again
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 7. Employees
# --------------------------------------------------------------------------- #
def paginate(collection, query: dict, sort: list, page: int, page_size: int, projection: Optional[dict] = None):
    """total always reflects the filters (R10). Skipping past the end returns an empty page without a huge skip."""
    total = db[collection].count_documents(query)
    skip = (page - 1) * page_size
    if skip >= total:
        return [], total
    cursor = db[collection].find(query, projection).sort(sort).skip(skip).limit(page_size)
    return list(cursor), total


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(UTC)
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)  # BSON dates hold milliseconds
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    docs, total = paginate("employees", query, [("emp_code", 1)], page, page_size)
    return {"items": [ser_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 8. Attendance
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = instant_from_ms(body.punched_at)
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, att_date, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        # Race-safe: the unique (emp_code, date) index lets exactly one concurrent insert win.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = instant_from_ms(body.punched_at)
    # Most recent record whose punch_in is at or before the punch-out (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punched_in = as_utc(rec["punch_in"])
    if ts <= punched_in:
        raise unprocessable(["body", "punched_at"], "punched_at must be after punch_in")
    if ts - punched_in > MAX_SHIFT:
        raise unprocessable(["body", "punched_at"], "punched_at is more than 24 hours after punch_in")

    derived = derive_fields(
        rec["status"], punched_in, ts, parse_date(rec["date"]), emp["shift_start"], emp["shift_end"]
    )
    # Atomic compare-and-set: the filter only matches while punch_out is still empty, so of two
    # simultaneous punch-outs exactly one updates the record and the other gets 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": derived["work_hours"],
                  "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return ser_attendance(updated)


def attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                      status: Optional[str]) -> dict:
    if date_from and date_to and date_from > date_to:
        raise unprocessable(["query", "date_from"], "date_from must not be after date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[DateStr] = None,
                    date_to: Optional[DateStr] = None, status: Optional[Status] = None,
                    page: Page = 1, page_size: PageSize = 20):
    query = attendance_filter(emp_code, date_from, date_to, status)
    docs, total = paginate("attendance_logs", query, ATTENDANCE_SORT, page, page_size, {"_id": 0})
    return {"items": [ser_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")
    att_date = parse_date(date)

    cur_status = rec["status"]
    cur_in, cur_out = as_utc(rec.get("punch_in")), as_utc(rec.get("punch_out"))
    req_in = instant_from_ms(body.punch_in) if body.punch_in is not None else None
    req_out = instant_from_ms(body.punch_out) if body.punch_out is not None else None

    # A request that changes nothing is a 422.
    if ((body.status is None or body.status == cur_status)
            and (req_in is None or req_in == cur_in) and (req_out is None or req_out == cur_out)):
        raise unprocessable(["body"], "request changes nothing")

    new_status = body.status or cur_status
    if new_status in ("ABSENT", "LEAVE"):
        # ABSENT / LEAVE clear both punch times, so supplying one is contradictory.
        if req_in is not None or req_out is not None:
            raise unprocessable(["body"], f"{new_status} cannot be combined with punch_in / punch_out")
        new_in = new_out = None
    else:
        new_in = req_in or cur_in
        if new_in is None:
            raise unprocessable(["body", "punch_in"], "a presence status requires a punch_in")
        new_out = req_out or cur_out

    if req_in is not None and attendance_date(req_in, emp["shift_start"], emp["shift_end"]) != att_date:
        raise unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
    if new_out is not None and (req_in is not None or req_out is not None):
        if new_out <= new_in:
            raise unprocessable(["body", "punch_out"], "punch_out must be after punch_in")
        if new_out - new_in > MAX_SHIFT:
            raise unprocessable(["body", "punch_out"], "punch_out must be within 24 hours of punch_in")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(new_status, new_in, new_out, att_date, emp["shift_start"], emp["shift_end"])}
    old = {"status": cur_status, "punch_in": cur_in, "punch_out": cur_out,
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
           "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {field: {"from": old[field], "to": new[field]} for field in new if old[field] != new[field]}
    if not changes:
        raise unprocessable(["body"], "request changes nothing")

    entry = {"at": utc_now_s(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record is exactly as we read it
    # (same core fields, same history length). A concurrent punch-out or correction makes the
    # filter miss, so nobody's history entry is silently lost -> the loser gets 409.
    hist = rec.get("history")
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "history": {"$size": len(hist)} if isinstance(hist, list) else hist,
    }
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return ser_attendance(updated)


# --------------------------------------------------------------------------- #
# 9. Analytics: pipeline builders (shared with the explain endpoint) + endpoints
# --------------------------------------------------------------------------- #
def _log_date() -> dict:
    """The stored 'YYYY-MM-DD' string as a real date, so $dayOfWeek can be used on it."""
    return {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None, "onNull": None}}


def _is_present() -> dict:
    return {"$in": ["$status", list(PRESENCE_STATUSES)]}


def _is_weekday() -> dict:
    # $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6
    return {"$in": [{"$dayOfWeek": _log_date()}, [2, 3, 4, 5, 6]]}


def _presence_weight() -> dict:
    """1 for a presence record, 0.5 for a half day, 0 otherwise (R5, R6)."""
    half = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
    return {"$cond": [_is_present(), half, 0]}


def _late_flag() -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def _log_stats() -> dict:
    """$group accumulators over a set of attendance records (stored derived values are authoritative)."""
    has_hours = {"$and": [_is_present(), {"$isNumber": "$work_hours"}]}
    return {
        # present_days counts Mon-Fri only (R7); half days weigh 0.5 (R5)
        "present_days": {"$sum": {"$cond": [_is_weekday(), _presence_weight(), 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_days": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": _late_flag()},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        # Decimal128 sum so the later average is exact (no float drift in 2-decimal hours)
        "work_hours_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, 0]}},
        "work_hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
    }


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": None, **_log_stats()}},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from employees so people with zero logs still count (R9); logs are joined per employee."""
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {"_id": None, **_log_stats()}},
            ],
            "as": "m",
        }},
        # preserveNullAndEmptyArrays keeps employees whose join produced nothing
        {"$unwind": {"path": "$m", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$m.present_days", 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": ["$m.work_hours_sum", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$m.work_hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$m.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$m.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$m.leave_days", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$m.on_duty_days", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": _late_flag()}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def _half_up_4(expr: Any) -> dict:
    """Round a Decimal128 expression to 4 places half-up. $round would round half-to-even, so use floor(x*10^4 + 0.5)."""
    scale = Decimal128("10000")
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, Decimal128("0.5")]}}, scale]}


def department_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    first, last = d_from.isoformat(), d_to.isoformat()
    lower = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    upper = lower + timedelta(days=(d_to - d_from).days + 1)  # $densify's upper bound is exclusive
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "date": 1, "weight": _presence_weight(), "late": _late_flag()}},
            ],
            "as": "logs",
        }},
        # Employees without logs yield one row with no date; it is mapped to `from` below. That
        # guarantees $densify always has at least one input document to work from.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$logs.date",
                    "present_count": {"$sum": {"$ifNull": ["$logs.weight", 0]}},
                    "late_count": {"$sum": {"$ifNull": ["$logs.late", 0]}}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": {"$ifNull": ["$_id", first]},
                                                    "format": "%Y-%m-%d"}}}},
        {"$group": {"_id": "$day", "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        {"$sort": {"day": 1}},
        # Gap filling: exactly one row per calendar day in [from, to]
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        # R9: headcount for each day = department employees who have joined on or before that day
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$match": {"$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$addFields": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _half_up_4({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}),
            None,
        ]}}},
        # 7-row moving average inside the requested range ($avg ignores null rates)
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"mavg": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$toDouble": "$rate"},
            "moving_avg_7d": {"$cond": [{"$eq": [{"$ifNull": ["$mavg", None]}, None]}, None,
                                        {"$toDouble": _half_up_4("$mavg")}]},
        }},
    ]


MonthQuery = Annotated[str, AfterValidator(_valid_month)]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQuery):
    emp = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    stats = rows[0] if rows else {}
    days = working_days(parse_date(first), parse_date(last), parse_date(emp["joined_on"]))
    present = to_decimal(stats.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(present / days * 100, 2) if days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQuery, department: Optional[str] = None):
    rows = db.employees.aggregate(department_summary_pipeline(month, department))
    items = []
    for r in rows:
        hours_n = int(r["work_hours_count"])
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            # mean over records (sum of hours / number of records), not a mean of per-employee means
            "avg_work_hours": round_half_up(to_decimal(r["work_hours_sum"]) / hours_n, 2) if hours_n else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQuery, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def validate_trend_range(d_from: str, d_to: str) -> tuple[date, date]:
    start, end = parse_date(d_from), parse_date(d_to)
    if end < start:
        raise unprocessable(["query", "to"], "to must not be before from")
    if (end - start).days + 1 > 92:
        raise unprocessable(["query", "to"], "range must not exceed 92 days")
    return start, end


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_: Annotated[DateStr, Query(alias="from")],
                     to: DateStr):
    start, end = validate_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    rows = list(db.employees.aggregate(department_trend_pipeline(department, start, end)))
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# 10. Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary",
                        "late_leaderboard", "department_trend"]


def _explain(command: dict) -> dict:
    out = db.command({"explain": command, "verbosity": "executionStats"})
    # BSON types (dates, Decimal128, ...) -> plain JSON
    return json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))


def _require(**values: Any) -> None:
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise unprocessable(["query", missing[0]], f"missing required query parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """Runs MongoDB's explain (executionStats) on the same query / pipeline the real endpoint builds."""
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": query, "sort": dict(ATTENDANCE_SORT),
                   "skip": (page - 1) * page_size, "limit": page_size, "projection": {"_id": 0}}
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        _require(month=month)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_summary_pipeline(month, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        _require(month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(month, limit, department),
                   "cursor": {}}
    else:  # department_trend
        _require(department=department, **{"from": from_, "to": to})
        start, end = validate_trend_range(from_, to)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_trend_pipeline(department, start, end),
                   "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": _explain(command)}
