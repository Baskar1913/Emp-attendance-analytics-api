# DECISIONS.md

1. **Indexes.** A unique index on `employees.emp_code` prevents duplicate employee identities. A unique compound index on `(emp_code, date)` guarantees one attendance record per employee per IST attendance day, including concurrent requests. Supporting indexes cover department/code listing, joined-date headcount, date-sorted attendance, employee/date/status filtering, punch-out lookup, and monthly late ranking. I rejected a standalone status index because it would not provide the required date ordering and would add write overhead.

2. **Punch-in race.** Both requests may pass an initial read, but only one insert can satisfy the unique `(emp_code, date)` index. That request returns `201`; MongoDB rejects the competing insert and the API maps the duplicate-key error to `409`.

3. **Ties.** The leaderboard uses competition ranking. Equal totals share a rank and the next rank is skipped (`1, 2, 2, 4`). The limit is applied after ranking, so all rows tied at the cutoff remain included; ties are ordered by `emp_code`.

4. **Headcount.** Department summary starts from employees who joined by month end and looks up their attendance. Employees without logs still count; later joiners do not.

5. **100x data.** I would measure query plans and workload, then consider pre-aggregated reporting collections and cursor-based pagination for large result sets.
