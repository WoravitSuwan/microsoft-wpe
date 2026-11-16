"""ฐานข้อมูล SQLite: ผู้ใช้ เจ้าของข้อมูล (พนักงานสอบสวน) และคดี"""
import os
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
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'officer',   -- admin | officer
    owner_id INTEGER REFERENCES owners(id) ON DELETE SET NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
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

    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (case_id)
);
CREATE INDEX IF NOT EXISTS idx_cases_owner ON cases(owner_id);
CREATE INDEX IF NOT EXISTS idx_cases_caseid ON cases(case_id);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    filename TEXT, kind TEXT, user_id INTEGER, summary TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
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


def get_db():
    if "db" not in g:
        path = current_app.config["DATABASE"]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        g.db = sqlite3.connect(path)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
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


def upsert_case(db, owner_id, case_id, fields, *, in_tpo=None, force_owner=False,
                allow_move=True):
    """เพิ่ม/อัปเดตคดีของเจ้าของ โดยไม่ลบข้อมูลที่กรอกเองไว้แล้ว

    - fields ที่เป็น None/ว่าง จะไม่เขียนทับค่าเดิม
    - Case ID หนึ่งมีเจ้าของได้คนเดียว ถ้าเคยอยู่กับเจ้าของอื่นจะย้ายมา
      ยกเว้น allow_move=False (ผู้ใช้ทั่วไป) หรือถูกล็อกเจ้าของไว้และไม่ได้ force_owner
    คืนค่า 'created' | 'updated' | 'locked'
    """
    row = db.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is not None and row["owner_id"] != owner_id:
        if not allow_move or (row["owner_locked"] and not force_owner):
            return "locked"
        db.execute("UPDATE cases SET owner_id = ?, owner_locked = ? WHERE id = ?",
                   (owner_id, 1 if force_owner else 0, row["id"]))
    clean = {k: v for k, v in fields.items() if v not in (None, "")}
    if clean.get("statement_taken") and not (row is not None and row["statement_taken"]):
        clean["seq12"] = next_seq12(db, owner_id)
    if in_tpo is not None:
        clean["in_tpo"] = 1 if in_tpo else 0
    if row is None:
        clean.update(owner_id=owner_id, case_id=case_id)
        cols = ", ".join(clean)
        db.execute(f"INSERT INTO cases ({cols}) VALUES ({', '.join('?' * len(clean))})",
                   list(clean.values()))
        return "created"
    if clean:
        sets = ", ".join(f"{k} = ?" for k in clean)
        db.execute(f"UPDATE cases SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                   [*clean.values(), row["id"]])
    return "updated"


def next_seq12(db, owner_id):
    return (db.execute("SELECT MAX(seq12) FROM cases WHERE owner_id = ?",
                       (owner_id,)).fetchone()[0] or 0) + 1


def placeholder_case_id(criminal_no):
    """คดีในระบบ C. ที่ไม่มี Case ID ใช้รหัสภายในนี้แทน (ไม่แสดงในรายงาน)"""
    return f"#C.{criminal_no}"
