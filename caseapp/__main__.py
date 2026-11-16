"""python -m caseapp  → เปิดเว็บที่ http://127.0.0.1:5000"""
import os

from .app import create_app

create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)))
