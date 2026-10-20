"""SQLite storage. One file on disk (DATA_DIR/leadpuller.db), safe for the web threads and the worker."""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import cfg

DATA_DIR = cfg.DATA_DIR
DB_PATH = os.path.join(DATA_DIR, "leadpuller.db")
_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, created_at REAL);
CREATE TABLE IF NOT EXISTS presets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, prompt TEXT, filters_json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS pulls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, prompt TEXT, filters_json TEXT, notes_json TEXT,
  status TEXT,              -- draft, previewed, collecting, collected, researching, paused, done, cancelled, failed
  paused_from TEXT,
  target INTEGER DEFAULT 0, per_company_cap INTEGER DEFAULT 0,
  total_matches INTEGER, next_token TEXT, pages_fetched INTEGER DEFAULT 0, rows_seen INTEGER DEFAULT 0,
  search_credits INTEGER DEFAULT 0, research_credits INTEGER DEFAULT 0,
  stop_reason TEXT, error TEXT, cloned_from INTEGER,
  created_at REAL, updated_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pull_id INTEGER, search_result_id TEXT,
  name TEXT, first_name TEXT, last_name TEXT, title TEXT, company TEXT, domain TEXT,
  country TEXT, state TEXT, city TEXT, company_country TEXT, li_url TEXT,
  seniority TEXT, department TEXT, size TEXT, industry TEXT,
  fit TEXT, fit_reason TEXT,
  kept INTEGER DEFAULT 1, skip_reason TEXT, selected INTEGER DEFAULT 1,
  request_id TEXT, research_status TEXT, research_message TEXT, submitted_at REAL,
  email TEXT, email_status TEXT, email_confidence TEXT, personal_email TEXT,
  phone TEXT, phone_type TEXT, company_phone TEXT,
  raw_search TEXT, raw_research TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS ix_contacts_pull ON contacts(pull_id);
CREATE INDEX IF NOT EXISTS ix_contacts_req ON contacts(request_id);
CREATE TABLE IF NOT EXISTS research_requests (
  request_id TEXT PRIMARY KEY, pull_id INTEGER, guess_sid TEXT, status TEXT,
  created_at REAL, resolved_at REAL);
CREATE INDEX IF NOT EXISTS ix_rr_pull ON research_requests(pull_id, resolved_at);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, pull_id INTEGER, created_at REAL);
CREATE TABLE IF NOT EXISTS exclusions (key TEXT PRIMARY KEY, kind TEXT, value TEXT, source TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS companies (
  key TEXT PRIMARY KEY, name TEXT, domain TEXT, first_at REAL, last_at REAL, first_pull_id INTEGER,
  last_pull_id INTEGER, people INTEGER DEFAULT 0, released_at REAL);
CREATE TABLE IF NOT EXISTS searches (
  hash TEXT PRIMARY KEY, filters_json TEXT, cursor TEXT, cursor_pull_id INTEGER, cursor_at REAL,
  results_seen INTEGER DEFAULT 0, exhausted_at REAL, runs INTEGER DEFAULT 0, last_pull_id INTEGER, updated_at REAL);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, pull_id INTEGER, created_at REAL, count INTEGER, split TEXT,
  include_risky INTEGER, extras INTEGER, countries_json TEXT);
CREATE TABLE IF NOT EXISTS column_sets (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, columns_json TEXT,
  created_at REAL);
CREATE TABLE IF NOT EXISTS downloaded (email TEXT PRIMARY KEY, batch_id INTEGER, at REAL);
CREATE TABLE IF NOT EXISTS domains (domain TEXT PRIMARY KEY, has_mx INTEGER, detail TEXT, checked_at REAL);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, pull_id INTEGER, kind TEXT, credits INTEGER,
  balance_after INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, api_key TEXT, priority INTEGER DEFAULT 0,
  enabled INTEGER DEFAULT 1, monthly_budget INTEGER DEFAULT 0, source TEXT DEFAULT 'ui',
  balance INTEGER, balance_at REAL, last_error TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS balance_marks (
  account_id INTEGER, month TEXT, first_balance INTEGER, first_at REAL, last_balance INTEGER, last_at REAL,
  added INTEGER DEFAULT 0, PRIMARY KEY(account_id, month));
CREATE TABLE IF NOT EXISTS collect_pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, pull_id INTEGER, at REAL, rows INTEGER, kept INTEGER, credits INTEGER);
CREATE INDEX IF NOT EXISTS ix_cp_pull ON collect_pages(pull_id, id);
"""


MIGRATIONS = {
    "pulls": [("company_cooldown", "INTEGER"), ("search_hash", "TEXT"), ("started_from", "TEXT"),
              ("from_top", "INTEGER DEFAULT 0"), ("test_size", "INTEGER DEFAULT 0"), ("test_state", "TEXT"),
              ("test_rate", "REAL"), ("bal_before", "INTEGER"), ("bal_after", "INTEGER")],
    "contacts": [("timezone", "TEXT"), ("funding", "TEXT"), ("news", "TEXT"), ("title_started_at", "TEXT"),
                 ("last_modified_at", "TEXT"), ("email_chance", "REAL"), ("retry", "INTEGER DEFAULT 0"),
                 ("batch_id", "INTEGER"), ("filled_free", "INTEGER DEFAULT 0"), ("email_pick", "TEXT")],
    "seen": [("outcome", "TEXT"), ("outcome_at", "REAL")],
    "companies": [("tried", "INTEGER DEFAULT 0"), ("tried_last_at", "REAL")],
    "batches": [("columns_json", "TEXT")],
    # v1.3
    "pulls+": [("account_id", "INTEGER"), ("account_choice", "TEXT"), ("est_search_credits", "INTEGER"),
               ("spend_cap", "INTEGER"), ("keep_ack_page", "INTEGER DEFAULT 0"), ("claude_filters_json", "TEXT"),
               ("edited_from", "INTEGER"), ("moved_to", "INTEGER"), ("pause_kind", "TEXT"),
               ("collect_started_credits", "INTEGER")],
    "contacts+": [("skip_kind", "TEXT"), ("email_chance_n", "INTEGER"), ("email_chance_basis", "TEXT")],
    "research_requests": [("account_id", "INTEGER"), ("gave_up", "INTEGER DEFAULT 0"), ("late_polled_at", "REAL")],
    "ledger": [("account_id", "INTEGER")],
    "searches": [("cursor_account_id", "INTEGER")],
}


def connect():
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(DATA_DIR, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            # Upgrades for databases made by older versions: add any missing columns (never drops data).
            for table, cols in MIGRATIONS.items():
                table = table.rstrip("+")
                have = {r[1] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for col, typ in cols:
                    if col not in have:
                        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            _conn.execute("CREATE INDEX IF NOT EXISTS ix_contacts_batch ON contacts(batch_id)")
            _conn.execute("CREATE INDEX IF NOT EXISTS ix_contacts_skip ON contacts(pull_id, kept, skip_kind)")
            _conn.execute("CREATE INDEX IF NOT EXISTS ix_ledger_acct ON ledger(account_id, at)")
            _conn.execute("CREATE INDEX IF NOT EXISTS ix_rr_late ON research_requests(gave_up, created_at)")
            _ensure_env_account(_conn)
        return _conn


def _ensure_env_account(c):
    """The SEAMLESS_API_KEY in .env (if any) is kept as an account, so older setups keep working.
    Rows from before v1.3 (no account recorded) belong to it."""
    key = cfg.SEAMLESS_API_KEY
    row = c.execute("SELECT id, api_key FROM accounts WHERE source='env'").fetchone()
    if key and not row:
        n = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        old_bal = c.execute("SELECT value FROM settings WHERE key='credit_balance'").fetchone()
        old_at = c.execute("SELECT value FROM settings WHERE key='credit_balance_at'").fetchone()
        c.execute("INSERT INTO accounts(name, api_key, priority, enabled, source, balance, balance_at, created_at) "
                  "VALUES(?,?,?,?,?,?,?,?)", ("Account from .env", key, n + 1, 1, "env",
                                               json.loads(old_bal[0]) if old_bal else None,
                                               json.loads(old_at[0]) if old_at else None, time.time()))
        row = c.execute("SELECT id, api_key FROM accounts WHERE source='env'").fetchone()
    elif key and row and row[1] != key:
        c.execute("UPDATE accounts SET api_key=?, balance=NULL, balance_at=NULL WHERE id=?", (key, row[0]))
    if row:
        for t in ("ledger", "research_requests", "pulls"):
            c.execute(f"UPDATE {t} SET account_id=? WHERE account_id IS NULL", (row[0],)) if t != "pulls" else \
                c.execute("UPDATE pulls SET account_id=? WHERE account_id IS NULL AND (search_credits>0 OR "
                          "research_credits>0)", (row[0],))


def reset_for_tests(path):
    """Point the module at a fresh database file (tests only)."""
    global _conn, DB_PATH, DATA_DIR
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        DATA_DIR = os.path.dirname(path)
        DB_PATH = path


@contextmanager
def tx():
    c = connect()
    with _lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def q(sql, args=()):
    c = connect()
    with _lock:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def q1(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql, args=()):
    c = connect()
    with _lock:
        cur = c.execute(sql, args)
        return cur.lastrowid


# ---------- settings ----------
def company_key(company, domain):
    d = norm_domain(domain)
    if d:
        return "d:" + d
    n = norm(company)
    return ("n:" + n) if n else None


def get_setting(key, default=None):
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def set_setting(key, value):
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, json.dumps(value)))


# ---------- pulls ----------
def get_pull(pull_id):
    p = q1("SELECT * FROM pulls WHERE id=?", (pull_id,))
    if p:
        p["filters"] = json.loads(p.pop("filters_json") or "{}")
        p["notes"] = json.loads(p.pop("notes_json") or "[]")
    return p


def update_pull(pull_id, **fields):
    if "filters" in fields:
        fields["filters_json"] = json.dumps(fields.pop("filters"))
    if "notes" in fields:
        fields["notes_json"] = json.dumps(fields.pop("notes"))
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    x(f"UPDATE pulls SET {cols} WHERE id=?", (*fields.values(), pull_id))


def with_defaults(filters):
    """New pulls start with the .env defaults for the 'fewer people without email' filters."""
    f = dict(filters or {})
    if cfg.UPDATED_WITHIN_MONTHS and "updatedWithinMonths" not in f:
        f["updatedWithinMonths"] = cfg.UPDATED_WITHIN_MONTHS
    if cfg.REQUIRE_COMPANY_WEBSITE and "requireCompanyWebsite" not in f:
        f["requireCompanyWebsite"] = True
    if cfg.REQUIRE_LINKEDIN and "requireLinkedIn" not in f:
        f["requireLinkedIn"] = True
    return f


def create_pull(name, prompt, filters, notes, target=0, per_company_cap=None, cloned_from=None, cooldown=None):
    now = time.time()
    cap = cfg.MAX_PER_COMPANY if per_company_cap is None else per_company_cap
    cooldown = cfg.COMPANY_COOLDOWN_DAYS if cooldown is None else cooldown
    return x("""INSERT INTO pulls(name,prompt,filters_json,notes_json,status,target,per_company_cap,company_cooldown,
                cloned_from,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
             (name, prompt, json.dumps(filters), json.dumps(notes), "draft", target, cap, cooldown,
              cloned_from, now, now))


# ---------- credits ----------
def log_credits(pull_id, kind, credits, balance_after=None, note="", account_id=None):
    x("INSERT INTO ledger(at,pull_id,kind,credits,balance_after,note,account_id) VALUES(?,?,?,?,?,?,?)",
      (time.time(), pull_id, kind, credits, balance_after, note, account_id))
    if pull_id:
        col = "search_credits" if kind == "search" else "research_credits"
        x(f"UPDATE pulls SET {col}={col}+? WHERE id=?", (credits, pull_id))


def month_start(now=None):
    t = time.gmtime(now)
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1)) - time.timezone


def month_key(now=None):
    return time.strftime("%Y-%m", time.gmtime(now))


def month_spend(account_id=None):
    if account_id is None:
        row = q1("SELECT COALESCE(SUM(credits),0) AS s FROM ledger WHERE at>=?", (month_start(),))
    else:
        row = q1("SELECT COALESCE(SUM(credits),0) AS s FROM ledger WHERE at>=? AND account_id=?",
                 (month_start(), account_id))
    return int(row["s"])


def spend_since(account_id, since):
    return int(q1("SELECT COALESCE(SUM(credits),0) AS s FROM ledger WHERE at>=? AND account_id=?",
                  (since, account_id))["s"])


# ---------- dedupe ----------
def norm(s):
    return " ".join((s or "").lower().replace(",", " ").replace(".", " ").split())


def norm_li(url):
    u = (url or "").strip().lower().split("?")[0].rstrip("/")
    for p in ("https://", "http://", "www."):
        if u.startswith(p):
            u = u[len(p):]
    if u.startswith("www."):
        u = u[4:]
    return u


def norm_domain(d):
    d = (d or "").strip().lower()
    for p in ("https://", "http://"):
        if d.startswith(p):
            d = d[len(p):]
    d = d.split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def person_keys(name="", company="", li_url="", email=""):
    keys = []
    if li_url and norm_li(li_url):
        keys.append("li:" + norm_li(li_url))
    if name and company:
        keys.append("nc:" + norm(name) + "|" + norm(company))
    if email:
        keys.append("em:" + email.strip().lower())
    return keys
