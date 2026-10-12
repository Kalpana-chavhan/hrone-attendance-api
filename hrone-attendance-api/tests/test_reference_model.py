"""Sanity-check the REFERENCE model in tests/verify_analytics.py against answers worked out by hand."""
import os, sys
os.environ["MONGO_DB"] = "attendance_verify"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mongomock
import pytest
import verify_analytics as v


@pytest.fixture(scope="module")
def db():
    d = mongomock.MongoClient(tz_aware=True)["attendance_verify"]
    v.seed(d)
    return d


def test_leaderboard_ties_1_2_2_4(db):
    rows = v.ref_leaderboard(db, "2026-07", 50, "Support")["items"]
    assert [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in rows] == [
        (1, "EMP0101", 50), (2, "EMP0102", 30), (2, "EMP0103", 30), (4, "EMP0104", 10)]
    assert [r["emp_code"] for r in v.ref_leaderboard(db, "2026-07", 2, "Support")["items"]] == ["EMP0101", "EMP0102", "EMP0103"]
    assert "EMP9999" not in [r["emp_code"] for r in v.ref_leaderboard(db, "2026-07", 50)["items"]]


def test_summary_counts_zero_log_and_future_people(db):
    items = {i["department"]: i for i in v.ref_summary(db, "2026-07")["items"]}
    assert items["HR"]["headcount"] == 1 and items["HR"]["present_days"] == 0 and items["HR"]["avg_work_hours"] is None
    assert "Future" not in items
    assert items["Engineering"]["headcount"] == 2                     # EMP0007 joins in August
    assert "Future" in {i["department"] for i in v.ref_summary(db, "2026-09")["items"]}
    assert (items["Stored"]["late_count"], items["Stored"]["total_late_minutes"]) == (1, 7)


def test_monthly_joiner_and_trend_shape(db):
    assert v.ref_monthly(db, "EMP0002", "2026-07")["working_days"] == 15
    t = v.ref_trend(db, "HR", "2026-07-01", "2026-07-14")["items"]
    assert len(t) == 14
    wk = [r for r in t if not r["is_working_day"]]
    assert all(r["attendance_rate"] is None for r in wk)
    assert all(r["attendance_rate"] == 0.0 for r in t if r["is_working_day"])
    assert t[0]["moving_avg_7d"] == 0.0
    eng = v.ref_trend(db, "Engineering", "2026-07-10", "2026-07-16")["items"]
    assert [r["headcount"] for r in eng] == [1, 1, 1, 2, 2, 2, 2]     # EMP0002 joins Mon 13th


def test_trend_moving_average_hand_computed():
    d = mongomock.MongoClient(tz_aware=True)["attendance_verify"]
    d.employees.insert_many([v.employee("EMP0001", "X", "2026-07-01"), v.employee("EMP0002", "X", "2026-07-01"),
                             v.employee("EMP0003", "X", "2026-07-01")])
    # Wed 1st: 1 of 3 present -> 0.3333 ; Thu 2nd: no records -> 0.0 ; Fri 3rd: 2 of 3 (one half day) -> 0.5
    d.attendance_logs.insert_many([
        v.make_log("EMP0001", "2026-07-01", "PRESENT", v.ist("2026-07-01", "09:30"), v.ist("2026-07-01", "18:30")),
        v.make_log("EMP0001", "2026-07-03", "PRESENT", v.ist("2026-07-03", "09:30"), v.ist("2026-07-03", "18:30")),
        v.make_log("EMP0002", "2026-07-03", "WFH", v.ist("2026-07-03", "09:30"), v.ist("2026-07-03", "12:30")),   # half day
    ])
    rows = v.ref_trend(d, "X", "2026-07-01", "2026-07-06")["items"]       # Wed..Mon, includes Sat/Sun
    assert [r["attendance_rate"] for r in rows] == [0.3333, 0.0, 0.5, None, None, 0.0]
    assert [r["moving_avg_7d"] for r in rows] == [0.3333, 0.1667, 0.2778, 0.2778, 0.2778, 0.2083]
    assert [r["present_count"] for r in rows] == [1, 0, 1.5, 0, 0, 0]
