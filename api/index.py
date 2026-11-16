"""จุดเริ่มต้นสำหรับ Vercel (Python runtime เรียกตัวแปร app แบบ WSGI)"""
import html
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from caseapp.app import ConfigError, create_app  # noqa: E402

try:
    app = create_app()
except ConfigError as exc:  # ยังตั้งค่า Environment Variables ไม่ครบ: แสดงวิธีแก้แทน error 500
    _missing = html.escape(str(exc))

    def app(environ, start_response):
        body = f"""<!doctype html><html lang="th"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ตั้งค่าระบบยังไม่ครบ</title>
<body style="font-family:Tahoma,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.6">
<h1>ตั้งค่าระบบยังไม่ครบ</h1>
<p>ยังไม่ได้กำหนด: <b>{_missing}</b></p>
<ol>
<li>Vercel → Project → <b>Storage</b> → เพิ่มฐานข้อมูล <b>Neon (Postgres)</b> และเชื่อมกับโปรเจกต์นี้
(ระบบจะตั้งค่า <code>DATABASE_URL</code> ให้อัตโนมัติ)</li>
<li>Vercel → Project → Settings → <b>Environment Variables</b> → เพิ่ม <code>SECRET_KEY</code>
เป็นข้อความสุ่มยาว ๆ (อย่างน้อย 32 ตัวอักษร)</li>
<li>Deployments → <b>Redeploy</b></li>
</ol></body></html>""".encode()
        start_response("503 Service Unavailable", [("Content-Type", "text/html; charset=utf-8"),
                                                   ("Content-Length", str(len(body)))])
        return [body]
