# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)`, the punch-in race guard that also serves the monthly summary and joins; `(date desc, emp_code)` for list order, date ranges and the leaderboard's month scan; `(status, date desc, emp_code)` for status filters; `(emp_code, punch_in desc)` to find the record to punch out. `employees`: unique `emp_code`, `(department, joined_on)` and `joined_on` for headcount. Rejected: an index on `status` alone, since five values are far too unselective.

2. **Punch-in race.** Both requests build the same document and call `insert_one`. The unique index lets the database accept exactly one; the other gets `DuplicateKeyError`, which I turn into 409. There is no read-then-insert check to race.

3. **Ties.** The leaderboard uses `$rank`, so tied employees share a rank. `limit` filters on `rank <= limit` after ranking, so everyone tied at the cutoff is returned and the list can exceed `limit`.

4. **Headcount.** The summary starts from `employees`, not the logs, and joins each person's logs for the month. Someone with no logs still produces a row, so still counts.

5. **100x data.** Pre-aggregate: keep daily per-department totals in a summary collection updated on each write, so analytics read small documents instead of scanning raw logs.
