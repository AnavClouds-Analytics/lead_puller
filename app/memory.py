"""The shared memory of everyone ever pulled (on every Seamless account), the do-not-pull list, and the free
sync of contacts already researched in your Seamless accounts."""
import datetime
import json
import time

from . import db, seamless
from .config import cfg


# ---------------------------------------------------------------- already pulled
def find(keys):
    """The memory entry for any of these keys (None = never pulled)."""
    if not keys:
        return None
    marks = ",".join("?" * len(keys))
    rows = db.q(f"SELECT * FROM memory WHERE key IN ({marks})", keys)
    if not rows:
        return None
    order = {"em": 0, "li": 1, "nc": 2}
    rows.sort(key=lambda r: order.get(r["key"][:2], 9))
    return rows[0]


def remember(keys, source, pull_id=None, account_id=None, outcome="pending"):
    now = time.time()
    for k in keys:
        db.x("""INSERT INTO memory(key,source,pull_id,account_id,outcome,at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET outcome=CASE WHEN memory.outcome='email' THEN 'email'
                ELSE excluded.outcome END""", (k, source, pull_id, account_id, outcome, now))


def set_outcome(keys, outcome):
    for k in keys:
        db.x("UPDATE memory SET outcome=? WHERE key=? AND outcome!='email'", (outcome, k))


def why_seen(entry):
    kind = {"em": "email already in memory", "li": "same LinkedIn", "nc": "same name + company"}[entry["key"][:2]]
    if entry["source"] == "seamless":
        return "already researched in Seamless", kind
    return "already pulled before", kind


# ---------------------------------------------------------------- do-not-pull
def exclusion_key(kind, value):
    v = (value or "").strip()
    if kind == "email":
        e = db.norm_email(v)
        return "em:" + e if e else None
    if kind == "domain":
        d = db.norm_domain(v.split("@")[-1])
        return "domain:" + d if "." in d else None
    if kind == "linkedin":
        li = db.norm_li(v)
        return "li:" + li if li else None
    if kind == "company":
        n = db.norm_company(v)
        return "cn:" + n if len(n) >= 2 else None
    return None


def add_exclusion(kind, value, source):
    key = exclusion_key(kind, value)
    if not key:
        return False
    cur = db.q1("SELECT key FROM exclusions WHERE key=?", (key,))
    if cur:
        return False
    db.x("INSERT INTO exclusions(key,kind,value,source,created_at) VALUES(?,?,?,?,?)",
         (key, kind, value.strip(), source, time.time()))
    return True


def guess_kind(line):
    s = line.strip()
    if "linkedin.com" in s.lower():
        return "linkedin"
    if "@" in s and not s.startswith("@"):
        return "email"
    if "." in s and " " not in s:
        return "domain"
    return "company"


class Blocklist:
    """Loaded once per search page: the do-not-pull list as quick lookups."""

    def __init__(self):
        rows = db.q("SELECT key FROM exclusions")
        self.keys = {r["key"] for r in rows}
        self.names = [k[3:] for k in self.keys if k.startswith("cn:")]

    def reason(self, name, company, domain, li_url, email=None):
        for k in db.person_keys(name, company, li_url, email):
            if k in self.keys:
                return "On your do-not-pull list"
        d = db.norm_domain(domain)
        if d:
            parts = d.split(".")
            for i in range(len(parts) - 1):
                if "domain:" + ".".join(parts[i:]) in self.keys:
                    return "Company on your do-not-pull list"
        em = db.norm_email(email)
        if em and "domain:" + em.split("@")[1] in self.keys:
            return "Company on your do-not-pull list"
        c = " " + db.norm_company(company) + " "
        if c.strip() and any(n and (" " + n + " ") in c for n in self.names):
            return "Company on your do-not-pull list"
        return None


# ---------------------------------------------------------------- saved contacts in Seamless (free)
def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _saved_keys(rec):
    name = rec.get("fullName") or f"{rec.get('firstName') or ''} {rec.get('lastName') or ''}".strip()
    company = rec.get("companyName") or rec.get("company") or ""
    li = rec.get("lIProfileUrl") or rec.get("liUrl") or rec.get("linkedInProfileUrl") or ""
    emails = [rec.get(k) for k in ("email", "email1", "email2", "email3") if rec.get(k)]
    keys = db.person_keys(name, company, li)
    for e in emails:
        em = db.norm_email(e)
        if em and "em:" + em not in keys:
            keys.append("em:" + em)
    return keys, name, company, li, (emails[0] if emails else None)


def sync_saved(account, force=False, now=None):
    """Read the contacts already researched in this Seamless account (free) and remember them, so the tool
    never pays for them again. Incremental: only what's new since the last sync."""
    if not cfg.SYNC_SEAMLESS_SAVED:
        return 0
    now = now or time.time()
    row = db.q1("SELECT * FROM saved_sync WHERE account_id=?", (account["id"],))
    if row and not force and now - row["synced_at"] < cfg.SAVED_SYNC_HOURS * 3600:
        return 0
    start = (row["synced_at"] - 2 * 86400) if row else now - cfg.SAVED_LOOKBACK_DAYS * 86400
    added = 0
    for page in range(1, cfg.SAVED_MAX_PAGES + 1):
        recs = seamless.saved_contacts(_iso(start), _iso(now + 86400), page, account)
        if not recs:
            break
        for rec in recs:
            keys, name, company, li, email = _saved_keys(rec)
            if not keys:
                continue
            if not find(keys):
                added += 1
            remember(keys, "seamless", None, account["id"], "email" if email else "no_email")
            li_key = next((k for k in keys if k.startswith("li:")), None)
            nc_key = next((k for k in keys if k.startswith("nc:")), None)
            if db.q1("SELECT 1 x FROM saved_contacts WHERE account_id=? AND COALESCE(email,'')=? AND "
                     "COALESCE(li_key,'')=? AND COALESCE(nc_key,'')=?",
                     (account["id"], email or "", li_key or "", nc_key or "")):
                continue
            db.x("INSERT INTO saved_contacts(account_id,li_key,nc_key,email,record_json,at) VALUES(?,?,?,?,?,?)",
                 (account["id"], li_key, nc_key, email, json.dumps(rec), now))
        if len(recs) < 500:
            break
    db.x("""INSERT INTO saved_sync(account_id,synced_at,read) VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE
            SET synced_at=excluded.synced_at, read=excluded.read""", (account["id"], now, added))
    return added


def saved_match(account_id, name, company, li_url):
    """A saved contact for exactly this person: same LinkedIn, or the only one with this name + company."""
    li = db.norm_li(li_url)
    if li:
        r = db.q1("SELECT record_json FROM saved_contacts WHERE li_key=? AND email IS NOT NULL ORDER BY at DESC",
                  ("li:" + li,))
        if r:
            return json.loads(r["record_json"])
    keys = db.person_keys(name, company)
    nc = next((k for k in keys if k.startswith("nc:")), None)
    if nc:
        rows = db.q("SELECT DISTINCT email, record_json FROM saved_contacts WHERE nc_key=? AND email IS NOT NULL", (nc,))
        if len({r["email"].lower() for r in rows}) == 1:
            return json.loads(rows[0]["record_json"])
    return None
