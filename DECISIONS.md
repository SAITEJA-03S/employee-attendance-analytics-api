# DECISIONS.md

1. **Indexes.**
   - Unique index `{emp_code: 1}` on `employees`: Enforces business code uniqueness and guarantees concurrent create safety.
   - Unique compound index `{emp_code: 1, date: 1}` on `attendance_logs`: Guarantees single record per employee per date and powers race-safe punch-ins.
   - Compound index `{date: -1, emp_code: 1}` on `attendance_logs`: Backs `/attendance` listing with fast index scans for sorting and date range filters.
   - Compound index `{department: 1, joined_on: 1, emp_code: 1}` on `employees`: Accelerates cross-collection department analytics and headcount queries.
   - *Rejected*: Quad-field index `{date: 1, status: 1, late_minutes: -1, emp_code: 1}` as existing compound indexes already efficiently cover the pipeline equality filters and sorts without write overhead.

2. **Punch-in race.**
   When two identical punch-in requests arrive simultaneously, both validate the employee and construct the record for date $D$. Both issue `insert_one()`. The underlying MongoDB unique compound index `{emp_code: 1, date: 1}` atomically accepts the first write (yielding 201) and throws a `DuplicateKeyError` for the second write. The application catches `DuplicateKeyError` and returns HTTP 409 Conflict. No check-then-insert race window exists.

3. **Ties.**
   The leaderboard aggregation uses MongoDB `$setWindowFields` with `$rank` to compute standard competition ranking (1, 2, 2, 4). The condition `rank <= limit` applies after ranking. If two employees tie at rank 10 with `limit=10`, both are assigned rank 10 and included in the output (returning 11 items). Tied rows are ordered by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
   The department summary pipeline matches directly from `employees` where `joined_on <= month_end` (R9). It performs a `$lookup` to join the month's `attendance_logs` with `$unwind(preserveNullAndEmptyArrays=True)`. When grouping by department, `$addToSet: "$emp_code"` collects all valid employees regardless of log presence, ensuring zero-log employees are fully counted in `headcount`.

5. **100x Data Scaling.**
   At 100x scale (~10M records), I would shard `attendance_logs` on `{department: 1, date: 1}` and implement write-side incremental aggregation rollups (updating daily/monthly summary collections during punch-out and regularizations) to serve analytics in $O(1)$ time rather than executing full pipeline scans.
