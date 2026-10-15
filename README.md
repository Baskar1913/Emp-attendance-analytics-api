# Employee Attendance & Analytics API

**HROne Software Engineer Hiring — Engineering Assignment, Contract v2.0**

A REST API for employee records, daily attendance, attendance corrections, and reporting. The service uses **Python 3.11+, FastAPI, Pydantic, PyMongo, and MongoDB 6.0+**. FastAPI provides the HTTP interface and interactive Swagger documentation; MongoDB stores employee and attendance records and performs reporting aggregations. A separate frontend is not required by the assignment.

## 1. Project overview

The API supports five workflows:

1. **Employee management:** create an employee and retrieve a sorted, paginated list, optionally filtered by department.
2. **Attendance tracking:** punch an employee in and out, with the service calculating late minutes, work hours, overtime, and half-day status.
3. **Manual correction:** regularize an existing record and append an audit-history entry describing the reason and the actual before/after changes.
4. **Analytics:** return monthly employee summaries, department summaries, a tie-aware late-comer leaderboard, and a department trend with missing calendar days filled in.
5. **Query-plan inspection:** expose MongoDB `executionStats` through an explain endpoint to help verify query and aggregation performance.

The assignment is evaluated against a larger hidden dataset. Plausible output on a few sample rows is not proof that every business rule is correct; use the testing checklist below before submission.

## 2. Technology and architecture

| Component | Responsibility |
|---|---|
| Python 3.11+ | Application language |
| FastAPI | HTTP routes, request validation, error responses, OpenAPI/Swagger UI |
| Pydantic v2 | Request schema validation and rejection of invalid fields |
| PyMongo | MongoDB queries, writes, indexes, and aggregation pipelines |
| MongoDB 6.0+ | Persistent storage, unique constraints, server-side analytics |
| `python-dotenv` | Optional local `.env` loading during development |

All application endpoints and helper functions are in `app/main.py`, as requested by the candidate kit. Configuration is read from `MONGO_URI` and `MONGO_DB`; connection details must not be hard-coded or committed.

### Data collections

**`employees`** stores one document per employee. `emp_code` is the business identifier and must be unique. Documents include name, email, department, shift start/end in `HH:MM` IST, joining date, and server-generated `created_at`.

**`attendance_logs`** stores at most one document for each `(emp_code, date)` pair. `date` is the attendance day in IST and, for an overnight shift, is the date on which the shift began. Each record includes status, punch timestamps, computed fields, and `history`.

MongoDB's `_id` is internal. API clients identify employees by `emp_code`, and an attendance record by `(emp_code, date)`.

## 3. Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer (the assignment evaluator uses MongoDB 7.0)
- Git
- VS Code or another code editor

MongoDB must be reachable from the machine running the API. A local MongoDB installation or MongoDB Atlas can be used. The application does not require external services.

## 4. Setup in VS Code on Windows

Open the project folder in VS Code. Select **Terminal → New Terminal** and run the commands from the repository root.

### 4.1 Create and activate a virtual environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

If PowerShell blocks activation, use a terminal profile that permits virtual-environment activation or run the environment's Python directly with `.\.venv\Scripts\python.exe`.

### 4.2 Install dependencies

```powershell
pip install -r requirements.txt
```

### 4.3 Configure MongoDB

Set these variables in the same PowerShell terminal before starting the application:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

For Atlas, use the connection URI from your own Atlas project instead of the local URI. Never paste credentials into source code, screenshots, public issues, or the repository.

A local `.env` file is also supported for development. `.env` is ignored by Git and must never be committed. Runtime environment variables should be used for deployment configuration.

### 4.4 Start the API

```powershell
uvicorn app.main:app --port 8000
```

Leave the terminal running. To stop the server, press `Ctrl+C`.

### 4.5 Open the API documentation

- Swagger UI: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>
- OpenAPI JSON: <http://127.0.0.1:8000/openapi.json>
- Readiness check: <http://127.0.0.1:8000/health>

Swagger is the interactive API tester; no separate frontend is required. Expand an endpoint, select **Try it out**, enter its parameters/body, click **Execute**, and inspect the status code and response body.

### macOS/Linux alternative

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

## 5. API reference

Instants are integer Unix epoch milliseconds at the API boundary. Calendar dates use `YYYY-MM-DD`, months use `YYYY-MM`, and shift times use 24-hour `HH:MM` in IST.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Readiness probe; succeeds only when MongoDB answers |
| `POST` | `/employees` | Create an employee; `201` on success, `409` for duplicate employee code |
| `GET` | `/employees` | List employees with optional `department`, `page`, and `page_size` |
| `POST` | `/attendance/punch-in` | Create the attendance record for the shift/attendance date |
| `POST` | `/attendance/punch-out` | Close the matching open record and calculate derived values |
| `GET` | `/attendance` | Filter and paginate attendance records |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct a record and append an audit entry |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| `GET` | `/analytics/departments/summary` | Department summary for a month |
| `GET` | `/analytics/leaderboard/late` | Rank late-comers for a month, preserving ties |
| `GET` | `/analytics/departments/{department}/trend` | Daily department trend with gap filling and a seven-day moving average |
| `GET` | `/admin/explain/{endpoint}` | Return MongoDB execution statistics for a supported query/pipeline |

### Example: create an employee

```json
{
  "emp_code": "EMP042210",
  "name": "Arun Kumar",
  "email": "arun.kumar@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-15"
}
```

`emp_code` must be unique. `created_at` is set by the server and must not be sent by the client. Shift start/end must be valid `HH:MM` values and cannot be identical.

### Example: punch in

```json
{
  "emp_code": "EMP042210",
  "punched_at": 1792036800000,
  "status": "PRESENT"
}
```

The timestamp is an example epoch-millisecond value; use one that matches the intended test instant. `punched_at` may be omitted to use the current time. Accepted punch-in statuses are `PRESENT`, `WFH`, and `ON_DUTY`.

### Pagination and errors

- `page` starts at `1`.
- `page_size` defaults to `20`, has a minimum of `1`, and cannot exceed `100`.
- `total` must describe the filtered result set.
- `404` indicates a required employee/attendance record is missing.
- `409` indicates a conflict, such as a duplicate employee or duplicate punch.
- `422` indicates invalid input, including invalid date/time formats or timestamps.
- `503` indicates the service cannot reach MongoDB for a database-dependent operation/readiness check.

## 6. Business rules to preserve

These rules come from the assignment contract.

- **R1 — Time zone and attendance date:** shifts and attendance dates use IST (UTC+05:30). Instants are truncated to whole seconds before calculations and storage. For an overnight shift (`shift_end <= shift_start`), the shift ends on the next calendar day; a punch-in before the overnight shift's end time belongs to the previous shift-start date.
- **R2 — Late arrival:** there is a 10-minute grace period. Lateness is recorded only when punch-in is strictly more than 10 minutes after shift start. Once late, record whole elapsed minutes from shift start (floor), not minutes after the grace period.
- **R3 — Overtime:** count whole minutes after shift end only when elapsed overtime is at least 30 minutes.
- **R4 — Work hours:** elapsed seconds divided by 3600, rounded to two decimal places using half-up rounding. `work_hours` remains `null` until punch-out.
- **R5 — Half day:** after rounding work hours, `half_day` is true if the rounded value is below `4.50`.
- **R6 — Presence:** `PRESENT`, `WFH`, and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not.
- **R7 — Working days:** Monday through Friday, without a holiday calendar. A mid-month joiner is counted from `joined_on`. Weekend attendance can contribute to late/overtime totals but not weekday present-day counts.
- **R8 — Rounding:** reported numbers use half-up rounding to two decimal places; rates use four decimal places.
- **R9 — Headcount:** include employees who joined on or before the period end, even if they have no attendance logs. Exclude employees who join after the period end.
- **R10 — Pagination:** page numbering starts at one, page size is at most 100, and totals reflect active filters.

### Analytics expectations

- Monthly employee summaries calculate working days from the joining date and present days from weekday records, with half days counting as `0.5`.
- Department summaries count eligible employees even when they have no logs. Average work hours is calculated over qualifying attendance records, not as an average of employee-level averages.
- The late leaderboard uses competition ranking: ties share a rank and the next rank is skipped (`1, 2, 2, 4`). The limit is applied after ranking, so tied employees at the cutoff can be included; ties are ordered by employee code.
- The department trend returns one row for every calendar date in the requested range, including weekends and days without records. Its seven-day moving average uses the current date and up to six preceding rows within the requested range, so initial rows use a shorter window. A working day with no attendance records has a zero rate; non-working days have a null rate.

## 7. Indexes and concurrency

Indexes are created idempotently when the application starts. A unique index on `employees.emp_code` protects employee uniqueness; a unique compound index on `(emp_code, date)` is the database-level guarantee that an employee cannot have two attendance records for one attendance date. A pre-check alone cannot prevent two simultaneous requests from attempting the same insert.

Other indexes support department/employee listings, date-sorted attendance reads, employee/date lookups, and date/late-minute ranking. The explain endpoint helps inspect whether a real execution plan uses indexes. Run it against the same filters as the corresponding API endpoint; an index definition alone does not prove that a query plan avoids collection scans.

Concurrent punch-out and regularization behavior must be tested with parallel requests, not inferred from a single Swagger request. Every successful correction should append an audit entry and must not overwrite previous history.

## 8. Testing checklist before submission

Use Swagger for ordinary endpoint tests and a small automated script/test runner for concurrency. Record the status code and response body for each case.

1. **Health:** MongoDB available → `200`; MongoDB unavailable → `503`.
2. **Employee creation:** valid employee → `201`; duplicate `emp_code` → `409`; invalid email/code/date/time or identical shift times → `422`.
3. **Employee listing:** default pagination; page 1 includes the first records; department filter is exact and case-sensitive; empty results have `items: []` and `total: 0`; `page_size > 100` is rejected.
4. **Punch-in:** known employee → `201`; unknown employee → `404`; repeated attendance day → `409`; invalid timestamp type/range → `422`; check `09:40:00` and `09:40:01` for a `09:30` shift.
5. **Punch-out:** open record → `200`; missing employee/open punch → `404`; already closed record → conflict; punch-out must be after punch-in and within 24 hours; test overnight shifts and overtime below/at the 30-minute threshold.
6. **Derived values:** verify work-hours half-up rounding and the `4.50` half-day boundary; ensure punch-in/out do not append manual-correction history.
7. **Regularization:** verify allowed fields, status/time combinations, actual changed fields only in `history[].changes`, timestamp serialization, unchanged request → `422`, and missing record → `404`.
8. **Analytics:** include employees with no logs, mid-month joiners, weekend logs, absent/leave records, half-days, leaderboard ties, and dates with no trend records.
9. **Aggregation and explain:** confirm analytics use MongoDB aggregation pipelines; call all five supported explain targets and inspect `executionStats` for index scans and collection scans on a realistically sized dataset.
10. **Concurrency:** parallel identical punch-ins should yield exactly one `201` and the rest `409`; parallel punch-outs should yield exactly one successful `200`; concurrent regularizations must not silently lose history entries.
11. **Clean clone:** install from `requirements.txt`, set environment variables, start with the documented Uvicorn command, and check that all indexes are created automatically.

**Do not mark the project fully verified merely because a few happy-path calls succeed.** Manual checks so far cover health, employee creation, duplicate-code handling, listing/department filtering, and basic punch-in/punch-out responses. The full edge-case, parallel-request, hidden-data, and explain-plan checklist still needs to be run against MongoDB before claiming that every requirement passes.

## 9. Required repository contents

```text
hrone-attendance-analytics-api/
├── app/
│   ├── __init__.py
│   └── main.py
├── requirements.txt
├── README.md
├── REVIEW.md
├── DECISIONS.md
└── .gitignore
```

Do not commit `.env`, credentials, a virtual environment, data dumps, or a Dockerfile. Keep the application code in `app/main.py`, as requested by the candidate kit. The repository must be public, and its URL must be submitted using the method and by the deadline stated in the HROne assignment communication.

## 10. Suggested Git commands

Run from the project root after reviewing the files:

```powershell
git init
git add app/main.py requirements.txt README.md REVIEW.md DECISIONS.md .gitignore
git status
git commit -m "Complete employee attendance analytics API assignment"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/hrone-attendance-analytics-api.git
git push -u origin main
```

Create the public repository on GitHub first and replace the example remote URL with your own. If Git is already initialized or a remote already exists, inspect `git status` and `git remote -v` rather than initializing again or adding a duplicate remote blindly.

## 11. Explaining the project in a review

A concise explanation you can adapt in your own words:

> This is a FastAPI REST service backed by MongoDB. It stores employees and attendance records, computes attendance-derived values at write time, and exposes reporting endpoints that use MongoDB aggregation pipelines. Unique indexes protect employee identity and the one-record-per-employee-per-attendance-day guarantee. Manual corrections are audited by appending history entries. The explain endpoint exposes execution statistics so query plans can be inspected. I tested the main API flows and would demonstrate the edge-case and concurrency checks separately.

Be prepared to explain the code and design decisions yourself. The assignment explicitly includes a live walkthrough, so use this documentation to understand the implementation rather than as a substitute for understanding it.
