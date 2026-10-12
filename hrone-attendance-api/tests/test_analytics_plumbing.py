"""Everything around the aggregation pipelines that can be tested without a MongoDB server:
parameter validation, 404/422 paths, R8 rounding of what the pipelines return, and the exact
commands the explain endpoint would send. (The pipelines themselves: tests/verify_analytics.py.)"""
from datetime import date

import pytest
from bson.decimal128 import Decimal128

import app.main as m


class Coll:
    def __init__(self, rows=(), found=True):
        self.rows, self.found, self.pipeline = list(rows), found, None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        return iter(self.rows)

    def find_one(self, *a, **k):
        return {"_id": 1, "emp_code": "EMP0001", "joined_on": self.joined, "department": "X"} if self.found else None

    joined = "2026-01-05"


class FakeDB:
    def __init__(self, employees=None, logs=None):
        self.employees, self.attendance_logs = employees or Coll(), logs or Coll()
        self.commands = []

    def command(self, cmd):
        self.commands.append(cmd)
        return {"ok": 1.0, "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}


@pytest.fixture()
def api(client):
    def make(**kw):
        m.db = FakeDB(**kw)
        return client, m.db
    return make


def test_monthly_rounding_and_defaults(api):
    c, db = api(logs=Coll([{"_id": None, "present_days": 17.5, "leave_days": 1, "late_count": 3,
                            "total_late_minutes": 70, "total_overtime_minutes": 100}]))
    r = c.get("/analytics/employees/EMP0001/monthly?month=2026-07").json()
    assert r == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 17.5, "leave_days": 1,
                 "late_count": 3, "total_late_minutes": 70, "total_overtime_minutes": 100,
                 "attendance_pct": 76.09}                       # 17.5 / 23 * 100 = 76.087...
    assert db.attendance_logs.pipeline[0]["$match"] == {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}}


def test_monthly_no_logs_and_not_joined_yet(api):
    c, _ = api(logs=Coll([]))
    r = c.get("/analytics/employees/EMP0001/monthly?month=2026-07").json()
    assert (r["present_days"], r["attendance_pct"], r["late_count"]) == (0.0, 0.0, 0)
    emps = Coll(); emps.joined = "2026-08-01"
    c, _ = api(employees=emps, logs=Coll([]))
    r = c.get("/analytics/employees/EMP0001/monthly?month=2026-07").json()
    assert (r["working_days"], r["attendance_pct"]) == (0, None)


def test_monthly_errors(api):
    c, _ = api(employees=Coll(found=False))
    assert c.get("/analytics/employees/EMP0001/monthly?month=2026-07").status_code == 404
    c, _ = api()
    for month in ("2026-13", "2026-7", "26-07", "0000-01", "2026-07-01", ""):
        assert c.get(f"/analytics/employees/EMP0001/monthly?month={month}").status_code == 422, month
    assert c.get("/analytics/employees/EMP0001/monthly").status_code == 422


def test_summary_half_up_average_from_exact_decimal_sum(api):
    rows = [{"_id": "Eng", "headcount": 3, "present_days": 40.5, "work_hours_sum": Decimal128("18.25"), "work_hours_count": 2,
             "late_count": 4, "total_late_minutes": 90, "leave_count": 1, "on_duty_count": 2},
            {"_id": "HR", "headcount": 1, "present_days": 0, "work_hours_sum": 0, "work_hours_count": 0,
             "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}]
    c, db = api(employees=Coll(rows))
    r = c.get("/analytics/departments/summary?month=2026-07").json()
    assert r["month"] == "2026-07"
    assert r["items"][0]["avg_work_hours"] == 9.13              # 18.25 / 2 = 9.125 -> half-up 9.13 (banker's would give 9.12)
    assert r["items"][0]["present_days"] == 40.5
    assert r["items"][1] == {"department": "HR", "headcount": 1, "present_days": 0.0, "avg_work_hours": None,
                             "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
    assert db.employees.pipeline[0] == {"$match": {"joined_on": {"$lte": "2026-07-31"}}}
    c.get("/analytics/departments/summary?month=2026-07&department=Sales")
    assert db.employees.pipeline[0]["$match"]["department"] == "Sales"
    assert c.get("/analytics/departments/summary").status_code == 422


def test_leaderboard_params(api):
    c, db = api(logs=Coll([{"rank": 1, "emp_code": "EMP0001"}]))
    assert c.get("/analytics/leaderboard/late?month=2026-07").json() == {"month": "2026-07", "items": [{"rank": 1, "emp_code": "EMP0001"}]}
    stages = {list(s)[0] for s in db.attendance_logs.pipeline}
    assert {"$setWindowFields", "$lookup", "$unwind"} <= stages
    assert {"$match": {"rank": {"$lte": 10}}} in db.attendance_logs.pipeline        # limit is applied after ranking
    c.get("/analytics/leaderboard/late?month=2026-07&limit=3&department=Sales")
    assert {"$match": {"rank": {"$lte": 3}}} in db.attendance_logs.pipeline
    assert {"$match": {"emp.department": "Sales"}} in db.attendance_logs.pipeline
    for q in ("limit=0", "limit=51", "limit=x", ""):
        assert c.get(f"/analytics/leaderboard/late?{'month=2026-07&' if q else ''}{q}").status_code == 422, q


def test_trend_validation_and_pipeline_bounds(api):
    c, db = api(employees=Coll([{"date": "2026-07-01"}]))
    url = "/analytics/departments/Engineering/trend"
    assert c.get(f"{url}?from=2026-07-01&to=2026-07-31").status_code == 200
    densify = next(s["$densify"] for s in db.employees.pipeline if "$densify" in s)
    lo, hi = densify["range"]["bounds"]
    assert (hi - lo).days == 31 and lo.date() == date(2026, 7, 1)                  # upper bound is exclusive
    assert c.get(f"{url}?from=2026-07-02&to=2026-07-01").status_code == 422
    assert c.get(f"{url}?from=2026-01-01&to=2026-04-03").status_code == 422        # 93 days
    assert c.get(f"{url}?from=2026-01-01&to=2026-04-02").status_code == 200        # 92 days
    assert c.get(f"{url}?from=2026-07-01").status_code == 422
    assert c.get(f"{url}?from=2026-02-30&to=2026-03-01").status_code == 422
    c, _ = api(employees=Coll(found=False))
    assert c.get(f"{url}?from=2026-07-01&to=2026-07-02").status_code == 404


def test_explain_builds_the_same_commands_as_the_endpoints(api):
    c, db = api()
    r = c.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&status=WFH&page=3&page_size=10")
    assert r.status_code == 200 and set(r.json()) == {"endpoint", "collection", "explain"}
    cmd = db.commands[-1]["explain"]
    assert db.commands[-1]["verbosity"] == "executionStats"
    assert cmd["find"] == "attendance_logs" and cmd["skip"] == 20 and cmd["limit"] == 10
    assert cmd["filter"] == {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01"}, "status": "WFH"}
    assert cmd["sort"] == {"date": -1, "emp_code": 1}
    c.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert db.commands[-1]["explain"]["pipeline"] == m.employee_monthly_pipeline("EMP0001", "2026-07")
    c.get("/admin/explain/department_summary?month=2026-07&department=Sales")
    assert db.commands[-1]["explain"]["pipeline"] == m.department_summary_pipeline("2026-07", "Sales")
    assert db.commands[-1]["explain"]["aggregate"] == "employees"
    c.get("/admin/explain/late_leaderboard?month=2026-07&limit=5")
    assert db.commands[-1]["explain"]["pipeline"] == m.late_leaderboard_pipeline("2026-07", 5, None)
    c.get("/admin/explain/department_trend?department=Eng&from=2026-07-01&to=2026-07-10")
    assert db.commands[-1]["explain"]["pipeline"] == m.department_trend_pipeline("Eng", date(2026, 7, 1), date(2026, 7, 10))


def test_explain_validation(api):
    c, _ = api()
    assert c.get("/admin/explain/nope").status_code == 422
    assert c.get("/admin/explain/employee_monthly").status_code == 422
    assert c.get("/admin/explain/employee_monthly?emp_code=EMP0001").status_code == 422
    assert c.get("/admin/explain/department_summary").status_code == 422
    assert c.get("/admin/explain/late_leaderboard").status_code == 422
    assert c.get("/admin/explain/department_trend?department=Eng&from=2026-07-01").status_code == 422
    assert c.get("/admin/explain/department_trend?department=Eng&from=2026-07-05&to=2026-07-01").status_code == 422
    assert c.get("/admin/explain/attendance_list?date_from=2026-07-09&date_to=2026-07-01").status_code == 422
    assert c.get("/admin/explain/attendance_list?page=0").status_code == 422
