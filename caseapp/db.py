"""ฐานข้อมูล: ผู้ใช้ เจ้าของข้อมูล (พนักงานสอบสวน) และคดี

ใช้ได้ 2 แบบ ด้วย SQL ชุดเดียวกัน
- SQLite (ค่าเริ่มต้น สำหรับเครื่องตัวเอง)
- PostgreSQL เมื่อกำหนด DATABASE_URL / POSTGRES_URL (เช่น Neon บน Vercel)
"""
import contextlib
import os
import re
import sqlite3

from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,          -- ยศ ชื่อ สกุล เช่น พ.ต.ต.อำพล ทองอร่าม
    position TEXT DEFAULT '',           -- ตำแหน่ง/สังกัด เช่น สว.(สอบสวน) กก.1 บก.สอท.4
    as_of TEXT,                         -- วันที่ข้อมูล (YYYY-MM-DD) ว่าง = วันนี้
    tpo_date TEXT,                      -- วันที่ไฟล์ export thaipoliceonline ล่าสุด
    notes TEXT DEFAULT '',              -- หมายเหตุเพิ่มเติม (บรรทัดละ 1 ข้อ)
    created_at TEXT DEFAULT {NOW}
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'officer',   -- admin | officer
    owner_id INTEGER REFERENCES owners(id) ON DELETE SET NULL,
    created_at TEXT DEFAULT {NOW}
);

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
    case_id TEXT NOT NULL,
    in_tpo INTEGER NOT NULL DEFAULT 0,  -- 1 = มีในไฟล์ thaipoliceonline (อยู่ในรายการ 1.1)
    no_case_id INTEGER NOT NULL DEFAULT 0, -- 1 = คดีในระบบ C. ที่ยังไม่มี Case ID (case_id เป็นค่าภายใน)
    owner_locked INTEGER NOT NULL DEFAULT 0, -- 1 = กำหนดเจ้าของเอง ไม่ให้ import ทับ

    -- ข้อมูลจาก thaipoliceonline
    report_date TEXT,                   -- วันที่รับแจ้ง (YYYY-MM-DD)
    victim TEXT,
    gender TEXT, age TEXT,
    case_type TEXT, case_type_l1 TEXT, case_type_l2 TEXT, case_type_l3 TEXT,
    case_group_type TEXT, detail TEXT,
    damage REAL,
    bch TEXT, bk TEXT, unit TEXT,
    system_investigator TEXT,           -- พนักงานสอบสวนตามระบบ
    detective TEXT,
    status TEXT,                        -- สถานะ thaipoliceonline
    tpo_criminal_no TEXT,               -- เลขคดีอาญาตาม thaipoliceonline (คอลัมน์ G ข้อ 1.1)
    phone TEXT, bank_ref TEXT, transfer_start TEXT, transfer_end TEXT, crime_group TEXT,

    -- 1.2 สอบปากคำแล้ว (CCMS: มีสำนวนแล้ว)
    statement_taken INTEGER NOT NULL DEFAULT 0,
    seq12 INTEGER,                      -- ลำดับในรายการ 1.2
    seq15 INTEGER,                      -- ลำดับในรายการ 1.5 (จากไฟล์รายงานเดิม)

    -- 1.3 / 1.4 คดีอาญา ระบบ C.
    criminal_no TEXT,
    complainant TEXT,
    complaint_date TEXT,                -- YYYY-MM-DDTHH:MM
    c_status TEXT,                      -- สถานะในระบบ C.
    actual_status TEXT,                 -- สถานะจริงของสำนวน
    c_action TEXT,                      -- การดำเนินการ (1.4)

    -- 1.5 ค้างเกิน 6 เดือน
    court TEXT, backlog_reason TEXT, backlog_detail TEXT, backlog_plan TEXT, backlog_evidence TEXT,
    backlog_victim TEXT, backlog_damage REAL, backlog_date TEXT, -- ใช้แทนค่าหลักในข้อ 1.5 ถ้ากรอก

    updated_at TEXT DEFAULT {NOW},
    UNIQUE (case_id)
);
CREATE INDEX IF NOT EXISTS idx_cases_owner ON cases(owner_id);
CREATE INDEX IF NOT EXISTS idx_cases_caseid ON cases(case_id);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    filename TEXT, kind TEXT, user_id INTEGER, summary TEXT,
    created_at TEXT DEFAULT {NOW}
);

-- ไฟล์ที่อัปโหลดเป็นส่วน ๆ (Vercel รับคำขอได้ไม่เกิน 4.5 MB) ลบทิ้งหลังนำเข้า
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    data BLOB NOT NULL,
    created_at TEXT DEFAULT {NOW},
    UNIQUE (token, seq)
);
"""

TPO_FIELDS = [
    "report_date", "victim", "gender", "age", "case_type", "case_type_l1", "case_type_l2",
    "case_type_l3", "case_group_type", "detail", "damage", "bch", "bk", "unit",
    "system_investigator", "detective", "status", "tpo_criminal_no", "phone", "bank_ref", "transfer_start",
    "transfer_end", "crime_group",
]

MANUAL_FIELDS = [
    "statement_taken", "criminal_no", "complainant", "complaint_date", "c_status",
    "actual_status", "c_action", "court", "backlog_reason", "backlog_detail",
    "backlog_plan", "backlog_evidence", "backlog_victim", "backlog_damage", "backlog_date",
]

# คอลัมน์ที่เพิ่มภายหลัง: เพิ่มให้ฐานข้อมูลเดิมอัตโนมัติ
MIGRATIONS = {
    "cases": {"no_case_id": "INTEGER NOT NULL DEFAULT 0", "tpo_criminal_no": "TEXT",
              "seq12": "INTEGER", "seq15": "INTEGER", "backlog_victim": "TEXT", "backlog_damage": "REAL",
              "backlog_date": "TEXT"},
}


class Row(dict):
    """แถวผลลัพธ์ที่ใช้ได้ทั้ง row["col"] และ row[0] เหมือน sqlite3.Row"""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _PgCursor:
    def __init__(self, cur, returns_id=False):
        self._cur = cur
        self._returns_id = returns_id
        self._id = None

    @property
    def lastrowid(self):
        """อ่าน id เมื่อต้องใช้เท่านั้น (ไม่บังคับ sync ระหว่าง pipeline)"""
        if self._returns_id and self._id is None:
            self._id = self._cur.fetchone()["id"]
        return self._id

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)


class PgConnection:
    """ห่อ psycopg ให้เรียกแบบเดียวกับ sqlite3 (placeholder ?, lastrowid)"""

    def __init__(self, url):
        import psycopg

        def row_factory(cur):
            names = [d.name for d in cur.description] if cur.description else []
            return lambda values: Row(zip(names, values))

        self._conn = psycopg.connect(url, row_factory=row_factory, prepare_threshold=None)

    @staticmethod
    def translate(sql):
        sql = sql.replace("%", "%%").replace("?", "%s")
        return re.sub(r"GROUP_CONCAT\(", "STRING_AGG(", sql)

    def execute(self, sql, params=()):
        sql = self.translate(sql)
        is_insert = re.match(r"\s*INSERT\b", sql, re.I) and "RETURNING" not in sql.upper()
        if is_insert:
            sql += " RETURNING id"
        cur = self._conn.execute(sql, tuple(params))
        return _PgCursor(cur, returns_id=bool(is_insert))

    def pipeline(self):
        return self._conn.pipeline()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def database_url():
    for key in ("DATABASE_URL", "POSTGRES_URL"):
        url = os.environ.get(key, "")
        if url.startswith(("postgres://", "postgresql://")):
            return url
    return None


def connect(config):
    url = config.get("DATABASE_URL")
    if url:
        return PgConnection(url)
    path = config["DATABASE"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config)
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _statements(schema):
    no_comments = re.sub(r"--[^\n]*", "", schema)
    return [x.strip() for x in no_comments.split(";") if x.strip()]


def init_db(app):
    with app.app_context():
        db = get_db()
        if isinstance(db, PgConnection):
            schema = (SCHEMA.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY")
                      .replace(" REAL", " DOUBLE PRECISION")
                      .replace(" BLOB", " BYTEA")
                      .replace("{NOW}", "(to_char(now() AT TIME ZONE 'Asia/Bangkok', "
                                        "'YYYY-MM-DD HH24:MI:SS'))"))
            db.execute("SELECT pg_advisory_xact_lock(4815162342)")  # กันหลาย instance สร้างพร้อมกัน
            for stmt in _statements(schema):
                db.execute(stmt)
            for table, cols in MIGRATIONS.items():
                for col, decl in cols.items():
                    decl = decl.replace("REAL", "DOUBLE PRECISION")
                    db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {decl}")
        else:
            db.executescript(SCHEMA.replace("{NOW}", "(datetime('now', '+7 hours'))"))
            for table, cols in MIGRATIONS.items():
                have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
                for col, decl in cols.items():
                    if col not in have:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        db.commit()
    app.teardown_appcontext(close_db)


def get_or_create_owner(db, name, position=""):
    name = " ".join((name or "").split())
    row = db.execute("SELECT * FROM owners WHERE name = ?", (name,)).fetchone()
    if row:
        if position and not row["position"]:
            db.execute("UPDATE owners SET position = ? WHERE id = ?", (position, row["id"]))
        return row["id"], False
    cur = db.execute("INSERT INTO owners (name, position) VALUES (?, ?)", (name, position or ""))
    return cur.lastrowid, True


class CaseCache:
    """สถานะคดีในหน่วยความจำระหว่างนำเข้า ลดจำนวน query (สำคัญกับ Postgres บนคลาวด์)"""

    def __init__(self, db):
        self.rows = {r["case_id"]: {"owner_id": r["owner_id"], "owner_locked": r["owner_locked"],
                                    "statement_taken": r["statement_taken"]}
                     for r in db.execute("SELECT case_id, owner_id, owner_locked, statement_taken "
                                         "FROM cases")}
        self.seq12 = {r["owner_id"]: r["m"] or 0 for r in db.execute(
            "SELECT owner_id, MAX(seq12) AS m FROM cases GROUP BY owner_id")}

    def get(self, case_id):
        return self.rows.get(case_id)

    def next_seq12(self, owner_id):
        self.seq12[owner_id] = self.seq12.get(owner_id, 0) + 1
        return self.seq12[owner_id]


def batch(db):
    """ส่งคำสั่งเขียนหลายคำสั่งรวดเดียว (Postgres pipeline) — SQLite ไม่ต้องทำอะไร"""
    return db.pipeline() if isinstance(db, PgConnection) else contextlib.nullcontext()


def upsert_case(db, owner_id, case_id, fields, *, in_tpo=None, force_owner=False,
                allow_move=True, cache=None):
    """เพิ่ม/อัปเดตคดีของเจ้าของ โดยไม่ลบข้อมูลที่กรอกเองไว้แล้ว

    - fields ที่เป็น None/ว่าง จะไม่เขียนทับค่าเดิม
    - Case ID หนึ่งมีเจ้าของได้คนเดียว ถ้าเคยอยู่กับเจ้าของอื่นจะย้ายมา
      ยกเว้น allow_move=False (ผู้ใช้ทั่วไป) หรือถูกล็อกเจ้าของไว้และไม่ได้ force_owner
    - cache (CaseCache) ใช้ตอนนำเข้าจำนวนมาก
    คืนค่า 'created' | 'updated' | 'locked'
    """
    if cache is not None:
        row = cache.get(case_id)
    else:
        row = db.execute("SELECT owner_id, owner_locked, statement_taken FROM cases "
                         "WHERE case_id = ?", (case_id,)).fetchone()
    if row is not None and row["owner_id"] != owner_id:
        if not allow_move or (row["owner_locked"] and not force_owner):
            return "locked"
        locked = 1 if force_owner else 0
        db.execute("UPDATE cases SET owner_id = ?, owner_locked = ? WHERE case_id = ?",
                   (owner_id, locked, case_id))
        if cache is not None:
            row.update(owner_id=owner_id, owner_locked=locked)
    clean = {k: v for k, v in fields.items() if v not in (None, "")}
    if clean.get("statement_taken") and not (row is not None and row["statement_taken"]):
        clean["seq12"] = (cache.next_seq12(owner_id) if cache is not None
                          else next_seq12(db, owner_id))
    if in_tpo is not None:
        clean["in_tpo"] = 1 if in_tpo else 0
    if row is None:
        clean.update(owner_id=owner_id, case_id=case_id)
        cols = ", ".join(clean)
        db.execute(f"INSERT INTO cases ({cols}) VALUES ({', '.join('?' * len(clean))})",
                   list(clean.values()))
        if cache is not None:
            cache.rows[case_id] = {"owner_id": owner_id, "owner_locked": 0,
                                   "statement_taken": clean.get("statement_taken", 0)}
        return "created"
    if clean:
        sets = ", ".join(f"{k} = ?" for k in clean)
        db.execute(f"UPDATE cases SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE case_id = ?",
                   [*clean.values(), case_id])
        if cache is not None and clean.get("statement_taken"):
            row["statement_taken"] = 1
    return "updated"


def next_seq12(db, owner_id):
    return (db.execute("SELECT MAX(seq12) FROM cases WHERE owner_id = ?",
                       (owner_id,)).fetchone()[0] or 0) + 1


def placeholder_case_id(criminal_no):
    """คดีในระบบ C. ที่ไม่มี Case ID ใช้รหัสภายในนี้แทน (ไม่แสดงในรายงาน)"""
    return f"#C.{criminal_no}"
