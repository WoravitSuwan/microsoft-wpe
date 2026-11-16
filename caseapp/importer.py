"""นำเข้าไฟล์ Excel 2 แบบ

1. ไฟล์ export จาก thaipoliceonline (เช่น ค่าเริ่มต้น__20261116_083829.xlsx)
   แถวหัวตารางมีคอลัมน์ "เลขรับแจ้ง" ... "พนักงานสอบสวน" ... "สถานะ"
2. ไฟล์รายงานรูปแบบเอกสารแนบ (เช่น เอกสารแนบ_รายงานข้อมูลคดีและCaseID_พ.ต.ต.อำพล.xlsx)
   มีชีต 1.1 – 1.5 ใช้เพื่อย้ายข้อมูลรายงานเดิมเข้าระบบ
"""
import re
from collections import Counter

import zipfile

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from .db import CaseCache, get_or_create_owner, placeholder_case_id, upsert_case
from .thaidate import to_iso

INVESTIGATING = "ระหว่างสอบสวน"

TPO_COLUMNS = {
    "เลขรับแจ้ง": "case_id",
    "เลขคดีอาญา": "tpo_criminal_no",
    "วันที่รับแจ้ง": "report_date",
    "ผู้เสียหาย": "victim",
    "เพศ": "gender",
    "อายุ": "age",
    "ประเภทคดี": "case_type",
    "ประเภทคดีระดับ 1": "case_type_l1",
    "ประเภทคดีระดับ 2": "case_type_l2",
    "ประเภทคดีระดับ 3": "case_type_l3",
    "ประเภทกลุ่มคดี": "case_group_type",
    "รายละเอียด": "detail",
    "มูลค่าความเสียหาย": "damage",
    "บช.": "bch",
    "บก./ภ.จว.": "bk",
    "หน่วยที่รับผิดชอบ": "unit",
    "พนักงานสอบสวน": "system_investigator",
    "พนักงานสืบสวน": "detective",
    "สถานะ": "status",
    "เบอร์โทรศัพท์ผู้เสียหาย": "phone",
    "เลขอ้างอิงธนาคาร": "bank_ref",
    "วันที่เริ่มโอน": "transfer_start",
    "วันสุดท้ายที่โอน": "transfer_end",
    "กลุ่มคดี": "crime_group",
}


class ImportError_(Exception):
    pass


def _s(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _h(v):
    """ทำให้หัวตารางเทียบกันได้ (ตัดช่องว่างซ้ำ)"""
    return " ".join(str(v).split()) if v is not None else None


def criminal_no_of(text):
    """'บก.สอท.4 120/2566' -> '120/2566'"""
    m = re.search(r"(\d+\s*/\s*\d{4})", text or "")
    return re.sub(r"\s", "", m[1]) if m else None


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def detect_kind(wb):
    names = wb.sheetnames
    if any(n.startswith("1.1") for n in names) and "สรุป" in names:
        return "report"
    for ws in wb.worksheets:
        for row in ws.iter_rows(max_row=10, values_only=True):
            cells = [_h(c) for c in row]
            if "เลขรับแจ้ง" in cells and "พนักงานสอบสวน" in cells:
                return "tpo"
    return None


def first_row_text(wb):
    """ข้อความแถวแรกที่มีข้อมูล ใช้บอกผู้ใช้ว่าไฟล์มีหัวตารางอะไร"""
    for ws in wb.worksheets:
        for row in ws.iter_rows(max_row=10, values_only=True):
            cells = [_h(c) for c in row if _h(c)]
            if cells:
                return f"ชีต '{ws.title}': " + ", ".join(cells[:8])
    return "ไฟล์ว่าง"


def load(path_or_file):
    """เปิดแบบ read-only (ใช้หน่วยความจำน้อย รองรับไฟล์ใหญ่บน serverless)"""
    try:
        wb = openpyxl.load_workbook(path_or_file, data_only=True, read_only=True)
        for ws in wb.worksheets:
            ws.reset_dimensions()  # บางระบบ export ขนาดชีตผิด: อ่านทุกแถวจริง
        return wb
    except (zipfile.BadZipFile, InvalidFileException, KeyError) as exc:
        raise ImportError_(
            "ไฟล์นี้ไม่ใช่ Excel แบบ .xlsx (อาจเป็น .xls รุ่นเก่า, .csv หรือไฟล์เว็บที่ตั้งชื่อเป็น .xls) "
            "กรุณาเปิดไฟล์ใน Excel แล้ว Save As เป็น \"Excel Workbook (.xlsx)\" แล้วนำเข้าใหม่") from exc
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"เปิดไฟล์ Excel ไม่ได้: {exc}") from exc


# ---------------------------------------------------------------- thaipoliceonline

def read_tpo_rows(wb):
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        for row in rows:
            cells = [_h(c) for c in row]
            if "เลขรับแจ้ง" in cells and "พนักงานสอบสวน" in cells:
                idx = {TPO_COLUMNS[c]: i for i, c in enumerate(cells) if c in TPO_COLUMNS}
                for data in rows:
                    rec = {k: data[i] if i < len(data) else None for k, i in idx.items()}
                    if _s(rec.get("case_id")):
                        yield rec
                return
    raise ImportError_("ไม่พบหัวตาราง 'เลขรับแจ้ง' / 'พนักงานสอบสวน' ในไฟล์")


NO_INVESTIGATOR = "(ไม่ระบุพนักงานสอบสวน)"


def investigator_of(rec):
    return " ".join(str(rec.get("system_investigator") or "").split()) or NO_INVESTIGATOR


def tpo_investigators(wb):
    """รายชื่อพนักงานสอบสวนในไฟล์ thaipoliceonline พร้อมจำนวนคดี (มากไปน้อย)"""
    return Counter(investigator_of(rec) for rec in read_tpo_rows(wb)).most_common()


def import_tpo(db, wb, *, owner_id=None, only=None):
    """owner_id=None: แยกเจ้าของตามคอลัมน์พนักงานสอบสวน, ไม่ใช่ None: เข้าเจ้าของ/ชุดข้อมูลนี้ทั้งหมด

    only: ชุดชื่อพนักงานสอบสวน ถ้ากำหนด จะนำเข้าเฉพาะแถวของคนเหล่านี้ (แถวอื่นข้าม)
    """
    skipped = 0
    stats = Counter()
    per_owner = Counter()
    owners_new = []
    owner_cache = {}
    units = {}
    cache = CaseCache(db, None if owner_id is None else [owner_id])
    for rec in read_tpo_rows(wb):
        if only is not None and investigator_of(rec) not in only:
            skipped += 1
            continue
        fields = {}
        for k, v in rec.items():
            if k == "case_id":
                continue
            if k == "damage":
                fields[k] = _num(v)
            elif k == "report_date":
                fields[k] = to_iso(v)
            else:
                fields[k] = _s(v)
        case_id = _s(rec["case_id"])
        if owner_id is None:
            name = investigator_of(rec)
            if name not in owner_cache:
                owner_cache[name], created = get_or_create_owner(db, name)
                if created:
                    owners_new.append(name)
            oid = owner_cache[name]
            if fields.get("unit"):
                units.setdefault(oid, fields["unit"])
            target = oid
        else:
            target = owner_id
        result = upsert_case(db, target, case_id, fields, in_tpo=True, cache=cache)
        stats[result] += 1
        per_owner[target] += 1
        # เลขคดีอาญาจาก thaipoliceonline ใช้เป็นค่าเริ่มต้นของข้อ 1.3 ถ้ายังไม่มีข้อมูลระบบ C.
        no = criminal_no_of(fields.get("tpo_criminal_no"))
        if no:
            db.execute("""UPDATE cases SET criminal_no = ? WHERE owner_id = ? AND case_id = ?
                          AND (criminal_no IS NULL OR criminal_no = '')
                          AND NOT EXISTS (SELECT 1 FROM cases x WHERE x.owner_id = cases.owner_id
                                          AND x.criminal_no = ?)""", (no, target, case_id, no))
    # ตั้งตำแหน่งเริ่มต้นจากหน่วยที่รับผิดชอบ (ถ้ายังว่าง)
    for oid, unit in units.items():
        pos = re.sub(r"\s*บช\.สอท\.?$", "", unit).strip()
        db.execute("UPDATE owners SET position = ? WHERE id = ? AND (position IS NULL OR position = '')",
                   (pos, oid))
    return {"rows": sum(stats.values()), "skipped": skipped, "created": stats["created"],
            "updated": stats["updated"], "locked": 0,
            "owners_new": owners_new, "per_owner": dict(per_owner)}


# ---------------------------------------------------------------- report format

def _sheet(wb, prefix):
    for n in wb.sheetnames:
        if n.startswith(prefix):
            return wb[n]
    return None


def _rows(ws, ncols):
    if ws is None:
        return []
    out = []
    for row in ws.iter_rows(min_row=5, values_only=True):
        row = list(row[:ncols]) + [None] * (ncols - len(row))
        if any(_s(v) for v in row[1:]):
            out.append(row)
    return out


def import_report(db, wb, *, owner_id=None):
    """นำเข้าไฟล์รายงานเดิม (รูปแบบเอกสารแนบ) ให้เป็นข้อมูลของเจ้าของคนเดียว"""
    s11 = _sheet(wb, "1.1")
    rows11 = _rows(s11, 9)
    header = _s(s11["A2"].value) if s11 is not None else ""
    position = ""
    as_of = None
    names = Counter(_s(r[7]) for r in rows11 if _s(r[7]))
    name = names.most_common(1)[0][0] if names else ""
    if name and header and name in header:
        position = header.split(name, 1)[1].strip(" ·")
    if owner_id is None:
        if not name:
            raise ImportError_("ไม่พบชื่อพนักงานสอบสวนในชีต 1.1 กรุณาเลือกเจ้าของข้อมูล")
        owner_id, _ = get_or_create_owner(db, name, position)
    elif name:  # ชุดข้อมูลส่วนตัวที่ยังไม่มีชื่อ: ใช้ชื่อจากรายงาน
        db.execute("UPDATE owners SET name = ? WHERE id = ? AND name = ''", (name, owner_id))
    m = re.search(r"ข้อมูล ณ วันที่\s*([0-9]+\s*\S+\s*[0-9]{4})", header or "")
    if m:
        as_of = to_iso(m[1])
    stats = Counter()

    cache = CaseCache(db, [owner_id])

    def put(case_id, fields, in_tpo=None, criminal_no=None):
        """คืน Case ID ที่ใช้จริง (คดีที่ไม่มี Case ID ใช้รหัสภายในจากเลขคดีอาญา)"""
        case_id = _s(case_id)
        if not case_id:
            if not criminal_no:
                return None
            case_id = placeholder_case_id(criminal_no)
            fields = {**fields, "no_case_id": 1}
        stats[upsert_case(db, owner_id, case_id, fields, in_tpo=in_tpo, cache=cache)] += 1
        return case_id

    for r in rows11:
        put(r[1], {"report_date": to_iso(r[2]), "victim": _s(r[3]), "case_type": _s(r[4]),
                   "damage": _num(r[5]), "tpo_criminal_no": _s(r[6]),
                   "system_investigator": _s(r[7]), "status": _s(r[8])}, in_tpo=True)
    for r in _rows(_sheet(wb, "1.2"), 5):
        exists = cache.get(owner_id, _s(r[1])) is not None
        # ชื่อ/สถานะใช้จากข้อ 1.1 ถ้ามีอยู่แล้ว
        put(r[1], {"statement_taken": 1} if exists else
            {"statement_taken": 1, "victim": _s(r[2]), "status": _s(r[3])})
    for r in _rows(_sheet(wb, "1.3"), 7):
        no = _s(r[1])
        reported = _s(r[6]) or ""
        actual = None
        if reported.startswith("ต้องปรับปรุง"):
            actual = INVESTIGATING
        elif reported == "จำหน่ายแล้ว":
            actual = "จำหน่าย"
        elif reported:
            actual = reported
        put(r[2], {"criminal_no": no, "complainant": _s(r[3]),
                   "complaint_date": to_iso(r[4], with_time=True), "c_status": _s(r[5]),
                   "actual_status": actual}, criminal_no=no)
    for r in _rows(_sheet(wb, "1.4"), 7):
        no = _s(r[1])
        put(r[2], {"criminal_no": no, "complainant": _s(r[3]), "c_status": _s(r[4]),
                   "actual_status": _s(r[5]), "c_action": _s(r[6])}, criminal_no=no)
    for seq, r in enumerate(_rows(_sheet(wb, "1.5"), 12), start=1):
        no = _s(r[2])
        cid = put(r[3], {"seq15": seq, "criminal_no": no, "court": _s(r[6]), "backlog_reason": _s(r[8]),
                         "backlog_detail": _s(r[9]), "backlog_plan": _s(r[10]),
                         "backlog_evidence": _s(r[11])}, criminal_no=no)
        if not cid:
            continue
        cur = db.execute("SELECT * FROM cases WHERE owner_id = ? AND case_id = ?",
                         (owner_id, cid)).fetchone()
        if cur is None:
            continue
        # ข้อ 1.5 อาจใช้ชื่อ/ความเสียหาย/วันที่ต่างจากข้อมูลหลัก เก็บแยกไว้เฉพาะที่ต่าง
        extra = {}
        victim, damage, when = _s(r[4]), _num(r[5]), to_iso(r[7])
        if victim and victim != (cur["victim"] or cur["complainant"]):
            extra["backlog_victim"] = victim
        if damage is not None and damage != cur["damage"]:
            extra["backlog_damage"] = damage
        if when and when != (cur["complaint_date"] or "")[:10]:
            extra["backlog_date"] = when
        if extra:
            db.execute(f"UPDATE cases SET {', '.join(k + ' = ?' for k in extra)} WHERE id = ?",
                       [*extra.values(), cur["id"]])

    # หมายเหตุ: ข้อที่ระบบสร้างได้เองแทนด้วย {1.1} {1.2} {1.3} {1.5} เพื่อคงลำดับเดิม
    notes = []
    summ = wb["สรุป"] if "สรุป" in wb.sheetnames else None
    if summ is not None:
        started = False
        for (v,) in summ.iter_rows(min_col=1, max_col=1, values_only=True):
            v = _s(v)
            if v == "หมายเหตุ":
                started = True
                continue
            if started and v:
                text = re.sub(r"^\d+\.\s*", "", v)
                m = re.match(r"^ข้อ (1\.[1235])\s", text)
                notes.append("{" + m[1] + "}" if m else text)
    db.execute("UPDATE owners SET notes = ?, as_of = COALESCE(?, as_of), "
               "position = CASE WHEN position = '' THEN ? ELSE position END WHERE id = ?",
               ("\n".join(notes), as_of, position, owner_id))
    return {"owner_id": owner_id, "rows": sum(stats.values()), "created": stats["created"],
            "updated": stats["updated"], "locked": 0}
