"""SQLite storage (one file in DATA_DIR). Every step of a pull is saved, so a restart picks up exactly where it was."""
import contextlib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata

from .config import cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, created_at REAL);
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY, name TEXT, api_key TEXT UNIQUE, priority INTEGER, enabled INTEGER DEFAULT 1,
  monthly_budget INTEGER DEFAULT 0, source TEXT DEFAULT 'ui', balance INTEGER, balance_at REAL, last_error TEXT,
  created_at REAL);
CREATE TABLE IF NOT EXISTS pulls(
  id INTEGER PRIMARY KEY, name TEXT, prompt TEXT, filters_json TEXT, target INTEGER, max_credits INTEGER,
  max_per_company INTEGER, account_choice TEXT DEFAULT 'auto', status TEXT, stop_reason TEXT, error TEXT,
  pause_kind TEXT, created_at REAL, started_at REAL, finished_at REAL, updated_at REAL,
  current_account_id INTEGER, rerun_of INTEGER, stopping INTEGER DEFAULT 0, errors_in_row INTEGER DEFAULT 0,
  balances_json TEXT DEFAULT '{}', last_poll REAL DEFAULT 0, notes_json TEXT DEFAULT '[]', rate_ack INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pull_slices(
  id INTEGER PRIMARY KEY, pull_id INTEGER, idx INTEGER, label TEXT, size_value TEXT, skey TEXT,
  state TEXT DEFAULT 'todo', pages INTEGER DEFAULT 0, rows INTEGER DEFAULT 0, kept INTEGER DEFAULT 0,
  search_credits INTEGER DEFAULT 0, recent_json TEXT DEFAULT '[]', note TEXT, total INTEGER);
CREATE INDEX IF NOT EXISTS ps_pull ON pull_slices(pull_id, idx);
CREATE TABLE IF NOT EXISTS scan_state(
  account_id INTEGER, skey TEXT, body_hash TEXT, next_token TEXT, started_at REAL, exhausted_at REAL,
  pages INTEGER DEFAULT 0, rows INTEGER DEFAULT 0, kept INTEGER DEFAULT 0, updated_at REAL,
  PRIMARY KEY(account_id, skey));
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY, pull_id INTEGER, account_id INTEGER, slice_id INTEGER, skey TEXT, sid TEXT,
  state TEXT, skip_reason TEXT, skip_kind TEXT,
  name TEXT, first_name TEXT, last_name TEXT, title TEXT, seniority TEXT, department TEXT, company TEXT,
  domain TEXT, li_url TEXT, country TEXT, region TEXT, city TEXT, industry TEXT, size TEXT, row_email TEXT,
  request_id TEXT, submitted_at REAL, research_status TEXT, research_message TEXT, resolved_at REAL,
  email TEXT, email_status TEXT, email_confidence TEXT, email_2 TEXT, email_3 TEXT, phone TEXT, phone_type TEXT,
  company_phone TEXT, technologies TEXT, contact_json TEXT, banked_at REAL, filled_free INTEGER DEFAULT 0,
  dup_email INTEGER DEFAULT 0, created_at REAL);
CREATE INDEX IF NOT EXISTS c_pull ON contacts(pull_id, state);
CREATE INDEX IF NOT EXISTS c_sid ON contacts(account_id, sid);
CREATE INDEX IF NOT EXISTS c_bank ON contacts(state, account_id, skey);
CREATE INDEX IF NOT EXISTS c_req ON contacts(request_id);
CREATE TABLE IF NOT EXISTS research_requests(
  request_id TEXT PRIMARY KEY, pull_id INTEGER, account_id INTEGER, contact_id INTEGER, guess_sid TEXT,
  created_at REAL, resolved_at REAL, status TEXT, gave_up INTEGER DEFAULT 0, late_polled_at REAL);
CREATE INDEX IF NOT EXISTS rr_open ON research_requests(pull_id, resolved_at);
CREATE TABLE IF NOT EXISTS memory(key TEXT PRIMARY KEY, source TEXT, pull_id INTEGER, account_id INTEGER,
  outcome TEXT, at REAL);
CREATE TABLE IF NOT EXISTS saved_contacts(
  id INTEGER PRIMARY KEY, account_id INTEGER, li_key TEXT, nc_key TEXT, email TEXT, record_json TEXT, at REAL);
CREATE INDEX IF NOT EXISTS sc_li ON saved_contacts(li_key);
CREATE INDEX IF NOT EXISTS sc_nc ON saved_contacts(nc_key);
CREATE TABLE IF NOT EXISTS saved_sync(account_id INTEGER PRIMARY KEY, synced_at REAL, read INTEGER);
CREATE TABLE IF NOT EXISTS exclusions(key TEXT PRIMARY KEY, kind TEXT, value TEXT, source TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY, at REAL, pull_id INTEGER, account_id INTEGER, kind TEXT, credits INTEGER,
  balance_after INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS l_pull ON ledger(pull_id);
CREATE INDEX IF NOT EXISTS l_acct ON ledger(account_id, at);
CREATE TABLE IF NOT EXISTS techs(name TEXT PRIMARY KEY, seen INTEGER DEFAULT 0, checked_total INTEGER, checked_at REAL);
CREATE TABLE IF NOT EXISTS dns(domain TEXT PRIMARY KEY, result TEXT, at REAL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

_local = threading.local()
_path = None
_write_lock = threading.RLock()


def db_path():
    return _path or os.path.join(cfg.DATA_DIR, "leadpuller2.db")


def connect(path=None):
    global _path
    if path:
        _path = path
    os.makedirs(os.path.dirname(os.path.abspath(db_path())), exist_ok=True)
    con = _conn()
    con.executescript(SCHEMA)
    return con


def _conn():
    c = getattr(_local, "con", None)
    if c is None or getattr(_local, "path", None) != db_path():
        c = sqlite3.connect(db_path(), timeout=30, check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=OFF")
        _local.con, _local.path = c, db_path()
    return c


def reset_for_tests(path):
    global _path
    _path = path
    _local.con = None
    if os.path.exists(path):
        os.remove(path)
    connect(path)


def q(sql, args=()):
    return [dict(r) for r in _conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = _conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def x(sql, args=()):
    with _write_lock:
        cur = _conn().execute(sql, args)
        return cur.lastrowid


@contextlib.contextmanager
def tx():
    """One atomic write transaction (all or nothing)."""
    with _write_lock:
        c = _conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------- normalizing (memory keys)
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


_COMPANY_TAIL = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "gmbh", "plc", "sa", "ag", "bv", "pvt"}


def norm_company(s):
    words = norm(s).split()
    while words and words[-1] in _COMPANY_TAIL:
        words.pop()
    return " ".join(words)


def norm_li(url):
    """LinkedIn profile → its slug ('john-smith-12'), whatever the URL spelling (http, www, ?trk=, trailing /)."""
    u = (url or "").strip().lower()
    m = re.search(r"linkedin\.com/(?:in|pub)/([^/?#\s]+)", u)
    if not m:
        return ""
    return re.sub(r"[^a-z0-9\-_%]", "", m.group(1))


def norm_email(e):
    e = (e or "").strip().lower()
    return e if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) else ""


def norm_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0]
    if d.startswith("www."):
        d = d[4:]
    return d.strip(".")


def person_keys(name, company, li_url=None, email=None):
    """Every key one person is remembered by. A match on ANY key means 'already pulled'."""
    keys = []
    li = norm_li(li_url)
    if li:
        keys.append("li:" + li)
    em = norm_email(email)
    if em:
        keys.append("em:" + em)
    n, c = norm(name), norm_company(company)
    if n and c and len(n.split()) >= 2:
        keys.append(f"nc:{n}|{c}")
    return keys


# ---------------------------------------------------------------- ledger
def month_key(t=None):
    return time.strftime("%Y-%m", time.gmtime(t if t is not None else time.time()))


def month_start(t=None):
    import calendar
    tm = time.gmtime(t if t is not None else time.time())
    return calendar.timegm((tm.tm_year, tm.tm_mon, 1, 0, 0, 0, 0, 0, 0))


def log_credits(pull_id, account_id, kind, credits, balance_after=None, note=""):
    x("INSERT INTO ledger(at,pull_id,account_id,kind,credits,balance_after,note) VALUES(?,?,?,?,?,?,?)",
      (time.time(), pull_id, account_id, kind, int(credits), balance_after, note))


def month_spend(account_id=None):
    since = month_start()
    if account_id is None:
        return q1("SELECT COALESCE(SUM(credits),0) s FROM ledger WHERE at>=?", (since,))["s"]
    return q1("SELECT COALESCE(SUM(credits),0) s FROM ledger WHERE at>=? AND account_id=?", (since, account_id))["s"]


def pull_spend(pull_id, kind=None):
    if kind:
        return q1("SELECT COALESCE(SUM(credits),0) s FROM ledger WHERE pull_id=? AND kind=?", (pull_id, kind))["s"]
    return q1("SELECT COALESCE(SUM(credits),0) s FROM ledger WHERE pull_id=?", (pull_id,))["s"]


def get_meta(k, default=None):
    r = q1("SELECT v FROM meta WHERE k=?", (k,))
    return json.loads(r["v"]) if r else default


def set_meta(k, v):
    x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


# ---------------------------------------------------------------- pulls
def get_pull(pid):
    p = q1("SELECT * FROM pulls WHERE id=?", (pid,))
    if p:
        p["filters"] = json.loads(p.pop("filters_json") or "{}")
        p["balances"] = json.loads(p.pop("balances_json") or "{}")
        p["notes"] = json.loads(p.pop("notes_json") or "[]")
    return p


def update_pull(pid, **fields):
    if "filters" in fields:
        fields["filters_json"] = json.dumps(fields.pop("filters"))
    if "balances" in fields:
        fields["balances_json"] = json.dumps(fields.pop("balances"))
    if "notes" in fields:
        fields["notes_json"] = json.dumps(fields.pop("notes"))
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    x(f"UPDATE pulls SET {cols} WHERE id=?", (*fields.values(), pid))
