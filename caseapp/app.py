"""เว็บรายงานข้อมูลคดีและ Case ID แยกตามเจ้าของ (พนักงานสอบสวน)"""
import io
import json
import os
import re
import secrets
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
    )
    app.config.update(test_config)
    init_db(app)
    app.jinja_env.filters.update(
        ce=ce_slash, be=be_slash, th=thai_short, thtime=thai_short_time,
        money=lambda v: "" if v in (None, "") else f"{float(v):,.2f}",
    )
    app.jinja_env.globals.update(report=report)
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

def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*a, **kw):
        if g.user["role"] != "admin":
            abort(403)
        return view(*a, **kw)
    return wrapped


def owner_or_404(owner_id):
    """คืนข้อมูลเจ้าของ ถ้าผู้ใช้มีสิทธิ์ (admin หรือเป็นเจ้าของข้อมูลนั้นเอง)"""
    row = get_db().execute("SELECT * FROM owners WHERE id = ?", (owner_id,)).fetchone()
    if row is None:
        abort(404)
    if g.user["role"] != "admin" and g.user["owner_id"] != row["id"]:
        abort(403)
    return row


def case_or_404(case_pk):
    row = get_db().execute("SELECT * FROM cases WHERE id = ?", (case_pk,)).fetchone()
    if row is None:
        abort(404)
    owner_or_404(row["owner_id"])
    return row


def _csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def _form_text(name):
    v = request.form.get(name, "")
    v = " ".join(v.split()) if "\n" not in v else v.strip()
    return v or None


def _search_text(item):
    data = item["case"] if "case" in item else item
    return " ".join(str(v) for v in data.values() if v is not None).lower()


def _role(text):
    return "admin" if text == "admin" else "officer"


def _int(text):
    return int(text) if (text or "").strip().isdigit() else None


def _money(text):
    t = (text or "").replace(",", "").strip()
    return float(t) if re.match(r"^\d+(\.\d+)?$", t) else None


def _register(app):
    app.jinja_env.globals["csrf_token"] = _csrf_token

    @app.before_request
    def load_user():
        uid = session.get("uid")
        g.user = None
        if uid:
            g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if request.method == "POST" and not app.config.get("TESTING_NO_CSRF"):
            token = session.get("csrf")
            sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
            if not token or not secrets.compare_digest(sent, token):
                abort(400, "CSRF token ไม่ถูกต้อง กรุณาโหลดหน้าใหม่")
        if request.endpoint not in ("setup", "static"):
            if get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                return redirect(url_for("setup"))

    @app.context_processor
    def inject():
        owners = []
        if g.get("user") is not None and g.user["role"] == "admin":
            owners = get_db().execute("SELECT id, name FROM owners ORDER BY name").fetchall()
        return {"nav_owners": owners}

    # ---------------------------------------------------------- auth
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        db = get_db()
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            return redirect(url_for("login"))
        if request.method == "POST":
            u, p = _form_text("username"), request.form.get("password", "")
            if not u or len(p) < 6:
                flash("กรุณากรอกชื่อผู้ใช้ และรหัสผ่านอย่างน้อย 6 ตัวอักษร", "error")
            else:
                cur = db.execute("INSERT INTO users (username, password_hash, display_name, role) "
                                 "VALUES (?, ?, ?, 'admin')",
                                 (u, generate_password_hash(p), _form_text("display_name") or u))
                db.commit()
                session.clear()
                session["uid"] = cur.lastrowid
                flash("สร้างผู้ดูแลระบบแล้ว", "ok")
                return redirect(url_for("index"))
        return render_template("setup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            row = get_db().execute("SELECT * FROM users WHERE username = ?",
                                   (_form_text("username"),)).fetchone()
            if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
                session.clear()
                session["uid"] = row["id"]
                nxt = request.args.get("next") or ""
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                                else url_for("index"))
            flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "error")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/password", methods=["GET", "POST"])
    @login_required
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
                flash("เปลี่ยนรหัสผ่านแล้ว", "ok")
                return redirect(url_for("index"))
        return render_template("password.html")

    # ---------------------------------------------------------- dashboard
    @app.route("/")
    @login_required
    def index():
        db = get_db()
        if g.user["role"] != "admin":
            if g.user["owner_id"]:
                return redirect(url_for("owner_view", owner_id=g.user["owner_id"]))
            return render_template("no_owner.html")
        owners = db.execute("""
            SELECT o.*, COUNT(c.id) AS n_cases,
                   SUM(c.in_tpo) AS n_tpo, SUM(c.statement_taken) AS n_stmt,
                   SUM(CASE WHEN c.criminal_no IS NOT NULL AND c.criminal_no != '' THEN 1 ELSE 0 END) AS n_crim,
                   (SELECT GROUP_CONCAT(username, ', ') FROM users u WHERE u.owner_id = o.id) AS users
            FROM owners o LEFT JOIN cases c ON c.owner_id = o.id
            GROUP BY o.id ORDER BY o.name""").fetchall()
        imports = db.execute("""SELECT i.*, u.username FROM imports i LEFT JOIN users u
                                ON u.id = i.user_id ORDER BY i.id DESC LIMIT 10""").fetchall()
        return render_template("index.html", owners=owners, imports=imports,
                               loads=lambda s: json.loads(s or "{}"))

    # ---------------------------------------------------------- owner report
    @app.route("/owners/<int:owner_id>")
    @login_required
    def owner_view(owner_id):
        owner = owner_or_404(owner_id)
        rep = report.build(get_db(), owner_id)
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
        return render_template("owner.html", owner=owner, rep=rep, counts=rep.counts(), tab=tab,
                               items=items, q=q, page=page, pages=pages, total=len(rows))

    @app.route("/owners/<int:owner_id>/export")
    @login_required
    def owner_export(owner_id):
        owner = owner_or_404(owner_id)
        rep = report.build(get_db(), owner_id)
        data = report.to_xlsx(rep)
        name = report.export_filename(owner["name"])
        resp = send_file(io.BytesIO(data), as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp.headers["Content-Disposition"] = (
            f"attachment; filename=\"report.xlsx\"; filename*=UTF-8''{quote(name)}")
        return resp

    @app.route("/owners/<int:owner_id>/settings", methods=["POST"])
    @login_required
    def owner_settings(owner_id):
        owner_or_404(owner_id)
        db = get_db()
        fields = {"position": _form_text("position") or "",
                  "as_of": to_iso(request.form.get("as_of")),
                  "tpo_date": to_iso(request.form.get("tpo_date")),
                  "notes": request.form.get("notes", "").strip()}
        if g.user["role"] == "admin" and _form_text("name"):
            fields["name"] = _form_text("name")
        try:
            db.execute(f"UPDATE owners SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                       [*fields.values(), owner_id])
            db.commit()
            flash("บันทึกข้อมูลเจ้าของแล้ว", "ok")
        except Exception:  # noqa: BLE001 - ชื่อซ้ำ
            db.rollback()
            flash("ชื่อนี้มีอยู่แล้ว", "error")
        return redirect(url_for("owner_view", owner_id=owner_id, tab="settings"))

    @app.route("/owners/<int:owner_id>/bulk", methods=["POST"])
    @login_required
    def owner_bulk(owner_id):
        """วางข้อมูลจาก Excel: kind=s12 (Case ID[, ผู้เสียหาย]) หรือ s13 (เลขคดี, Case ID, ผู้ร้องทุกข์, วันที่, สถานะ C.)"""
        owner_or_404(owner_id)
        db = get_db()
        kind = request.form.get("kind")
        text = request.form.get("data", "")
        is_admin = g.user["role"] == "admin"
        cache = CaseCache(db)
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
                exists = cache.get(cid) is not None
                res = upsert_case(db, owner_id, cid, allow_move=is_admin, cache=cache, fields={
                    "statement_taken": 1,
                    "victim": parts[1] if len(parts) > 1 and not exists else None})
            elif kind == "s13":
                parts += [""] * (5 - len(parts))
                no, cid, who, when, cst = parts[:5]
                if not re.match(r"^\d+/\d{4}$", no):
                    bad.append(line)
                    continue
                fields = {"criminal_no": no, "complainant": who or None,
                          "complaint_date": to_iso(when, with_time=True), "c_status": cst or None}
                if not cid:
                    cid, fields["no_case_id"] = placeholder_case_id(no), 1
                res = upsert_case(db, owner_id, cid, fields, allow_move=is_admin, cache=cache)
            else:
                abort(400)
            if res == "locked":
                bad.append(line + "  (Case ID นี้เป็นของเจ้าของอื่น)")
            else:
                n += 1
        db.commit()
        flash(f"บันทึก {n} รายการ" + (f" · ข้ามไป {len(bad)} บรรทัด: " + " | ".join(bad[:5])
                                       if bad else ""), "ok" if not bad else "warn")
        return redirect(url_for("owner_view", owner_id=owner_id,
                                tab="s12" if kind == "s12" else "s13"))

    # ---------------------------------------------------------- cases
    @app.route("/owners/<int:owner_id>/cases/new", methods=["GET", "POST"])
    @login_required
    def case_new(owner_id):
        owner = owner_or_404(owner_id)
        if request.method == "POST":
            cid = _form_text("case_id")
            db = get_db()
            if not cid:
                flash("กรุณากรอก Case ID", "error")
            elif db.execute("SELECT 1 FROM cases WHERE case_id = ?", (cid,)).fetchone():
                flash("Case ID นี้มีในระบบแล้ว", "error")
            else:
                cur = db.execute("INSERT INTO cases (owner_id, case_id, victim, in_tpo) "
                                 "VALUES (?, ?, ?, 0)", (owner_id, cid, _form_text("victim")))
                db.commit()
                return redirect(url_for("case_edit", case_pk=cur.lastrowid))
        return render_template("case_new.html", owner=owner)

    @app.route("/cases/<int:case_pk>", methods=["GET", "POST"])
    @login_required
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
            new_cid = _form_text("new_case_id")
            if case["no_case_id"] and new_cid:
                if db.execute("SELECT 1 FROM cases WHERE case_id = ?", (new_cid,)).fetchone():
                    flash(f"Case ID {new_cid} มีในระบบแล้ว", "error")
                    return redirect(request.full_path)
                vals.update(case_id=new_cid, no_case_id=0)
            if g.user["role"] == "admin" and request.form.get("owner_id"):
                new_owner = int(request.form["owner_id"])
                if new_owner != case["owner_id"]:
                    vals["owner_id"] = new_owner
                    vals["owner_locked"] = 1
            sets = ", ".join(f"{k} = ?" for k in vals)
            db.execute(f"UPDATE cases SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                       [*vals.values(), case_pk])
            db.commit()
            flash(f"บันทึก {case['case_id']} แล้ว", "ok")
            back = request.form.get("back") or url_for("owner_view", owner_id=case["owner_id"])
            return redirect(back if back.startswith("/") else url_for("index"))
        owners = db.execute("SELECT id, name FROM owners ORDER BY name").fetchall()
        return render_template("case_edit.html", c=case, owners=owners,
                               owner=owner_or_404(case["owner_id"]),
                               back=request.args.get("back", ""))

    @app.route("/cases/<int:case_pk>/delete", methods=["POST"])
    @login_required
    def case_delete(case_pk):
        case = case_or_404(case_pk)
        db = get_db()
        db.execute("DELETE FROM cases WHERE id = ?", (case_pk,))
        db.commit()
        flash(f"ลบ {case['case_id']} แล้ว", "ok")
        return redirect(url_for("owner_view", owner_id=case["owner_id"]))

    # ---------------------------------------------------------- import
    def _owner_mode():
        """เจ้าของปลายทางของการนำเข้า: None = แยกอัตโนมัติ (ผู้ดูแลเท่านั้น)"""
        if g.user["role"] != "admin":
            return g.user["owner_id"]
        mode = request.form.get("owner", "auto")
        if mode == "auto":
            return None
        owner_or_404(int(mode))
        return int(mode)

    def _run_import(stream, filename, owner_id):
        """นำเข้าไฟล์ แล้วคืนเลขที่ประวัติการนำเข้า (ImportError_ ถ้าไฟล์ไม่ถูกต้อง)"""
        db = get_db()
        is_admin = g.user["role"] == "admin"
        try:
            wb = importer.load(stream)
            kind = importer.detect_kind(wb)
            if kind == "tpo":
                with batch(db):
                    res = importer.import_tpo(db, wb, owner_id=owner_id, allow_move=is_admin)
                m = re.search(r"(20\d{2})(\d{2})(\d{2})", filename)
                tpo_date = (f"{m[1]}-{m[2]}-{m[3]}" if m else date.today().isoformat())
                for oid in res["per_owner"]:
                    db.execute("UPDATE owners SET tpo_date = ? WHERE id = ?", (tpo_date, oid))
                label = "thaipoliceonline"
            elif kind == "report":
                with batch(db):
                    res = importer.import_report(db, wb, owner_id=owner_id, allow_move=is_admin)
                res["per_owner"] = {res["owner_id"]: res["rows"]}
                label = "รายงานเดิม (เอกสารแนบ)"
            else:
                raise importer.ImportError_(
                    "ไม่รู้จักรูปแบบไฟล์ ต้องเป็นไฟล์ export thaipoliceonline หรือไฟล์รายงานเอกสารแนบ")
            names = {o["id"]: o["name"] for o in db.execute("SELECT id, name FROM owners")}
            summary = {"kind": label, "rows": res["rows"], "created": res["created"],
                       "updated": res["updated"], "locked": res["locked"],
                       "owners": {names[k]: v for k, v in res["per_owner"].items()}}
            cur = db.execute("INSERT INTO imports (filename, kind, user_id, summary) "
                             "VALUES (?, ?, ?, ?)",
                             (filename, kind, g.user["id"], json.dumps(summary, ensure_ascii=False)))
            import_id = cur.lastrowid
            db.commit()
            return import_id
        except Exception:
            db.rollback()
            raise

    @app.route("/import", methods=["GET", "POST"])
    @login_required
    def import_view():
        db = get_db()
        is_admin = g.user["role"] == "admin"
        if not is_admin and not g.user["owner_id"]:
            return render_template("no_owner.html")
        if request.method == "POST":
            f = request.files.get("file")
            if not f or not f.filename:
                flash("กรุณาเลือกไฟล์ Excel", "error")
                return redirect(url_for("import_view"))
            try:
                import_id = _run_import(f.stream, f.filename, _owner_mode())
            except importer.ImportError_ as exc:
                flash(str(exc), "error")
                return redirect(url_for("import_view"))
            return redirect(url_for("import_result", import_id=import_id))
        owners = db.execute("SELECT id, name FROM owners ORDER BY name").fetchall()
        return render_template("import.html", owners=owners, is_admin=is_admin,
                               chunk_size=UPLOAD_CHUNK, max_size=UPLOAD_MAX)

    @app.route("/import/chunk", methods=["POST"])
    @login_required
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
        db = get_db()
        db.execute("DELETE FROM uploads WHERE token = ? AND seq = ? AND user_id = ?",
                   (token, seq, g.user["id"]))
        db.execute("INSERT INTO uploads (token, user_id, seq, data) VALUES (?, ?, ?, ?)",
                   (token, g.user["id"], seq, data))
        db.commit()
        return {"ok": True}

    @app.route("/import/finish", methods=["POST"])
    @login_required
    def import_finish():
        """รวมส่วนของไฟล์แล้วนำเข้า คืน JSON {url} หรือ {error}"""
        db = get_db()
        if g.user["role"] != "admin" and not g.user["owner_id"]:
            return {"error": "บัญชีนี้ยังไม่ได้ผูกกับเจ้าของข้อมูล"}, 403
        token = request.form.get("token", "")
        total = _int(request.form.get("chunks")) or 0
        rows = db.execute("SELECT seq, data FROM uploads WHERE token = ? AND user_id = ? "
                          "ORDER BY seq", (token, g.user["id"])).fetchall()
        db.execute("DELETE FROM uploads WHERE token = ? AND user_id = ?", (token, g.user["id"]))
        # ล้างส่วนที่ค้างจากการอัปโหลดที่ไม่สำเร็จ (เก่ากว่า 1 วัน)
        db.execute("DELETE FROM uploads WHERE created_at < ?",
                   ((datetime.now(timezone.utc) + timedelta(hours=7) - timedelta(days=1))
                    .strftime("%Y-%m-%d %H:%M:%S"),))
        db.commit()
        if total < 1 or [r["seq"] for r in rows] != list(range(total)):
            return {"error": "อัปโหลดไฟล์ไม่ครบ กรุณาลองใหม่"}, 400
        data = b"".join(bytes(r["data"]) for r in rows)
        try:
            import_id = _run_import(io.BytesIO(data), request.form.get("filename", "upload.xlsx"),
                                    _owner_mode())
        except importer.ImportError_ as exc:
            return {"error": str(exc)}, 400
        return {"url": url_for("import_result", import_id=import_id)}

    @app.route("/imports/<int:import_id>")
    @login_required
    def import_result(import_id):
        db = get_db()
        row = db.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        if row is None:
            abort(404)
        if g.user["role"] != "admin" and row["user_id"] != g.user["id"]:
            abort(403)
        s = json.loads(row["summary"])
        ids = {o["name"]: o["id"] for o in db.execute("SELECT id, name FROM owners")}
        return render_template("import_done.html", s=s, filename=row["filename"],
                               owner_ids={n: ids.get(n) for n in s["owners"]})

    # ---------------------------------------------------------- admin
    @app.route("/admin/users", methods=["GET", "POST"])
    @admin_required
    def users():
        db = get_db()
        if request.method == "POST":
            action = request.form.get("action")
            try:
                if action == "create":
                    u, p = _form_text("username"), request.form.get("password", "")
                    if not u or len(p) < 6:
                        raise ValueError("กรุณากรอกชื่อผู้ใช้ และรหัสผ่านอย่างน้อย 6 ตัวอักษร")
                    db.execute("INSERT INTO users (username, password_hash, display_name, role, owner_id) "
                               "VALUES (?, ?, ?, ?, ?)",
                               (u, generate_password_hash(p), _form_text("display_name") or u,
                                _role(request.form.get("role")),
                                _int(request.form.get("owner_id"))))
                    flash(f"สร้างผู้ใช้ {u} แล้ว", "ok")
                elif action == "update":
                    uid = int(request.form["id"])
                    db.execute("UPDATE users SET role = ?, owner_id = ?, display_name = ? WHERE id = ?",
                               (_role(request.form.get("role")), _int(request.form.get("owner_id")),
                                _form_text("display_name") or "", uid))
                    if request.form.get("password"):
                        if len(request.form["password"]) < 6:
                            raise ValueError("รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร")
                        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                                   (generate_password_hash(request.form["password"]), uid))
                    flash("บันทึกผู้ใช้แล้ว", "ok")
                elif action == "delete":
                    uid = int(request.form["id"])
                    if uid == g.user["id"]:
                        raise ValueError("ลบบัญชีของตัวเองไม่ได้")
                    db.execute("DELETE FROM users WHERE id = ?", (uid,))
                    flash("ลบผู้ใช้แล้ว", "ok")
                if db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0] == 0:
                    raise ValueError("ต้องมีผู้ดูแลระบบอย่างน้อย 1 คน")
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                flash(str(exc) if isinstance(exc, ValueError) else "ชื่อผู้ใช้นี้มีอยู่แล้ว", "error")
            return redirect(url_for("users"))
        rows = db.execute("SELECT u.*, o.name AS owner_name FROM users u LEFT JOIN owners o "
                          "ON o.id = u.owner_id ORDER BY u.username").fetchall()
        owners = db.execute("SELECT id, name FROM owners ORDER BY name").fetchall()
        return render_template("users.html", users=rows, owners=owners)

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
        elif action == "delete":
            oid = int(request.form["id"])
            db.execute("DELETE FROM owners WHERE id = ?", (oid,))
            db.commit()
            flash("ลบเจ้าของข้อมูลและคดีทั้งหมดของเจ้าของนี้แล้ว", "ok")
        return redirect(url_for("index"))

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", msg="ไม่มีสิทธิ์เข้าถึงข้อมูลของเจ้าของนี้"), 403

    @app.errorhandler(404)
    def notfound(_e):
        return render_template("error.html", msg="ไม่พบข้อมูล"), 404

