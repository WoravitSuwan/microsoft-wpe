"""ทดสอบด้วยข้อมูลสมมติ (ห้ามใส่ข้อมูลคดีจริงใน repo)"""
import io
import os
import re
import uuid

import openpyxl
import pytest

from caseapp import create_app
from caseapp.thaidate import parse_date, thai_short, thai_short_time

TPO_HEADER = ["เลขรับแจ้ง", "เลขคดีอาญา", "วันที่รับแจ้ง", "ผู้เสียหาย", "เพศ", "อายุ", "ประเภทคดี",
              "ประเภทคดีระดับ 1", "ประเภทคดีระดับ 2", "ประเภทคดีระดับ 3", "ประเภทกลุ่มคดี",
              "รายละเอียด", "มูลค่าความเสียหาย", "บช.", "บก./ภ.จว.", "หน่วยที่รับผิดชอบ",
              "พนักงานสอบสวน", "พนักงานสืบสวน", "สถานะ", "เบอร์โทรศัพท์ผู้เสียหาย",
              "เลขอ้างอิงธนาคาร", "วันที่เริ่มโอน", "วันสุดท้ายที่โอน", "กลุ่มคดี"]
A = "พ.ต.ต.ทดสอบ หนึ่ง"
B = "ร.ต.อ.ตัวอย่าง สอง"


def tpo_file(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Grid"
    ws.append(TPO_HEADER)
    for cid, crim, date, victim, damage, inv, status in rows:
        r = [None] * len(TPO_HEADER)
        r[0], r[1], r[2], r[3], r[12], r[16], r[18] = cid, crim, date, victim, damage, inv, status
        r[6] = "คดีหลอกลวงทดสอบ"
        r[15] = "กก.9 บก.ทดสอบ บช.สอท."
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


ROWS = [
    ("T001", None, "01/02/2025", "ผู้เสียหาย ก", "1000.00", A, "สิ้นสุด"),
    ("T002", "50/2569", "05/03/2025", "ผู้เสียหาย ข", "250000.50", A, "อายัดบัญชี"),
    ("T003", None, "10/03/2025", "ผู้เสียหาย ค", "300.00", B, "พบพนักงานสอบสวน"),
]


PG_URL = os.environ.get("TEST_DATABASE_URL")  # เช่น postgresql://postgres@127.0.0.1:5433/postgres
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


@pytest.fixture()
def make_app(tmp_path, backend):
    """สร้างแอปที่มีฐานข้อมูลว่างของตัวเอง (Postgres ใช้ schema แยกต่อการทดสอบ)"""
    schemas = []

    def make(name="t", **extra):
        cfg = {"TESTING": True, "TESTING_NO_CSRF": True, "SECRET_KEY": "test", **extra}
        if backend == "postgres":
            import psycopg
            schema = f"t_{uuid.uuid4().hex[:12]}"
            with psycopg.connect(PG_URL, autocommit=True) as conn:
                conn.execute(f"CREATE SCHEMA {schema}")
            schemas.append(schema)
            sep = "&" if "?" in PG_URL else "?"
            cfg["DATABASE_URL"] = f"{PG_URL}{sep}options=-csearch_path%3D{schema}"
        else:
            cfg["DATABASE"] = str(tmp_path / f"{name}.db")
            cfg["DATABASE_URL"] = None
        return create_app(cfg)

    yield make
    if schemas:
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            for schema in schemas:
                conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture()
def app(make_app):
    return make_app()


@pytest.fixture()
def admin(app):
    c = app.test_client()
    c.post("/setup", data={"username": "admin", "password": "secret1"})
    return c


def do_import(client, f, owner="auto", name="ค่าเริ่มต้น__20261116_083829.xlsx"):
    return client.post("/import", data={"owner": owner, "file": (f, name)},
                       content_type="multipart/form-data")


def owner_ids(app):
    from caseapp.db import get_db
    with app.app_context():
        return {r["name"]: r["id"] for r in get_db().execute("SELECT id, name FROM owners")}


def export(client, oid):
    r = client.get(f"/owners/{oid}/export")
    assert r.status_code == 200
    return openpyxl.load_workbook(io.BytesIO(r.data))


def test_thai_dates():
    assert parse_date("30 ต.ค. 2569 14:42").strftime("%Y-%m-%d %H:%M") == "2026-10-30 14:42"
    assert parse_date("16/04/2569").strftime("%Y-%m-%d") == "2026-04-16"
    assert parse_date("13/05/2022").strftime("%Y-%m-%d") == "2022-05-13"
    assert thai_short("2026-11-16") == "16 พ.ย. 2569"
    assert thai_short("2026-11-16", short_year=True) == "16 พ.ย. 69"
    assert thai_short_time("2026-01-28T00:00") == "28 ม.ค. 2569 0:00"


def test_requires_setup_then_login(app):
    c = app.test_client()
    assert c.get("/").headers["Location"].endswith("/setup")
    c.post("/setup", data={"username": "admin", "password": "secret1"})
    c.post("/logout")
    assert "/login" in c.get("/").headers["Location"]


def test_import_splits_by_owner(app, admin):
    r = do_import(admin, tpo_file(ROWS))
    assert r.status_code == 200
    ids = owner_ids(app)
    assert set(ids) == {A, B}
    wb = export(admin, ids[A])
    assert wb.sheetnames == ["สรุป", "1.1 Case ID ทั้งหมด", "1.2 สอบปากคำแล้ว", "1.3 คดีอาญา (C.)",
                             "1.4 ปรับปรุงข้อมูล C.", "1.5 ค้างเกิน 6 เดือน"]
    s11 = wb["1.1 Case ID ทั้งหมด"]
    assert [c.value for c in s11[4]] == ["ลำดับ", "Case ID", "วันที่รับแจ้ง", "ผู้เสียหาย", "ประเภทคดี",
                                         "มูลค่าความเสียหาย (บาท)", "เลขคดีอาญา", "พนักงานสอบสวน", "สถานะ"]
    assert [s11.cell(r, 2).value for r in (5, 6)] == ["T001", "T002"]
    assert s11["F6"].value == 250000.5
    assert s11.cell(7, 2).value is None  # T003 เป็นของ B
    # เลขคดีอาญาจาก thaipoliceonline เข้าข้อ 1.3 อัตโนมัติ
    assert wb["1.3 คดีอาญา (C.)"]["B5"].value == "50/2569"
    assert wb["สรุป"]["C5"].value.startswith("=COUNTA('1.1 Case ID ทั้งหมด'!B5:B")
    assert "16 พ.ย. 69" in wb["สรุป"]["A20"].value or any(
        "16 พ.ย. 69" in str(c.value) for c in wb["สรุป"]["A"])


def test_reimport_keeps_manual_fields(app, admin):
    do_import(admin, tpo_file(ROWS))
    ids = owner_ids(app)
    admin.post(f"/owners/{ids[A]}/bulk", data={"kind": "s12", "data": "T001\nX999999\tคนนอกรายการ"})
    do_import(admin, tpo_file(ROWS))
    wb = export(admin, ids[A])
    s12 = wb["1.2 สอบปากคำแล้ว"]
    assert [s12.cell(r, 2).value for r in (5, 6)] == ["T001", "X999999"]
    assert [s12.cell(r, 5).value for r in (5, 6)] == ["อยู่ในรายการ 1.1", "ไม่อยู่ในรายการ 1.1"]


def test_criminal_backlog_and_c_update(app, admin):
    do_import(admin, tpo_file(ROWS))
    oid = owner_ids(app)[A]
    admin.post(f"/owners/{oid}/settings", data={"position": "สว.(สอบสวน) กก.9", "as_of": "2026-11-16",
                                                 "tpo_date": "2026-11-16", "notes": ""})
    admin.post(f"/owners/{oid}/bulk", data={"kind": "s13", "data":
        "50/2569\tT002\tนาย ข\t16 เม.ย. 2569 15:35\tจำหน่าย\n"
        "60/2569\t\tนาง ไม่มีเคส\t01/11/2569\tระหว่างสอบสวน"})
    from caseapp.db import get_db
    with app.app_context():
        pk = get_db().execute("SELECT id FROM cases WHERE case_id = 'T002'").fetchone()[0]
    admin.post(f"/cases/{pk}", data={"criminal_no": "50/2569", "c_status": "จำหน่าย",
                                     "actual_status": "ระหว่างสอบสวน", "complaint_date": "2026-04-16T15:35",
                                     "backlog_reason": "รอผลหมายเรียกผู้ต้องหา"})
    wb = export(admin, oid)
    s13 = wb["1.3 คดีอาญา (C.)"]
    rows = {s13.cell(r, 2).value: [s13.cell(r, j).value for j in range(1, 8)] for r in (5, 6)}
    assert rows["60/2569"][2] is None  # ไม่มี Case ID
    assert rows["50/2569"][4] == "16 เม.ย. 2569 15:35"
    assert rows["50/2569"][6] == "ต้องปรับปรุงข้อมูล (ข้อ 1.4)"
    assert wb["1.4 ปรับปรุงข้อมูล C."]["C5"].value == "T002"
    s15 = wb["1.5 ค้างเกิน 6 เดือน"]
    assert s15["B5"].value == "(ข) ต้องปรับปรุงข้อมูลในระบบ C."
    assert s15["H5"].value == "16/04/2569"
    assert s15["G5"].fill.fgColor.rgb == "FFFFF2CC"  # ศาลยังว่าง = สีเหลือง
    assert s15.cell(6, 3).value is None  # 60/2569 ยังไม่ถึง 6 เดือน
    assert wb["1.1 Case ID ทั้งหมด"]["A2"].value == "ข้อมูล ณ วันที่ 16 พ.ย. 2569 · พ.ต.ต.ทดสอบ หนึ่ง สว.(สอบสวน) กก.9"


def test_report_roundtrip(app, admin, make_app):
    """ไฟล์ที่ export ออกไป นำเข้ากลับ (เป็นรายงานเดิม) แล้ว export ซ้ำได้ค่าเท่าเดิม"""
    do_import(admin, tpo_file(ROWS))
    oid = owner_ids(app)[A]
    admin.post(f"/owners/{oid}/bulk", data={"kind": "s12", "data": "T002"})
    admin.post(f"/owners/{oid}/bulk", data={"kind": "s13", "data": "50/2569\tT002\tนาย ข\t16 เม.ย. 2569 15:35\tระหว่างสอบสวน"})
    admin.post(f"/owners/{oid}/settings", data={"position": "", "as_of": "2026-11-16", "tpo_date": "", "notes": "หมายเหตุพิเศษ"})
    first = admin.get(f"/owners/{oid}/export").data

    app2 = make_app("t2")
    c2 = app2.test_client()
    c2.post("/setup", data={"username": "admin", "password": "secret1"})
    do_import(c2, io.BytesIO(first), name="เอกสารแนบ.xlsx")
    oid2 = owner_ids(app2)[A]
    a = openpyxl.load_workbook(io.BytesIO(first))
    b = export(c2, oid2)
    for name in a.sheetnames:
        va = [[c.value for c in r] for r in a[name].iter_rows()]
        vb = [[c.value for c in r] for r in b[name].iter_rows()]
        assert va == vb, name


def test_officer_sees_only_own_data(app, admin):
    do_import(admin, tpo_file(ROWS))
    ids = owner_ids(app)
    admin.post("/admin/users", data={"action": "create", "username": "a", "password": "secret1",
                                     "role": "officer", "owner_id": ids[A]})
    off = app.test_client()
    off.post("/login", data={"username": "a", "password": "secret1"})
    assert off.get("/").headers["Location"].endswith(f"/owners/{ids[A]}")
    assert off.get(f"/owners/{ids[A]}").status_code == 200
    assert off.get(f"/owners/{ids[B]}").status_code == 403
    assert off.get(f"/owners/{ids[B]}/export").status_code == 403
    assert off.get("/admin/users").status_code == 403
    from caseapp.db import get_db
    with app.app_context():
        pk_b = get_db().execute("SELECT id FROM cases WHERE case_id = 'T003'").fetchone()[0]
    assert off.get(f"/cases/{pk_b}").status_code == 403
    # ผู้ใช้ทั่วไปนำเข้าไฟล์ที่มีคดีของคนอื่น: คดีของคนอื่นไม่ถูกย้าย
    r = do_import(off, tpo_file(ROWS))
    assert r.status_code == 200
    with app.app_context():
        owner_t003 = get_db().execute("SELECT owner_id FROM cases WHERE case_id = 'T003'").fetchone()[0]
    assert owner_t003 == ids[B]
    # วางรายการ Case ID ของคนอื่นก็ไม่ย้าย
    off.post(f"/owners/{ids[A]}/bulk", data={"kind": "s12", "data": "T003"})
    with app.app_context():
        row = get_db().execute("SELECT owner_id, statement_taken FROM cases WHERE case_id = 'T003'").fetchone()
    assert (row["owner_id"], row["statement_taken"]) == (ids[B], 0)


def test_admin_reassign_owner_is_sticky(app, admin):
    do_import(admin, tpo_file(ROWS))
    ids = owner_ids(app)
    from caseapp.db import get_db
    with app.app_context():
        pk = get_db().execute("SELECT id FROM cases WHERE case_id = 'T003'").fetchone()[0]
    admin.post(f"/cases/{pk}", data={"owner_id": ids[A]})
    do_import(admin, tpo_file(ROWS))  # แยกอัตโนมัติ: ต้องไม่ย้ายกลับไป B
    wb = export(admin, ids[A])
    s11 = wb["1.1 Case ID ทั้งหมด"]
    assert [s11.cell(r, 2).value for r in (5, 6, 7)] == ["T001", "T002", "T003"]
    notes = " ".join(str(c.value) for c in wb["สรุป"]["A"] if c.value)
    assert re.search(r"1 รายการที่ระบบยังระบุ " + re.escape(B), notes)


def test_csrf_enforced(make_app):
    app = make_app("c", TESTING_NO_CSRF=False)
    c = app.test_client()
    assert c.post("/setup", data={"username": "x", "password": "secret1"}).status_code == 400


def test_duplicate_owner_name_recovers(app, admin):
    """ชื่อซ้ำต้องไม่ทำให้ transaction ค้าง (สำคัญกับ Postgres)"""
    admin.post("/admin/owners", data={"action": "create", "name": A})
    r = admin.post("/admin/owners", data={"action": "create", "name": A})
    assert r.status_code == 302
    assert admin.get("/").status_code == 200
    assert list(owner_ids(app)) == [A]


def test_vercel_requires_env(monkeypatch):
    import caseapp.app as mod
    monkeypatch.setattr(mod, "ON_VERCEL", True)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    with pytest.raises(mod.ConfigError):
        mod.create_app()
