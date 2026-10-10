import pytest
from fastapi.testclient import TestClient
from app.main import app, db, create_indexes
import sample_seed

client = TestClient(app)

@pytest.fixture(autouse=True)
def seed_db():
    # Re-seed database before each test
    create_indexes()
    import pathlib, os
    from bson import json_util
    here = pathlib.Path("sample_data")
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)

def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_create_employee():
    payload = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert isinstance(data["created_at"], int)

    # Duplicate create -> 409
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409

def test_list_employees():
    res = client.get("/employees?page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert data["total"] == 6
    assert len(data["items"]) == 6
    # Ascending sort check
    codes = [item["emp_code"] for item in data["items"]]
    assert codes == sorted(codes)

def test_punch_in_and_punch_out():
    # Punch in for EMP0002 on 2026-07-06 at 09:25 IST (1783310100000 ms = 2026-07-06 03:55:00 UTC)
    ms_in = 1783310100000
    res_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": ms_in,
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    data_in = res_in.json()
    assert data_in["emp_code"] == "EMP0002"
    assert data_in["date"] == "2026-07-06"
    assert data_in["late_minutes"] == 0
    assert data_in["punch_out"] is None

    # Duplicate punch-in -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": ms_in,
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

    # Punch out at 18:35 IST (1783343100000 ms = 2026-07-06 13:05:00 UTC)
    ms_out = 1783343100000
    res_out = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0002",
        "punched_at": ms_out
    })
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["punch_out"] == ms_out
    assert data_out["work_hours"] == 9.17
    assert data_out["half_day"] is False

    # Second punch out -> 409
    res_out_dup = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0002",
        "punched_at": ms_out
    })
    assert res_out_dup.status_code == 409

def test_list_attendance():
    res = client.get("/attendance?date_from=2026-07-01&date_to=2026-07-31")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 9
    # Check descending sort by date
    dates = [item["date"] for item in data["items"]]
    assert dates == sorted(dates, reverse=True)

def test_regularize_attendance():
    # Regularize EMP0001 on 2026-07-06
    # Original punch_in 09:28 IST (1783310280000 ms)
    # Update status to WFH
    payload = {
        "status": "WFH",
        "reason": "WFH approval by manager",
        "regularized_by": "manager.jane"
    }
    res = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "WFH"
    assert len(data["history"]) == 1
    hist = data["history"][0]
    assert hist["by"] == "manager.jane"
    assert "status" in hist["changes"]
    assert hist["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}

def test_analytics_employee_monthly():
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["working_days"] == 23 # July 2026 has 23 weekdays
    assert data["present_days"] >= 1.0

def test_analytics_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) >= 1
    # Check headcount includes all employees
    eng = next((d for d in data["items"] if d["department"] == "Engineering"), None)
    assert eng is not None
    assert eng["headcount"] == 2 # Asha Rao and Vikram Shah

def test_analytics_late_leaderboard():
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    for item in data["items"]:
        assert item["rank"] >= 1
        assert item["total_late_minutes"] > 0

def test_analytics_department_trend():
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 7 # Exactly 7 calendar days
    days = [item["date"] for item in data["items"]]
    assert days == ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06", "2026-07-07"]

def test_explain_endpoints():
    endpoints = [
        "/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07"
    ]
    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 200, f"Failed explain for {ep}: {res.text}"
        data = res.json()
        assert "explain" in data
        assert "collection" in data
