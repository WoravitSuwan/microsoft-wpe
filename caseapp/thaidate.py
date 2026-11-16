"""ตัวช่วยแปลงวันที่ ค.ศ./พ.ศ. และรูปแบบวันที่ภาษาไทยที่ใช้ในรายงาน"""
import re
from datetime import date, datetime

MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
          "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


def _year(y: int) -> int:
    """ปีที่มากกว่า 2400 ถือเป็น พ.ศ. แปลงเป็น ค.ศ."""
    return y - 543 if y > 2400 else y


def parse_date(text):
    """แปลงข้อความวันที่หลายรูปแบบเป็น datetime (ค.ศ.) คืน None ถ้าแปลงไม่ได้

    รองรับ: 13/05/2022, 16/04/2569, 11/07/2023 16:11, 30 ต.ค. 2569 14:42,
    2026-11-16, 2026-11-16T14:42 และค่า datetime/date จาก Excel
    """
    if text is None or text == "":
        return None
    if isinstance(text, datetime):
        return text
    if isinstance(text, date):
        return datetime(text.year, text.month, text.day)
    s = str(text).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2}))?", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return datetime(_year(y), mo, d, int(m[4] or 0), int(m[5] or 0))
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        return datetime(_year(y), mo, d, int(m[4] or 0), int(m[5] or 0))
    m = re.match(r"^(\d{1,2})\s*([ก-๙.]+)\s*(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if m:
        mon = m[2].replace(" ", "")
        if not mon.endswith("."):
            mon += "."
        if mon in MONTHS:
            return datetime(_year(int(m[3])), MONTHS.index(mon) + 1, int(m[1]),
                            int(m[4] or 0), int(m[5] or 0))
    return None


def to_iso(text, with_time=False):
    dt = parse_date(text)
    if not dt:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M" if with_time else "%Y-%m-%d")


def thai_short(d, short_year=False) -> str:
    """16 พ.ย. 2569 (short_year=True: 16 พ.ย. 69)"""
    dt = parse_date(d)
    if not dt:
        return ""
    y = dt.year + 543
    return f"{dt.day} {MONTHS[dt.month - 1]} {y % 100 if short_year else y:02d}"


def thai_short_time(d) -> str:
    """30 ต.ค. 2569 14:42 (วันที่ 2 หลัก ชั่วโมงไม่เติม 0 ตามระบบ C.)"""
    dt = parse_date(d)
    if not dt:
        return ""
    return f"{dt.day:02d} {MONTHS[dt.month - 1]} {dt.year + 543} {dt.hour}:{dt.minute:02d}"


def be_slash(d) -> str:
    """16/04/2569"""
    dt = parse_date(d)
    if not dt:
        return ""
    return f"{dt.day:02d}/{dt.month:02d}/{dt.year + 543}"


def ce_slash(d) -> str:
    """13/05/2022 (รูปแบบเดียวกับระบบ thaipoliceonline)"""
    dt = parse_date(d)
    if not dt:
        return ""
    return dt.strftime("%d/%m/%Y")


def minus_months(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))
