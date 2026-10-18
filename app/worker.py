"""Pull pipeline: preview -> collect (search pages) -> research (enrich) -> done.

Collecting and researching run in a background thread in small steps (one search page, one research
batch, one poll), with all state in the database, so jobs survive closing the browser or restarting
the server, and pause/stop take effect within seconds.

Money rules enforced here:
  * nobody is researched twice (people memory never expires; optional paid retry for no-email people)
  * every skip rule runs before research, and is re-checked right before paying
  * a research call that may have charged is never resent automatically
  * test batch: research a few first and pause if too few emails come back
"""
import hashlib
import json
import logging
import math
import re
import threading
import time

from . import claude, db, dnscheck, seamless
from .config import cfg
from .filters import NATIVE_EXCLUDES, search_filter_count, to_search_body

log = logging.getLogger("leadpuller")

# Sizes and timings come from .env (see app/config.py). POLL_EVERY can be overridden by tests.
POLL_EVERY = None
POLL_CHUNK = 100
PENDING_TIMEOUT = 45 * 60
US_ALIASES = {"united states", "us", "usa", "united states of america"}
ACTIVE = ("collecting", "researching")
DUPLICATE_STATUSES = ("duplicate", "contact-already-researched")
NO_EMAIL_STATUSES = ("done", "not found", "missing", "no answer", "error", "timed out",
                     "no license or credits available")
STOP_CODES = {"insufficientCredits", "missingLicense", "badKey", "noKey"}
SPENDING_OFF = "Spending is switched off (SPENDING_ENABLED=0 in .env)."


class PullError(Exception):
    pass


# ---------------------------------------------------------------- small helpers
def _split_name(name):
    parts = (name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _day(t):
    return time.strftime("%d %b %Y", time.localtime(t)) if t else ""


def _date_age_months(iso):
    """Months since an ISO date (YYYY-MM-DD...), or None if unknown."""
    if not iso:
        return None
    try:
        t = time.mktime(time.strptime(str(iso)[:10], "%Y-%m-%d"))
    except ValueError:
        return None
    return (time.time() - t) / (30.44 * 86400)


def contact_from_search(row):
    first, last = row.get("firstName"), row.get("lastName")
    if not first:
        first, last = _split_name(row.get("name"))
    industries = row.get("industries") or []
    funding = ""
    if row.get("companyLatestFundingClassifications") or row.get("companyLatestFundingDate"):
        cls = ", ".join(row.get("companyLatestFundingClassifications") or [])
        funding = " · ".join(x for x in (cls, str(row.get("companyLatestFundingDate") or "")[:7]) if x)
    news = ""
    items = [n for n in (row.get("newsAndEvents") or []) if isinstance(n, dict)]
    if items:
        items.sort(key=lambda n: str(n.get("date") or ""), reverse=True)
        n = items[0]
        news = " · ".join(x for x in (n.get("type") or "", str(n.get("date") or "")[:7], (n.get("title") or "")[:140]) if x)
    return {
        "search_result_id": row.get("searchResultId"),
        "name": row.get("name") or f"{first} {last}".strip(),
        "first_name": first or "", "last_name": last or "",
        "title": row.get("title") or "", "company": row.get("company") or "",
        "domain": db.norm_domain(row.get("domain")), "country": row.get("country") or "",
        "state": row.get("state") or "", "city": row.get("city") or "",
        "company_country": row.get("companyCountry") or "", "li_url": row.get("liUrl") or "",
        "seniority": row.get("seniority") or "", "department": row.get("department") or "",
        "size": row.get("employeeSizeRange") or "",
        "industry": industries[0] if industries else "",
        "timezone": row.get("timezone") or "", "funding": funding, "news": news,
        "title_started_at": row.get("titleStartedAt") or "", "last_modified_at": row.get("lastModifiedAt") or "",
    }


# ---------------------------------------------------------------- blocked companies / do-not-pull
def has_words(text, phrase):
    """Whole-word, case-insensitive: 'CTO' matches 'CTO' and 'Co-founder & CTO' but not 'Director';
    'intern' does not match 'International'."""
    t = " " + re.sub(r"[^a-z0-9+#]+", " ", (text or "").lower()) + " "
    p = re.sub(r"[^a-z0-9+#]+", " ", (phrase or "").lower()).strip()
    return bool(p) and (" " + p + " ") in t


def _domain_and_parents(domain):
    d = db.norm_domain(domain)
    parts = d.split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)] if d else []


def _name_blocked(company):
    """Whole-word phrase match against blocked company names ('Infosys' blocks 'Infosys BPM Ltd')."""
    name = " " + db.norm(company) + " "
    if not name.strip():
        return None
    for r in db.q("SELECT value FROM exclusions WHERE kind='company_name'"):
        phrase = db.norm(r["value"])
        if phrase and (" " + phrase + " ") in name:
            return r["value"]
    return None


def blocked_company(company, domain):
    for d in _domain_and_parents(domain):
        if db.q1("SELECT key FROM exclusions WHERE key=?", ("domain:" + d,)):
            return f"Blocked company ({d})"
    hit = _name_blocked(company)
    if hit:
        return f"Blocked company (“{hit}”)"
    return None


# ---------------------------------------------------------------- people memory
def _people_rows(keys):
    if not keys:
        return []
    return db.q(f"SELECT * FROM seen WHERE key IN ({','.join('?' * len(keys))})", keys)


def people_reason(name, company, li_url):
    """(reason, retry). reason=None means OK to pay for. retry=True means a paid retry of a no-email person."""
    keys = db.person_keys(name, company, li_url)
    if keys and db.q1(f"SELECT key FROM exclusions WHERE key IN ({','.join('?' * len(keys))})", keys):
        return "On your do-not-pull list", False
    rows = _people_rows(keys)
    if not rows:
        return None, False
    last = max(rows, key=lambda r: r["created_at"] or 0)
    if all((r["outcome"] or "") == "no_email" for r in rows):
        days = cfg.NO_EMAIL_RETRY_DAYS
        since = max(r["outcome_at"] or r["created_at"] or 0 for r in rows)
        if days and time.time() - since >= days * 86400:
            return None, True
        return f"Pulled before, no email found (pull #{last['pull_id']})", False
    return f"Already pulled before (pull #{last['pull_id']})", False


def _mark_people(c, pull_id, outcome, now=None):
    now = now or time.time()
    for k in db.person_keys(c["name"], c["company"], c["li_url"]):
        if outcome == "pending":
            db.x("""INSERT INTO seen(key,pull_id,created_at,outcome,outcome_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET pull_id=excluded.pull_id, created_at=excluded.created_at,
                    outcome='pending', outcome_at=excluded.outcome_at""", (k, pull_id, now, "pending", now))
        else:
            db.x("UPDATE seen SET outcome=?, outcome_at=? WHERE key=?", (outcome, now, k))


# ---------------------------------------------------------------- company memory
def _company_tried(name, domain, pull_id, at):
    key = db.company_key(name, domain)
    if not key:
        return
    db.x("""INSERT INTO companies(key, name, domain, first_at, last_at, first_pull_id, last_pull_id, people, tried,
                                  tried_last_at)
            VALUES(?,?,?,?,NULL,?,NULL,0,1,?)
            ON CONFLICT(key) DO UPDATE SET tried=COALESCE(companies.tried,0)+1, tried_last_at=excluded.tried_last_at,
              name=COALESCE(NULLIF(excluded.name,''), companies.name),
              domain=COALESCE(NULLIF(excluded.domain,''), companies.domain)""",
         (key, name or "", db.norm_domain(domain), at, pull_id, at))


def _company_emailed(name, domain, pull_id, at):
    """Cooldown starts only when someone at the company actually got an email."""
    key = db.company_key(name, domain)
    if not key:
        return
    db.x("""INSERT INTO companies(key, name, domain, first_at, last_at, first_pull_id, last_pull_id, people, tried,
                                  tried_last_at)
            VALUES(?,?,?,?,?,?,?,1,1,?)
            ON CONFLICT(key) DO UPDATE SET last_at=excluded.last_at, last_pull_id=excluded.last_pull_id,
              people=COALESCE(companies.people,0)+1""",
         (key, name or "", db.norm_domain(domain), at, at, pull_id, pull_id, at))


def _cooldown_reason(c, pull):
    days = pull.get("company_cooldown") or 0
    key = db.company_key(c["company"], c["domain"])
    if not days or not key:
        return None
    row = db.q1("SELECT * FROM companies WHERE key=?", (key,))
    if not row or not row["last_at"] or row["last_pull_id"] == pull["id"]:
        return None
    if row["released_at"] and row["released_at"] >= row["last_at"]:
        return None
    until = row["last_at"] + days * 86400
    if time.time() < until:
        return (f"Company emailed {time.strftime('%d %b', time.localtime(row['last_at']))} (pull #{row['last_pull_id']}); "
                f"skipped until {_day(until)}")
    return None


# ---------------------------------------------------------------- skip rules
def _skip_reason(c, pull, company_counts, in_pull_keys):
    """(reason, retry). Cheap checks first; the network check (email server) last."""
    f = pull["filters"]
    title, company = c["title"], c["company"]
    if f.get("requireCompanyWebsite") and not c["domain"]:
        return "No company website", False
    if f.get("requireLinkedIn") and not db.norm_li(c["li_url"]):
        return "No LinkedIn profile", False
    must = f.get("titleMustContain", [])
    if must and not any(has_words(title, w) for w in must):
        return "Title doesn't contain " + " / ".join(must[:3]), False
    for w in f.get("excludeTitleWords", []):
        if has_words(title, w):
            return f"Title contains “{w}”", False
    for w in f.get("excludeCompanyWords", []):
        if has_words(company, w):
            return f"Company contains “{w}”", False
    if c["industry"] and c["industry"] in f.get("excludeIndustries", []):
        return f"Industry {c['industry']}", False
    if c["domain"] and c["domain"] in {db.norm_domain(d) for d in f.get("excludeDomains", [])}:
        return "Domain on this pull's skip list", False
    if c["country"] and c["country"].lower() in {x.lower() for x in f.get("excludeCountries", [])}:
        return f"Country {c['country']} skipped", False
    if c["state"] and c["state"].lower() in {x.lower() for x in f.get("excludeStates", [])}:
        return f"State {c['state']} skipped", False
    if c["seniority"] and c["seniority"] in f.get("excludeSeniority", []):
        return f"Seniority {c['seniority']} skipped", False
    age = _date_age_months(c.get("title_started_at"))
    if age is not None:
        if f.get("roleMinMonths") and age < f["roleMinMonths"]:
            return f"In current role under {f['roleMinMonths']} months", False
        if f.get("roleMaxYears") and age > f["roleMaxYears"] * 12:
            return f"In current role over {f['roleMaxYears']} years", False
    keys = db.person_keys(c["name"], c["company"], c["li_url"])
    if any(k in in_pull_keys for k in keys):
        return "Duplicate inside this pull", False
    blocked = blocked_company(c["company"], c["domain"])
    if blocked:
        return blocked, False
    reason, retry = people_reason(c["name"], c["company"], c["li_url"])
    if reason:
        return reason, False
    reason = _cooldown_reason(c, pull)
    if reason:
        return reason, False
    cap = pull["per_company_cap"] or 0
    ck = db.company_key(c["company"], c["domain"])
    if cap and ck and company_counts.get(ck, 0) >= cap:
        return f"Company limit reached ({cap} per company)", False
    if cfg.SKIP_NO_EMAIL_SERVER and c["domain"] and dnscheck.has_mail_server(c["domain"]) == "no":
        return "Company domain can't receive email", False
    return None, retry


def _pull_state(pull_id):
    company_counts, in_pull_keys = {}, set()
    for r in db.q("SELECT name, company, domain, li_url, kept FROM contacts WHERE pull_id=?", (pull_id,)):
        if r["kept"]:
            ck = db.company_key(r["company"], r["domain"])
            if ck:
                company_counts[ck] = company_counts.get(ck, 0) + 1
        in_pull_keys.update(db.person_keys(r["name"], r["company"], r["li_url"]))
    return company_counts, in_pull_keys


COLS = ("search_result_id", "name", "first_name", "last_name", "title", "company", "domain", "country", "state", "city",
        "company_country", "li_url", "seniority", "department", "size", "industry", "timezone", "funding", "news",
        "title_started_at", "last_modified_at")


def _store_rows(pull, rows):
    """Insert a page of search rows, applying skip rules. Returns ids of newly kept contacts."""
    company_counts, in_pull_keys = _pull_state(pull["id"])
    kept_ids = []
    now = time.time()
    for row in rows:
        c = contact_from_search(row)
        if not c["search_result_id"]:
            continue
        reason, retry = _skip_reason(c, pull, company_counts, in_pull_keys)
        cid = db.x(f"""INSERT INTO contacts(pull_id, {", ".join(COLS)}, kept, skip_reason, selected, retry,
                       raw_search, created_at) VALUES(?, {", ".join("?" * len(COLS))}, ?,?,?,?,?,?)""",
                   (pull["id"], *[c[k] for k in COLS], 0 if reason else 1, reason, 0 if reason else 1,
                    1 if retry else 0, json.dumps(row), now))
        in_pull_keys.update(db.person_keys(c["name"], c["company"], c["li_url"]))
        if not reason:
            kept_ids.append(cid)
            ck = db.company_key(c["company"], c["domain"])
            if ck:
                company_counts[ck] = company_counts.get(ck, 0) + 1
    return kept_ids


def score_contacts(pull, contact_ids):
    if not contact_ids or not cfg.FIT_CHECK or not claude.api_key():
        return None
    marks = ",".join("?" * len(contact_ids))
    people = db.q(f"""SELECT id, title, seniority, department, company, industry, size, country
                      FROM contacts WHERE id IN ({marks})""", contact_ids)
    try:
        for i in range(0, len(people), 50):
            chunk = people[i:i + 50]
            ratings = claude.score_fit(pull["prompt"] or pull["name"], pull["filters"],
                                       [dict(p, id=str(p["id"])) for p in chunk])
            for pid, (fit, reason) in ratings.items():
                db.x("UPDATE contacts SET fit=?, fit_reason=? WHERE id=? AND pull_id=?",
                     (fit, reason, int(pid), pull["id"]))
    except claude.ClaudeError as e:
        return f"Fit check skipped: {e}"
    return None


def resolve_locations(filters):
    """Check location values against Seamless's place list (free). A misspelt place silently returns zero
    results (or, for an exclusion, excludes nothing), so fix it or flag it."""
    notes = []
    plan = (("contactCountry", "country"), ("excludeCountries", "country"), ("contactState", "state-country"),
            ("excludeStates", "state-country"), ("locations", None), ("excludeLocations", None))
    for key, types in plan:
        if key not in filters:
            continue
        fixed = []
        for val in filters[key]:
            v = val.strip().lstrip("-")
            if v.lower() in US_ALIASES or (key in ("contactState", "excludeStates") and len(v) == 2):
                fixed.append(v)
                continue
            try:
                hits = seamless.lookup_location(v, types)
            except seamless.SeamlessError:
                fixed.append(v)
                continue
            if not hits:
                notes.append(f"Seamless doesn't recognise the place “{v}”, so it was removed.")
                continue
            best = hits[0]["location"]
            if best.split(",")[0].strip().lower() != v.split(",")[0].strip().lower():
                notes.append(f"Place “{v}” matched to Seamless's “{best}”.")
            fixed.append(best if key not in ("contactCountry", "excludeCountries") or "," not in best else v)
        if fixed:
            filters[key] = fixed
        else:
            filters.pop(key)
    return filters, notes


BUDGET_MSG = "this month's credit budget is used up (MONTHLY_CREDIT_BUDGET in .env)"


def budget_left():
    budget = cfg.MONTHLY_CREDIT_BUDGET
    if not budget:
        return None
    return max(0, int(budget) - db.month_spend())


# ---------------------------------------------------------------- search memory
def search_hash(filters):
    return hashlib.sha1(json.dumps(to_search_body(filters), sort_keys=True).encode()).hexdigest()[:20]


def _remember_position(pull, next_token, rows_count, reset=False):
    """Save where this search got to, so the next run of the same filters starts there."""
    h = pull.get("search_hash") or search_hash(pull["filters"])
    now = time.time()
    db.x("""INSERT INTO searches(hash, filters_json, cursor, cursor_pull_id, cursor_at, results_seen, exhausted_at,
                                 runs, last_pull_id, updated_at)
            VALUES(?,?,?,?,?,?,?,0,?,?)
            ON CONFLICT(hash) DO UPDATE SET cursor=excluded.cursor, cursor_pull_id=excluded.cursor_pull_id,
              cursor_at=excluded.cursor_at,
              results_seen=CASE WHEN ? THEN excluded.results_seen ELSE searches.results_seen + excluded.results_seen END,
              exhausted_at=excluded.exhausted_at, last_pull_id=excluded.last_pull_id, updated_at=excluded.updated_at""",
         (h, json.dumps(to_search_body(pull["filters"]), sort_keys=True), next_token, pull["id"], now, rows_count,
          None if next_token else now, pull["id"], now, 1 if reset else 0))


# ---------------------------------------------------------------- preview / collect
def _check_spending():
    if not cfg.SPENDING_ENABLED:
        raise PullError(SPENDING_OFF + " Ask your developer to set it to 1 and restart.")


def run_preview(pull_id, from_top=False):
    pull = db.get_pull(pull_id)
    if not pull:
        raise PullError("Pull not found.")
    if pull["status"] not in ("draft", "previewed", "collected", "cancelled"):
        raise PullError("This pull is already running.")
    if pull["status"] == "collected" and db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL",
                                               (pull_id,))["n"]:
        raise PullError("Research has started on this pull. Use “Run again” for a new preview.")
    if not search_filter_count(pull["filters"]):
        raise PullError("Add at least one search filter (title, seniority, industry, country...) first.")
    _check_spending()
    left = budget_left()
    if left is not None and left < 3:
        raise PullError(BUDGET_MSG[0].upper() + BUDGET_MSG[1:] + ".")
    filters, loc_notes = (dict(pull["filters"]), []) if pull["filters"].get("savedSearchId") else \
        resolve_locations(dict(pull["filters"]))
    notes = [n for n in pull["notes"] if not n.startswith(("Place “", "Seamless doesn't recognise", "Location “"))]
    notes += loc_notes
    db.x("DELETE FROM contacts WHERE pull_id=?", (pull_id,))
    db.update_pull(pull_id, filters=filters, notes=notes, next_token=None, pages_fetched=0, rows_seen=0,
                   total_matches=None, stop_reason=None, error=None)
    h = search_hash(filters)
    db.update_pull(pull_id, search_hash=h, from_top=1 if from_top else 0, started_from=None)
    pull = db.get_pull(pull_id)

    # Same filters as an earlier pull? Start where that one stopped instead of paying for the same people again.
    start, started_from = None, None
    prev = db.q1("SELECT * FROM searches WHERE hash=?", (h,))
    if prev and cfg.CONTINUE_SAME_SEARCH and not from_top:
        if prev["cursor"]:
            start = prev["cursor"]
            started_from = (f"Same search as pull #{prev['cursor_pull_id']} ({_day(prev['cursor_at'])}). Continuing "
                            f"where it stopped, after the {prev['results_seen']:,} results already paid for.")
        elif prev["exhausted_at"]:
            started_from = (f"You went through every result of this search before (pull #{prev['last_pull_id']}, "
                            f"{_day(prev['exhausted_at'])}). Starting from the top; people already pulled are skipped.")
    rows, sup = [], {}
    if start:
        try:
            rows, sup = seamless.search_contacts(to_search_body(filters), cfg.PREVIEW_SIZE, start, pull_id)
        except seamless.SeamlessError as e:
            if e.code in STOP_CODES:
                raise
            rows = []
        if not rows:
            start = None
            started_from = "Seamless no longer accepted the saved position for this search, so it starts from the top."
    if not start:
        rows, sup = seamless.search_contacts(to_search_body(filters), cfg.PREVIEW_SIZE, None, pull_id)
    kept = _store_rows(pull, rows)
    fit_note = score_contacts(pull, kept)
    nxt = sup.get("nextToken") if sup.get("isMore", True) else None
    db.update_pull(pull_id, status="previewed", total_matches=sup.get("total", len(rows)), next_token=nxt,
                   pages_fetched=1, rows_seen=len(rows), error=fit_note, started_from=started_from)
    _remember_position(db.get_pull(pull_id), nxt, len(rows), reset=start is None)
    db.x("UPDATE searches SET runs=runs+1 WHERE hash=?", (h,))
    return db.get_pull(pull_id)


def start_collect(pull_id, target, cap):
    pull = db.get_pull(pull_id)
    if pull["status"] not in ("previewed", "collected"):
        raise PullError("Preview the pull first.")
    _check_spending()
    target = int(target)
    if target < 1:
        raise PullError("Enter how many people you want.")
    db.update_pull(pull_id, target=target, per_company_cap=int(cap or 0), stop_reason=None, error=None)
    if int(cap or 0) != (pull["per_company_cap"] or 0):
        _reapply_cap(pull_id, int(cap or 0))
    db.x("UPDATE contacts SET kept=1, selected=1, skip_reason=NULL WHERE pull_id=? AND skip_reason='Over your target' "
         "AND submitted_at IS NULL", (pull_id,))
    kept = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1", (pull_id,))["n"]
    if kept >= target or not pull["next_token"]:
        _finish_collect(pull_id, "Reached your target." if kept >= target else "No more matching people in Seamless.")
        return db.get_pull(pull_id)
    db.update_pull(pull_id, status="collecting")
    return db.get_pull(pull_id)


def _reapply_cap(pull_id, cap):
    if not cap:
        db.x("UPDATE contacts SET kept=1, selected=1, skip_reason=NULL WHERE pull_id=? AND skip_reason LIKE 'Company limit%'",
             (pull_id,))
        return
    counts = {}
    for r in db.q("SELECT id, company, domain, kept, skip_reason FROM contacts WHERE pull_id=? ORDER BY id", (pull_id,)):
        if not r["kept"] and not (r["skip_reason"] or "").startswith("Company limit"):
            continue
        ck = db.company_key(r["company"], r["domain"])
        counts[ck] = counts.get(ck, 0) + 1
        if ck and counts[ck] > cap:
            db.x("UPDATE contacts SET kept=0, selected=0, skip_reason=? WHERE id=?",
                 (f"Company limit reached ({cap} per company)", r["id"]))
        elif (r["skip_reason"] or "").startswith("Company limit"):
            db.x("UPDATE contacts SET kept=1, selected=1, skip_reason=NULL WHERE id=?", (r["id"],))


def _finish_collect(pull_id, reason):
    pull = db.get_pull(pull_id)
    ids = [r["id"] for r in db.q("SELECT id FROM contacts WHERE pull_id=? AND kept=1 ORDER BY id", (pull_id,))]
    if pull["target"] and len(ids) > pull["target"]:
        extra = ids[pull["target"]:]
        marks = ",".join("?" * len(extra))
        db.x(f"UPDATE contacts SET kept=0, selected=0, skip_reason='Over your target' WHERE id IN ({marks})", extra)
    db.update_pull(pull_id, status="collected", stop_reason=reason)
    compute_email_chances(pull_id)


# ---------------------------------------------------------------- email chance (learned from your own pulls)
def _fresh(c):
    age = _date_age_months(c.get("last_modified_at"))
    return "unknown" if age is None else ("fresh" if age <= 12 else "old")


def compute_email_chances(pull_id):
    """Each person's chance of an email, from people of the same kind you researched before.
    Uses the most specific group with enough history (EMAIL_CHANCE_MIN_SAMPLES), else falls back."""
    need = cfg.EMAIL_CHANCE_MIN_SAMPLES
    hist = db.q(f"""SELECT country, size, li_url, last_modified_at, email, email_status FROM contacts
                    WHERE submitted_at IS NOT NULL AND pull_id != ?
                    AND LOWER(research_status) IN ('done','not found','missing','no answer')""", (pull_id,))
    if len(hist) < need:
        db.x("UPDATE contacts SET email_chance=NULL WHERE pull_id=?", (pull_id,))
        return False
    levels = [lambda c: ("cs l", c["country"], c["size"], bool(db.norm_li(c["li_url"]))),
              lambda c: ("cs", c["country"], c["size"]),
              lambda c: ("cl", c["country"], bool(db.norm_li(c["li_url"])), _fresh(c)),
              lambda c: ("c", c["country"]),
              lambda c: ("all",)]
    tables = []
    for fn in levels:
        t = {}
        for h in hist:
            k = fn(h)
            a = t.setdefault(k, [0, 0])
            a[0] += 1
            a[1] += 1 if (h["email"] and (h["email_status"] or "").lower() != "invalid") else 0
        tables.append(t)
    for c in db.q("SELECT id, country, size, li_url, last_modified_at FROM contacts WHERE pull_id=? AND kept=1",
                  (pull_id,)):
        chance = None
        for fn, t in zip(levels, tables):
            n, e = t.get(fn(c), (0, 0))
            if n >= need:
                chance = round(100.0 * e / n, 1)
                break
        db.x("UPDATE contacts SET email_chance=? WHERE id=?", (chance, c["id"]))
    return True


# ---------------------------------------------------------------- research choice
def _balance_now():
    try:
        return seamless.test_connection().get("balance")
    except seamless.SeamlessError:
        return None


def confirm_research(pull_id, choice, first_n=None, chance_min=None):
    pull = db.get_pull(pull_id)
    if pull["status"] != "collected":
        raise PullError("Collect the list first.")
    _check_spending()
    rows = db.q("""SELECT id, fit, email_chance FROM contacts WHERE pull_id=? AND kept=1 AND selected=1
                   AND submitted_at IS NULL ORDER BY id""", (pull_id,))
    if choice == "high":
        chosen = [r["id"] for r in rows if (r["fit"] or "high") == "high"]
    elif choice == "high_medium":
        chosen = [r["id"] for r in rows if r["fit"] != "low"]
    elif choice == "first_n":
        chosen = [r["id"] for r in rows][: max(0, int(first_n or 0))]
    elif choice == "chance":
        m = cfg.EMAIL_CHANCE_MIN if chance_min in (None, "") else float(chance_min)
        if all(r["email_chance"] is None for r in rows):
            raise PullError("Not enough history yet to judge email chance. Pick another option for now.")
        chosen = [r["id"] for r in rows if r["email_chance"] is not None and r["email_chance"] >= m]
    else:
        chosen = [r["id"] for r in rows]
    n = len(chosen)
    if not n:
        raise PullError("Nobody is selected for research.")
    left = budget_left()
    if left is not None and n > left:
        raise PullError(f"Researching {n} people needs {n} credits, but only {left} are left in this month's budget.")
    bal = db.get_setting("credit_balance")
    if bal is not None and n > bal:
        raise PullError(f"Researching {n} people needs {n} credits, but your Seamless balance is {bal}.")
    chosen_set = set(chosen)
    for r in rows:
        if r["id"] not in chosen_set:
            db.x("UPDATE contacts SET selected=0 WHERE id=?", (r["id"],))
    already = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (pull_id,))["n"]
    test = cfg.TEST_BATCH_SIZE if (cfg.TEST_BATCH_SIZE and n > cfg.TEST_BATCH_SIZE and not already) else 0
    db.update_pull(pull_id, status="researching", stop_reason=None, error=None, test_size=test,
                   test_state="running" if test else None, test_rate=None,
                   bal_before=_balance_now() if not already else pull.get("bal_before"), bal_after=None)
    return db.get_pull(pull_id)


# ---------------------------------------------------------------- pause / resume / stop
def pause(pull_id):
    pull = db.get_pull(pull_id)
    if pull["status"] in ACTIVE:
        db.update_pull(pull_id, status="paused", paused_from=pull["status"])


def resume(pull_id):
    pull = db.get_pull(pull_id)
    if pull["status"] == "paused":
        _check_spending()
        fields = {"status": pull["paused_from"] or "collecting", "error": None, "stop_reason": None}
        if pull.get("test_state") == "failed":
            fields["test_state"] = "accepted"
        db.update_pull(pull_id, **fields)


def cancel(pull_id):
    """Stop spending on this pull. Collecting: keep what's collected. Researching: send nobody else;
    people already sent (already paid for) still come back."""
    pull = db.get_pull(pull_id)
    phase = pull["paused_from"] if pull["status"] == "paused" else pull["status"]
    if phase == "collecting":
        _finish_collect(pull_id, "Stopped by you.")
        return
    if phase == "researching":
        db.x("UPDATE contacts SET selected=0 WHERE pull_id=? AND submitted_at IS NULL AND kept=1", (pull_id,))
        fields = {"status": "researching", "stop_reason": "Stopped by you. Finishing people already sent.",
                  "error": None}
        if pull.get("test_state") in ("running", "failed"):
            fields["test_state"] = "stopped"
        db.update_pull(pull_id, **fields)


def stop_all():
    stopped = 0
    for r in db.q("SELECT id FROM pulls WHERE status IN ('collecting','researching','paused')"):
        cancel(r["id"])
        stopped += 1
    return stopped


# ---------------------------------------------------------------- background: collect
def step_collect(pull):
    pid = pull["id"]
    if not cfg.SPENDING_ENABLED:
        db.update_pull(pid, status="paused", paused_from="collecting", error=f"Paused: {SPENDING_OFF}")
        return
    kept = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1", (pid,))["n"]
    if kept >= pull["target"]:
        return _finish_collect(pid, "Reached your target.")
    if not pull["next_token"]:
        return _finish_collect(pid, "No more matching people in Seamless.")
    # Guard against paying for page after page of people who all get skipped.
    allowance = max(20, math.ceil(pull["target"] / 10) * cfg.SEARCH_SPEND_GUARD)
    if pull["search_credits"] >= allowance and kept < pull["target"]:
        return _finish_collect(pid, f"Stopped early: spent {pull['search_credits']} search credits but most results "
                                    f"were people you already have or skip. Loosen the filters for more.")
    left = budget_left()
    page = cfg.PAGE_SIZE
    if left is not None and left < math.ceil(page / 10):
        db.update_pull(pid, status="paused", paused_from="collecting",
                       error=f"Paused: {BUDGET_MSG}. Raise it, restart the app, then Resume.")
        return
    need = pull["target"] - kept
    limit = page if need > 20 else max(10, math.ceil(need * 1.5 / 10) * 10)
    rows, sup = seamless.search_contacts(to_search_body(pull["filters"]), min(limit, page),
                                         pull["next_token"], pid)
    kept_ids = _store_rows(pull, rows)          # stored even if stopped meanwhile: these were paid for
    more = sup.get("nextToken") if (rows and sup.get("isMore", True)) else None
    db.update_pull(pid, next_token=more, pages_fetched=pull["pages_fetched"] + 1,
                   rows_seen=pull["rows_seen"] + len(rows), total_matches=sup.get("total", pull["total_matches"]))
    _remember_position(pull, more, len(rows))
    note = score_contacts(db.get_pull(pid), kept_ids)
    if note:
        db.update_pull(pid, error=note)
    now_status = db.get_pull(pid)["status"]
    if now_status == "collected":               # Stop was pressed while this page was loading: trim to target
        _finish_collect(pid, db.get_pull(pid)["stop_reason"] or "Stopped by you.")


# ---------------------------------------------------------------- background: research
_last_poll = {}


def _same_person(c, contact):
    """True only when a research answer clearly describes this contact (LinkedIn, else name + company)."""
    if not contact:
        return False
    li_a, li_b = db.norm_li(c["li_url"]), db.norm_li(contact.get("lIProfileUrl") or contact.get("liProfileUrl") or "")
    if li_a and li_b:
        return li_a == li_b
    name = contact.get("fullName") or contact.get("name") or \
        f"{contact.get('firstName') or ''} {contact.get('lastName') or ''}".strip()
    comp = contact.get("companyName") or contact.get("company") or ""
    return bool(name and comp) and db.norm(name) == db.norm(c["name"]) and db.norm(comp) == db.norm(c["company"])


STATUS_RANK = {"valid": 3, "verified": 3, "deliverable": 3, "risky": 2, "catch-all": 2, "catchall": 2,
               "accept_all": 2, "unknown": 1, "": 1, "invalid": 0, "undeliverable": 0}


def _pct(v):
    try:
        return float(str(v or "").strip().rstrip("%") or 0)
    except ValueError:
        return 0.0


def pick_email(contact):
    """Seamless can return up to 3 work emails, each with a status and a confidence %.
    EMAIL_PICK=best (default): the best status (valid > risky/unknown > invalid), then the highest confidence,
    then Seamless's own primary as the tie-break. EMAIL_PICK=seamless: always Seamless's primary.
    Returns (email, status, confidence, which) — one email per person, never two."""
    primary = (contact.get("email") or "").strip()
    cands = []
    for i in (1, 2, 3):
        e = (contact.get(f"email{i}") or "").strip()
        if e and "@" in e and e.lower() not in [c[0].lower() for c in cands]:
            cands.append((e, contact.get(f"email{i}EmailAI"), contact.get(f"email{i}TotalAI"), f"email {i}"))
    if primary and "@" in primary and primary.lower() not in [c[0].lower() for c in cands]:
        cands.insert(0, (primary, None, None, "Seamless primary"))
    if not cands:
        return None, None, None, None
    def is_primary(c):
        return bool(primary) and c[0].lower() == primary.lower()
    if cfg.EMAIL_PICK == "seamless" and primary:
        c = next(c for c in cands if is_primary(c))
    else:
        c = max(cands, key=lambda c: (STATUS_RANK.get((c[1] or "").strip().lower(), 1), _pct(c[2]), is_primary(c)))
    which = "Seamless primary" if is_primary(c) else c[3]
    return c[0], c[1], c[2], which


def contact_fields(contact):
    """Map a Seamless contact record (research poll or org contacts) to our columns."""
    email, estatus, conf, which = pick_email(contact)
    fields = {"email": email, "email_status": estatus, "email_confidence": conf, "email_pick": which,
              "personal_email": contact.get("personalEmail"),
              "phone": contact.get("contactPhone1"), "phone_type": contact.get("contactPhone1DataType"),
              "company_phone": contact.get("companyPhone1"), "raw_research": json.dumps(contact)}
    for col, keys in (("first_name", ("firstName",)), ("last_name", ("lastName",)),
                      ("title", ("jobTitle", "title")), ("company", ("companyName", "company")),
                      ("li_url", ("lIProfileUrl", "liProfileUrl")), ("country", ("country",)),
                      ("city", ("city",)), ("timezone", ("timezone",))):
        for key in keys:
            if contact.get(key):
                fields[col] = contact[key]
                break
    if contact.get("companyDomain"):
        fields["domain"] = db.norm_domain(contact["companyDomain"])
    return fields


def _record_outcome(pull_id, cid):
    """After a person's final answer: update people memory and (only if emailed) company memory."""
    c = db.q1("SELECT * FROM contacts WHERE id=?", (cid,))
    st = (c["research_status"] or "").lower()
    if c["email"]:
        _mark_people(c, pull_id, "email")
        db.x("INSERT OR IGNORE INTO seen(key,pull_id,created_at,outcome,outcome_at) VALUES(?,?,?,?,?)",
             ("em:" + c["email"].lower(), pull_id, time.time(), "email", time.time()))
        _company_emailed(c["company"], c["domain"], pull_id, time.time())
    elif st in DUPLICATE_STATUSES:
        _mark_people(c, pull_id, "duplicate")
    else:
        _mark_people(c, pull_id, "no_email")


def _apply_result(pull_id, row):
    rid = row.get("requestId")
    req = db.q1("SELECT * FROM research_requests WHERE request_id=?", (rid,)) if rid else None
    sid = row.get("searchResultId")
    guessed = False
    if not sid:
        # No searchResultId in this answer. The position-based guess is only trusted if the person's
        # identity in the answer matches the person we sent; otherwise wait for a fuller answer.
        sid = (req or {}).get("guess_sid")
        guessed = True
    if not sid:
        return False
    c = db.q1("SELECT id, name, company, li_url FROM contacts WHERE pull_id=? AND search_result_id=?", (pull_id, sid))
    if not c:
        return False
    if guessed and not _same_person(c, row.get("contact") or {}):
        return False
    status = (row.get("status") or "").strip()
    st = status.lower()
    terminal = st in seamless.TERMINAL
    contact = row.get("contact") or {}
    if req and req.get("resolved_at"):
        return terminal          # already handled (duplicated row in the answer)
    fields = {"research_status": status, "research_message": row.get("message") or "", "request_id": rid}
    if contact:
        fields.update(contact_fields(contact))
    cols = ", ".join(f"{k}=?" for k in fields)
    db.x(f"UPDATE contacts SET {cols} WHERE id=?", (*fields.values(), c["id"]))
    if rid and terminal:
        db.x("UPDATE research_requests SET status=?, resolved_at=? WHERE request_id=? AND resolved_at IS NULL",
             (status, time.time(), rid))
        if st in DUPLICATE_STATUSES:
            # Logged as 1 credit at submit; Seamless's de-duplication normally doesn't charge these.
            db.log_credits(pull_id, "research", -1, None, "already researched before - normally not charged")
        _record_outcome(pull_id, c["id"])
    return terminal


def _test_batch_check(pull):
    """While a test batch runs: how many more may be sent now (None = no limit)."""
    if pull.get("test_state") != "running":
        return None
    sent = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (pull["id"],))["n"]
    return max(0, (pull["test_size"] or 0) - sent)


def _test_batch_verdict(pull):
    pid = pull["id"]
    pending = db.q1("SELECT COUNT(*) n FROM research_requests WHERE pull_id=? AND resolved_at IS NULL", (pid,))["n"]
    if pending:
        return
    r = db.q1(f"""SELECT COUNT(*) n, SUM(CASE WHEN email IS NOT NULL AND email!='' THEN 1 ELSE 0 END) e
                  FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                  AND LOWER(research_status) NOT IN ({",".join("?" * len(DUPLICATE_STATUSES))})""",
              (pid, *DUPLICATE_STATUSES))
    n, e = r["n"] or 0, r["e"] or 0
    rate = (100.0 * e / n) if n else 100.0
    if n and rate < cfg.TEST_BATCH_MIN_EMAIL_RATE:
        left = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND selected=1 AND submitted_at IS NULL",
                     (pid,))["n"]
        db.update_pull(pid, status="paused", paused_from="researching", test_state="failed", test_rate=rate,
                       error=(f"Test batch: only {e} of {n} had an email ({rate:.0f}%, below your "
                              f"{cfg.TEST_BATCH_MIN_EMAIL_RATE}% level). {left:,} people are waiting and nothing more "
                              f"has been spent. Continue anyway, or Stop and change the filters."))
    else:
        db.update_pull(pid, test_state="passed", test_rate=rate)


_saved_cache = {}


def _saved_index():
    """Your org's saved Seamless contacts (free to read), indexed for strict matching. Cached 10 minutes."""
    if _saved_cache.get("at", 0) > time.time() - 600:
        return _saved_cache["idx"]
    end = time.time()
    start = end - cfg.ORG_LOOKBACK_DAYS * 86400
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    by_li, by_nc, read = {}, {}, 0
    for page in range(1, cfg.ORG_LOOKUP_MAX_PAGES + 1):
        if _fill_stop.get("all"):
            break
        rows = seamless.org_contacts(iso(start), iso(end), page)
        if not rows:
            break
        read += len(rows)
        for r in rows:
            li = db.norm_li(r.get("lIProfileUrl") or r.get("liProfileUrl") or "")
            if li:
                by_li.setdefault(li, []).append(r)
            name = r.get("fullName") or r.get("fullname") or r.get("name") or \
                f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip()
            comp = r.get("companyName") or r.get("company") or ""
            if name and comp:
                by_nc.setdefault(db.norm(name) + "|" + db.norm(comp), []).append(r)
        if len(rows) < 500:
            break
    idx = {"li": by_li, "nc": by_nc, "read": read}
    _saved_cache.update(at=time.time(), idx=idx)
    return idx


def _saved_match(idx, c):
    """Strict: exact LinkedIn with one consistent email, else exact name+company with exactly one record."""
    li = db.norm_li(c["li_url"])
    if li and idx["li"].get(li):
        emails = {(r.get("email") or r.get("email1") or "").lower() for r in idx["li"][li]}
        if len(emails) == 1:
            return idx["li"][li][0]
    cands = idx["nc"].get(db.norm(c["name"]) + "|" + db.norm(c["company"]), [])
    return cands[0] if len(cands) == 1 else None


def _fill_free(pull_id, c, record, now):
    f = contact_fields(record)
    if not f.get("email"):
        return False
    f.update(research_message="Filled in from your Seamless saved contacts (no charge).", filled_free=1)
    cols = ", ".join(f"{k}=?" for k in f)
    db.x(f"UPDATE contacts SET {cols} WHERE id=? AND pull_id=?", (*f.values(), c["id"], pull_id))
    return True


def step_research(pull):
    pid = pull["id"]
    # 1. submit the next batch (unless spending is off, a test batch is full, or nobody is waiting)
    room = _test_batch_check(pull)
    limit = cfg.RESEARCH_BATCH if room is None else min(cfg.RESEARCH_BATCH, room)
    todo = []
    if limit > 0 and cfg.SPENDING_ENABLED:
        todo = db.q("""SELECT id, search_result_id, name, company, domain, li_url, retry FROM contacts
                       WHERE pull_id=? AND kept=1 AND selected=1 AND request_id IS NULL AND submitted_at IS NULL
                       ORDER BY id LIMIT ?""", (pid, limit))
    if todo:
        # Re-check right before paying: another pull may have taken the same person or company meanwhile,
        # or the block list may have changed.
        fresh = []
        for t in todo:
            reason, retry = people_reason(t["name"], t["company"], t["li_url"])
            reason = reason or blocked_company(t["company"], t["domain"]) or _cooldown_reason(t, pull)
            if reason:
                db.x("UPDATE contacts SET kept=0, selected=0, skip_reason=? WHERE id=?", (reason, t["id"]))
            else:
                t["retry"] = 1 if retry else 0
                fresh.append(t)
        todo = fresh
    if todo:
        left = budget_left()
        if left is not None and left < len(todo):
            todo = todo[:left]
            if not todo:
                db.update_pull(pid, status="paused", paused_from="researching",
                               error=f"Paused: {BUDGET_MSG}. Raise it, restart the app, then Resume.")
                return
        now = time.time()
        # Paid retries: first try the free saved-contacts lookup.
        retries = [t for t in todo if t["retry"]]
        if retries:
            try:
                idx = _saved_index()
            except seamless.SeamlessError:
                idx = None
            for t in retries:
                rec = _saved_match(idx, t) if idx else None
                if rec and _fill_free(pid, t, rec, now):
                    db.x("UPDATE contacts SET submitted_at=?, research_status='done' WHERE id=?", (now, t["id"]))
                    _mark_people(t, pid, "pending", now)
                    _company_tried(t["company"], t["domain"], pid, now)
                    _record_outcome(pid, t["id"])
                    t["done"] = True
        groups = [([t for t in todo if not t["retry"]], False), ([t for t in todo if t["retry"] and not t.get("done")], True)]
        for group, skip_dedup in groups:
            if not group:
                continue
            sids = [t["search_result_id"] for t in group]
            try:
                request_ids = seamless.research_contacts(sids, pid, skip_dedup=skip_dedup)
            except seamless.SeamlessError as e:
                if e.code == "unconfirmed":
                    db.update_pull(pid, status="paused", paused_from="researching", error=(
                        f"Paused: {e} while sending {len(sids)} people for research, so the tool did not resend them "
                        f"(that could charge twice). Check your Seamless balance. Resume sends them again; anyone "
                        f"Seamless already researched comes back as “already researched” and is normally not charged "
                        f"again — use “Fill in from Seamless saved contacts” afterwards to get their details free."))
                    return
                raise
            now = time.time()
            for t in group:
                db.x("UPDATE contacts SET submitted_at=?, research_status='queued' WHERE id=?", (now, t["id"]))
                _mark_people(t, pid, "pending", now)
                _company_tried(t["company"], t["domain"], pid, now)
            # Provisional link by position, used only if a poll row comes back without its searchResultId.
            same_len = len(request_ids) == len(sids)
            for i, rid in enumerate(request_ids):
                db.x("INSERT OR IGNORE INTO research_requests(request_id,pull_id,guess_sid,status,created_at) "
                     "VALUES(?,?,?,?,?)", (rid, pid, sids[i] if same_len else None, "queued", now))
            if not request_ids:
                for t in group:
                    db.x("UPDATE contacts SET research_status='error', research_message=? WHERE id=?",
                         ("Seamless did not accept this person for research.", t["id"]))
                    _record_outcome(pid, t["id"])
    # 2. poll what is pending (free; continues even when spending is switched off)
    every = cfg.POLL_SECONDS if POLL_EVERY is None else POLL_EVERY
    if time.time() - _last_poll.get(pid, 0) >= every:
        _last_poll[pid] = time.time()
        pending = db.q("SELECT request_id, created_at FROM research_requests WHERE pull_id=? AND resolved_at IS NULL "
                       "ORDER BY created_at LIMIT ?", (pid, POLL_CHUNK))
        if pending:
            rows = seamless.poll_research([p["request_id"] for p in pending])
            for row in rows:
                _apply_result(pid, row)
            now = time.time()
            for p in pending:
                if now - p["created_at"] > PENDING_TIMEOUT:
                    req = db.q1("SELECT * FROM research_requests WHERE request_id=? AND resolved_at IS NULL",
                                (p["request_id"],))
                    if not req:
                        continue
                    db.x("UPDATE research_requests SET status='timed out', resolved_at=? WHERE request_id=?",
                         (now, p["request_id"]))
                    c = db.q1("SELECT id FROM contacts WHERE pull_id=? AND search_result_id=? AND "
                              "research_status IN ('queued','researching')", (pid, req["guess_sid"]))
                    if c:
                        db.x("UPDATE contacts SET research_status='timed out', research_message=? WHERE id=?",
                             ("Seamless took too long to answer.", c["id"]))
                        _record_outcome(pid, c["id"])
    pull = db.get_pull(pid)
    if pull["status"] != "researching":
        return
    # 3. test batch finished?
    if pull.get("test_state") == "running" and _test_batch_check(pull) == 0:
        _test_batch_verdict(pull)
        pull = db.get_pull(pid)
        if pull["status"] != "researching":
            return
    # 4. finished?
    left_to_submit = db.q1("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND selected=1
                              AND submitted_at IS NULL""", (pid,))["n"]
    pending_n = db.q1("SELECT COUNT(*) n FROM research_requests WHERE pull_id=? AND resolved_at IS NULL", (pid,))["n"]
    if not pending_n and left_to_submit and not cfg.SPENDING_ENABLED:
        db.update_pull(pid, status="paused", paused_from="researching", error=f"Paused: {SPENDING_OFF}")
        return
    if pull.get("test_state") == "running" and not left_to_submit and not pending_n:
        _test_batch_verdict(pull)
        pull = db.get_pull(pid)
        if pull["status"] != "researching":
            return
    if not left_to_submit and not pending_n:
        for c in db.q("""SELECT id FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                         AND research_status IN ('queued','researching')""", (pid,)):
            db.x("UPDATE contacts SET research_status='no answer', research_message=? WHERE id=?",
                 ("Seamless returned no result.", c["id"]))
            _record_outcome(pid, c["id"])
        db.update_pull(pid, status="done", finished_at=time.time(), bal_after=_balance_now(),
                       stop_reason=pull["stop_reason"] if pull["stop_reason"] else None)


# ---------------------------------------------------------------- loop
_failures = {}


def tick():
    for pull in [db.get_pull(r["id"]) for r in db.q(
            "SELECT id FROM pulls WHERE status IN ('collecting','researching') ORDER BY id")]:
        try:
            if pull["status"] == "collecting":
                step_collect(pull)
            else:
                step_research(pull)
            _failures.pop(pull["id"], None)
        except seamless.SeamlessError as e:
            if e.code in STOP_CODES:
                db.update_pull(pull["id"], status="paused", paused_from=pull["status"], error=f"Paused: {e}")
            elif e.code == "rateLimited":
                continue
            else:
                _fail(pull, e)
        except Exception as e:  # never let one pull kill the loop
            log.exception("pull %s step failed", pull["id"])
            _fail(pull, e)


def _fail(pull, e):
    n = _failures.get(pull["id"], 0) + 1
    _failures[pull["id"]] = n
    if n >= 5:
        db.update_pull(pull["id"], status="paused", paused_from=pull["status"],
                       error=f"Paused after repeated errors: {e}")
        _failures.pop(pull["id"], None)


class Worker(threading.Thread):
    def __init__(self, interval=1.0):
        super().__init__(daemon=True, name="leadpuller-worker")
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                tick()
            except Exception:
                log.exception("worker tick failed")
            self.stop_event.wait(self.interval)

    def stop(self):
        self.stop_event.set()


# ---------------------------------------------------------------- fill in "already researched" people (free)
_fill_stop = {}
_fill_running = {}


def fill_from_saved(pull_id):
    """Seamless doesn't return people your org researched before. Their records are in your org's saved
    contacts (free to read). Match strictly so no one gets someone else's details. Stoppable."""
    if _fill_running.get(pull_id):
        raise PullError("Fill-in is already running for this pull.")
    want = db.q(f"""SELECT id, name, first_name, last_name, company, domain, li_url FROM contacts WHERE pull_id=?
                    AND submitted_at IS NOT NULL AND LOWER(research_status) IN ({",".join("?" * len(DUPLICATE_STATUSES))})
                    AND (email IS NULL OR email='')""", (pull_id, *DUPLICATE_STATUSES))
    if not want:
        return {"checked": 0, "filled": 0, "saved_contacts_read": 0, "stopped": False}
    _fill_running[pull_id] = True
    _fill_stop.pop("all", None)
    _saved_cache.clear()
    try:
        idx = _saved_index()
        stopped = bool(_fill_stop.pop("all", None))
        filled = 0
        now = time.time()
        for c in want:
            rec = _saved_match(idx, c)
            if rec and _fill_free(pull_id, c, rec, now):
                _record_outcome(pull_id, c["id"])
                filled += 1
        return {"checked": len(want), "filled": filled, "saved_contacts_read": idx["read"], "stopped": stopped}
    finally:
        _fill_running.pop(pull_id, None)
        _saved_cache.clear()


def stop_fill():
    _fill_stop["all"] = True
