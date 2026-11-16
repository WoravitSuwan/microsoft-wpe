"""เว็บรายงานข้อมูลคดีและ Case ID

- ผู้ใช้ทั่วไป: เปิดลิงก์แล้วใช้ได้เลย ไม่ต้องล็อกอิน แต่ละเบราว์เซอร์มี "ชุดข้อมูลส่วนตัว" ของตัวเอง
  (คนอื่นที่เปิดลิงก์เดียวกันมองไม่เห็น) และต้องกรอกชื่อทุกครั้งที่ดาวน์โหลด
- ผู้ดูแลระบบ: เข้าสู่ระบบจากปุ่มบนเว็บ เห็นทุกชุดข้อมูลและบันทึกการใช้งาน (log)
"""
import io
import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote

from flask import (Flask, abort, flash, g, redirect, render_template, request, send_file,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import importer, report
from .db import (MANUAL_FIELDS, CaseCache, batch, database_url, get_db, init_db,
                 placeholder_case_id, upsert_case)
from .thaidate import be_slash, ce_slash, thai_short, thai_short_time, to_iso


ON_VERCEL = bool(os.environ.get("VERCEL"))
UPLOAD_CHUNK = 2 * 1024 * 1024    # ขนาดต่อส่วน (ต่ำกว่าเพดาน 4.5 MB ของ Vercel)
UPLOAD_MAX = 60 * 1024 * 1024     # ขนาดไฟล์สูงสุดที่รับ
PAGE_SIZE = 1000                  # แถวต่อหน้าในตาราง
LOG_PAGE = 200                    # แถวต่อหน้าในบันทึกการใช้งาน
LOGIN_FAIL_LIMIT = 10             # ล็อกอินผิดได้กี่ครั้งต่อ IP ใน 15 นาที

ACTIONS = {
    "workspace_create": "เริ่มใช้งาน (สร้างชุดข้อมูล)",
    "workspace_link": "เปิดด้วยลิงก์ส่วนตัว",
    "import": "นำเข้าไฟล์",
    "import_failed": "นำเข้าไม่สำเร็จ",
    "download": "ดาวน์โหลด Excel",
    "settings": "แก้ไขตั้งค่ารายงาน",
    "case_edit": "แก้ไขคดี",
    "case_add": "เพิ่ม Case ID",
    "case_delete": "ลบคดี",
    "bulk": "วางข้อมูลหลายรายการ",
    "purge": "ลบข้อมูลทั้งหมดของชุดข้อมูล",
    "admin_login": "ผู้ดูแลเข้าสู่ระบบ",
    "admin_login_failed": "เข้าสู่ระบบไม่สำเร็จ",
    "admin_logout": "ผู้ดูแลออกจากระบบ",
    "admin_create": "สร้างบัญชีผู้ดูแล",
    "admin_user": "จัดการบัญชีผู้ดูแล",
    "owner_delete": "ผู้ดูแลลบชุดข้อมูล",
    "reset": "ล้างข้อมูลทั้งระบบ",
}


class ConfigError(RuntimeError):
    pass


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    test_config = test_config or {}
    secret = test_config.get("SECRET_KEY") or os.environ.get("SECRET_KEY")
    db_url = test_config.get("DATABASE_URL", database_url())
    if ON_VERCEL and not test_config:
        # ไฟล์ระบบบน Vercel เป็นแบบชั่วคราว: ต้องใช้ Postgres และ SECRET_KEY จาก Environment Variables
        missing = [n for n, v in (("DATABASE_URL (Postgres)", db_url), ("SECRET_KEY", secret)) if not v]
        if missing:
            raise ConfigError(", ".join(missing))
    app.config.update(
        SECRET_KEY=secret or _secret(app),
        DATABASE=os.environ.get("CASEAPP_DB", os.path.join(app.instance_path, "caseapp.sqlite3")),
        DATABASE_URL=db_url,
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,  # Vercel รับคำขอได้ไม่เกิน 4.5 MB
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=ON_VERCEL,
        SESSION_COOKIE_HTTPONLY=True,
        PERMANENT_SESSION_LIFETIME=timedelta(days=365),  # ชุดข้อมูลส่วนตัวอยู่กับเบราว์เซอร์ 1 ปี
    )
    app.config.update(test_config)
    init_db(app)
    app.jinja_env.filters.update(
        initial=_initial,
        ce=ce_slash, be=be_slash, th=thai_short, thtime=thai_short_time,
        money=lambda v: "" if v in (None, "") else f"{float(v):,.2f}",
    )
    app.jinja_env.globals.update(
        report=report,
        actions=ACTIONS,
        # ชื่อหน่วยที่แสดงบนเว็บ (เปลี่ยนได้ด้วย Environment Variables)
        unit_short=os.environ.get("UNIT_SHORT", "กก.1 บก.สอท.4"),
        unit_full=os.environ.get("UNIT_FULL", "กองกำกับการ 1 กองบังคับการตำรวจสืบสวนสอบสวน"
                                              "อาชญากรรมทางเทคโนโลยี 4"),
        # เปลี่ยนทุก deploy เพื่อไม่ให้เบราว์เซอร์ใช้ CSS/JS เก่า
        asset_v=(os.environ.get("VERCEL_GIT_COMMIT_SHA") or str(int(time.time())))[:10],
    )
    _register(app)
    return app


def _secret(app):
    os.makedirs(app.instance_path, exist_ok=True)
    path = os.path.join(app.instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


# ------------------------------------------------------------------ helpers

def client_ip():
    """IP จริงของผู้ใช้

    บน Vercel: อ่านจาก header ที่ Vercel ตั้งเองที่ขอบเครือข่าย (Vercel เขียนทับค่าที่ผู้ใช้ส่งมา จึงปลอมไม่ได้)
    ที่อื่น: ใช้ที่อยู่ของการเชื่อมต่อจริง ไม่เชื่อ X-Forwarded-For ที่ผู้ใช้ส่งมาเอง
    (ถ้าวางหลัง reverse proxy ของตัวเอง ตั้ง TRUST_PROXY=1)
    """
    if ON_VERCEL:
        for h in ("x-vercel-forwarded-for", "x-real-ip", "x-forwarded-for"):
            v = request.headers.get(h, "").split(",")[0].strip()
            if v:
                return v[:64]
    if os.environ.get("TRUST_PROXY"):
        v = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if v:
            return v[:64]
    return (request.remote_addr or "")[:64]


def device_of(ua):
    """สรุปเบราว์เซอร์/ระบบปฏิบัติการจาก User-Agent ให้อ่านง่าย"""
    ua = ua or ""
    os_ = next((n for k, n in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                               ("Windows", "Windows"), ("Mac OS", "macOS"), ("CrOS", "ChromeOS"),
                               ("Linux", "Linux")) if k in ua), "")
    br = next((n for k, n in (("Line/", "LINE"), ("FBAN", "Facebook"), ("Edg/", "Edge"),
                              ("OPR/", "Opera"), ("SamsungBrowser", "Samsung"), ("Firefox/", "Firefox"),
                              ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "")
    return " · ".join(x for x in (br, os_) if x) or ua[:60]


def is_admin():
    return g.get("user") is not None and g.user["role"] == "admin"


def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not is_admin():
            if g.get("user") is None:
                return redirect(url_for("login", next=request.path))
            abort(403)
        return view(*a, **kw)
    return wrapped


def current_ws(create=False):
    """ชุดข้อมูลส่วนตัวของเบราว์เซอร์นี้ (owners.token) — create=True สร้างใหม่ถ้ายังไม่มี"""
    if "ws" in g and (g.ws is not None or not create):
        return g.ws
    db = get_db()
    row = None
    token = session.get("ws")
    if token:
        row = db.execute("SELECT * FROM owners WHERE token = ?", (token,)).fetchone()
    if row is None and create:
        token = secrets.token_urlsafe(24)
        oid = db.execute("INSERT INTO owners (name, position, token, last_seen) VALUES ('', '', ?, ?)",
                         (token, _now())).lastrowid
        db.commit()
        session["ws"] = token
        session.permanent = True
        row = db.execute("SELECT * FROM owners WHERE id = ?", (oid,)).fetchone()
        log("workspace_create", owner_id=oid)
    g.ws = row
    return row


def owner_or_404(owner_id):
    """ผู้ดูแลเปิดได้ทุกชุด ผู้ใช้ทั่วไปเปิดได้เฉพาะชุดข้อมูลส่วนตัวของเบราว์เซอร์ตัวเอง"""
    row = get_db().execute("SELECT * FROM owners WHERE id = ?", (owner_id,)).fetchone()
    if row is None:
        abort(404)
    if is_admin():
        return row
    ws = current_ws()
    if ws is None or ws["id"] != row["id"]:
        abort(403)
    return row


def case_or_404(case_pk):
    row = get_db().execute("SELECT * FROM cases WHERE id = ?", (case_pk,)).fetchone()
    if row is None:
        abort(404)
    owner_or_404(row["owner_id"])
    return row


def actor_id():
    """ผู้กระทำ (ใช้กับไฟล์ที่อัปโหลด/ประวัติการนำเข้า): ผู้ดูแล = id บวก, ชุดข้อมูลส่วนตัว = -id"""
    if is_admin():
        return g.user["id"]
    ws = current_ws()
    return -ws["id"] if ws is not None else 0


def owner_label(owner):
    if owner is None:
        return ""
    return owner["name"] or f"ชุดข้อมูล #{owner['id']} (ยังไม่ระบุชื่อ)"


def log(action, detail="", owner_id=None, actor=None):
    """บันทึกการใช้งาน (ผู้ดูแลดูได้ที่หน้า "บันทึกการใช้งาน")"""
    db = get_db()
    if actor is None:
        if is_admin():
            actor = f"ผู้ดูแล: {g.user['username']}"
        else:
            ws = g.get("ws")
            actor = owner_label(ws) if ws is not None else "ผู้ใช้ทั่วไป"
    try:
        db.execute("INSERT INTO logs (actor, owner_id, action, detail, ip, ua) VALUES (?, ?, ?, ?, ?, ?)",
                   (actor[:200], owner_id, action, (detail or "")[:1000], client_ip(),
                    device_of(request.headers.get("User-Agent"))[:200]))
        db.commit()
    except Exception:  # noqa: BLE001 - log ไม่สำเร็จต้องไม่ทำให้งานหลักพัง
        db.rollback()


def _now():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")


def _csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def _form_text(name):
    v = request.form.get(name, "")
    v = " ".join(v.split()) if "\n" not in v else v.strip()
    return v or None


def _initial(name):
    """อักษรย่อบนรูปโปรไฟล์: พยัญชนะแรกของชื่อจริง (ข้ามยศ/คำนำหน้า/สระหน้า)"""
    parts = (name or "").split()
    if not parts:
        return "?"
    first = parts[-2] if len(parts) >= 2 else parts[0]
    first = first.rsplit(".", 1)[-1] or first
    for prefix in ("หญิง", "นางสาว", "นาย", "นาง"):
        if first.startswith(prefix) and len(first) > len(prefix):
            first = first[len(prefix):]
    for ch in first:
        if ch not in "เแโใไ":
            return ch.upper()
    return first[:1].upper() or "?"


def _search_text(item):
    data = item["case"] if "case" in item else item
    return " ".join(str(v) for v in data.values() if v is not None).lower()


def _int(text):
    return int(text) if (text or "").strip().isdigit() else None


def _money(text):
    t = (text or "").replace(",", "").strip()
    return float(t) if re.match(r"^\d+(\.\d+)?$", t) else None


def _keep_ws_clear():
    """ล้าง session (เข้า/ออกจากระบบผู้ดูแล) แต่คงชุดข้อมูลส่วนตัวของเบราว์เซอร์ไว้"""
    ws = session.get("ws")
    session.clear()
    if ws:
        session["ws"] = ws
        session.permanent = True


def _register(app):
    app.jinja_env.globals.update(csrf_token=_csrf_token, owner_label=owner_label)

    @app.before_request
    def load_user():
        uid = session.get("uid")
        g.user = None
        if uid:
            g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if g.user is not None and g.user["role"] != "admin":
                g.user = None  # ระบบนี้ล็อกอินได้เฉพาะผู้ดูแล
        if request.method == "POST" and not app.config.get("TESTING_NO_CSRF"):
            token = session.get("csrf")
            sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
            if not token or not secrets.compare_digest(sent, token):
                abort(400, "CSRF token ไม่ถูกต้อง กรุณาโหลดหน้าใหม่")

    @app.after_request
    def no_store(resp):
        # หน้าเว็บมีข้อมูลคดี และต้องได้โค้ดล่าสุดเสมอหลัง deploy (กันเบราว์เซอร์ใช้หน้าเก่าจาก cache)
        if resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject():
        ws = current_ws() if request.endpoint != "static" else None
        return {"ws": ws, "is_admin": is_admin()}

    # ---------------------------------------------------------- admin auth
    def _no_users():
        return get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

    def _create_first_admin():
        """ระบบยังไม่มีผู้ดูแล: ชื่อผู้ใช้/รหัสผ่านที่กรอกครั้งแรกบันทึกเป็นผู้ดูแลระบบ คืนข้อความผิดพลาดหรือ None"""
        db = get_db()
        u, p = _form_text("username"), request.form.get("password", "")
        if not u or not re.fullmatch(r"[A-Za-z0-9_.@-]{3,40}", u):
            return "ชื่อผู้ใช้ต้องเป็นตัวอักษรภาษาอังกฤษ ตัวเลข หรือ . _ - @ ยาว 3–40 ตัว"
        if len(p) < 6:
            return "รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร"
        if "password2" in request.form and p != request.form.get("password2"):
            return "ยืนยันรหัสผ่านไม่ตรงกัน"
        if not _no_users():  # มีคนบันทึกไปก่อนแล้ว
            return "ระบบมีผู้ดูแลแล้ว กรุณาเข้าสู่ระบบ"
        uid = db.execute("INSERT INTO users (username, password_hash, display_name, role) "
                         "VALUES (?, ?, ?, 'admin')",
                         (u, generate_password_hash(p), _form_text("display_name") or u)).lastrowid
        db.commit()
        _keep_ws_clear()
        session["uid"] = uid
        g.user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        log("admin_create", f"บัญชีผู้ดูแลคนแรก: {u}")
        flash(f"บันทึก {u} เป็นผู้ดูแลระบบแล้ว — ใช้ชื่อผู้ใช้และรหัสผ่านนี้เข้าสู่ระบบครั้งต่อไป", "ok")
        return None

    def _too_many_failures():
        since = (datetime.now(timezone.utc) + timedelta(hours=7) - timedelta(minutes=15)).strftime(
            "%Y-%m-%d %H:%M:%S")
        n = get_db().execute("SELECT COUNT(*) FROM logs WHERE action = 'admin_login_failed' AND ip = ? "
                             "AND created_at >= ?", (client_ip(), since)).fetchone()[0]
        return n >= LOGIN_FAIL_LIMIT

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """เข้าสู่ระบบผู้ดูแล (ระบบยังไม่มีผู้ดูแล: ครั้งแรกที่กดเข้าสู่ระบบจะบันทึกเป็นผู้ดูแล)"""
        if is_admin():
            return redirect(url_for("index"))
        first_run = _no_users()
        if first_run and request.method == "POST":
            error = _create_first_admin()
            if error is None:
                return redirect(url_for("index"))
            flash(error, "error")
            return render_template("login.html", first_run=True, username=_form_text("username") or "")
        if first_run:
            return render_template("login.html", first_run=True, username="")
        if request.method == "POST":
            username = _form_text("username") or ""
            if _too_many_failures():
                flash("เข้าสู่ระบบผิดหลายครั้งเกินไป กรุณารอ 15 นาทีแล้วลองใหม่", "error")
                return render_template("login.html")
            row = get_db().execute("SELECT * FROM users WHERE username = ? AND role = 'admin'",
                                   (username,)).fetchone()
            if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
                _keep_ws_clear()
                session["uid"] = row["id"]
                g.user = row
                log("admin_login")
                nxt = request.args.get("next") or ""
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                                else url_for("index"))
            log("admin_login_failed", f"ชื่อผู้ใช้ที่กรอก: {username[:60]}", actor="ไม่ทราบ")
            flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "error")
        return render_template("login.html")

    @app.route("/setup", methods=["GET", "POST"])
    @app.route("/register", methods=["GET", "POST"])
    def old_links():
        """ลิงก์รุ่นก่อน (สมัครสมาชิก/ตั้งค่าครั้งแรก) ไม่ใช้แล้ว"""
        return redirect(url_for("index"))

    @app.route("/logout", methods=["POST"])
    def logout():
        if is_admin():
            log("admin_logout")
        _keep_ws_clear()
        return redirect(url_for("index"))

    @app.route("/password", methods=["GET", "POST"])
    @admin_required
    def password():
        if request.method == "POST":
            if not check_password_hash(g.user["password_hash"], request.form.get("old", "")):
                flash("รหัสผ่านเดิมไม่ถูกต้อง", "error")
            elif len(request.form.get("new", "")) < 6:
                flash("รหัสผ่านใหม่ต้องมีอย่างน้อย 6 ตัวอักษร", "error")
            else:
                db = get_db()
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                           (generate_password_hash(request.form["new"]), g.user["id"]))
                db.commit()
                log("admin_user", "เปลี่ยนรหัสผ่านตัวเอง")
                flash("เปลี่ยนรหัสผ่านแล้ว", "ok")
                return redirect(url_for("index"))
        return render_template("password.html")

    @app.route("/w/<token>")
    def open_workspace(token):
        """ลิงก์ส่วนตัว: เปิดชุดข้อมูลเดิมบนเบราว์เซอร์/เครื่องอื่น"""
        row = get_db().execute("SELECT * FROM owners WHERE token = ?", (token,)).fetchone()
        if row is None:
            flash("ลิงก์ส่วนตัวนี้ใช้ไม่ได้แล้ว (ชุดข้อมูลอาจถูกลบ)", "error")
            return redirect(url_for("index"))
        session["ws"] = token
        session.permanent = True
        g.ws = row
        log("workspace_link", owner_id=row["id"])
        return redirect(url_for("owner_view", owner_id=row["id"]))

    # ---------------------------------------------------------- home
    @app.route("/")
    def index():
        db = get_db()
        if not is_admin():
            ws = current_ws()
            if ws is not None and db.execute("SELECT 1 FROM cases WHERE owner_id = ? LIMIT 1",
                                             (ws["id"],)).fetchone():
                return redirect(url_for("owner_view", owner_id=ws["id"]))
            return redirect(url_for("import_view"))
        owners = db.execute("""
            SELECT o.*, COUNT(c.id) AS n_cases,
                   SUM(c.in_tpo) AS n_tpo, SUM(c.statement_taken) AS n_stmt,
                   SUM(CASE WHEN c.criminal_no IS NOT NULL AND c.criminal_no != '' THEN 1 ELSE 0 END) AS n_crim
            FROM owners o LEFT JOIN cases c ON c.owner_id = o.id
            GROUP BY o.id ORDER BY o.token IS NULL DESC, o.name, o.id""").fetchall()
        downloads = db.execute("SELECT COUNT(*) FROM logs WHERE action = 'download'").fetchone()[0]
        recent = db.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 8").fetchall()
        return render_template("index.html", owners=owners, downloads=downloads, recent=recent)

    # ---------------------------------------------------------- owner report
    @app.route("/owners/<int:owner_id>")
    def owner_view(owner_id):
        owner = owner_or_404(owner_id)
        db = get_db()
        if owner["token"] and not is_admin():
            db.execute("UPDATE owners SET last_seen = ? WHERE id = ?", (_now(), owner_id))
            db.commit()
        rep = report.build(db, owner_id)
        tab = request.args.get("tab", "summary")
        # แบ่งหน้าตารางละ PAGE_SIZE แถว (Vercel ส่งคำตอบได้ไม่เกิน 4.5 MB) ค้นหาได้ทุกหน้า
        lists = {"s11": rep.s11, "s12": rep.s12, "s13": rep.s13, "s14": rep.s14, "s15": rep.s15}
        q = (request.args.get("q") or "").strip()
        rows = list(enumerate(lists.get(tab, []), start=1))
        if q:
            needle = q.lower()
            rows = [(i, x) for i, x in rows if needle in _search_text(x)]
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        page = min(max(1, _int(request.args.get("page")) or 1), pages)
        items = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        private_link = (url_for("open_workspace", token=owner["token"], _external=True)
                        if owner["token"] else "")
        return render_template("owner.html", owner=owner, rep=rep, counts=rep.counts(), tab=tab,
                               items=items, q=q, page=page, pages=pages, total=len(rows),
                               private_link=private_link)

    @app.route("/owners/<int:owner_id>/export")
    def owner_export(owner_id):
        """ดาวน์โหลด Excel: ต้องกรอกชื่อผู้ดาวน์โหลดทุกครั้ง (บันทึกใน log)"""
        owner = owner_or_404(owner_id)
        by = " ".join((request.args.get("by") or "").split())[:120]
        if len(by) < 2:
            return render_template("download_name.html", owner=owner,
                                   suggest=owner["name"] or "", error=bool(request.args.get("by")))
        db = get_db()
        if not owner["name"]:  # ชุดข้อมูลที่ยังไม่มีชื่อ: ใช้ชื่อที่กรอกเป็นชื่อในรายงาน
            db.execute("UPDATE owners SET name = ? WHERE id = ?", (by, owner_id))
            db.commit()
            owner = db.execute("SELECT * FROM owners WHERE id = ?", (owner_id,)).fetchone()
            g.pop("ws", None)
        rep = report.build(db, owner_id)
        data = report.to_xlsx(rep)
        name = report.export_filename(owner["name"])
        log("download", f"ผู้ดาวน์โหลด: {by} · ไฟล์: {name} · {rep.counts()['s11']} Case ID",
            owner_id=owner_id, actor=by)
        resp = send_file(io.BytesIO(data), as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp.headers["Content-Disposition"] = (
            f"attachment; filename=\"report.xlsx\"; filename*=UTF-8''{quote(name)}")
        return resp

    @app.route("/owners/<int:owner_id>/settings", methods=["POST"])
    def owner_settings(owner_id):
        owner = owner_or_404(owner_id)
        db = get_db()
        fields = {"position": _form_text("position") or "",
                  "as_of": to_iso(request.form.get("as_of")),
                  "tpo_date": to_iso(request.form.get("tpo_date")),
                  "notes": request.form.get("notes", "").strip()}
        if _form_text("name"):
            fields["name"] = _form_text("name")
        try:
            db.execute(f"UPDATE owners SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                       [*fields.values(), owner_id])
            db.commit()
            log("settings", f"ชื่อ: {fields.get('name', owner['name'])}", owner_id=owner_id)
            flash("บันทึกการตั้งค่ารายงานแล้ว", "ok")
        except Exception:  # noqa: BLE001 - ชื่อซ้ำกับเจ้าของที่ผู้ดูแลจัดการ
            db.rollback()
            flash("ชื่อนี้มีอยู่แล้ว", "error")
        return redirect(url_for("owner_view", owner_id=owner_id, tab="settings"))

    @app.route("/owners/<int:owner_id>/purge", methods=["POST"])
    def owner_purge(owner_id):
        """ลบข้อมูลคดีทั้งหมดของชุดข้อมูลนี้ (ข้อมูลลับ)

        ผู้ใช้ทั่วไป: พิมพ์คำว่า "ลบ" เพื่อยืนยัน · ผู้ดูแล: ใส่รหัสผ่าน
        delete_workspace=1 (ชุดข้อมูลส่วนตัว): ลบชุดข้อมูลและลิงก์ส่วนตัวด้วย แล้วเริ่มใหม่
        """
        owner = owner_or_404(owner_id)
        db = get_db()
        back = url_for("owner_view", owner_id=owner_id, tab="settings") + "#danger"
        if is_admin():
            if not check_password_hash(g.user["password_hash"], request.form.get("password", "")):
                flash("รหัสผ่านไม่ถูกต้อง ยังไม่ได้ลบข้อมูล", "error")
                return redirect(back)
        elif (request.form.get("confirm") or "").strip() != "ลบ":
            flash('กรุณาพิมพ์คำว่า "ลบ" เพื่อยืนยัน ยังไม่ได้ลบข้อมูล', "error")
            return redirect(back)
        n = db.execute("SELECT COUNT(*) FROM cases WHERE owner_id = ?", (owner_id,)).fetchone()[0]
        label = owner_label(owner)
        db.execute("DELETE FROM cases WHERE owner_id = ?", (owner_id,))
        db.execute("UPDATE owners SET notes = '', as_of = NULL, tpo_date = NULL WHERE id = ?", (owner_id,))
        if owner["token"]:
            db.execute("DELETE FROM uploads WHERE user_id = ?", (-owner_id,))
            db.execute("DELETE FROM imports WHERE user_id = ?", (-owner_id,))
        db.commit()
        whole = owner["token"] and request.form.get("delete_workspace") == "1"
        log("purge", f"{label}: ลบคดี {n:,} รายการ" + (" และลบชุดข้อมูล/ลิงก์ส่วนตัว" if whole else ""),
            owner_id=owner_id)
        if whole:
            db.execute("DELETE FROM owners WHERE id = ?", (owner_id,))
            db.commit()
            if session.get("ws") == owner["token"]:
                session.pop("ws", None)
            flash(f"ลบข้อมูล {n:,} รายการ และชุดข้อมูลนี้เรียบร้อยแล้ว", "ok")
            return redirect(url_for("index"))
        flash(f"ลบข้อมูลคดีทั้งหมดของ {label} แล้ว ({n:,} รายการ)", "ok")
        return redirect(url_for("owner_view", owner_id=owner_id))

    @app.route("/owners/<int:owner_id>/bulk", methods=["POST"])
    def owner_bulk(owner_id):
        """วางข้อมูลจาก Excel: kind=s12 (Case ID[, ผู้เสียหาย]) หรือ s13 (เลขคดี, Case ID, ผู้ร้องทุกข์, วันที่, สถานะ C.)"""
        owner_or_404(owner_id)
        db = get_db()
        kind = request.form.get("kind")
        if kind not in ("s12", "s13"):
            abort(400)
        text = request.form.get("data", "")
        cache = CaseCache(db, [owner_id])
        n, bad = 0, []
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split("\t" if "\t" in line else ",")]
            if kind == "s12":
                cid = parts[0]
                if not re.match(r"^[A-Za-z]{1,3}\d{3,}$", cid):
                    bad.append(line)
                    continue
                exists = cache.get(owner_id, cid) is not None
                upsert_case(db, owner_id, cid, cache=cache, fields={
                    "statement_taken": 1,
                    "victim": parts[1] if len(parts) > 1 and not exists else None})
            else:
                parts += [""] * (5 - len(parts))
                no, cid, who, when, cst = parts[:5]
                if not re.match(r"^\d+/\d{4}$", no):
                    bad.append(line)
                    continue
                fields = {"criminal_no": no, "complainant": who or None,
                          "complaint_date": to_iso(when, with_time=True), "c_status": cst or None}
                if not cid:
                    cid, fields["no_case_id"] = placeholder_case_id(no), 1
                upsert_case(db, owner_id, cid, fields, cache=cache)
            n += 1
        db.commit()
        log("bulk", f"{'1.2 สอบปากคำ' if kind == 's12' else '1.3 คดีอาญา'}: {n} รายการ", owner_id=owner_id)
        flash(f"บันทึก {n} รายการ" + (f" · ข้ามไป {len(bad)} บรรทัด: " + " | ".join(bad[:5])
                                       if bad else ""), "ok" if not bad else "warn")
        return redirect(url_for("owner_view", owner_id=owner_id, tab=kind))

    # ---------------------------------------------------------- cases
    @app.route("/owners/<int:owner_id>/cases/new", methods=["GET", "POST"])
    def case_new(owner_id):
        owner = owner_or_404(owner_id)
        if request.method == "POST":
            cid = _form_text("case_id")
            db = get_db()
            if not cid:
                flash("กรุณากรอก Case ID", "error")
            elif db.execute("SELECT 1 FROM cases WHERE owner_id = ? AND case_id = ?",
                            (owner_id, cid)).fetchone():
                flash("Case ID นี้มีในรายงานนี้แล้ว", "error")
            else:
                cur = db.execute("INSERT INTO cases (owner_id, case_id, victim, in_tpo) "
                                 "VALUES (?, ?, ?, 0)", (owner_id, cid, _form_text("victim")))
                db.commit()
                log("case_add", cid, owner_id=owner_id)
                return redirect(url_for("case_edit", case_pk=cur.lastrowid))
        return render_template("case_new.html", owner=owner)

    @app.route("/cases/<int:case_pk>", methods=["GET", "POST"])
    def case_edit(case_pk):
        case = case_or_404(case_pk)
        db = get_db()
        if request.method == "POST":
            vals = {}
            for f in MANUAL_FIELDS:
                if f == "statement_taken":
                    vals[f] = 1 if request.form.get(f) else 0
                elif f == "complaint_date":
                    vals[f] = to_iso(request.form.get(f), with_time=True)
                elif f == "backlog_date":
                    vals[f] = to_iso(request.form.get(f))
                elif f == "backlog_damage":
                    vals[f] = _money(request.form.get(f))
                else:
                    vals[f] = _form_text(f)
            if not case["in_tpo"]:
                vals["victim"] = _form_text("victim")
                vals["damage"] = _money(request.form.get("damage"))
            target = case["owner_id"]
            if is_admin() and _int(request.form.get("owner_id")):
                target = _int(request.form.get("owner_id"))
            new_cid = _form_text("new_case_id") if case["no_case_id"] else None
            cid = new_cid or case["case_id"]
            if (new_cid or target != case["owner_id"]) and db.execute(
                    "SELECT 1 FROM cases WHERE owner_id = ? AND case_id = ? AND id != ?",
                    (target, cid, case_pk)).fetchone():
                flash(f"Case ID {cid} มีอยู่แล้วในรายงานปลายทาง", "error")
                return redirect(request.full_path)
            if new_cid:
                vals.update(case_id=new_cid, no_case_id=0)
            if target != case["owner_id"]:
                vals["owner_id"] = target
            sets = ", ".join(f"{k} = ?" for k in vals)
            db.execute(f"UPDATE cases SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                       [*vals.values(), case_pk])
            db.commit()
            log("case_edit", cid, owner_id=case["owner_id"])
            flash(f"บันทึก {cid} แล้ว", "ok")
            back = request.form.get("back") or url_for("owner_view", owner_id=case["owner_id"])
            return redirect(back if back.startswith("/") else url_for("index"))
        owners = (db.execute("SELECT id, name, token FROM owners ORDER BY name").fetchall()
                  if is_admin() else [])
        return render_template("case_edit.html", c=case, owners=owners,
                               owner=owner_or_404(case["owner_id"]),
                               back=request.args.get("back", ""))

    @app.route("/cases/<int:case_pk>/delete", methods=["POST"])
    def case_delete(case_pk):
        case = case_or_404(case_pk)
        db = get_db()
        db.execute("DELETE FROM cases WHERE id = ?", (case_pk,))
        db.commit()
        log("case_delete", case["case_id"], owner_id=case["owner_id"])
        flash(f"ลบ {case['case_id']} แล้ว", "ok")
        return redirect(url_for("owner_view", owner_id=case["owner_id"]))

    # ---------------------------------------------------------- import
    def _owner_mode():
        """ปลายทางของการนำเข้า: ผู้ดูแลเลือกได้ (None = แยกตามชื่อ) · ผู้ใช้ทั่วไป = ชุดข้อมูลของตัวเอง"""
        if not is_admin():
            return current_ws(create=True)["id"]
        mode = request.form.get("owner", "auto")
        if mode == "auto":
            return None
        owner_or_404(int(mode))
        return int(mode)

    def _run_import(stream, filename, owner_id, only=None):
        """นำเข้าไฟล์ แล้วคืนเลขที่ประวัติการนำเข้า (ImportError_ ถ้าไฟล์ไม่ถูกต้อง)"""
        db = get_db()
        try:
            wb = importer.load(stream)
            kind = importer.detect_kind(wb)
            if kind == "tpo":
                with batch(db):
                    res = importer.import_tpo(db, wb, owner_id=owner_id, only=only)
                m = re.search(r"(20\d{2})(\d{2})(\d{2})", filename)
                tpo_date = (f"{m[1]}-{m[2]}-{m[3]}" if m else date.today().isoformat())
                for oid in res["per_owner"]:
                    db.execute("UPDATE owners SET tpo_date = ? WHERE id = ?", (tpo_date, oid))
                # ชุดข้อมูลที่ยังไม่มีชื่อ + เลือกพนักงานสอบสวนคนเดียว: ใช้ชื่อนั้นเป็นชื่อในรายงาน
                if owner_id is not None and only and len(only) == 1:
                    db.execute("UPDATE owners SET name = ? WHERE id = ? AND name = ''",
                               (next(iter(only)), owner_id))
                label = "thaipoliceonline"
            elif kind == "report":
                with batch(db):
                    res = importer.import_report(db, wb, owner_id=owner_id)
                res["per_owner"] = {res["owner_id"]: res["rows"]}
                label = "รายงานเดิม (เอกสารแนบ)"
            else:
                raise importer.ImportError_(
                    "ไม่รู้จักรูปแบบไฟล์ ต้องเป็นไฟล์ export thaipoliceonline (มีคอลัมน์ 'เลขรับแจ้ง' และ "
                    "'พนักงานสอบสวน') หรือไฟล์รายงานเอกสารแนบ — พบ " + importer.first_row_text(wb))
            owners = {o["id"]: o for o in db.execute("SELECT * FROM owners")}
            summary = {"kind": label, "rows": res["rows"], "skipped": res.get("skipped", 0),
                       "created": res["created"], "updated": res["updated"],
                       "owners": [[owner_label(owners[k]), k, v] for k, v in res["per_owner"].items()]}
            cur = db.execute("INSERT INTO imports (filename, kind, user_id, summary) "
                             "VALUES (?, ?, ?, ?)",
                             (filename, kind, actor_id(), json.dumps(summary, ensure_ascii=False)))
            import_id = cur.lastrowid
            db.commit()
            g.pop("ws", None)  # อ่านชื่อชุดข้อมูลใหม่หลังนำเข้า (อาจเพิ่งได้ชื่อ)
            current_ws()
            log("import", f"{filename} · {label} · {res['rows']:,} แถว"
                + (f" · เลือก: {', '.join(sorted(only))[:300]}" if only else ""),
                owner_id=owner_id)
            return import_id
        except Exception:
            db.rollback()
            raise

    @app.route("/import", methods=["GET", "POST"])
    def import_view():
        db = get_db()
        if request.method == "POST":
            f = request.files.get("file")
            if not f or not f.filename:
                flash("กรุณาเลือกไฟล์ Excel", "error")
                return redirect(url_for("import_view"))
            try:
                import_id = _run_import(f.stream, f.filename, _owner_mode())
            except importer.ImportError_ as exc:
                log("import_failed", f"{f.filename}: {exc}"[:500])
                flash(str(exc), "error")
                return redirect(url_for("import_view"))
            return redirect(url_for("import_result", import_id=import_id))
        owners = (db.execute("SELECT id, name FROM owners WHERE token IS NULL ORDER BY name").fetchall()
                  if is_admin() else [])
        ws = current_ws()
        return render_template("import.html", owners=owners, my_name=(ws["name"] if ws else ""),
                               chunk_size=UPLOAD_CHUNK, max_size=UPLOAD_MAX)

    @app.route("/import/chunk", methods=["POST"])
    def import_chunk():
        """รับไฟล์ทีละส่วน (ส่วนละไม่เกิน UPLOAD_CHUNK) เก็บไว้ในฐานข้อมูลชั่วคราว"""
        token = request.headers.get("X-Upload-Token", "")
        try:
            seq = int(request.headers.get("X-Chunk-Index", ""))
        except ValueError:
            seq = -1
        data = request.get_data(cache=False)
        if (not re.fullmatch(r"[0-9a-f]{32}", token) or not 0 <= seq < UPLOAD_MAX // UPLOAD_CHUNK + 1
                or not data or len(data) > UPLOAD_CHUNK):
            return {"error": "ข้อมูลส่วนของไฟล์ไม่ถูกต้อง"}, 400
        if not is_admin():
            current_ws(create=True)
        db = get_db()
        me = actor_id()
        try:
            db.execute("DELETE FROM uploads WHERE token = ? AND seq = ? AND user_id = ?", (token, seq, me))
            db.execute("INSERT INTO uploads (token, user_id, seq, data) VALUES (?, ?, ?, ?)",
                       (token, me, seq, data))
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            app.logger.exception("chunk upload failed")
            return {"error": f"บันทึกส่วนของไฟล์ไม่สำเร็จ ({type(exc).__name__}: {str(exc)[:300]})"}, 500
        return {"ok": True}

    def _uploaded_file(delete):
        """รวมส่วนของไฟล์ที่อัปโหลด คืน bytes หรือ None ถ้าไม่ครบ"""
        db = get_db()
        token = request.form.get("token", "")
        total = _int(request.form.get("chunks")) or 0
        me = actor_id()
        rows = db.execute("SELECT seq, data FROM uploads WHERE token = ? AND user_id = ? "
                          "ORDER BY seq", (token, me)).fetchall()
        if delete:
            db.execute("DELETE FROM uploads WHERE token = ? AND user_id = ?", (token, me))
            # ล้างส่วนที่ค้างจากการอัปโหลดที่ไม่สำเร็จ (เก่ากว่า 1 วัน)
            db.execute("DELETE FROM uploads WHERE created_at < ?",
                       ((datetime.now(timezone.utc) + timedelta(hours=7) - timedelta(days=1))
                        .strftime("%Y-%m-%d %H:%M:%S"),))
            db.commit()
        if total < 1 or [r["seq"] for r in rows] != list(range(total)):
            return None
        return b"".join(bytes(r["data"]) for r in rows)

    @app.route("/import/inspect", methods=["POST"])
    def import_inspect():
        """อ่านไฟล์ที่อัปโหลดแล้ว บอกว่ามีพนักงานสอบสวนใครบ้าง ให้เลือกก่อนนำเข้า"""
        data = _uploaded_file(delete=False)
        if data is None:
            return {"error": "อัปโหลดไฟล์ไม่ครบ กรุณาลองใหม่"}, 400
        try:
            wb = importer.load(io.BytesIO(data))
            kind = importer.detect_kind(wb)
            if kind == "tpo":
                existing = ({o["name"] for o in get_db().execute(
                    "SELECT name FROM owners WHERE token IS NULL")} if is_admin() else set())
                people = [{"name": n, "rows": c, "exists": n in existing}
                          for n, c in importer.tpo_investigators(wb)]
                return {"kind": "tpo", "rows": sum(p["rows"] for p in people), "investigators": people}
            if kind == "report":
                return {"kind": "report"}
            return {"error": "ไม่รู้จักรูปแบบไฟล์ ต้องเป็นไฟล์ export thaipoliceonline (มีคอลัมน์ "
                             "'เลขรับแจ้ง' และ 'พนักงานสอบสวน') หรือไฟล์รายงานเอกสารแนบ — พบ "
                             + importer.first_row_text(wb)}, 400
        except importer.ImportError_ as exc:
            return {"error": str(exc)}, 400
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("inspect failed")
            return {"error": f"อ่านไฟล์ไม่สำเร็จ ({type(exc).__name__}: {str(exc)[:300]})"}, 500

    @app.route("/import/finish", methods=["POST"])
    def import_finish():
        """รวมส่วนของไฟล์แล้วนำเข้า คืน JSON {url} หรือ {error}

        names (ส่งได้หลายค่า): นำเข้าเฉพาะคดีของพนักงานสอบสวนเหล่านี้
        """
        names = [" ".join(n.split()) for n in request.form.getlist("names") if n.strip()]
        if request.form.get("select") == "1" and not names:
            return {"error": "กรุณาเลือกพนักงานสอบสวนอย่างน้อย 1 คน"}, 400
        data = _uploaded_file(delete=True)
        if data is None:
            return {"error": "อัปโหลดไฟล์ไม่ครบ กรุณาลองใหม่"}, 400
        filename = request.form.get("filename", "upload.xlsx")
        try:
            import_id = _run_import(io.BytesIO(data), filename, _owner_mode(),
                                    only=set(names) if names else None)
        except importer.ImportError_ as exc:
            log("import_failed", f"{filename}: {exc}"[:500])
            return {"error": str(exc)}, 400
        except Exception as exc:  # noqa: BLE001 - แจ้งสาเหตุให้ผู้ใช้เห็น และบันทึกใน log
            app.logger.exception("import failed: %s", filename)
            log("import_failed", f"{filename}: {type(exc).__name__}"[:500])
            return {"error": f"นำเข้าไม่สำเร็จ ({type(exc).__name__}: {str(exc)[:300]})"}, 500
        return {"url": url_for("import_result", import_id=import_id)}

    @app.route("/imports/<int:import_id>")
    def import_result(import_id):
        db = get_db()
        row = db.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        if row is None:
            abort(404)
        if not is_admin() and row["user_id"] != actor_id():
            abort(403)
        s = json.loads(row["summary"])
        return render_template("import_done.html", s=s, filename=row["filename"])

    # ---------------------------------------------------------- admin
    @app.route("/admin/logs")
    @admin_required
    def admin_logs():
        """บันทึกการใช้งาน: ใคร ทำอะไร เมื่อไหร่ จาก IP/อุปกรณ์ใด"""
        db = get_db()
        action = request.args.get("action") or ""
        q = (request.args.get("q") or "").strip()
        where, params = [], []
        if action in ACTIONS:
            where.append("l.action = ?")
            params.append(action)
        if q:
            where.append("(l.actor LIKE ? OR l.detail LIKE ? OR l.ip LIKE ? OR o.name LIKE ?)")
            params += [f"%{q}%"] * 4
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        total = db.execute(f"SELECT COUNT(*) FROM logs l LEFT JOIN owners o ON o.id = l.owner_id{sql_where}",
                           params).fetchone()[0]
        pages = max(1, -(-total // LOG_PAGE))
        page = min(max(1, _int(request.args.get("page")) or 1), pages)
        rows = db.execute(f"SELECT l.*, o.name AS owner_name, o.token AS owner_token FROM logs l "
                          f"LEFT JOIN owners o ON o.id = l.owner_id{sql_where} "
                          f"ORDER BY l.id DESC LIMIT {LOG_PAGE} OFFSET {(page - 1) * LOG_PAGE}",
                          params).fetchall()
        return render_template("logs.html", rows=rows, action=action, q=q, page=page, pages=pages,
                               total=total)

    @app.route("/admin/users", methods=["GET", "POST"])
    @admin_required
    def users():
        """บัญชีผู้ดูแลระบบ (ผู้ใช้ทั่วไปไม่ต้องมีบัญชี)"""
        db = get_db()
        if request.method == "POST":
            action = request.form.get("action")
            try:
                if action == "create":
                    u, p = _form_text("username"), request.form.get("password", "")
                    if not u or not re.fullmatch(r"[A-Za-z0-9_.@-]{3,40}", u) or len(p) < 6:
                        raise ValueError("ชื่อผู้ใช้ภาษาอังกฤษ/ตัวเลข 3–40 ตัว และรหัสผ่านอย่างน้อย 6 ตัวอักษร")
                    db.execute("INSERT INTO users (username, password_hash, display_name, role) "
                               "VALUES (?, ?, ?, 'admin')",
                               (u, generate_password_hash(p), _form_text("display_name") or u))
                    msg = f"เพิ่มผู้ดูแล {u}"
                elif action == "update":
                    uid = int(request.form["id"])
                    db.execute("UPDATE users SET display_name = ? WHERE id = ?",
                               (_form_text("display_name") or "", uid))
                    msg = f"แก้ไขผู้ดูแล #{uid}"
                    if request.form.get("password"):
                        if len(request.form["password"]) < 6:
                            raise ValueError("รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร")
                        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                                   (generate_password_hash(request.form["password"]), uid))
                        msg += " (ตั้งรหัสผ่านใหม่)"
                elif action == "delete":
                    uid = int(request.form["id"])
                    if uid == g.user["id"]:
                        raise ValueError("ลบบัญชีของตัวเองไม่ได้")
                    db.execute("DELETE FROM users WHERE id = ?", (uid,))
                    msg = f"ลบผู้ดูแล #{uid}"
                else:
                    abort(400)
                db.commit()
                log("admin_user", msg)
                flash(msg + " แล้ว", "ok")
            except ValueError as exc:
                db.rollback()
                flash(str(exc), "error")
            except Exception:  # noqa: BLE001
                db.rollback()
                flash("ชื่อผู้ใช้นี้มีอยู่แล้ว", "error")
            return redirect(url_for("users"))
        rows = db.execute("SELECT * FROM users WHERE role = 'admin' ORDER BY username").fetchall()
        return render_template("users.html", users=rows)

    @app.route("/admin/reset", methods=["POST"])
    @admin_required
    def admin_reset():
        """ล้างข้อมูลทั้งระบบ (ผู้ดูแลเท่านั้น) ต้องยืนยันด้วยรหัสผ่าน + พิมพ์คำว่า "ลบทั้งหมด"

        mode=keep: ลบคดี ชุดข้อมูล ประวัติ ไฟล์ค้าง และผู้ดูแลคนอื่น เหลือบัญชีผู้ดูแลที่กดลบ
        mode=all : ลบทุกอย่างรวมบัญชีตัวเอง → การเข้าสู่ระบบผู้ดูแลครั้งแรกจะบันทึกเป็นผู้ดูแลใหม่
        บันทึกการใช้งาน (log) เก็บไว้เสมอ
        """
        db = get_db()
        back = url_for("users") + "#reset"
        if not check_password_hash(g.user["password_hash"], request.form.get("password", "")):
            flash("รหัสผ่านไม่ถูกต้อง ยังไม่ได้ลบข้อมูล", "error")
            return redirect(back)
        if (request.form.get("confirm") or "").strip() != "ลบทั้งหมด":
            flash('กรุณาพิมพ์คำว่า "ลบทั้งหมด" เพื่อยืนยัน', "error")
            return redirect(back)
        n_cases = db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        n_ws = db.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
        for table in ("cases", "uploads", "imports", "settings"):
            db.execute(f"DELETE FROM {table}")
        db.execute("UPDATE users SET owner_id = NULL")
        db.execute("DELETE FROM owners")
        db.execute("DELETE FROM users WHERE id != ?", (g.user["id"],))
        db.commit()
        everything = request.form.get("mode") == "all"
        log("reset", f"ลบคดี {n_cases:,} รายการ, ชุดข้อมูล {n_ws:,} ชุด"
            + (" และบัญชีผู้ดูแลทั้งหมด" if everything else ""))
        if everything:
            db.execute("DELETE FROM users")
            db.commit()
            session.clear()
            return redirect(url_for("login"))
        flash(f"ล้างข้อมูลทั้งระบบแล้ว: คดี {n_cases:,} รายการ, ชุดข้อมูล {n_ws:,} ชุด", "ok")
        return redirect(url_for("users"))

    @app.route("/admin/owners", methods=["POST"])
    @admin_required
    def owners_admin():
        db = get_db()
        action = request.form.get("action")
        if action == "create" and _form_text("name"):
            try:
                cur = db.execute("INSERT INTO owners (name, position) VALUES (?, ?)",
                                 (_form_text("name"), _form_text("position") or ""))
                db.commit()
                return redirect(url_for("owner_view", owner_id=cur.lastrowid, tab="settings"))
            except Exception:  # noqa: BLE001
                db.rollback()
                flash("ชื่อนี้มีอยู่แล้ว", "error")
        elif action in ("delete", "delete_many"):
            ids = ([_int(request.form.get("id"))] if action == "delete"
                   else [_int(x) for x in request.form.getlist("ids")])
            ids = [i for i in ids if i]
            names = []
            for oid in ids:
                row = db.execute("SELECT * FROM owners WHERE id = ?", (oid,)).fetchone()
                if row is not None:
                    names.append(owner_label(row))
                    db.execute("DELETE FROM owners WHERE id = ?", (oid,))
            db.commit()
            log("owner_delete", ", ".join(names)[:900])
            flash(f"ลบ {len(names)} ชุดข้อมูล พร้อมคดีทั้งหมดแล้ว", "ok")
        return redirect(url_for("index"))

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", msg="ไม่มีสิทธิ์เข้าถึงข้อมูลชุดนี้"), 403

    @app.errorhandler(404)
    def notfound(_e):
        return render_template("error.html", msg="ไม่พบข้อมูล"), 404
