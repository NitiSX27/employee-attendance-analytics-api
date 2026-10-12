
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, model_validator, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()  # does not override variables already set in the environment

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # datetimes come back as aware UTC, never naive
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
EMP, LOGS = "employees", "attendance_logs"

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
GRACE_SECONDS = 600
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000

DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


@asynccontextmanager
async def lifespan(_app):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def create_indexes():
    """Idempotent: re-running with the same specs is a no-op."""
    db[EMP].create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_uq")
    db[EMP].create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")
    db[EMP].create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    db[EMP].create_index([("joined_on", ASCENDING)], name="joined")
    # unique => the punch-in race guard; also serves per-employee ranges and $lookup joins
    db[LOGS].create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date")
    # global date ranges + the default list sort (date desc, emp_code asc)
    db[LOGS].create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    db[LOGS].create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )


# --------------------------------------------------------------------------- #
# Errors / validation helpers
# --------------------------------------------------------------------------- #
def unprocessable(msg: str, loc: str = "request"):
    raise RequestValidationError([{"type": "value_error", "loc": ("query", loc), "msg": msg, "input": None}])


def parse_date(s: str, name: str = "date") -> Date:
    try:
        if not re.match(DATE_RE, s):
            raise ValueError
        return Date.fromisoformat(s)
    except ValueError:
        unprocessable(f"{name} must be a valid YYYY-MM-DD date", name)


def month_bounds(month: str) -> tuple[str, str]:
    """('2026-07-01', '2026-07-31') for '2026-07'."""
    try:
        y, m = int(month[:4]), int(month[5:])
        last = calendar.monthrange(y, m)[1]
        return Date(y, m, 1).isoformat(), Date(y, m, last).isoformat()
    except ValueError:
        unprocessable("month must be YYYY-MM", "month")


def check_range(a: Optional[str], b: Optional[str], na: str, nb: str):
    da = parse_date(a, na) if a else None
    db_ = parse_date(b, nb) if b else None
    if da and db_ and da > db_:
        unprocessable(f"{na} must not be after {nb}", na)


# --------------------------------------------------------------------------- #
# Time + business-rule helpers (R1-R5)
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def trunc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.replace(microsecond=0)


def now_utc() -> datetime:
    return datetime.now(UTC)


def hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm(shift_end) <= hm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST date of the punch-in; overnight shifts before shift_end belong to the previous day."""
    ist = punch_in.astimezone(IST)
    eh, em = hm(shift_end)
    if is_overnight(shift_start, shift_end) and (ist.hour, ist.minute, ist.second) < (eh, em, 0):
        return ist.date() - timedelta(days=1)
    return ist.date()


def shift_window(day: Date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    sh, sm = hm(shift_start)
    eh, em = hm(shift_end)
    start = datetime.combine(day, time(sh, sm), tzinfo=IST)
    end = datetime.combine(day, time(eh, em), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, day: Date, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    start, _ = shift_window(day, shift_start, shift_end)
    secs = int((punch_in - start).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 to 2 decimals, half-up, using exact decimal arithmetic."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, day: Date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only if >= 30."""
    _, end = shift_window(day, shift_start, shift_end)
    mins = int((punch_out - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def derive(status: str, pi: Optional[datetime], po: Optional[datetime], day: Date, emp: dict) -> dict:
    """All four derived fields for a final (status, punch_in, punch_out)."""
    if status not in PRESENCE or pi is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(pi, day, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if po is not None:
        wh = compute_work_hours(pi, po)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(po, day, emp["shift_start"], emp["shift_end"])
        out["half_day"] = wh < 4.5
    return out


def round_half_up(x, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(x)).quantize(q, ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
Ms = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]  # ints only: no floats, strings, seconds
StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v):
        Date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[Ms] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[Ms] = None


class RegularizeIn(BaseModel):
    status: Optional[StatusT] = None
    punch_in: Optional[Ms] = None
    punch_out: Optional[Ms] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serializers (Mongo document -> API shape; no _id, instants as epoch ms)
# --------------------------------------------------------------------------- #
def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": to_ms(d.get("created_at")),
    }


def _history_value(field: str, v):
    return to_ms(v) if field in ("punch_in", "punch_out") else v


def record_out(d: dict) -> dict:
    history = [
        {
            "at": to_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": {
                k: {"from": _history_value(k, c.get("from")), "to": _history_value(k, c.get("to"))}
                for k, c in h.get("changes", {}).items()
            },
        }
        for h in d.get("history") or []
    ]
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day")),
        "history": history,
    }


def get_employee(emp_code: str) -> dict:
    emp = db[EMP].find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db[EMP].insert_one(doc)  # the unique index decides concurrent duplicates
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db[EMP].count_documents(q)
    cur = db[EMP].find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else trunc(now_utc())
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db[LOGS].insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else trunc(now_utc())
    # latest record whose punch_in <= ts (a null punch_in never satisfies a range filter)
    rec = db[LOGS].find_one(
        {"emp_code": body.emp_code, "date": {"$lte": ts.astimezone(IST).date().isoformat()}, "punch_in": {"$lte": ts}},
        sort=[("date", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pi = trunc(rec["punch_in"])
    if ts <= pi:
        unprocessable("punched_at must be after punch_in", "punched_at")
    if ts - pi > timedelta(hours=24):
        unprocessable("punched_at is more than 24 hours after punch_in", "punched_at")

    day = Date.fromisoformat(rec["date"])
    d = derive(rec["status"], pi, ts, day, emp)
    changes = {"punch_out": ts, "work_hours": d["work_hours"], "overtime_minutes": d["overtime_minutes"], "half_day": d["half_day"]}
    # conditional update: only the first of two simultaneous punch-outs matches
    res = db[LOGS].update_one({"_id": rec["_id"], "punch_out": None}, {"$set": changes})
    if res.modified_count == 0:
        raise HTTPException(409, "already punched out")
    return record_out({**rec, **changes})


def attendance_spec(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """The one place the list query is built; the endpoint and /admin/explain both use it."""
    q: dict = {}
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
    return {
        "coll": LOGS,
        "find": {"filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size},
    }


DateQ = Annotated[Optional[str], Query(pattern=DATE_RE)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: DateQ = None,
    date_to: DateQ = None,
    status: Optional[StatusT] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    check_range(date_from, date_to, "date_from", "date_to")
    f = attendance_spec(emp_code, date_from, date_to, status, page, page_size)["find"]
    coll = db[LOGS]
    total = coll.count_documents(f["filter"]) if f["filter"] else coll.estimated_document_count()
    cur = coll.find(f["filter"], {"_id": 0}, sort=list(f["sort"].items()), skip=f["skip"], limit=f["limit"])
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = get_employee(emp_code)
    rec = db[LOGS].find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status = rec["status"]
    old_pi, old_po = trunc(rec.get("punch_in")), trunc(rec.get("punch_out"))
    status = body.status or old_status
    new_pi = from_ms(body.punch_in) if body.punch_in is not None else None
    new_po = from_ms(body.punch_out) if body.punch_out is not None else None

    if status in PRESENCE:
        pi = new_pi or old_pi
        po = new_po or old_po
        if pi is None:
            unprocessable("a presence status requires punch_in")
        if attendance_date(pi, emp["shift_start"], emp["shift_end"]).isoformat() != date:
            unprocessable("punch_in must fall on the record's attendance date")
        if po is not None and not (pi < po <= pi + timedelta(hours=24)):
            unprocessable("punch_out must be after punch_in and within 24 hours")
    else:  # ABSENT / LEAVE clear both punch times
        if new_pi or new_po:
            unprocessable("punch times cannot be supplied with status ABSENT or LEAVE")
        pi = po = None

    if (status, pi, po) == (old_status, old_pi, old_po):
        unprocessable("request changes nothing")

    new = {"status": status, "punch_in": pi, "punch_out": po, **derive(status, pi, po, Date.fromisoformat(date), emp)}
    old = {
        "status": old_status, "punch_in": old_pi, "punch_out": old_po,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day")),
    }
    changes = {k: {"from": old[k], "to": v} for k, v in new.items() if old[k] != v}
    entry = {"at": trunc(now_utc()), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n_hist = len(rec.get("history") or [])

    # optimistic concurrency: only apply if nobody changed history / punches since we read the record
    res = db[LOGS].update_one(
        {
            "_id": rec["_id"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n_hist]},
        },
        {"$set": new, "$push": {"history": entry}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out({**rec, **new, "history": (rec.get("history") or []) + [entry]})


# --------------------------------------------------------------------------- #
# Analytics - pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def _present_value(weekday_only: bool):
    """Per-log contribution to present_days: 1, 0.5 for a half day, 0 if not a presence status."""
    cond = [{"$in": ["$status", PRESENCE]}]
    if weekday_only:
        cond.append({"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]})
    return {
        "$cond": [
            {"$and": cond},
            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
            0,
        ]
    }


_LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
_IS_LATE = {"$cond": [{"$gt": [_LATE_MIN, 0]}, 1, 0]}
_IS = lambda s: {"$cond": [{"$eq": ["$status", s]}, 1, 0]}  # noqa: E731
_HAS_HOURS = {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]}


def _half_up(expr, places: int):
    """Half-up rounding inside the pipeline ($round is banker's). The epsilon absorbs float noise."""
    k = 10**places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, k]}, 0.5, 1e-7]}}, k]}


def monthly_spec(emp_code: str, month: str) -> dict:
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_value(True)},
                "leave_days": {"$sum": _IS("LEAVE")},
                "late_count": {"$sum": _IS_LATE},
                "total_late_minutes": {"$sum": _LATE_MIN},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    return {"coll": LOGS, "pipeline": pipeline, "hint": "emp_date"}


def summary_spec(month: str, department: Optional[str]) -> dict:
    start, end = month_bounds(month)
    match: dict = {"joined_on": {"$lte": end}}  # R9
    if department is not None:
        match["department"] = department
    per_employee = [  # one small doc per employee, computed from that employee's logs in the month
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_value(True)},
                "late_count": {"$sum": _IS_LATE},
                "total_late_minutes": {"$sum": _LATE_MIN},
                "leave_count": {"$sum": _IS("LEAVE")},
                "on_duty_count": {"$sum": _IS("ON_DUTY")},
                # work hours kept as integer hundredths so the sum is exact
                "wh_sum": {"$sum": {"$cond": [_HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                "wh_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
            }
        },
    ]
    fields = ["present_days", "late_count", "total_late_minutes", "leave_count", "on_duty_count", "wh_sum", "wh_n"]
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": LOGS, "localField": "emp_code", "foreignField": "emp_code", "pipeline": per_employee, "as": "s"}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # counts employees, with or without logs
                **{f: {"$sum": {"$ifNull": [{"$arrayElemAt": [f"$s.{f}", 0]}, 0]}} for f in fields},
            }
        },
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, **{f: 1 for f in fields}}},
    ]
    return {"coll": EMP, "pipeline": pipeline, "hint": "dept_joined" if department is not None else "joined"}


def leaderboard_spec(month: str, limit: int, department: Optional[str]) -> dict:
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": _LATE_MIN}, "late_count": {"$sum": _IS_LATE}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": EMP, "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees drop out here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff all stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                "total_late_minutes": 1, "late_count": 1,
            }
        },
    ]
    return {"coll": LOGS, "pipeline": pipeline, "hint": "date_emp"}


def trend_spec(department: str, d_from: Date, d_to: Date) -> dict:
    n_days = (d_to - d_from).days + 1
    start = datetime.combine(d_from, time(0), tzinfo=UTC)
    per_log = [
        {"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$project": {"_id": 0, "date": 1, "p": _present_value(False), "l": _IS_LATE}},
    ]
    # one zero-valued row per calendar day, so days without any log still appear (gap filling)
    calendar_rows = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$set": {"n": {"$range": [0, n_days]}}},
        {"$unwind": "$n"},
        {
            "$project": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$n"}}}},
                "present_count": {"$literal": 0},
                "late_count": {"$literal": 0},
            }
        },
    ]
    headcount = [  # R9 per day
        {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
        {"$count": "n"},
    ]
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {"from": LOGS, "localField": "emp_code", "foreignField": "emp_code", "pipeline": per_log, "as": "s"}},
        {"$unwind": "$s"},
        {"$group": {"_id": "$s.date", "present_count": {"$sum": "$s.p"}, "late_count": {"$sum": "$s.l"}}},
        {"$unionWith": {"coll": EMP, "pipeline": calendar_rows}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$sort": {"_id": 1}},
        {"$lookup": {"from": EMP, "let": {"day": "$_id"}, "pipeline": headcount, "as": "hc"}},
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, 5]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        _half_up({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        # $avg ignores nulls; the window never reaches rows before the first day of the range
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"ma": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
        {
            "$project": {
                "_id": 0, "date": "$_id", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$cond": [{"$eq": ["$ma", None]}, None, _half_up("$ma", 4)]},
            }
        },
    ]
    return {"coll": EMP, "pipeline": pipeline, "hint": None}


def run_pipeline(spec: dict) -> list:
    opts = {"hint": spec["hint"]} if spec["hint"] else {}  # never send hint=None to the server
    return list(db[spec["coll"]].aggregate(spec["pipeline"], **opts))


# --------------------------------------------------------------------------- #
# Analytics - endpoints
# --------------------------------------------------------------------------- #
MonthQ = Annotated[str, Query(pattern=MONTH_RE)]


def count_weekdays(a: Date, b: Date) -> int:
    n, d = 0, a
    while d <= b:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    emp = get_employee(emp_code)
    first, last = (Date.fromisoformat(x) for x in month_bounds(month))
    working_days = count_weekdays(max(first, Date.fromisoformat(emp["joined_on"])), last)  # R7
    rows = run_pipeline(monthly_spec(emp_code, month))
    r = rows[0] if rows else {}
    present = r.get("present_days", 0)
    pct = round_half_up(Decimal(str(present)) * 100 / working_days, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present,
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    items = []
    for r in run_pipeline(summary_spec(month, department)):
        n = r.pop("wh_n")
        total = r.pop("wh_sum")
        r["avg_work_hours"] = round_half_up(Decimal(total) / n / 100, 2) if n else None
        items.append(r)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    return {"month": month, "items": run_pipeline(leaderboard_spec(month, limit, department))}


def check_trend_range(frm: str, to: str) -> tuple[Date, Date]:
    d_from, d_to = parse_date(frm, "from"), parse_date(to, "to")
    if d_to < d_from:
        unprocessable("to must not be before from", "to")
    if (d_to - d_from).days + 1 > 92:
        unprocessable("range must not exceed 92 days", "to")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=DATE_RE)],
    to: Annotated[str, Query(pattern=DATE_RE)],
):
    d_from, d_to = check_trend_range(from_, to)
    if db[EMP].find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": run_pipeline(trend_spec(department, d_from, d_to))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _require(**kw):
    missing = [k for k, v in kw.items() if v is None]
    if missing:
        unprocessable(f"missing required parameter(s): {', '.join(missing)}", missing[0])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_RE)] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: DateQ = None,
    date_to: DateQ = None,
    status: Optional[StatusT] = None,
    from_: Annotated[Optional[str], Query(alias="from", pattern=DATE_RE)] = None,
    to: DateQ = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        check_range(date_from, date_to, "date_from", "date_to")
        spec = attendance_spec(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        spec = monthly_spec(emp_code, month)
    elif endpoint == "department_summary":
        _require(month=month)
        spec = summary_spec(month, department)
    elif endpoint == "late_leaderboard":
        _require(month=month)
        spec = leaderboard_spec(month, limit, department)
    else:
        _require(department=department, **{"from": from_}, to=to)
        spec = trend_spec(department, *check_trend_range(from_, to))

    if "find" in spec:
        f = spec["find"]
        inner = {"find": spec["coll"], "filter": f["filter"], "sort": f["sort"], "skip": f["skip"], "limit": f["limit"]}
    else:
        inner = {"aggregate": spec["coll"], "pipeline": spec["pipeline"], "cursor": {}}
        if spec["hint"]:
            inner["hint"] = spec["hint"]
    raw = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": spec["coll"], "explain": json.loads(json_util.dumps(raw))}
