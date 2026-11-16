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
    """ผู้ดูแล: กดเข้าสู่ระบบครั้งแรก = บันทึกเป็นผู้ดูแล"""
    c = app.test_client()
    r = c.post("/login", data={"username": "admin", "password": "secret1", "password2": "secret1"})
    assert r.status_code == 302
    return c


def do_import(client, f, owner="auto", name="ค่าเริ่มต้น__20261116_083829.xlsx"):
    return client.post("/import", data={"owner": owner, "file": (f, name)},
                       content_type="multipart/form-data", follow_redirects=True)


def upload_parts(client, data, token="ab" * 16, chunk=1 << 20):
    n = max(1, -(-len(data) // chunk))
    for i in range(n):
        r = client.post("/import/chunk", data=data[i * chunk:(i + 1) * chunk],
                        headers={"X-Upload-Token": token, "X-Chunk-Index": str(i)})
        assert r.status_code == 200, r.get_data(as_text=True)
    return {"token": token, "chunks": n, "filename": "ค่าเริ่มต้น__20261116_083829.xlsx"}


def chunked_import(client, data, chunk, owner="auto", token="ab" * 16, skip=None):
    n = max(1, -(-len(data) // chunk))
    for i in range(n):
        if i == skip:
            continue
        r = client.post("/import/chunk", data=data[i * chunk:(i + 1) * chunk],
                        headers={"X-Upload-Token": token, "X-Chunk-Index": str(i),
                                 "Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.get_data(as_text=True)
    return client.post("/import/finish", data={"token": token, "chunks": n, "owner": owner,
                                               "filename": "ค่าเริ่มต้น__20261116_083829.xlsx"})


def guest_import(client, names, token="ab" * 16):
    """ผู้ใช้ทั่วไป (ไม่ล็อกอิน) อัปโหลด + เลือกพนักงานสอบสวน คืน owner id ของชุดข้อมูลส่วนตัว"""
    up = upload_parts(client, tpo_file(ROWS).getvalue(), token=token)
    r = client.post("/import/finish", data={**up, "select": "1", "names": names})
    assert r.status_code == 200, r.get_data(as_text=True)
    url = client.get("/").headers["Location"]
    return int(re.search(r"/owners/(\d+)", url)[1])


def q(app, sql, params=()):
    from caseapp.db import get_db
    with app.app_context():
        return get_db().execute(sql, params).fetchall()


def owner_ids(app):
    return {r["name"]: r["id"] for r in q(app, "SELECT id, name FROM owners WHERE token IS NULL")}


def export(client, oid, by="ผู้ทดสอบ ระบบ"):
    r = client.get(f"/owners/{oid}/export", query_string={"by": by})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return openpyxl.load_workbook(io.BytesIO(r.data))


def logs(app, action=None):
    rows = q(app, "SELECT * FROM logs ORDER BY id")
    return [r for r in rows if action is None or r["action"] == action]


def test_thai_dates():
    assert parse_date("30 ต.ค. 2569 14:42").strftime("%Y-%m-%d %H:%M") == "2026-10-30 14:42"
    assert parse_date("16/04/2569").strftime("%Y-%m-%d") == "2026-04-16"
    assert parse_date("13/05/2022").strftime("%Y-%m-%d") == "2022-05-13"
    assert thai_short("2026-11-16") == "16 พ.ย. 2569"
    assert thai_short("2026-11-16", short_year=True) == "16 พ.ย. 69"
    assert thai_short_time("2026-01-28T00:00") == "28 ม.ค. 2569 0:00"


# ---------------------------------------------------------------- ผู้ใช้ทั่วไป: ไม่ต้องล็อกอิน

def test_open_link_and_use_without_login(app):
    c = app.test_client()
    assert c.get("/").headers["Location"].endswith("/import")
    page = c.get("/import").get_data(as_text=True)
    assert "ไม่ต้องล็อกอิน" in page and "เข้าสู่ระบบผู้ดูแล" in page
    oid = guest_import(c, [A])
    page = c.get(f"/owners/{oid}").get_data(as_text=True)
    assert A in page and "ลิงก์ส่วนตัว" not in page  # ลิงก์ส่วนตัวอยู่ในแท็บตั้งค่า
    assert [r["case_id"] for r in q(app, "SELECT case_id FROM cases WHERE owner_id = ? ORDER BY case_id",
                                    (oid,))] == ["T001", "T002"]
    assert q(app, "SELECT name FROM owners WHERE id = ?", (oid,))[0]["name"] == A


def test_workspaces_are_private_per_browser(app):
    a, b = app.test_client(), app.test_client()
    oa = guest_import(a, [A])
    assert b.get(f"/owners/{oa}").status_code == 403
    assert b.get(f"/owners/{oa}/export", query_string={"by": "x y"}).status_code == 403
    assert b.post(f"/owners/{oa}/purge", data={"confirm": "ลบ"}).status_code == 403
    # อีกเบราว์เซอร์นำเข้าไฟล์เดียวกันได้ชุดของตัวเอง (Case ID ซ้ำข้ามชุดข้อมูลได้)
    ob = guest_import(b, [A], token="cd" * 16)
    assert ob != oa
    assert len(q(app, "SELECT id FROM cases WHERE case_id = 'T001'")) == 2
    assert b.get(f"/owners/{ob}").status_code == 200


def test_download_requires_name_and_is_logged(app):
    c = app.test_client()
    oid = guest_import(c, [A, B])  # เลือก 2 คน → ชุดข้อมูลยังไม่มีชื่อ
    assert q(app, "SELECT name FROM owners WHERE id = ?", (oid,))[0]["name"] == ""
    r = c.get(f"/owners/{oid}/export")
    assert r.status_code == 200 and r.mimetype == "text/html" and "ชื่อผู้ดาวน์โหลด" in r.get_data(as_text=True)
    assert c.get(f"/owners/{oid}/export", query_string={"by": " "}).mimetype == "text/html"
    wb = export(c, oid, by="พ.ต.ท.ผู้ดาวน์โหลด ทดสอบ")
    assert "พ.ต.ท.ผู้ดาวน์โหลด ทดสอบ" in wb["1.1 Case ID ทั้งหมด"]["A2"].value  # ใช้เป็นชื่อในรายงาน
    d = logs(app, "download")
    assert len(d) == 1 and d[0]["actor"] == "พ.ต.ท.ผู้ดาวน์โหลด ทดสอบ" and d[0]["owner_id"] == oid
    assert d[0]["ip"] == "127.0.0.1"


def test_log_ip_is_real_not_spoofed(app, monkeypatch):
    c = app.test_client()
    # ไม่ได้อยู่บน Vercel: X-Forwarded-For ที่ผู้ใช้ส่งมาเองต้องไม่ถูกเชื่อ
    c.get("/import")
    up = upload_parts(c, tpo_file(ROWS).getvalue())
    c.post("/import/finish", data={**up, "select": "1", "names": [A]},
           headers={"X-Forwarded-For": "6.6.6.6", "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120"})
    row = logs(app, "import")[-1]
    assert row["ip"] == "127.0.0.1" and row["ua"] == "Chrome · Windows"
    # บน Vercel: ใช้ header ที่ Vercel ตั้งเอง
    import caseapp.app as mod
    monkeypatch.setattr(mod, "ON_VERCEL", True)
    oid = int(re.search(r"/owners/(\d+)", c.get("/").headers["Location"])[1])
    c.get(f"/owners/{oid}/export", query_string={"by": "ทดสอบ ไอพี"},
          headers={"x-vercel-forwarded-for": "203.0.113.9", "User-Agent": "Mozilla/5.0 (iPhone) Safari/604"})
    row = logs(app, "download")[-1]
    assert row["ip"] == "203.0.113.9" and row["ua"] == "Safari · iPhone"


def test_private_link_opens_workspace_elsewhere(app):
    a = app.test_client()
    oid = guest_import(a, [A])
    page = a.get(f"/owners/{oid}?tab=settings").get_data(as_text=True)
    link = re.search(r'id="plink" value="([^"]+)"', page)[1]
    other = app.test_client()
    r = other.get(link.replace("http://localhost", ""))
    assert r.headers["Location"].endswith(f"/owners/{oid}")
    assert other.get(f"/owners/{oid}").status_code == 200
    assert logs(app, "workspace_link")
    r = other.get("/w/not-a-real-token", follow_redirects=True)
    assert "ใช้ไม่ได้แล้ว" in r.get_data(as_text=True)


def test_guest_purge(app):
    c = app.test_client()
    oid = guest_import(c, [A])
    r = c.post(f"/owners/{oid}/purge", data={"confirm": "ไม่"}, follow_redirects=True)
    assert "ยังไม่ได้ลบข้อมูล" in r.get_data(as_text=True)
    assert len(q(app, "SELECT id FROM cases WHERE owner_id = ?", (oid,))) == 2
    c.post(f"/owners/{oid}/purge", data={"confirm": "ลบ"})
    assert q(app, "SELECT id FROM cases WHERE owner_id = ?", (oid,)) == []
    r = c.post(f"/owners/{oid}/purge", data={"confirm": "ลบ", "delete_workspace": "1"})
    assert r.headers["Location"].endswith("/")
    assert q(app, "SELECT id FROM owners WHERE id = ?", (oid,)) == []
    assert c.get("/").headers["Location"].endswith("/import")
    assert [r["action"] for r in logs(app, "purge")] == ["purge", "purge"]


def test_guest_cannot_reach_admin_pages(app, admin):
    c = app.test_client()
    for url in ("/admin/logs", "/admin/users"):
        assert "/login" in c.get(url).headers["Location"]
    assert c.post("/admin/reset", data={"confirm": "ลบทั้งหมด"}).status_code == 302
    assert c.post("/admin/owners", data={"action": "delete_many", "ids": ["1"]}).status_code == 302


# ---------------------------------------------------------------- ผู้ดูแล

def test_first_admin_login_then_normal_login(app):
    c = app.test_client()
    page = c.get("/login").get_data(as_text=True)
    assert "เข้าสู่ระบบผู้ดูแลครั้งแรก" in page
    r = c.post("/login", data={"username": "boss", "password": "secret1", "password2": "secret9"})
    assert "ยืนยันรหัสผ่านไม่ตรงกัน" in r.get_data(as_text=True)
    assert c.post("/login", data={"username": "boss", "password": "secret1", "password2": "secret1"}).status_code == 302
    assert "บันทึกการใช้งาน" in c.get("/").get_data(as_text=True)
    other = app.test_client()
    assert "เข้าสู่ระบบผู้ดูแลครั้งแรก" not in other.get("/login").get_data(as_text=True)
    r = other.post("/login", data={"username": "intruder", "password": "secret1", "password2": "secret1"})
    assert "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง" in r.get_data(as_text=True)
    assert [(r["username"], r["role"]) for r in q(app, "SELECT username, role FROM users")] == [("boss", "admin")]
    assert logs(app, "admin_login_failed")[0]["detail"].endswith("intruder")


def test_admin_login_keeps_own_workspace_and_sees_everything(app, admin):
    g1 = app.test_client()
    oid = guest_import(g1, [A])
    page = admin.get("/").get_data(as_text=True)
    assert A in page and "ส่วนตัว" in page
    assert admin.get(f"/owners/{oid}").status_code == 200
    logs_page = admin.get("/admin/logs").get_data(as_text=True)
    assert "นำเข้าไฟล์" in logs_page and "เริ่มใช้งาน" in logs_page and "127.0.0.1" in logs_page
    assert "นำเข้าไฟล์" in admin.get("/admin/logs?action=import").get_data(as_text=True)
    # ผู้ใช้ทั่วไปเข้าสู่ระบบผู้ดูแลแล้วออก ชุดข้อมูลของเบราว์เซอร์ยังอยู่
    g1.post("/login", data={"username": "admin", "password": "secret1"})
    g1.post("/logout")
    assert g1.get("/").headers["Location"].endswith(f"/owners/{oid}")


def test_login_throttle(app, admin, monkeypatch):
    import caseapp.app as mod
    monkeypatch.setattr(mod, "LOGIN_FAIL_LIMIT", 3)
    c = app.test_client()
    for _ in range(3):
        c.post("/login", data={"username": "admin", "password": "wrong"})
    r = c.post("/login", data={"username": "admin", "password": "secret1"})
    assert "รอ 15 นาที" in r.get_data(as_text=True)


def test_old_officer_accounts_cannot_log_in(app, admin):
    from werkzeug.security import generate_password_hash
    from caseapp.db import get_db
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO users (username, password_hash, role) VALUES ('off', ?, 'officer')",
                   (generate_password_hash("secret1"),))
        db.commit()
    c = app.test_client()
    r = c.post("/login", data={"username": "off", "password": "secret1"})
    assert "ไม่ถูกต้อง" in r.get_data(as_text=True)


def test_admin_import_splits_by_owner(app, admin):
    do_import(admin, tpo_file(ROWS))
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
    assert s11.cell(7, 2).value is None
    assert wb["1.3 คดีอาญา (C.)"]["B5"].value == "50/2569"
    assert wb["สรุป"]["C5"].value.startswith("=COUNTA('1.1 Case ID ทั้งหมด'!B5:B")


def test_reimport_keeps_manual_fields(app, admin):
    do_import(admin, tpo_file(ROWS))
    ids = owner_ids(app)
    admin.post(f"/owners/{ids[A]}/bulk", data={"kind": "s12", "data": "T001\nX999999\tคนนอกรายการ"})
    do_import(admin, tpo_file(ROWS))
    s12 = export(admin, ids[A])["1.2 สอบปากคำแล้ว"]
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
    pk = q(app, "SELECT id FROM cases WHERE owner_id = ? AND case_id = 'T002'", (oid,))[0]["id"]
    admin.post(f"/cases/{pk}", data={"criminal_no": "50/2569", "c_status": "จำหน่าย",
                                     "actual_status": "ระหว่างสอบสวน", "complaint_date": "2026-04-16T15:35",
                                     "backlog_reason": "รอผลหมายเรียกผู้ต้องหา"})
    wb = export(admin, oid)
    s13 = wb["1.3 คดีอาญา (C.)"]
    rows = {s13.cell(r, 2).value: [s13.cell(r, j).value for j in range(1, 8)] for r in (5, 6)}
    assert rows["60/2569"][2] is None
    assert rows["50/2569"][4] == "16 เม.ย. 2569 15:35"
    assert rows["50/2569"][6] == "ต้องปรับปรุงข้อมูล (ข้อ 1.4)"
    assert wb["1.4 ปรับปรุงข้อมูล C."]["C5"].value == "T002"
    s15 = wb["1.5 ค้างเกิน 6 เดือน"]
    assert s15["B5"].value == "(ข) ต้องปรับปรุงข้อมูลในระบบ C."
    assert s15["H5"].value == "16/04/2569"
    assert s15["G5"].fill.fgColor.rgb == "FFFFF2CC"
    assert s15.cell(6, 3).value is None
    assert wb["1.1 Case ID ทั้งหมด"]["A2"].value == "ข้อมูล ณ วันที่ 16 พ.ย. 2569 · พ.ต.ต.ทดสอบ หนึ่ง สว.(สอบสวน) กก.9"


def test_report_roundtrip(app, admin, make_app):
    """ไฟล์ที่ export ออกไป นำเข้ากลับ (เป็นรายงานเดิม) แล้ว export ซ้ำได้ค่าเท่าเดิม"""
    do_import(admin, tpo_file(ROWS))
    oid = owner_ids(app)[A]
    admin.post(f"/owners/{oid}/bulk", data={"kind": "s12", "data": "T002"})
    admin.post(f"/owners/{oid}/bulk", data={"kind": "s13", "data": "50/2569\tT002\tนาย ข\t16 เม.ย. 2569 15:35\tระหว่างสอบสวน"})
    admin.post(f"/owners/{oid}/settings", data={"position": "", "as_of": "2026-11-16", "tpo_date": "", "notes": "หมายเหตุพิเศษ"})
    first = admin.get(f"/owners/{oid}/export", query_string={"by": "ทดสอบ ระบบ"}).data
    app2 = make_app("t2")
    c2 = app2.test_client()
    c2.post("/login", data={"username": "admin", "password": "secret1", "password2": "secret1"})
    do_import(c2, io.BytesIO(first), name="เอกสารแนบ.xlsx")
    a = openpyxl.load_workbook(io.BytesIO(first))
    b = export(c2, owner_ids(app2)[A])
    for name in a.sheetnames:
        assert [[c.value for c in r] for r in a[name].iter_rows()] == \
               [[c.value for c in r] for r in b[name].iter_rows()], name


def test_admin_select_only_some_investigators(app, admin):
    up = upload_parts(admin, tpo_file(ROWS).getvalue())
    info = admin.post("/import/inspect", data=up).get_json()
    assert info["kind"] == "tpo" and info["rows"] == 3
    assert [(p["name"], p["rows"]) for p in info["investigators"]] == [(A, 2), (B, 1)]
    assert admin.post("/import/finish", data={**up, "select": "1"}).status_code == 400
    up = upload_parts(admin, tpo_file(ROWS).getvalue(), token="cd" * 16)
    r = admin.post("/import/finish", data={**up, "select": "1", "names": [A], "owner": "auto"})
    assert "ข้าม (ไม่ได้เลือก)" in admin.get(r.get_json()["url"]).get_data(as_text=True)
    assert list(owner_ids(app)) == [A]


def test_owner_table_pagination_and_search(app, admin, monkeypatch):
    import caseapp.app as mod
    monkeypatch.setattr(mod, "PAGE_SIZE", 1)
    do_import(admin, tpo_file(ROWS))
    oid = owner_ids(app)[A]
    p1 = admin.get(f"/owners/{oid}?tab=s11").get_data(as_text=True)
    p2 = admin.get(f"/owners/{oid}?tab=s11&page=2").get_data(as_text=True)
    assert "T001" in p1 and "T002" not in p1 and "(หน้า 1/2)" in p1
    assert "T002" in p2 and ">2</td>" in p2
    found = admin.get(f"/owners/{oid}?tab=s11&q=ผู้เสียหาย ข").get_data(as_text=True)
    assert "T002" in found and "T001" not in found and "พบ 1 รายการ" in found


def test_admin_purge_requires_password(app, admin):
    c = app.test_client()
    oid = guest_import(c, [A])
    admin.post(f"/owners/{oid}/purge", data={"password": "wrong", "confirm": "ลบ"})
    assert len(q(app, "SELECT id FROM cases WHERE owner_id = ?", (oid,))) == 2
    admin.post(f"/owners/{oid}/purge", data={"password": "secret1"})
    assert q(app, "SELECT id FROM cases WHERE owner_id = ?", (oid,)) == []


def test_delete_many_owners(app, admin):
    do_import(admin, tpo_file(ROWS))
    ids = owner_ids(app)
    r = admin.post("/admin/owners", data={"action": "delete_many", "ids": [ids[A], ids[B]]},
                   follow_redirects=True)
    assert "ลบ 2 ชุดข้อมูล" in r.get_data(as_text=True)
    assert owner_ids(app) == {}
    assert logs(app, "owner_delete")


def test_duplicate_managed_owner_name_recovers(app, admin):
    admin.post("/admin/owners", data={"action": "create", "name": A})
    r = admin.post("/admin/owners", data={"action": "create", "name": A})
    assert r.status_code == 302
    assert admin.get("/").status_code == 200
    assert list(owner_ids(app)) == [A]


def test_admin_reset_keep_and_all(app, admin):
    guest_import(app.test_client(), [A])
    do_import(admin, tpo_file(ROWS))
    admin.post("/admin/reset", data={"password": "secret1", "confirm": "ลบ"})
    assert q(app, "SELECT COUNT(*) AS n FROM owners")[0]["n"] == 3
    r = admin.post("/admin/reset", data={"password": "secret1", "confirm": "ลบทั้งหมด", "mode": "keep"},
                   follow_redirects=True)
    assert "ล้างข้อมูลทั้งระบบแล้ว" in r.get_data(as_text=True)
    for t in ("cases", "owners", "imports", "uploads"):
        assert q(app, f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] == 0, t
    assert logs(app, "reset")  # log เก็บไว้
    r = admin.post("/admin/reset", data={"password": "secret1", "confirm": "ลบทั้งหมด", "mode": "all"})
    assert r.headers["Location"].endswith("/login")
    assert "เข้าสู่ระบบผู้ดูแลครั้งแรก" in app.test_client().get("/login").get_data(as_text=True)
    assert app.test_client().get("/").headers["Location"].endswith("/import")  # ผู้ใช้ทั่วไปยังใช้ได้


# ---------------------------------------------------------------- อัปโหลด / ข้อผิดพลาด / ความปลอดภัย

def test_chunked_upload(app, monkeypatch):
    import caseapp.app as mod
    monkeypatch.setattr(mod, "UPLOAD_CHUNK", 1024)
    c = app.test_client()
    data = tpo_file(ROWS).getvalue()
    assert len(data) > 3 * 1024
    r = chunked_import(c, data, 1024)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "นำเข้าสำเร็จ" in c.get(r.get_json()["url"]).get_data(as_text=True)
    assert q(app, "SELECT COUNT(*) AS n FROM uploads")[0]["n"] == 0
    assert q(app, "SELECT COUNT(*) AS n FROM cases")[0]["n"] == 3


def test_chunked_upload_missing_part_and_other_browser(app, monkeypatch):
    import caseapp.app as mod
    monkeypatch.setattr(mod, "UPLOAD_CHUNK", 1024)
    a = app.test_client()
    data = tpo_file(ROWS).getvalue()
    r = chunked_import(a, data, 1024, skip=1)
    assert r.status_code == 400 and "ไม่ครบ" in r.get_json()["error"]
    for i in range(2):
        a.post("/import/chunk", data=data[i * 1024:(i + 1) * 1024],
               headers={"X-Upload-Token": "cd" * 16, "X-Chunk-Index": str(i)})
    other = app.test_client()
    assert other.post("/import/finish", data={"token": "cd" * 16, "chunks": 2}).status_code == 400
    r = a.post("/import/chunk", data=b"x" * 2048, headers={"X-Upload-Token": "ef" * 16, "X-Chunk-Index": "0"})
    assert r.status_code == 400


def test_import_error_messages(app, monkeypatch):
    import caseapp.app as mod
    monkeypatch.setattr(mod, "UPLOAD_CHUNK", 1024)
    c = app.test_client()
    r = chunked_import(c, b"\xd0\xcf\x11\xe0 old xls", 1024)
    assert r.status_code == 400 and "Save As" in r.get_json()["error"]
    wb = openpyxl.Workbook()
    wb.active.append(["ชื่อ", "เบอร์โทร"])
    buf = io.BytesIO()
    wb.save(buf)
    r = chunked_import(c, buf.getvalue(), 1024, token="cd" * 16)
    err = r.get_json()["error"]
    assert r.status_code == 400 and "ไม่รู้จักรูปแบบไฟล์" in err and "ชื่อ, เบอร์โทร" in err
    assert logs(app, "import_failed")
    assert c.get("/import").headers["Cache-Control"] == "no-store"


def test_csrf_enforced(make_app):
    app = make_app("c", TESTING_NO_CSRF=False)
    c = app.test_client()
    assert c.post("/login", data={"username": "x", "password": "secret1"}).status_code == 400
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/import").get_data(as_text=True))[1]
    hdr = {"X-Upload-Token": "ab" * 16, "X-Chunk-Index": "0"}
    assert c.post("/import/chunk", data=b"PK", headers=hdr).status_code == 400
    assert c.post("/import/chunk", data=b"PK", headers={**hdr, "X-CSRF-Token": token}).status_code == 200


def test_vercel_requires_env(monkeypatch):
    import caseapp.app as mod
    monkeypatch.setattr(mod, "ON_VERCEL", True)
    for k in ("SECRET_KEY", "DATABASE_URL", "POSTGRES_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(mod.ConfigError):
        mod.create_app()


# ---------------------------------------------------------------- ย้ายฐานข้อมูลรุ่นเดิม

OLD_OWNERS = """CREATE TABLE owners (id {pk}, name TEXT NOT NULL UNIQUE, position TEXT DEFAULT '',
    as_of TEXT, tpo_date TEXT, notes TEXT DEFAULT '', created_at TEXT)"""
OLD_CASES = """CREATE TABLE cases (id {pk}, owner_id INTEGER NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
    case_id TEXT NOT NULL, in_tpo INTEGER NOT NULL DEFAULT 0, owner_locked INTEGER NOT NULL DEFAULT 0,
    victim TEXT, statement_taken INTEGER NOT NULL DEFAULT 0, UNIQUE (case_id))"""


def _check_migrated(app):
    from caseapp.db import get_db, upsert_case
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT victim FROM cases WHERE case_id = 'T001'").fetchone()["victim"] == "เดิม"
        oid = db.execute("INSERT INTO owners (name, token) VALUES ('', 'tok')").lastrowid
        assert upsert_case(db, oid, "T001", {"victim": "ชุดใหม่"}) == "created"  # Case ID ซ้ำข้ามชุดได้แล้ว
        db.execute("INSERT INTO owners (name, token) VALUES (?, 'tok2')", (A,))  # ชื่อซ้ำกับชุดส่วนตัวได้
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM cases WHERE case_id = 'T001'").fetchone()[0] == 2


def test_migrates_old_sqlite_schema(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(OLD_OWNERS.format(pk="INTEGER PRIMARY KEY"))
    conn.execute(OLD_CASES.format(pk="INTEGER PRIMARY KEY"))
    conn.execute("INSERT INTO owners (id, name) VALUES (1, ?)", (A,))
    conn.execute("INSERT INTO cases (owner_id, case_id, victim, in_tpo) VALUES (1, 'T001', 'เดิม', 1)")
    conn.commit()
    conn.close()
    app = create_app({"DATABASE": str(path), "DATABASE_URL": None, "SECRET_KEY": "x"})
    _check_migrated(app)
    create_app({"DATABASE": str(path), "DATABASE_URL": None, "SECRET_KEY": "x"})  # ทำซ้ำได้


@pytest.mark.skipif(not PG_URL, reason="ต้องมี TEST_DATABASE_URL")
def test_migrates_old_postgres_schema():
    import psycopg
    schema = f"m_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        conn.execute(OLD_OWNERS.format(pk="SERIAL PRIMARY KEY"))
        conn.execute(OLD_CASES.format(pk="SERIAL PRIMARY KEY"))
        conn.execute("INSERT INTO owners (name) VALUES (%s)", (A,))
        conn.execute("INSERT INTO cases (owner_id, case_id, victim, in_tpo) VALUES (1, 'T001', 'เดิม', 1)")
    try:
        sep = "&" if "?" in PG_URL else "?"
        app = create_app({"DATABASE_URL": f"{PG_URL}{sep}options=-csearch_path%3D{schema}", "SECRET_KEY": "x"})
        _check_migrated(app)
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
