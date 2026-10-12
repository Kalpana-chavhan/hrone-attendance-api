from datetime import datetime, timedelta, timezone

import pytest
import app.main as m

IST = timezone(timedelta(hours=5, minutes=30))


def ist_ms(s: str, ms: int = 0) -> int:
    """'2026-07-06 09:40:01' (IST) -> epoch milliseconds."""
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    return int(dt.timestamp()) * 1000 + ms


def emp(client, code="EMP0001", dept="Engineering", start="09:30", end="18:30", joined="2026-01-05"):
    r = client.post("/employees", json=dict(emp_code=code, name="N", email="n@x.io", department=dept,
                                            shift_start=start, shift_end=end, joined_on=joined))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- pure rules
def d(s):  # IST string -> aware datetime
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)


@pytest.mark.parametrize("t,expected", [
    ("2026-07-06 09:40:00", 0), ("2026-07-06 09:40:01", 10), ("2026-07-06 10:15:59", 45),
    ("2026-07-06 09:00:00", 0), ("2026-07-06 09:39:59", 0),
])
def test_r2_late(t, expected):
    assert m.compute_late_minutes(d(t), m.parse_date("2026-07-06"), "09:30", "18:30") == expected


@pytest.mark.parametrize("t,expected", [
    ("2026-07-06 18:29:59", 0), ("2026-07-06 18:59:59", 0), ("2026-07-06 19:00:00", 30),
    ("2026-07-06 19:10:00", 40), ("2026-07-06 19:10:59", 40),
])
def test_r3_overtime(t, expected):
    assert m.compute_overtime(d(t), m.parse_date("2026-07-06"), "09:30", "18:30") == expected


def test_r4_half_up_and_r5():
    base = d("2026-07-06 09:00:00")
    # 4.505 h = 16218 s -> exactly a tie at the 3rd decimal -> half-up gives 4.51 (banker's/float would not)
    assert m.work_hundredths(base, base + timedelta(seconds=16218)) == 451
    assert m.work_hundredths(base, base + timedelta(seconds=16200)) == 450  # exactly 4.50
    r = m.derive_fields("PRESENT", base, base + timedelta(seconds=16200), m.parse_date("2026-07-06"), "09:30", "18:30")
    assert r["work_hours"] == 4.5 and r["half_day"] is False      # 4.50 is NOT < 4.50
    r = m.derive_fields("PRESENT", base, base + timedelta(seconds=16181), m.parse_date("2026-07-06"), "09:30", "18:30")
    assert r["work_hours"] == 4.49 and r["half_day"] is True
    # rounded value decides: 4.495 h (16182 s) rounds UP to 4.50 -> not a half day
    r = m.derive_fields("PRESENT", base, base + timedelta(seconds=16182), m.parse_date("2026-07-06"), "09:30", "18:30")
    assert r["work_hours"] == 4.5 and r["half_day"] is False


def test_r1_overnight_date_and_bounds():
    # shift 22:00-06:00 : 21:55 on the 6th -> 6th ; 05:30 on the 7th -> still the 6th ; 12:00 on the 7th -> 7th
    assert m.attendance_date(d("2026-07-06 21:55:00"), "22:00", "06:00").isoformat() == "2026-07-06"
    assert m.attendance_date(d("2026-07-07 05:59:59"), "22:00", "06:00").isoformat() == "2026-07-06"
    assert m.attendance_date(d("2026-07-07 06:00:00"), "22:00", "06:00").isoformat() == "2026-07-07"
    # date is the IST date, not the UTC date: 00:30 IST on the 7th is 19:00 UTC on the 6th
    assert m.attendance_date(d("2026-07-07 00:30:00"), "09:30", "18:30").isoformat() == "2026-07-07"
    # overnight overtime is measured against 06:00 the NEXT day
    assert m.compute_overtime(d("2026-07-07 06:40:00"), m.parse_date("2026-07-06"), "22:00", "06:00") == 40


def test_sample_documents_from_data_model():
    """The nine sample records in DATA_MODEL.md must be reproducible by our own rules."""
    day = m.parse_date("2026-07-06")
    r = m.derive_fields("PRESENT", d("2026-07-06 09:28:00"), d("2026-07-06 18:35:00"), day, "09:30", "18:30")
    assert r == dict(work_hours=9.12, late_minutes=0, overtime_minutes=0, half_day=False)
    r = m.derive_fields("PRESENT", d("2026-07-06 10:05:00"), d("2026-07-06 19:10:00"), day, "09:30", "18:30")
    assert r == dict(work_hours=9.08, late_minutes=35, overtime_minutes=40, half_day=False)
    r = m.derive_fields("WFH", d("2026-07-07 09:30:00"), d("2026-07-07 13:00:00"), m.parse_date("2026-07-07"), "09:30", "18:30")
    assert r["work_hours"] == 3.5 and r["half_day"] is True
    r = m.derive_fields("PRESENT", d("2026-07-06 21:55:00"), d("2026-07-07 06:40:00"), day, "22:00", "06:00")
    assert r == dict(work_hours=8.75, late_minutes=0, overtime_minutes=40, half_day=False)
    r = m.derive_fields("ABSENT", None, None, day, "09:30", "18:30")
    assert r == dict(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)


def test_rounding_and_working_days():
    assert m.round_half_up("2.675", 2) == 2.68 and m.round_half_up(0.125, 2) == 0.13
    # July 2026: Wed 1st .. Fri 31st = 23 weekdays ; joiner on Mon 13th -> 15
    f, l = m.parse_date("2026-07-01"), m.parse_date("2026-07-31")
    assert m.working_days(f, l, m.parse_date("2026-01-05")) == 23
    assert m.working_days(f, l, m.parse_date("2026-07-13")) == 15
    assert m.working_days(f, l, m.parse_date("2026-08-01")) == 0
    assert m.working_days(f, l, m.parse_date("2026-07-31")) == 1   # joined on the last day (a Friday)


# ---------------------------------------------------------------- employees
def test_employee_create_validation_and_conflict(client):
    e = emp(client)
    assert isinstance(e["created_at"], int) and "_id" not in e
    assert client.post("/employees", json=dict(emp_code="EMP0001", name="x", email="a@b.co", department="D", joined_on="2026-01-01")).status_code == 409
    bad = [
        dict(emp_code="emp1", name="x", email="a@b.co", department="D", joined_on="2026-01-01"),
        dict(emp_code="EMP0002", name="x", email="nope", department="D", joined_on="2026-01-01"),
        dict(emp_code="EMP0002", name="x", email="a@b.co", department="D", joined_on="2026-13-01"),
        dict(emp_code="EMP0002", name="x", email="a@b.co", department="D", joined_on="2026-02-30"),
        dict(emp_code="EMP0002", name="x", email="a@b.co", department="D", joined_on="2026-01-01", shift_start="9:30"),
        dict(emp_code="EMP0002", name="x", email="a@b.co", department="D", joined_on="2026-01-01", shift_start="10:00", shift_end="10:00"),
        dict(emp_code="EMP0002", name="", email="a@b.co", department="D", joined_on="2026-01-01"),
    ]
    for b in bad:
        assert client.post("/employees", json=b).status_code == 422, b


def test_employee_list_filter_sort_pagination(client):
    for i in range(5):
        emp(client, code=f"EMP000{i+1}", dept="A" if i % 2 == 0 else "B")
    r = client.get("/employees?page=1&page_size=2").json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0001", "EMP0002"] and r["total"] == 5
    r = client.get("/employees?page=2&page_size=2").json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0003", "EMP0004"]
    r = client.get("/employees?department=A").json()
    assert r["total"] == 3                                    # total follows the filter
    assert client.get("/employees?department=a").json()["total"] == 0   # case-sensitive
    assert client.get("/employees?page=99").json()["items"] == []
    for q in ("page=0", "page_size=0", "page_size=101", "page=abc"):
        assert client.get("/employees?" + q).status_code == 422, q


# ---------------------------------------------------------------- punch-in
def test_punch_in_rules_and_errors(client):
    emp(client)
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-06 09:40:01")))
    assert r.status_code == 201
    b = r.json()
    assert (b["date"], b["late_minutes"], b["status"], b["history"]) == ("2026-07-06", 10, "PRESENT", [])
    assert b["punch_out"] is None and b["work_hours"] is None and "_id" not in b and "id" not in b
    assert b["punch_in"] == ist_ms("2026-07-06 09:40:01")
    # same employee, same attendance date -> 409 (even at a different time that day)
    assert client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-06 12:00:00"))).status_code == 409
    assert client.post("/attendance/punch-in", json=dict(emp_code="EMP9999")).status_code == 404
    # sub-second values are truncated, and stored/returned truncated (R1)
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-07 09:40:00", 900)))
    assert r.json()["late_minutes"] == 0 and r.json()["punch_in"] == ist_ms("2026-07-07 09:40:00")
    for bad in (1783312500, 1783312500000.5, "1783312500000", True, 0, -5, 5_000_000_000_000):
        assert client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=bad)).status_code == 422, bad
    for st in ("ABSENT", "LEAVE", "FOO"):
        assert client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", status=st, punched_at=ist_ms("2026-07-08 09:00:00"))).status_code == 422
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", status="WFH", punched_at=ist_ms("2026-07-08 09:00:00"), late_minutes=99))
    assert r.status_code == 201 and r.json()["status"] == "WFH" and r.json()["late_minutes"] == 0   # client-sent derived field ignored


def test_overnight_punch_in_and_out(client):
    emp(client, code="EMP0005", start="22:00", end="06:00")
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP0005", punched_at=ist_ms("2026-07-06 21:55:00")))
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    r = client.post("/attendance/punch-out", json=dict(emp_code="EMP0005", punched_at=ist_ms("2026-07-07 06:40:00")))
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["overtime_minutes"], b["half_day"], b["date"]) == (8.75, 40, False, "2026-07-06")


# ---------------------------------------------------------------- punch-out
def test_punch_out_rules(client):
    emp(client)
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP9999")).status_code == 404
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-06 18:00:00"))).status_code == 404  # no punch-in
    client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-06 10:05:00")))
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-06 10:05:00"))).status_code == 422   # == punch_in
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-07 10:05:01"))).status_code == 422   # > 24h
    r = client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-06 19:10:00")))
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["late_minutes"], b["overtime_minutes"], b["half_day"]) == (9.08, 35, 40, False)
    assert b["punch_out"] == ist_ms("2026-07-06 19:10:00") and b["history"] == []      # punch-out never writes history
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-06 20:00:00"))).status_code == 409
    # half day
    client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", status="WFH", punched_at=ist_ms("2026-07-07 09:30:00")))
    b = client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-07 13:00:00"))).json()
    assert (b["work_hours"], b["half_day"]) == (3.5, True)


# ---------------------------------------------------------------- list
def test_attendance_list(client):
    emp(client, "EMP0001"); emp(client, "EMP0002")
    for day in ("06", "07", "08"):
        for c in ("EMP0001", "EMP0002"):
            client.post("/attendance/punch-in", json=dict(emp_code=c, punched_at=ist_ms(f"2026-07-{day} 09:00:00")))
    r = client.get("/attendance?page_size=100").json()
    assert r["total"] == 6
    assert [(x["date"], x["emp_code"]) for x in r["items"]][:3] == [("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001")]
    assert client.get("/attendance?emp_code=EMP0002").json()["total"] == 3
    assert client.get("/attendance?date_from=2026-07-07&date_to=2026-07-07").json()["total"] == 2
    assert client.get("/attendance?status=WFH").json()["total"] == 0
    assert client.get("/attendance?page=2&page_size=4").json()["items"].__len__() == 2
    for q in ("date_from=2026-07-09&date_to=2026-07-01", "status=NOPE", "date_from=07-01-2026", "date_from=2026-02-30", "page=0"):
        assert client.get("/attendance?" + q).status_code == 422, q


# ---------------------------------------------------------------- regularize
def seed_open_day(client):
    emp(client)
    client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-07 10:05:00")))
    client.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-07 18:30:00")))


def patch(client, body, code="EMP0001", day="2026-07-07"):
    return client.patch(f"/attendance/{code}/{day}", json={"reason": "biometric glitch", "regularized_by": "hr.admin", **body})


def test_regularize_happy_path_matches_data_model_sample_8(client):
    seed_open_day(client)
    r = patch(client, {"punch_in": ist_ms("2026-07-07 09:28:00")})
    assert r.status_code == 200
    b = r.json()
    assert (b["late_minutes"], b["work_hours"], b["overtime_minutes"]) == (0, 9.03, 0)
    assert len(b["history"]) == 1
    h = b["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    assert h["changes"]["punch_in"] == {"from": ist_ms("2026-07-07 10:05:00"), "to": ist_ms("2026-07-07 09:28:00")}
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert h["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
    assert set(h["changes"]) == {"punch_in", "late_minutes", "work_hours"}           # only what actually changed
    # appends, never overwrites
    b2 = patch(client, {"status": "WFH"}).json()
    assert len(b2["history"]) == 2 and b2["history"][0] == h and set(b2["history"][1]["changes"]) == {"status"}
    # visible through the list endpoint too, with ms values in history
    assert client.get("/attendance?emp_code=EMP0001").json()["items"][0]["history"][0]["changes"]["punch_in"]["to"] == ist_ms("2026-07-07 09:28:00")


def test_regularize_rules(client):
    seed_open_day(client)
    assert patch(client, {}).status_code == 422                                              # changes nothing
    assert patch(client, {"status": "PRESENT"}).status_code == 422                           # same status
    assert patch(client, {"punch_in": ist_ms("2026-07-07 10:05:00")}).status_code == 422     # same time
    assert patch(client, {"status": "LEAVE", "punch_in": ist_ms("2026-07-07 09:00:00")}).status_code == 422
    assert patch(client, {"punch_in": ist_ms("2026-07-08 09:00:00")}).status_code == 422     # other attendance date
    assert patch(client, {"punch_out": ist_ms("2026-07-07 09:00:00")}).status_code == 422    # before punch_in
    assert patch(client, {"punch_out": ist_ms("2026-07-08 10:05:01")}).status_code == 422    # > 24h
    assert patch(client, {"status": None}).status_code == 422                                # explicit null
    assert patch(client, {"punch_in": 1783312500}).status_code == 422                        # seconds
    assert client.patch("/attendance/EMP0001/2026-07-07", json={"reason": "abc", "regularized_by": "x", "status": "WFH"}).status_code == 422  # reason too short
    assert patch(client, {"status": "WFH"}, code="EMP9999").status_code == 404
    assert patch(client, {"status": "WFH"}, day="2026-07-20").status_code == 404
    assert patch(client, {"status": "WFH"}, day="2026-13-01").status_code == 422
    # LEAVE clears both punch times and zeroes derived fields
    b = patch(client, {"status": "LEAVE"}).json()
    assert (b["status"], b["punch_in"], b["punch_out"], b["work_hours"], b["late_minutes"], b["overtime_minutes"], b["half_day"]) == ("LEAVE", None, None, None, 0, 0, False)
    assert set(b["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}
    # a presence status needs a punch_in; supply one -> back to present, derived recomputed
    assert patch(client, {"status": "PRESENT"}).status_code == 422
    b = patch(client, {"status": "PRESENT", "punch_in": ist_ms("2026-07-07 09:50:00")}).json()
    assert (b["status"], b["late_minutes"], b["punch_out"]) == ("PRESENT", 20, None)


def test_regularize_stale_state_is_409_not_lost_update(client):
    """Simulate a concurrent writer between our read and our write: the guard must reject the write."""
    seed_open_day(client)
    real = m.db.attendance_logs.find_one_and_update
    def racing(flt, upd, **kw):
        m.db.attendance_logs.update_one({"_id": flt["_id"]}, {"$push": {"history": {"at": m.utc_now_s(), "by": "other", "reason": "other editor", "changes": {}}}})
        return real(flt, upd, **kw)
    m.db.attendance_logs = type("Proxy", (), {"__getattr__": lambda s, n: getattr(real.__self__, n), "find_one_and_update": staticmethod(racing)})()  # noqa
    try:
        assert patch(client, {"status": "WFH"}).status_code == 409
    finally:
        pass
