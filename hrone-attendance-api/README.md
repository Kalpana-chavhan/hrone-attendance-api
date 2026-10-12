# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out with business rules, audited manual corrections, and aggregation-pipeline analytics. All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
docker run -d -p 27017:27017 mongo:7                    # i used mongodb URI
cp .env.example .env                                    
uvicorn app.main:app --port 8000
```

`GET /health` returns 200 once MongoDB answers. Indexes are created at startup (idempotent). Interactive docs: `/docs`.

## Status

Every endpoint in `openapi.yaml` is implemented, including the explain endpoint. Nothing is known to be missing.

## Notes and assumptions

* Instants are epoch milliseconds in the API and UTC datetimes in MongoDB (truncated to whole seconds). Dates, months and shift times are IST strings.
* `moving_avg_7d` averages the *reported* (4-decimal rounded) `attendance_rate` values.
* `/admin/explain/{endpoint}` explains the same query/pipeline the real endpoint builds, but does not return 404 for unknown employees/departments (the plan is still shown).
* `PATCH` uses optimistic concurrency: a concurrent edit makes the loser's update miss and return 409, so no history entry is lost.

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests -q                                                    # business rules + CRUD + races (in-memory fake DB)
MONGO_DB=attendance_verify python tests/verify_analytics.py        # analytics vs a plain-Python reference (needs real MongoDB)
MONGO_DB=attendance_verify python tests/verify_analytics.py --big 100000   # speed + explain plans on 100k records
```

`verify_analytics.py` deletes everything in the database it is given, so it refuses any `MONGO_DB` whose name does not contain `verify` or `test`.
