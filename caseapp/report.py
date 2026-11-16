"""คำนวณข้อมูลรายงาน 1.1 – 1.5 ของเจ้าของแต่ละคน และสร้างไฟล์ Excel ตามแบบเอกสารแนบ"""
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .db import name_key
from .thaidate import (be_slash, ce_slash, minus_months, parse_date, thai_short,
                       thai_short_time)

DISPOSED = "จำหน่าย"
INVESTIGATING = "ระหว่างสอบสวน"
DEFAULT_ACTION = "ปรับสถานะในระบบ C. ให้ตรงกับสถานะจริงของสำนวน"
NEEDS_UPDATE = "ต้องปรับปรุงข้อมูล (ข้อ 1.4)"
GROUP_A = "(ก) ระหว่างสอบสวน"
GROUP_B = "(ข) ต้องปรับปรุงข้อมูลในระบบ C."
REASONS = ["อยู่ระหว่างรอข้อมูลธนาคาร", "รอผลหมายเรียกผู้ต้องหา", "อยู่ระหว่างสรุปสำนวน"]
C_STATUSES = ["ระหว่างสอบสวน", "จำหน่าย", "ส่งพนักงานอัยการ", "งดการสอบสวน", "โอนสำนวน"]


def _criminal_key(no):
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)", no or "")
    return (int(m[2]), int(m[1])) if m else (0, 0)


def _date_key(v):
    d = parse_date(v)
    return d.isoformat() if d else ""


def needs_update(c):
    return bool(c["c_status"] and c["actual_status"] and c["c_status"] != c["actual_status"])


def reported_status(c):
    if needs_update(c):
        return NEEDS_UPDATE
    st = c["actual_status"] or c["c_status"] or ""
    return "จำหน่ายแล้ว" if st == DISPOSED else st


def case_id(c):
    """Case ID ที่แสดงในรายงาน (คดีที่ยังไม่มี Case ID แสดงเป็นช่องว่าง)"""
    return "" if c["no_case_id"] else c["case_id"]


def real_status(c):
    return c["actual_status"] or c["c_status"] or ""


@dataclass
class Report:
    owner: dict
    as_of: date
    s11: list = field(default_factory=list)
    s12: list = field(default_factory=list)
    s13: list = field(default_factory=list)
    s14: list = field(default_factory=list)
    s15: list = field(default_factory=list)
    other_investigators: Counter = field(default_factory=Counter)

    @property
    def header_line(self):
        who = " ".join(x for x in (self.owner["name"], self.owner["position"] or "") if x)
        return f"ข้อมูล ณ วันที่ {thai_short(self.as_of)} · {who}"

    @property
    def summary_who(self):
        who = " ".join(x for x in (self.owner["name"], self.owner["position"] or "") if x)
        return f"{who} · ข้อมูล ณ วันที่ {thai_short(self.as_of)}"

    def counts(self):
        rep = Counter(reported_status(c) for c in self.s13)
        grp = Counter(r["group"] for r in self.s15)
        rea = Counter(r["case"]["backlog_reason"] or "" for r in self.s15)
        return {
            "s11": len(self.s11), "s12": len(self.s12), "s13": len(self.s13),
            "s13_disposed": rep["จำหน่ายแล้ว"], "s13_investigating": rep[INVESTIGATING],
            "s14": len(self.s14), "s15": len(self.s15),
            "s15_a": grp[GROUP_A], "s15_b": grp[GROUP_B],
            "reasons": [(r, rea[r]) for r in REASONS]
                       + [(r, n) for r, n in rea.items() if r and r not in REASONS],
            "reasons_missing": rea[""],
        }

    def notes(self):
        auto = {}
        tpo = thai_short(self.owner["tpo_date"] or self.as_of, short_year=True)
        n1 = f"ข้อ 1.1 จากไฟล์ export สถานะ Case ID ระบบ thaipoliceonline ({tpo})"
        if self.other_investigators:
            parts = ", ".join(f"{n} รายการที่ระบบยังระบุ {name}"
                              for name, n in self.other_investigators.most_common())
            n1 += f" ในจำนวนนี้มี {parts} เป็นพนักงานสอบสวน"
        auto["1.1"] = n1
        not_in = sum(1 for c in self.s12 if not c["in_tpo"])
        n2 = 'ข้อ 1.2 จาก CCMS หน้า "ส่งหมายเรียก[ผู้เสียหาย]" ช่อง "มีสำนวนแล้ว"'
        if not_in:
            n2 += f" มี {not_in} รายการที่ไม่อยู่ในรายการข้อ 1.1"
        auto["1.2"] = n2
        disposed_c = sum(1 for c in self.s13 if c["c_status"] == DISPOSED)
        n3 = "ข้อ 1.3 จากไฟล์ export ระบบ C. (CRIMES)"
        if disposed_c:
            n3 += f" ระบบ C. แสดงจำหน่าย {disposed_c} คดี"
            upd = sum(1 for c in self.s14 if c["c_status"] == DISPOSED)
            if upd:
                n3 += f" ในจำนวนนี้ {upd} คดีนับเป็นต้องปรับปรุงข้อมูล (ข้อ 1.4)"
        auto["1.3"] = n3
        auto["1.5"] = (f"ข้อ 1.5 นับอายุคดีจากวันที่รับคำร้องทุกข์ถึงวันที่ "
                       f"{thai_short(self.as_of, short_year=True)} "
                       "รายละเอียดการขอข้อมูลธนาคารและผู้ต้องหาจาก CCMS")
        # หมายเหตุเพิ่มเติม: ใส่ {1.1} {1.2} {1.3} {1.5} เพื่อกำหนดตำแหน่งข้อที่สร้างอัตโนมัติ
        custom = [x.strip() for x in (self.owner["notes"] or "").splitlines() if x.strip()]
        out, used = [], set()
        for line in custom:
            key = line.strip("{} ")
            if line.startswith("{") and key in auto:
                out.append(auto[key])
                used.add(key)
            else:
                out.append(line)
        missing = [k for k in ("1.1", "1.2", "1.3") if k not in used]
        out = [auto[k] for k in missing] + out
        if "1.5" not in used:
            out.append(auto["1.5"])
        if any(self._missing_15(r) for r in self.s15) and not any("ช่องสีเหลือง" in x for x in out):
            out.append("ช่องสีเหลือง = ช่องที่ยังต้องกรอกข้อมูลเพิ่มเติม ในชีต 1.5")
        return out

    @staticmethod
    def _missing_15(r):
        c = r["case"]
        return [k for k in ("court", "backlog_reason", "backlog_detail", "backlog_plan",
                            "backlog_evidence") if not c[k]]


def build(db, owner_id, as_of=None):
    owner = dict(db.execute("SELECT * FROM owners WHERE id = ?", (owner_id,)).fetchone())
    if as_of is None:
        as_of = parse_date(owner["as_of"]).date() if owner["as_of"] else date.today()
    cases = [dict(r) for r in db.execute("SELECT * FROM cases WHERE owner_id = ?", (owner_id,))]
    rep = Report(owner=owner, as_of=as_of)

    rep.s11 = sorted((c for c in cases if c["in_tpo"]),
                     key=lambda c: (_date_key(c["report_date"]), c["id"]))
    me = name_key(owner["name"])
    rep.other_investigators = Counter(
        c["system_investigator"] for c in rep.s11
        if c["system_investigator"] and name_key(c["system_investigator"]) != me)
    rep.s12 = sorted((c for c in cases if c["statement_taken"]),
                     key=lambda c: (c["seq12"] is None, c["seq12"] or 0, c["id"]))
    rep.s13 = sorted((c for c in cases if c["criminal_no"]),
                     key=lambda c: _criminal_key(c["criminal_no"]), reverse=True)
    rep.s14 = [c for c in rep.s13 if needs_update(c)]

    limit = minus_months(as_of, 6)
    s15 = []
    for c in rep.s13:
        d = parse_date(c["backlog_date"] or c["complaint_date"])
        if real_status(c) == INVESTIGATING and d and d.date() <= limit:
            s15.append({"case": c, "group": GROUP_B if needs_update(c) else GROUP_A,
                        "date": d, "age_days": (as_of - d.date()).days,
                        "victim": c["backlog_victim"] or c["victim"] or c["complainant"],
                        "damage": c["backlog_damage"] if c["backlog_damage"] is not None
                        else c["damage"]})
    # กลุ่ม (ก) ก่อน (ข) · ในกลุ่มคงลำดับจากรายงานเดิม (seq15) ที่เหลือเรียงวันที่ใหม่ไปเก่า
    s15.sort(key=lambda r: (r["date"].date(), _criminal_key(r["case"]["criminal_no"])),
             reverse=True)
    s15.sort(key=lambda r: (r["group"] != GROUP_A, r["case"]["seq15"] is None,
                            r["case"]["seq15"] or 0))
    rep.s15 = s15
    return rep


# ------------------------------------------------------------------ Excel export

FONT = "Tahoma"
HEAD_FILL = PatternFill("solid", fgColor="FF1F3864")
YELLOW = PatternFill("solid", fgColor="FFFFF2CC")
THIN = Side(style="thin", color="FFBFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
MONEY = r"#,##0.00;\(#,##0.00\);\-"


def _title(ws, title, sub):
    ws["A1"] = title
    ws["A1"].font = Font(name=FONT, bold=True, size=12)
    ws["A2"] = sub
    ws["A2"].font = Font(name=FONT, size=9, color="FF595959")
    ws.row_dimensions[1].height = 15.5


def _table(ws, headers, widths, rows, money_cols=(), yellow=None):
    for j, (h, w) in enumerate(zip(headers, widths), start=1):
        c = ws.cell(row=4, column=j, value=h)
        c.font = Font(name=FONT, bold=True, size=10, color="FFFFFFFF")
        c.fill = HEAD_FILL
        c.border = BORDER
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        if w:
            ws.column_dimensions[c.column_letter].width = w
    ws.row_dimensions[4].height = 25
    for i, row in enumerate(rows, start=5):
        for j, v in enumerate(row, start=1):
            c = ws.cell(row=i, column=j, value=v if v != "" else None)
            c.font = Font(name=FONT, size=10)
            c.border = BORDER
            c.alignment = Alignment(vertical="top", wrap_text=True)
            if j in money_cols:
                c.number_format = MONEY
            if yellow and (i - 5, j) in yellow:
                c.fill = YELLOW
    last = 4 + max(len(rows), 1)
    ws.auto_filter.ref = f"A4:{ws.cell(row=4, column=len(headers)).column_letter}{last}"
    ws.freeze_panes = "A5"
    ws.sheet_view.zoomScale = 100
    return last


def _num(v):
    if v is None:
        return None
    return int(v) if float(v).is_integer() else float(v)


def to_xlsx(rep: Report) -> bytes:
    wb = Workbook()
    summ = wb.active
    summ.title = "สรุป"
    sub = rep.header_line

    # 1.1
    ws = wb.create_sheet("1.1 Case ID ทั้งหมด")
    _title(ws, "1.1 Case ID ทั้งหมดในความรับผิดชอบ", sub)
    end11 = _table(ws, ["ลำดับ", "Case ID", "วันที่รับแจ้ง", "ผู้เสียหาย", "ประเภทคดี",
                        "มูลค่าความเสียหาย (บาท)", "เลขคดีอาญา", "พนักงานสอบสวน", "สถานะ"],
                   [7, 15, 12, 26, 32, 16, 20, 24, 18],
                   [[i, c["case_id"], ce_slash(c["report_date"]), c["victim"], c["case_type"],
                     _num(c["damage"]), c["tpo_criminal_no"], c["system_investigator"], c["status"]]
                    for i, c in enumerate(rep.s11, 1)], money_cols=(6,))
    # 1.2
    ws = wb.create_sheet("1.2 สอบปากคำแล้ว")
    _title(ws, "1.2 Case ID ที่ดำเนินการสอบปากคำแล้ว (ตาม CCMS: มีสำนวนแล้ว)", sub)
    end12 = _table(ws, ["ลำดับ", "Case ID", "ผู้เสียหาย", "สถานะ (thaipoliceonline)", "หมายเหตุ"],
                   [7, 15, 28, 22, 20],
                   [[i, c["case_id"], c["victim"], c["status"] if c["in_tpo"] else None,
                     "อยู่ในรายการ 1.1" if c["in_tpo"] else "ไม่อยู่ในรายการ 1.1"]
                    for i, c in enumerate(rep.s12, 1)])
    # 1.3
    ws = wb.create_sheet("1.3 คดีอาญา (C.)")
    _title(ws, "1.3 Case ID ที่รับเลขคดีอาญา (C.) แล้ว", sub)
    end13 = _table(ws, ["ลำดับ", "เลขคดีอาญา", "Case ID", "ผู้ร้องทุกข์", "วันที่รับคำร้องทุกข์",
                        "สถานะในระบบ C.", "สถานะที่รายงาน"],
                   [7, 13, 15, 28, 20, 16, 24],
                   [[i, c["criminal_no"], case_id(c), c["complainant"] or c["victim"],
                     thai_short_time(c["complaint_date"]), c["c_status"], reported_status(c)]
                    for i, c in enumerate(rep.s13, 1)])
    # 1.4
    ws = wb.create_sheet("1.4 ปรับปรุงข้อมูล C.")
    _title(ws, "1.4 คดีอาญาที่ต้องปรับปรุงข้อมูลในระบบ C.", sub)
    end14 = _table(ws, ["ลำดับ", "เลขคดีอาญา", "Case ID", "ผู้ร้องทุกข์", "สถานะในระบบ C.",
                        "สถานะจริง", "การดำเนินการ"],
                   [7, 13, 15, 28, 16, 14, 40],
                   [[i, c["criminal_no"], case_id(c), c["complainant"] or c["victim"],
                     c["c_status"], c["actual_status"], c["c_action"] or DEFAULT_ACTION]
                    for i, c in enumerate(rep.s14, 1)])
    # 1.5
    ws = wb.create_sheet("1.5 ค้างเกิน 6 เดือน")
    _title(ws, "1.5 สำนวนการสอบสวนที่ค้างเกิน 6 เดือน", sub)
    yellow = set()
    rows15 = []
    for i, r in enumerate(rep.s15):
        c = r["case"]
        row = [i + 1, r["group"], c["criminal_no"], case_id(c), r["victim"],
               _num(r["damage"]), c["court"], be_slash(r["date"]), c["backlog_reason"],
               c["backlog_detail"], c["backlog_plan"], c["backlog_evidence"]]
        for j in (7, 9, 10, 11, 12):
            if not row[j - 1]:
                yellow.add((i, j))
        rows15.append(row)
    end15 = _table(ws, ["ลำดับ", "กลุ่ม", "เลขคดีอาญา", "Case ID", "ผู้เสียหาย",
                        "ความเสียหาย (บาท)", "ศาล", "วันที่รับคำร้องทุกข์", "เหตุผลที่ค้าง",
                        "รายละเอียด", "แนวทางเร่งรัด", "หลักฐานที่แนบ"],
                   [15.2, 23.3, 12, 15, 26, 15, 20, 14, 22, 50, 40, 34],
                   rows15, money_cols=(6,), yellow=yellow)
    for i in range(len(rows15)):
        ws.row_dimensions[5 + i].height = 37.5

    # สรุป
    summ["A1"] = "สรุปข้อมูลคดีและ Case ID ในความรับผิดชอบ"
    summ["A1"].font = Font(name=FONT, bold=True, size=13)
    summ["A2"] = rep.summary_who
    summ["A2"].font = Font(name=FONT, size=9, color="FF595959")
    summ.row_dimensions[1].height = 16.5
    for col, w in zip("ABCD", (6, 48, 10, 9)):
        summ.column_dimensions[col].width = w
    for j, h in enumerate(["ข้อ", "รายการ", "จำนวน", "หน่วย"], start=1):
        c = summ.cell(row=4, column=j, value=h)
        c.font = Font(name=FONT, bold=True, size=10, color="FFFFFFFF")
        c.fill = HEAD_FILL
        c.border = BORDER
        c.alignment = Alignment(horizontal="center")

    s3, s5 = "'1.3 คดีอาญา (C.)'", "'1.5 ค้างเกิน 6 เดือน'"
    lines = [
        ("1.1", "จำนวน Case ID ทั้งหมดในความรับผิดชอบ",
         f"=COUNTA('1.1 Case ID ทั้งหมด'!B5:B{end11})", "รายการ"),
        ("1.2", "จำนวน Case ID ที่ดำเนินการสอบปากคำแล้ว",
         f"=COUNTA('1.2 สอบปากคำแล้ว'!B5:B{end12})", "รายการ"),
        ("1.3", "จำนวน Case ID ที่รับเลขคดีอาญา (C.) แล้ว", f"=COUNTA({s3}!B5:B{end13})", "คดี"),
        (None, "   - จำนวนคดีที่จำหน่ายแล้ว", f'=COUNTIF({s3}!G5:G{end13},"จำหน่ายแล้ว")', "คดี"),
        (None, "   - จำนวนคดีที่อยู่ระหว่างสอบสวน", f'=COUNTIF({s3}!G5:G{end13},"ระหว่างสอบสวน")', "คดี"),
        ("1.4", "จำนวนคดีอาญาที่ต้องปรับปรุงข้อมูลในระบบ C.",
         f"=COUNTA('1.4 ปรับปรุงข้อมูล C.'!B5:B{end14})", "คดี"),
        ("1.5", "จำนวนสำนวนการสอบสวนที่ค้างเกิน 6 เดือน", f"=COUNTA({s5}!C5:C{end15})", "สำนวน"),
        (None, f"   {GROUP_A}", f'=COUNTIF({s5}!B5:B{end15},"{GROUP_A}")', "สำนวน"),
        (None, f"   {GROUP_B}", f'=COUNTIF({s5}!B5:B{end15},"{GROUP_B}")', "สำนวน"),
    ]
    for reason, _n in rep.counts()["reasons"]:
        lines.append((None, f"   - {reason}", f'=COUNTIF({s5}!I5:I{end15},"{reason}")', "สำนวน"))
    row = 5
    for no, label, formula, unit in lines:
        bold = no is not None
        for j, v in enumerate((no, label, formula, unit), start=1):
            c = summ.cell(row=row, column=j, value=v)
            c.font = Font(name=FONT, size=10, bold=bold)
            c.border = BORDER
            if j == 3:
                c.number_format = "#,##0"
        row += 1
    row += 1
    summ.cell(row=row, column=1, value="หมายเหตุ").font = Font(name=FONT, size=10, bold=True)
    for i, note in enumerate(rep.notes(), start=1):
        row += 1
        summ.cell(row=row, column=1, value=f"{i}. {note}").font = Font(name=FONT, size=9)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_filename(owner_name):
    """เอกสารแนบ_รายงานข้อมูลคดีและCaseID_พ.ต.ต.อำพล.xlsx (ยศ + ชื่อ ไม่รวมนามสกุล)"""
    short = owner_name.rsplit(" ", 1)[0] if " " in owner_name else owner_name
    return f"เอกสารแนบ_รายงานข้อมูลคดีและCaseID_{short}.xlsx"
