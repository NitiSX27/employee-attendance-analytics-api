# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)` (race guard, per-employee queries, `$lookup` joins); `(date desc, emp_code)` for list sort and month ranges; `(status, date, emp_code)` for status filters. `employees`: unique `emp_code`, `(department, emp_code)`, `(department, joined_on)`, `joined_on`. Rejected: a separate index on `punch_in`; the unique index already finds the latest record by date.

2. **Punch-in race.** Both requests pass validation and call `insert_one`. The unique `(emp_code, date)` index lets one succeed (201); the other gets `DuplicateKeyError`, returned as 409. No check-then-insert.

3. **Ties.** `$rank` ranks (1, 2, 2, 4), then I keep `rank <= limit`. Everyone tied at the cutoff is returned, so there can be more than `limit` rows.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and `$lookup`s logs. Employees with no logs still count as a row.

5. **100x data.** Pre-aggregate monthly stats per employee into a summary collection, rather than computing from raw logs on every request.
