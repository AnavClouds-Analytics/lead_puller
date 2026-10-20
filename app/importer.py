"""One-time import from the old Lead Puller (v1.x) database, so nobody you already paid for is paid for again.

On the first start, if DATA_DIR still holds the old leadpuller.db, everything that matters is copied in:
everyone researched (as memory), every email downloaded, the do-not-pull list and the Seamless accounts.
The old file is only read, never changed. Runs once (IMPORT_OLD_DATABASE=0 turns it off).
"""
import os
import sqlite3
import time

from . import db, memory
from .config import cfg


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(con, t):
    return {r[1] for r in con.execute(f"PRAGMA table_info({t})")}


def import_v1(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    have = _tables(con)
    out = {"people": 0, "emails": 0, "do_not_pull": 0, "accounts": 0}
    now = time.time()
    if "contacts" in have:
        cols = _cols(con, "contacts")
        paid = "submitted_at IS NOT NULL" if "submitted_at" in cols else "1=1"
        for r in con.execute(f"SELECT name, company, li_url, email FROM contacts WHERE {paid}"):
            keys = db.person_keys(r["name"], r["company"], r["li_url"], r["email"])
            if keys:
                memory.remember(keys, "old", None, None, "email" if r["email"] else "no_email")
                out["people"] += 1
    if "seen" in have:
        # People whose rows were deleted with their pull: translate the old keys.
        outcome = "outcome" if "outcome" in _cols(con, "seen") else "NULL"
        for r in con.execute(f"SELECT key, {outcome} AS outcome FROM seen"):
            k = r["key"] or ""
            new = None
            if k.startswith("li:"):
                slug = db.norm_li("https://" + k[3:].lstrip("/"))
                new = "li:" + slug if slug else None
            elif k.startswith("em:"):
                e = db.norm_email(k[3:])
                new = "em:" + e if e else None
            elif k.startswith("nc:") and "|" in k:
                name, company = k[3:].split("|", 1)
                new = next((x for x in db.person_keys(name, company) if x.startswith("nc:")), None)
            if new:
                memory.remember([new], "old", None, None, "email" if r["outcome"] == "email" else "no_email")
    if "downloaded" in have:
        for r in con.execute("SELECT email FROM downloaded"):
            e = db.norm_email(r["email"])
            if e:
                memory.remember(["em:" + e], "old", None, None, "email")
                out["emails"] += 1
    if "exclusions" in have:
        kinds = {"email": "email", "domain": "domain", "linkedin": "linkedin", "company_name": "company"}
        for r in con.execute("SELECT key, kind, value FROM exclusions"):
            old_key = r["key"] or ""
            if r["kind"] in kinds:
                out["do_not_pull"] += memory.add_exclusion(kinds[r["kind"]], r["value"], "old Lead Puller")
            elif r["kind"] == "name_company" and old_key.startswith("nc:") and "|" in old_key:
                name, company = old_key[3:].split("|", 1)
                key = next((x for x in db.person_keys(name, company) if x.startswith("nc:")), None)
                if key and not db.q1("SELECT 1 x FROM exclusions WHERE key=?", (key,)):
                    db.x("INSERT INTO exclusions(key,kind,value,source,created_at) VALUES(?,?,?,?,?)",
                         (key, "person", f"{name} @ {company}", "old Lead Puller", now))
                    out["do_not_pull"] += 1
    if "accounts" in have and {"name", "api_key"} <= _cols(con, "accounts"):
        cols = _cols(con, "accounts")
        for r in con.execute("SELECT * FROM accounts ORDER BY " + ("priority" if "priority" in cols else "id")):
            if not r["api_key"] or db.q1("SELECT 1 x FROM accounts WHERE api_key=?", (r["api_key"],)):
                continue
            n = db.q1("SELECT COALESCE(MAX(priority),0) m FROM accounts")["m"]
            db.x("INSERT INTO accounts(name,api_key,priority,enabled,monthly_budget,source,created_at) "
                 "VALUES(?,?,?,?,?,?,?)", (r["name"], r["api_key"], n + 1,
                                           r["enabled"] if "enabled" in cols else 1,
                                           (r["monthly_budget"] or 0) if "monthly_budget" in cols else 0,
                                           "env" if "source" in cols and r["source"] == "env" else "ui", now))
            out["accounts"] += 1
    con.close()
    return out


def auto_import():
    """On the first start of 2.0: import the old database if it's next to the new one."""
    if not cfg.IMPORT_OLD_DATABASE or db.get_meta("old_import"):
        return None
    old = os.path.join(os.path.dirname(os.path.abspath(db.db_path())), "leadpuller.db")
    if not os.path.exists(old) or os.path.abspath(old) == os.path.abspath(db.db_path()):
        db.set_meta("old_import", {"at": time.time(), "found": False})
        return None
    try:
        out = import_v1(old)
    except sqlite3.Error as e:
        out = {"error": str(e)}
    db.set_meta("old_import", {"at": time.time(), "found": True, **out})
    return out
