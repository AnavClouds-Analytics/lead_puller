"""Downloads: the AnavMail CSV, everything Seamless returned (CSV or Excel), and people without email."""
import csv
import io

from . import db, memory
from .config import cfg

COLUMNS = [
    ("email", "Email", lambda c: c["email"]),
    ("first_name", "First name", lambda c: c["first_name"]),
    ("last_name", "Last name", lambda c: c["last_name"]),
    ("full_name", "Full name", lambda c: c["name"]),
    ("title", "Job title", lambda c: c["title"]),
    ("company", "Company", lambda c: c["company"]),
    ("domain", "Company website", lambda c: c["domain"]),
    ("linkedin", "LinkedIn", lambda c: c["li_url"]),
    ("phone", "Phone", lambda c: c["phone"]),
    ("phone_type", "Phone type", lambda c: c["phone_type"]),
    ("company_phone", "Company phone", lambda c: c["company_phone"]),
    ("city", "City", lambda c: c["city"]),
    ("state", "State / region", lambda c: c["region"]),
    ("country", "Country", lambda c: c["country"]),
    ("industry", "Industry", lambda c: c["industry"]),
    ("company_size", "Company size", lambda c: c["size"]),
    ("seniority", "Seniority", lambda c: c["seniority"]),
    ("department", "Department", lambda c: c["department"]),
    ("email_status", "Email status (Seamless)", lambda c: c["email_status"]),
    ("email_confidence", "Email confidence (Seamless)", lambda c: c["email_confidence"]),
    ("email_2", "Other email 1", lambda c: c["email_2"]),
    ("email_3", "Other email 2", lambda c: c["email_3"]),
    ("technologies", "Technologies", lambda c: c["technologies"]),
    ("result", "Seamless result", lambda c: c["research_status"]),
    ("note", "Note", lambda c: c["research_message"]),
]
COL = {k: (label, get) for k, label, get in COLUMNS}
NO_EMAIL_COLUMNS = ["full_name", "first_name", "last_name", "title", "company", "domain", "linkedin", "phone",
                    "phone_type", "company_phone", "city", "state", "country"]


def default_columns():
    cols = [c.strip() for c in cfg.DEFAULT_COLUMNS.split(",") if c.strip() in COL]
    return cols or ["email", "first_name", "last_name", "company"]


def parse_columns(text, default, need_email=False):
    cols = [c.strip() for c in (text or "").split(",") if c.strip() in COL] or list(default)
    if need_email and "email" not in cols:
        cols.insert(0, "email")
    return cols


def cell(v):
    """Text a spreadsheet would run as a formula (= + - @ tab) gets a leading apostrophe."""
    if v is None:
        return ""
    v = str(v)
    if v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def anavmail_rows(pid):
    """Everyone in this pull with a usable email: one row per email, invalid emails and do-not-pull left out."""
    rows = db.q("""SELECT * FROM contacts WHERE pull_id=? AND state='done' AND email IS NOT NULL AND email!=''
                   AND LOWER(COALESCE(email_status,''))!='invalid' AND dup_email=0 ORDER BY id""", (pid,))
    block = memory.Blocklist()
    out, seen = [], set()
    for c in rows:
        e = c["email"].strip().lower()
        if e in seen or block.reason(c["name"], c["company"], c["domain"], c["li_url"], c["email"]):
            continue
        seen.add(e)
        out.append(c)
    return out


def all_rows(pid):
    return db.q("SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL ORDER BY id", (pid,))


def no_email_rows(pid):
    return db.q("""SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL AND (email IS NULL OR email='')
                   AND state NOT IN ('queued') ORDER BY id""", (pid,))


def to_csv(rows, cols):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for c in rows:
        w.writerow([cell(COL[k][1](c)) for k in cols])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_xlsx(rows, cols):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "People"
    ws.append([COL[k][0] for k in cols])
    for c in rows:
        ws.append([cell(COL[k][1](c)) for k in cols])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def safe_name(name, fallback):
    import re
    s = re.sub(r"[^A-Za-z0-9]+", "-", name or "").strip("-")[:60]
    return s or fallback
