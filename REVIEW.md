# REVIEW.md

Defects found in the starter `app/main.py`, and how each was fixed.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Shift start is built with `punch_in.replace(hour, minute)` on a UTC/naive datetime, so it is not IST and not tied to the attendance date. | Punch in 10:05 IST: the "start" is 09:30 UTC, so the result is wrong by 5.5 hours. | Build the shift start in IST for the attendance date (`shift_window`). |
| 2 | `compute_late_minutes` | `int(seconds/60)` is floored before the `> 10` test. 09:40:01 gives 10, which is not `> 10`, so it returns 0. R2 says 10. | Shift 09:30, punch 09:40:01 expects 10, gets 0. | Compare in seconds (`secs > 600`), then floor to minutes. |
| 3 | `compute_late_minutes` | No overnight support: a 22:00 shift punched at 01:00 compares against 22:00 of the wrong day. | Overnight employee, punch-in after midnight. | `attendance_date` (R1) picks the shift's day, then `shift_window` builds start and end from it. |
| 4 | `compute_work_hours` | Python `round()` is banker's rounding on a float, not half-up. Sub-second parts are not truncated. | 16218 s = 4.505 h; expected 4.51, got 4.5 (or 4.51 by luck of the float). | Whole seconds, exact `Decimal` arithmetic, `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | Subtracts an aware IST datetime from the naive datetime PyMongo returns, so it raises `TypeError` (500). Also: no 30-minute minimum, no overnight shift end, `max(0, ...)` instead of the threshold. | Any punch-out on a record read from Mongo; 20 min after shift end should be 0, was 20. | `tz_aware=True` client, shift end per R1, minutes counted only if >= 30. |
| 6 | `/health` | Always returns ok without touching MongoDB. | Stop MongoDB: still 200. | Pings MongoDB, returns 503 on failure. |
| 7 | `create_employee` | Check-then-insert race, and no unique index. | Parallel POSTs with the same `emp_code` give several 201s. | Unique index on `emp_code`, catch `DuplicateKeyError` -> 409. |
| 8 | `create_employee` | `created_at = datetime.now()` is naive local time, and the response returns a datetime, not epoch ms. | Client machine not on UTC; response `created_at` is an ISO string. | Server-side UTC, serialized as epoch ms. |
| 9 | `EmployeeIn` | No validation: bad `emp_code`, shift format, email, impossible dates, `shift_start == shift_end` are accepted. | `joined_on: "2026-02-30"` returns 201. | Patterns and lengths from the contract, real date check, shift check -> 422. |
| 10 | `list_employees` | `skip = page * page_size` skips the first page. | `page=1` returns the second page; `page=0` gives a negative skip -> 500. | `(page - 1) * page_size`, `page >= 1`, `page_size` 1..100. |
| 11 | `list_employees` | `total` counts the whole collection, ignoring the department filter. No sort. | `department=X`: `total` is the global count; order is arbitrary. | `count_documents(q)`, sort by `emp_code`. |
| 12 | `punch_in` | Unknown employee: `emp` is `None`, so `emp["shift_start"]` raises `TypeError` -> 500. | POST an unknown `emp_code`. | `get_employee` -> 404. |
| 13 | `punch_in` | `punched_at` is not validated, and the date uses server-local time. Seconds, floats and strings are accepted or crash; no truncation to seconds; no IST date; no overnight rule. | `punched_at: 1783312500` (seconds) is accepted as 1970. | Strict int in [1e11, 4.1e12] -> 422; R1 date; truncate to whole seconds. |
| 14 | `punch_in` | `status` is any string, so `ABSENT` or `"banana"` can be stored. | POST `status: "ABSENT"` returns 201. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 15 | `punch_in` | `find_one` then `insert_one` is a race. | Two simultaneous requests both get 201 and create duplicate records. | Unique `(emp_code, date)` index; the loser's `DuplicateKeyError` -> 409. |
| 16 | `punch_in` response | Returns an `id` (the ObjectId) and raw datetimes. | Response has `id`, no epoch ms. | `record_out` builds the contract shape. |
| 17 | `list_attendance` | Loads the whole collection into memory, then sorts and slices in Python; no indexes. | Slow and memory-heavy at 100k records. | Filter, sort, skip and limit in MongoDB, backed by indexes. |
| 18 | `list_attendance` | Sorts by `date` only (ties unstable; spec says date desc, then `emp_code` asc). No validation of `status`, dates, `page`, `page_size`, or `date_from > date_to`. Exposes `id`; legacy records lack `history` and `half_day`. | `status=foo` returns 200; `page=0`. | Typed query params, 422 on bad ranges, `record_out` defaults missing fields. |
| 19 | startup | No indexes at all; client returns naive datetimes. | `explain` shows COLLSCAN. | Idempotent index creation in the lifespan hook; `tz_aware=True`. |

**Looked at and judged fine:** `load_dotenv()` does not override real environment variables, which is what the brief asks for. The IST constant (+05:30) is correct. The `"09:30"` / `"18:30"` defaults match the contract. `HTTPException(409, ...)` gives the default `{"detail": ...}` body the contract expects. Splitting `"HH:MM"` on `:` is fine once the format is validated.
