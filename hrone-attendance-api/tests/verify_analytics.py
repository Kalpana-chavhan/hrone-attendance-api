"""
Self-check for the analytics endpoints. NOT part of the application - it is the "test your own work" step.

It (1) seeds awkward data straight into a SCRATCH database, (2) computes the expected answers with plain
Python (an independent reference model that reads the raw documents), (3) calls the real endpoints in-process
and compares, and (4) checks that /admin/explain shows IXSCAN and no COLLSCAN.

Usage (from the repo root, with MongoDB running, e.g. `docker run -d -p 27017:27017 mongo:7`):

    MONGO_DB=attendance_verify python tests/verify_analytics.py            # correctness
    MONGO_DB=attendance_verify python tests/verify_analytics.py --big 100000   # + speed / explain on 100k records

The database name must contain "verify" or "test": the script DELETES everything in both collections.
"""
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

if not any(w in os.getenv("MONGO_DB", "") for w in ("verify", "test")):
    sys.exit('Refusing to run: set MONGO_DB to a scratch database whose name contains "verify" or "test".')

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402

UTC = timezone.utc
PRES = ("PRESENT", "WFH", "ON_DUTY")
D = Decimal


# --------------------------------------------------------------------------- seed data
def ist(day: str, hhmm: str, plus_days: int = 0, seconds: int = 0) -> datetime:
    h, mi = map(int, hhmm.split(":"))
    base = datetime.combine(date.fromisoformat(day) + timedelta(days=plus_days), datetime.min.time(), tzinfo=m.IST)
    return (base + timedelta(hours=h, minutes=mi, seconds=seconds)).astimezone(UTC)


def make_log(code, day, status, p_in=None, p_out=None, emp=None, **override):
    emp = emp or {"shift_start": "09:30", "shift_end": "18:30"}
    derived = m.derive_fields(status, p_in, p_out, date.fromisoformat(day), emp["shift_start"], emp["shift_end"])
    return {"emp_code": code, "date": day, "status": status, "punch_in": p_in, "punch_out": p_out,
            **derived, "history": [], **override}


def employee(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC)}


def seed(db, seed_value=7):
    rnd = random.Random(seed_value)
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    emps = [
        employee("EMP0001", "Engineering", "2026-01-05"),
        employee("EMP0002", "Engineering", "2026-07-13"),                  # mid-month joiner
        employee("EMP0003", "Sales", "2026-02-01"),
        employee("EMP0004", "Sales", "2026-02-01"),
        employee("EMP0005", "Ops", "2026-03-01", "22:00", "06:00"),         # overnight shift
        employee("EMP0006", "HR", "2026-01-01"),                           # no logs at all
        employee("EMP0007", "Engineering", "2026-08-15"),                  # not joined yet in July
        employee("EMP0008", "Future", "2026-09-01"),                       # whole department not joined yet
        # ties: late totals 50, 30, 30, 10, 0  -> ranks 1, 2, 2, 4 (and nothing for the 0)
        employee("EMP0101", "Support", "2026-01-01"), employee("EMP0102", "Support", "2026-01-01"),
        employee("EMP0103", "Support", "2026-01-01"), employee("EMP0104", "Support", "2026-01-01"),
        employee("EMP0105", "Support", "2026-01-01"),
        employee("EMP0106", "Stored", "2026-01-01"),                       # stored value must be used as-is
    ]
    db.employees.insert_many(emps)
    by_code = {e["emp_code"]: e for e in emps}

    logs = []
    start, end = date(2026, 6, 29), date(2026, 8, 3)                       # spills into the neighbouring months
    for code in ("EMP0001", "EMP0002", "EMP0003", "EMP0004", "EMP0005", "EMP0007"):
        e = by_code[code]
        day = start
        while day <= end:
            d_iso, weekend = day.isoformat(), day.weekday() >= 5
            day += timedelta(days=1)
            if d_iso < e["joined_on"] and code != "EMP0007":
                continue
            roll = rnd.random()
            if weekend and roll > 0.12:
                continue                                                    # weekend: rarely a record
            if not weekend and roll < 0.06:
                continue                                                    # missing weekday: no record at all
            if not weekend and roll < 0.12:
                logs.append(make_log(code, d_iso, "LEAVE", emp=e)); continue
            if not weekend and roll < 0.18:
                logs.append(make_log(code, d_iso, "ABSENT", emp=e)); continue
            status = rnd.choice(["PRESENT"] * 7 + ["WFH"] * 2 + ["ON_DUTY"])
            p_in = ist(d_iso, e["shift_start"]) + timedelta(minutes=rnd.randint(-30, 90), seconds=rnd.randint(0, 59))
            hours = rnd.choice([3.2, 4.49, 4.5, 4.51, 8, 9, 9.5, 10.4])
            p_out = None if rnd.random() < 0.08 else p_in + timedelta(hours=hours, seconds=rnd.randint(0, 59))
            logs.append(make_log(code, d_iso, status, p_in, p_out, emp=e))
    # tie fixture: late minutes 50 / 30 / 30 / 10 / 0 in July (single late records, 1 record each except EMP0101)
    for code, minutes in (("EMP0101", [30, 20]), ("EMP0102", [30]), ("EMP0103", [30]), ("EMP0104", [10]), ("EMP0105", [0])):
        for i, mins in enumerate(minutes):
            d_iso = f"2026-07-{6 + i:02d}"
            p_in = ist(d_iso, "09:30") + timedelta(minutes=mins, seconds=1 if mins else 0)
            logs.append(make_log(code, d_iso, "PRESENT", p_in, ist(d_iso, "18:30"), emp=by_code[code],
                                 late_minutes=mins))
    # stored value is authoritative: punch times say "on time", stored late_minutes says 7
    logs.append(make_log("EMP0106", "2026-07-08", "PRESENT", ist("2026-07-08", "09:30"), ist("2026-07-08", "18:30"),
                         late_minutes=7))
    # orphan log (no such employee) with a huge value: must be ignored by the leaderboard
    logs.append(make_log("EMP9999", "2026-07-08", "PRESENT", ist("2026-07-08", "11:00"), ist("2026-07-08", "19:00"),
                         late_minutes=500))
    # legacy documents: no half_day / history / overtime fields at all
    legacy = make_log("EMP0001", "2026-07-30", "PRESENT", ist("2026-07-30", "09:30"), ist("2026-07-30", "13:00"))
    for k in ("half_day", "history", "overtime_minutes"):
        legacy.pop(k, None)
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": "2026-07-30"})
    logs = [l for l in logs if not (l["emp_code"] == "EMP0001" and l["date"] == "2026-07-30")] + [legacy]
    db.attendance_logs.insert_many(logs)
    return len(emps), len(logs)


# --------------------------------------------------------------------------- reference model (plain Python)
def q2(x):  # R8
    return float(D(str(x)).quantize(D("0.01"), rounding=ROUND_HALF_UP))


def q4(x):
    return float(D(x).quantize(D("0.0001"), rounding=ROUND_HALF_UP))


def weight(l):
    return 0 if l["status"] not in PRES else (0.5 if l.get("half_day") else 1)


def is_weekday(day):
    return date.fromisoformat(day).weekday() < 5


def month_logs(db, month):
    first, last = m.month_bounds(month)
    return list(db.attendance_logs.find({"date": {"$gte": first, "$lte": last}})), first, last


def ref_monthly(db, code, month):
    logs, first, last = month_logs(db, month)
    logs = [l for l in logs if l["emp_code"] == code]
    e = db.employees.find_one({"emp_code": code})
    d, wd = max(date.fromisoformat(first), date.fromisoformat(e["joined_on"])), 0
    while d <= date.fromisoformat(last):
        wd += d.weekday() < 5
        d += timedelta(days=1)
    present = sum(weight(l) for l in logs if is_weekday(l["date"]))
    return {"emp_code": code, "month": month, "working_days": wd, "present_days": q2(present),
            "leave_days": sum(l["status"] == "LEAVE" for l in logs),
            "late_count": sum((l.get("late_minutes") or 0) > 0 for l in logs),
            "total_late_minutes": sum(l.get("late_minutes") or 0 for l in logs),
            "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in logs),
            "attendance_pct": q2(D(str(present)) / wd * 100) if wd else None}


def ref_summary(db, month, department=None):
    logs, first, last = month_logs(db, month)
    emps = [e for e in db.employees.find({"joined_on": {"$lte": last}}) if department in (None, e["department"])]
    items = []
    for dept in sorted({e["department"] for e in emps}):
        codes = {e["emp_code"] for e in emps if e["department"] == dept}
        ls = [l for l in logs if l["emp_code"] in codes]
        hours = [D(str(l["work_hours"])) for l in ls if l["status"] in PRES and l.get("work_hours") is not None]
        items.append({"department": dept, "headcount": len(codes),
                      "present_days": q2(sum(weight(l) for l in ls if is_weekday(l["date"]))),
                      "avg_work_hours": q2(sum(hours) / len(hours)) if hours else None,
                      "late_count": sum((l.get("late_minutes") or 0) > 0 for l in ls),
                      "total_late_minutes": sum(l.get("late_minutes") or 0 for l in ls),
                      "leave_count": sum(l["status"] == "LEAVE" for l in ls),
                      "on_duty_count": sum(l["status"] == "ON_DUTY" for l in ls)})
    return {"month": month, "items": items}


def ref_leaderboard(db, month, limit=10, department=None):
    logs, _, _ = month_logs(db, month)
    emps = {e["emp_code"]: e for e in db.employees.find({})}
    tot = {}
    for l in logs:
        e = emps.get(l["emp_code"])
        if e is None or (department is not None and e["department"] != department):
            continue
        t = tot.setdefault(l["emp_code"], [0, 0])
        t[0] += l.get("late_minutes") or 0
        t[1] += (l.get("late_minutes") or 0) > 0
    rows = sorted(((c, v) for c, v in tot.items() if v[0] > 0), key=lambda kv: (-kv[1][0], kv[0]))
    out = []
    for c, v in rows:
        rank = 1 + sum(1 for _, w in rows if w[0] > v[0])                  # standard competition ranking
        if rank <= limit:
            out.append({"rank": rank, "emp_code": c, "name": emps[c]["name"], "department": emps[c]["department"],
                        "total_late_minutes": v[0], "late_count": v[1]})
    return {"month": month, "items": out}


def ref_trend(db, department, d_from, d_to):
    emps = list(db.employees.find({"department": department}))
    logs = [l for l in db.attendance_logs.find({"emp_code": {"$in": [e["emp_code"] for e in emps]},
                                                 "date": {"$gte": d_from, "$lte": d_to}})]
    rows, rates = [], []
    d, end = date.fromisoformat(d_from), date.fromisoformat(d_to)
    while d <= end:
        iso = d.isoformat()
        day_logs = [l for l in logs if l["date"] == iso]
        head = sum(e["joined_on"] <= iso for e in emps)
        present = sum(weight(l) for l in day_logs)
        working = d.weekday() < 5
        rate = q4(D(str(present)) / head) if working and head > 0 else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append({"date": iso, "is_working_day": working, "headcount": head, "present_count": present,
                     "late_count": sum((l.get("late_minutes") or 0) > 0 for l in day_logs),
                     "attendance_rate": rate,
                     "moving_avg_7d": q4(sum(D(str(r)) for r in window) / len(window)) if window else None})
        d += timedelta(days=1)
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- comparison helpers
failures = 0


def same(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(a - b) < 1e-9
    return a == b


def check(name, actual, expected):
    global failures
    if same(actual, expected):
        print(f"  ok    {name}")
        return
    failures += 1
    print(f"  FAIL  {name}")
    if isinstance(actual, dict) and isinstance(expected, dict) and "items" in actual and "items" in expected:
        for i, (x, y) in enumerate(zip(actual["items"], expected["items"])):
            if not same(x, y):
                print(f"        first difference at row {i}:\n          api      = {x}\n          expected = {y}")
                break
        else:
            print(f"        row counts differ: api={len(actual['items'])} expected={len(expected['items'])}")
    else:
        print(f"        api      = {actual}\n        expected = {expected}")


def get(c, url):
    r = c.get(url)
    if r.status_code != 200:
        return {"status": r.status_code, "body": r.text[:300]}
    return r.json()


# --------------------------------------------------------------------------- scenarios
def run_correctness(c, db):
    print("employee monthly")
    for code in ("EMP0001", "EMP0002", "EMP0003", "EMP0005", "EMP0006", "EMP0007", "EMP0106"):
        for month in ("2026-07", "2026-06", "2026-08"):
            check(f"{code} {month}", get(c, f"/analytics/employees/{code}/monthly?month={month}"), ref_monthly(db, code, month))
    check("mid-month joiner has 15 working days", get(c, "/analytics/employees/EMP0002/monthly?month=2026-07")["working_days"], 15)
    check("not-yet-joined employee: 0 working days, null pct",
          {k: get(c, "/analytics/employees/EMP0007/monthly?month=2026-07")[k] for k in ("working_days", "attendance_pct")},
          {"working_days": 0, "attendance_pct": None})
    check("unknown employee -> 404", c.get("/analytics/employees/EMP4040/monthly?month=2026-07").status_code, 404)

    print("department summary")
    for month in ("2026-06", "2026-07", "2026-08"):
        check(month, get(c, f"/analytics/departments/summary?month={month}"), ref_summary(db, month))
    check("department filter", get(c, "/analytics/departments/summary?month=2026-07&department=Sales"), ref_summary(db, "2026-07", "Sales"))
    s = {i["department"]: i for i in get(c, "/analytics/departments/summary?month=2026-07")["items"]}
    check("HR counted with zero logs", {k: s["HR"][k] for k in ("headcount", "present_days", "avg_work_hours")},
          {"headcount": 1, "present_days": 0, "avg_work_hours": None})
    check("'Future' department omitted in July", "Future" in s, False)
    check("Engineering headcount excludes the August joiner", s["Engineering"]["headcount"], 2)
    check("stored late_minutes is used as-is", (s["Stored"]["late_count"], s["Stored"]["total_late_minutes"]), (1, 7))

    print("late leaderboard")
    for lim in (1, 2, 3, 10, 50):
        check(f"limit={lim}", get(c, f"/analytics/leaderboard/late?month=2026-07&limit={lim}"), ref_leaderboard(db, "2026-07", lim))
    sup = get(c, "/analytics/leaderboard/late?month=2026-07&department=Support&limit=50")["items"]
    check("ties: ranks are 1,2,2,4 (ordered by emp_code within a tie)",
          [(r["rank"], r["emp_code"]) for r in sup], [(1, "EMP0101"), (2, "EMP0102"), (2, "EMP0103"), (4, "EMP0104")])
    sup2 = get(c, "/analytics/leaderboard/late?month=2026-07&department=Support&limit=2")["items"]
    check("limit=2 applies AFTER ranking -> 3 rows (both tied rank-2 rows kept)", [r["emp_code"] for r in sup2],
          ["EMP0101", "EMP0102", "EMP0103"])
    check("orphan log ignored", "EMP9999" in [r["emp_code"] for r in get(c, "/analytics/leaderboard/late?month=2026-07&limit=50")["items"]], False)
    check("department filter ranks inside the department",
          get(c, "/analytics/leaderboard/late?month=2026-07&department=Sales"), ref_leaderboard(db, "2026-07", 10, "Sales"))

    print("department trend")
    cases = [("Engineering", "2026-07-01", "2026-07-31"), ("Engineering", "2026-06-25", "2026-08-05"),
             ("Sales", "2026-07-04", "2026-07-12"), ("HR", "2026-07-01", "2026-07-20"),
             ("Ops", "2026-07-06", "2026-07-06"), ("Engineering", "2026-07-10", "2026-07-16"),
             ("Future", "2026-08-25", "2026-09-05"), ("Engineering", "2026-05-01", "2026-07-31")]
    for dept, f, t in cases:
        check(f"{dept} {f}..{t}", get(c, f"/analytics/departments/{dept}/trend?from={f}&to={t}"), ref_trend(db, dept, f, t))
    rows = get(c, "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-31")["items"]
    check("exactly one row per day (31)", len(rows), 31)
    check("weekend rows have null rate", [r["attendance_rate"] for r in rows if not r["is_working_day"]], [None] * 8)
    check("first row's moving average = its own rate", rows[0]["moving_avg_7d"], rows[0]["attendance_rate"])
    check("unknown department -> 404", c.get("/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02").status_code, 404)
    check("to < from -> 422", c.get("/analytics/departments/HR/trend?from=2026-07-02&to=2026-07-01").status_code, 422)
    check("93 days -> 422", c.get("/analytics/departments/HR/trend?from=2026-01-01&to=2026-04-03").status_code, 422)
    check("92 days -> 200", c.get("/analytics/departments/HR/trend?from=2026-01-01&to=2026-04-02").status_code, 200)


def run_explain(c):
    print("explain plans (needs IXSCAN, no COLLSCAN)")
    urls = {
        "attendance_list": "/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20",
        "attendance_list (no filter)": "/admin/explain/attendance_list",
        "attendance_list (status)": "/admin/explain/attendance_list?status=LEAVE",
        "employee_monthly": "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "department_summary": "/admin/explain/department_summary?month=2026-07",
        "department_summary (dept)": "/admin/explain/department_summary?month=2026-07&department=Sales",
        "late_leaderboard": "/admin/explain/late_leaderboard?month=2026-07&limit=10",
        "department_trend": "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-31",
    }
    global failures
    for name, url in urls.items():
        r = c.get(url)
        text = r.text
        ok = r.status_code == 200 and "IXSCAN" in text and "COLLSCAN" not in text
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}: HTTP {r.status_code}, IXSCAN={'IXSCAN' in text}, COLLSCAN={'COLLSCAN' in text}")
    check("explain without required params -> 422", c.get("/admin/explain/employee_monthly").status_code, 422)
    check("explain unknown endpoint -> 422", c.get("/admin/explain/nope").status_code, 422)


def run_big(c, db, n):
    print(f"bulk load: {n} attendance records")
    rnd = random.Random(1)
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    n_emp = max(50, n // 100)
    db.employees.insert_many([employee(f"EMP{i:05d}", f"Dept{i % 20}", "2026-01-01") for i in range(1, n_emp + 1)])
    per_emp, start, batch = n // n_emp, date(2026, 4, 1), []
    t0 = time.time()
    for i in range(1, n_emp + 1):
        for k in range(per_emp):
            d_iso = (start + timedelta(days=k)).isoformat()
            hrs = round(rnd.uniform(3, 10), 2)
            late = rnd.choice([0, 0, 0, 12, 25, 45])
            batch.append({"emp_code": f"EMP{i:05d}", "date": d_iso, "status": rnd.choice(["PRESENT"] * 8 + ["WFH", "LEAVE"]),
                          "punch_in": ist(d_iso, "09:30"), "punch_out": ist(d_iso, "18:30"), "work_hours": hrs,
                          "late_minutes": late, "overtime_minutes": 0, "half_day": hrs < 4.5, "history": []})
        if len(batch) >= 20000:
            db.attendance_logs.insert_many(batch); batch = []
    if batch:
        db.attendance_logs.insert_many(batch)
    print(f"  loaded in {time.time() - t0:.1f}s")
    db.attendance_logs.drop_indexes(); db.employees.drop_indexes()
    m._indexes_ready = False
    t0 = time.time(); m.ensure_indexes(); print(f"  indexes built in {time.time() - t0:.1f}s")
    last_day = (start + timedelta(days=per_emp - 1)).isoformat()
    timings = {
        "GET /attendance": "/attendance?page=1&page_size=50",
        "GET /attendance?emp_code": "/attendance?emp_code=EMP00007&date_from=2026-04-01",
        "GET /attendance?status": "/attendance?status=LEAVE&page=3",
        "monthly": "/analytics/employees/EMP00007/monthly?month=2026-04",
        "summary": "/analytics/departments/summary?month=2026-04",
        "leaderboard": "/analytics/leaderboard/late?month=2026-04",
        "trend (92 days)": f"/analytics/departments/Dept3/trend?from=2026-04-01&to={min(last_day, '2026-06-30')}",
    }
    print("timings (budget: well under 1 s each)")
    for name, url in timings.items():
        t0 = time.time(); r = c.get(url); dt = time.time() - t0
        print(f"  {'ok  ' if r.status_code == 200 and dt < 1.5 else 'SLOW'}  {name:28s} {dt * 1000:7.0f} ms  HTTP {r.status_code}")


def main():
    global failures
    with TestClient(m.app) as c:                                            # runs the startup (index creation)
        assert c.get("/health").status_code == 200, "MongoDB not reachable - check MONGO_URI"
        db = m.db
        if "--big" in sys.argv:
            run_big(c, db, int(sys.argv[sys.argv.index("--big") + 1]))
            print("explain on the big dataset")
            for name, url in {
                "attendance_list": "/admin/explain/attendance_list?emp_code=EMP00007",
                "employee_monthly": "/admin/explain/employee_monthly?emp_code=EMP00007&month=2026-04",
                "department_summary": "/admin/explain/department_summary?month=2026-04",
                "late_leaderboard": "/admin/explain/late_leaderboard?month=2026-04",
                "department_trend": "/admin/explain/department_trend?department=Dept3&from=2026-04-01&to=2026-06-30",
            }.items():
                r = c.get(url); ok = r.status_code == 200 and "IXSCAN" in r.text and "COLLSCAN" not in r.text
                failures += not ok
                print(f"  {'ok  ' if ok else 'FAIL'}  {name}: HTTP {r.status_code}")
        else:
            print("seeded %d employees / %d attendance records into '%s'" % (*seed(db), db.name))
            run_correctness(c, db)
            run_explain(c)
    print("\nALL CHECKS PASSED" if not failures else f"\n{failures} CHECK(S) FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
