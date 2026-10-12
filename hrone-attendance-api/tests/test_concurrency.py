from concurrent.futures import ThreadPoolExecutor
from test_rules_and_crud import emp, ist_ms


def hammer(fn, n=20):
    with ThreadPoolExecutor(n) as ex:
        return [f.result() for f in [ex.submit(fn) for _ in range(n)]]


def test_parallel_punch_in_one_201_rest_409(client):
    emp(client)
    body = dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-06 09:00:00"))
    codes = sorted(r.status_code for r in hammer(lambda: client.post("/attendance/punch-in", json=body)))
    assert codes.count(201) == 1 and codes.count(409) == 19


def test_parallel_punch_out_exactly_one_200(client):
    emp(client)
    client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-06 09:00:00")))
    body = dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-06 18:00:00"))
    codes = [r.status_code for r in hammer(lambda: client.post("/attendance/punch-out", json=body))]
    assert codes.count(200) == 1 and codes.count(409) == 19


def test_parallel_employee_create_one_201(client):
    body = dict(emp_code="EMP0009", name="x", email="a@b.co", department="D", joined_on="2026-01-01")
    codes = [r.status_code for r in hammer(lambda: client.post("/employees", json=body))]
    assert codes.count(201) == 1 and codes.count(409) == 19


def test_parallel_regularize_no_lost_history(client):
    emp(client)
    client.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ist_ms("2026-07-07 10:05:00")))
    outs = iter(range(30))
    def one():
        i = next(outs)
        return client.patch("/attendance/EMP0001/2026-07-07", json=dict(reason="concurrent edit", regularized_by=f"u{i}",
                            punch_in=ist_ms(f"2026-07-07 09:{10+i:02d}:00")))
    res = hammer(one, 20)
    ok = [r for r in res if r.status_code == 200]
    assert ok and all(r.status_code in (200, 409, 422) for r in res)
    final = client.get("/attendance?emp_code=EMP0001").json()["items"][0]
    assert len(final["history"]) == len(ok)          # every successful PATCH left exactly one entry
