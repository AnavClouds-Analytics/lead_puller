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

from . import accounts, claude, db, dnscheck, seamless
from .config import cfg
from .filters import NATIVE_EXCLUDES, search_filter_count, to_search_body

log = logging.getLogger("leadpuller")

# Sizes and timings come from .env (see app/config.py). POLL_EVERY can be overridden by tests.
POLL_EVERY = None
POLL_CHUNK = 100
US_ALIASES = {"united states", "us", "usa", "united states of america"}
ACTIVE = ("collecting", "researching")
DUPLICATE_STATUSES = ("duplicate", "contact-already-researched")
NO_EMAIL_STATUSES = ("done", "not found", "missing", "no answer", "error", "timed out",
                     "no license or credits available")
STOP_CODES = {"insufficientCredits", "missingLicense", "badKey", "noKey"}
ACCOUNT_CODES = {"insufficientCredits", "missingLicense", "badKey", "noKey"}
OPEN_STATUSES = ("collecting", "collected", "researching", "paused")
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
# Every skip reason belongs to a kind, so the skip report can add them up and suggest a fix.
SKIP_KINDS = [  # (prefix of the reason text, kind, label, what to change)
    ("No company website", "no_website", "No company website",
     "These have no company website, so no work email. Keep “Must have a company website” on: it saves research credits."),
    ("No LinkedIn", "no_linkedin", "No LinkedIn profile", "Untick “Must have a LinkedIn profile” if you want them."),
    ("Title doesn't contain", "title_missing", "Title doesn't contain your words",
     "Seamless matched titles loosely. Tick “Exact title match”, or add the titles you do want to Job titles."),
    ("Title contains", "title_word", "Title contains a skip word",
     "Narrow Job titles or Seniority so Seamless doesn't return these."),
    ("Company contains", "company_word", "Company name contains a skip word",
     "Seamless can't exclude company names. Narrow Industries instead."),
    ("Industry ", "industry", "Skipped industry", "Pick the industries you want in Industries instead of skipping others."),
    ("Domain on this pull", "domain_skip", "Domain on this pull's skip list", ""),
    ("Country ", "country", "Skipped country", ""),
    ("State ", "state", "Skipped state", ""),
    ("Seniority ", "seniority", "Skipped seniority", "Choose the seniority levels you want in Seniority instead."),
    ("In current role", "role_time", "Time in current role", "Loosen the time-in-role limits."),
    ("Duplicate inside", "dup_pull", "Same person twice in Seamless's results", ""),
    ("Blocked company", "blocked", "Blocked company", "On your blocked-companies list. Seamless can't exclude them itself."),
    ("On your do-not-pull", "do_not_pull", "On your do-not-pull list", ""),
    ("Pulled before, no email", "no_email_before", "Pulled before (no email then)",
     "Same people as an earlier pull. Change the filters (another country, size band or industry) rather than "
     "repeating a search you've already worked through."),
    ("Already pulled before", "pulled_before", "Already pulled before",
     "Same people as an earlier pull. Change the filters (another country, size band or industry) rather than "
     "repeating a search you've already worked through."),
    ("In another open pull", "other_pull", "Already in another open pull", "Finish, stop or delete that other pull first."),
    ("Company emailed", "cooldown", "Company emailed recently (cooldown)",
     "Lower “Skip companies pulled in the last … days” for this pull, or target other companies."),
    ("Company limit", "company_cap", "More than your limit per company",
     "Raise “Max people per company”, or choose smaller company sizes (big companies return many people each)."),
    ("Company domain can't", "no_mail_server", "Company can't receive email", ""),
    ("Over your target", "over_target", "Beyond your target", "Collected past your target; not researched."),
]


def skip_kind(reason):
    for prefix, kind, _, _ in SKIP_KINDS:
        if (reason or "").startswith(prefix):
            return kind
    return "other" if reason else None


def _skip(cid, reason):
    db.x("UPDATE contacts SET kept=0, selected=0, skip_reason=?, skip_kind=? WHERE id=?", (reason, skip_kind(reason), cid))


def skip_report(pull_id):
    """Why people were skipped, and roughly how many search credits each reason cost."""
    pull = db.get_pull(pull_id)
    rows_seen = max(1, pull["rows_seen"] or 0)
    per_row = (pull["search_credits"] or 0) / rows_seen
    info = {k: (label, tip) for _, k, label, tip in SKIP_KINDS}
    out = []
    for r in db.q("""SELECT COALESCE(skip_kind,'other') k, COUNT(*) n, MIN(skip_reason) example FROM contacts
                     WHERE pull_id=? AND kept=0 GROUP BY 1 ORDER BY n DESC""", (pull_id,)):
        label, tip = info.get(r["k"], (r["example"] or "Other", ""))
        out.append({"kind": r["k"], "label": label, "n": r["n"], "credits": round(r["n"] * per_row, 1),
                    "tip": tip, "example": r["example"]})
    return out


def _open_pull_keys(pull_id):
    """People kept (not yet researched) in other open pulls: they're held there, so don't collect them twice."""
    keys = {}
    since = time.time() - cfg.OPEN_PULL_HOLD_DAYS * 86400
    if not cfg.OPEN_PULL_HOLD_DAYS:
        return keys
    for r in db.q(f"""SELECT c.name, c.company, c.li_url, c.pull_id FROM contacts c JOIN pulls p ON p.id=c.pull_id
                      WHERE c.pull_id!=? AND c.kept=1 AND c.selected=1 AND c.submitted_at IS NULL
                      AND p.status IN ({",".join("?" * len(OPEN_STATUSES))}) AND p.updated_at>=?""",
                  (pull_id, *OPEN_STATUSES, since)):
        for k in db.person_keys(r["name"], r["company"], r["li_url"]):
            keys[k] = r["pull_id"]
    return keys


def _skip_reason(c, pull, company_counts, in_pull_keys, open_keys=None):
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
    held = next((open_keys[k] for k in keys if open_keys and k in open_keys), None)
    if held:
        return f"In another open pull (#{held}), not researched yet", False
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
    open_keys = _open_pull_keys(pull["id"])
    kept_ids = []
    now = time.time()
    for row in rows:
        c = contact_from_search(row)
        if not c["search_result_id"]:
            continue
        reason, retry = _skip_reason(c, pull, company_counts, in_pull_keys, open_keys)
        cid = db.x(f"""INSERT INTO contacts(pull_id, {", ".join(COLS)}, kept, skip_reason, skip_kind, selected, retry,
                       raw_search, created_at) VALUES(?, {", ".join("?" * len(COLS))}, ?,?,?,?,?,?,?)""",
                   (pull["id"], *[c[k] for k in COLS], 0 if reason else 1, reason, skip_kind(reason),
                    0 if reason else 1, 1 if retry else 0, json.dumps(row), now))
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


def budget_left(account=None):
    """Credits that may still be spent (None = no limit): the overall monthly budget, the account's own
    monthly budget and its balance above ACCOUNT_RESERVE, whichever is smallest."""
    if account is not None:
        return accounts.usable(accounts.get(account["id"]) or account)
    budget = cfg.MONTHLY_CREDIT_BUDGET
    if not budget:
        return None
    return max(0, int(budget) - db.month_spend())


# ---------------------------------------------------------------- search memory
def search_hash(filters):
    return hashlib.sha1(json.dumps(to_search_body(filters), sort_keys=True).encode()).hexdigest()[:20]


def _remember_position(pull, next_token, rows_count, reset=False):
    """Save where this search got to, so the next run of the same filters starts there (whichever account)."""
    h = pull.get("search_hash") or search_hash(pull["filters"])
    now = time.time()
    db.x("""INSERT INTO searches(hash, filters_json, cursor, cursor_pull_id, cursor_at, results_seen, exhausted_at,
                                 runs, last_pull_id, updated_at, cursor_account_id)
            VALUES(?,?,?,?,?,?,?,0,?,?,?)
            ON CONFLICT(hash) DO UPDATE SET cursor=excluded.cursor, cursor_pull_id=excluded.cursor_pull_id,
              cursor_at=excluded.cursor_at,
              results_seen=CASE WHEN ? THEN excluded.results_seen ELSE searches.results_seen + excluded.results_seen END,
              exhausted_at=excluded.exhausted_at, last_pull_id=excluded.last_pull_id, updated_at=excluded.updated_at,
              cursor_account_id=excluded.cursor_account_id""",
         (h, json.dumps(to_search_body(pull["filters"]), sort_keys=True), next_token, pull["id"], now, rows_count,
          None if next_token else now, pull["id"], now, pull.get("account_id"), 1 if reset else 0))


# ---------------------------------------------------------------- preview / collect
def _check_spending():
    if not cfg.SPENDING_ENABLED:
        raise PullError(SPENDING_OFF + " Ask your developer to set it to 1 and restart.")


def _account(pull, need=1):
    try:
        return accounts.for_pull(pull, need)
    except accounts.NoAccount as e:
        raise PullError(str(e))


def run_preview(pull_id, from_top=False):
    pull = db.get_pull(pull_id)
    if not pull:
        raise PullError("Pull not found.")
    if pull["status"] not in ("draft", "previewed", "collected", "cancelled"):
        raise PullError("This pull is already running.")
    if pull["status"] == "collected" and db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL",
                                               (pull_id,))["n"]:
        raise PullError("Research has started on this pull. Use “Edit filters (new copy)” for a new preview.")
    if not search_filter_count(pull["filters"]):
        raise PullError("Add at least one search filter (title, seniority, industry, country...) first.")
    _check_spending()
    cost = math.ceil(cfg.PREVIEW_SIZE / 10)
    if (pull.get("account_choice") or "auto") == "auto":
        db.update_pull(pull_id, account_id=None)       # nothing paid for yet: pick the best account again
        pull["account_id"] = None
    acct = _account(pull, cost)
    room = budget_left(acct)
    if room is not None and room < cost:
        raise PullError(accounts.no_account_message(cost) if (pull.get("account_choice") or "auto") == "auto" else
                        f"{acct['name']} doesn't have {cost} credits to spare (balance, ACCOUNT_RESERVE or monthly budget).")
    filters, loc_notes = (dict(pull["filters"]), []) if pull["filters"].get("savedSearchId") else \
        resolve_locations(dict(pull["filters"]))
    notes = [n for n in pull["notes"] if not n.startswith(("Place “", "Seamless doesn't recognise", "Location “"))]
    notes += loc_notes
    db.x("DELETE FROM contacts WHERE pull_id=?", (pull_id,))
    db.x("DELETE FROM collect_pages WHERE pull_id=?", (pull_id,))
    db.update_pull(pull_id, filters=filters, notes=notes, next_token=None, pages_fetched=0, rows_seen=0,
                   total_matches=None, stop_reason=None, error=None, pause_kind=None, est_search_credits=None,
                   spend_cap=None, keep_ack_page=0)
    h = search_hash(filters)
    db.update_pull(pull_id, search_hash=h, from_top=1 if from_top else 0, started_from=None)
    pull = db.get_pull(pull_id)

    # Same filters as an earlier pull (with any account)? Start where that one stopped instead of paying again.
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
            rows, sup = seamless.search_contacts(to_search_body(filters), cfg.PREVIEW_SIZE, start, pull_id, account=acct)
        except seamless.SeamlessError as e:
            if e.code in STOP_CODES:
                raise
            rows = []
        if not rows:
            start = None
            started_from = ("Seamless no longer accepted the saved position for this search (it expired, or it was "
                            "made with another Seamless account), so it starts from the top. People already pulled "
                            "are skipped.")
    if not start:
        rows, sup = seamless.search_contacts(to_search_body(filters), cfg.PREVIEW_SIZE, None, pull_id, account=acct)
    kept = _store_rows(pull, rows)
    fit_note = score_contacts(pull, kept)
    nxt = sup.get("nextToken") if sup.get("isMore", True) else None
    db.update_pull(pull_id, status="previewed", total_matches=sup.get("total", len(rows)), next_token=nxt,
                   pages_fetched=1, rows_seen=len(rows), error=fit_note, started_from=started_from)
    _remember_position(db.get_pull(pull_id), nxt, len(rows), reset=start is None)
    db.x("UPDATE searches SET runs=runs+1 WHERE hash=?", (h,))
    return db.get_pull(pull_id)


def _keep_rate(pull_id):
    """Share of search results kept so far in this pull (what the preview sample tells us)."""
    r = db.q1("""SELECT COUNT(*) n, SUM(CASE WHEN kept=1 OR skip_kind='over_target' THEN 1 ELSE 0 END) k
                 FROM contacts WHERE pull_id=?""", (pull_id,))
    if not r["n"]:
        return None
    return (r["k"] or 0) / r["n"]


def estimate_collect(pull_id, target):
    """Search credits to reach the target, from the preview's keep rate. Research is 1 credit per person on top."""
    pull = db.get_pull(pull_id)
    target = max(0, int(target or 0))
    kept = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1", (pull_id,))["n"]
    rate = _keep_rate(pull_id)
    need = max(0, target - kept)
    if rate is None or rate <= 0:
        more = None
    else:
        more = math.ceil(need / rate / 10)
    if more is not None and pull.get("total_matches") is not None:
        left_in_seamless = max(0, (pull["total_matches"] or 0) - (pull["rows_seen"] or 0))
        more = min(more, math.ceil(left_in_seamless / 10))
    acct = accounts.get(pull.get("account_id"))
    room = budget_left(acct) if acct else None
    total = None if more is None else more + target
    out = {"target": target, "kept": kept, "keep_rate": None if rate is None else round(rate * 100, 1),
           "search_credits": more, "research_credits": target, "total": total,
           "cap": None if more is None else max(math.ceil(more * cfg.COLLECT_SPEND_CAP_PCT / 100), more + 5),
           "account": acct["name"] if acct else None, "account_id": acct["id"] if acct else None, "usable": room,
           "short": bool(total is not None and room is not None and room < total), "others": []}
    if out["short"]:
        out["others"] = accounts.others_with_room(acct["id"], total)
    return out


def start_collect(pull_id, target, cap):
    pull = db.get_pull(pull_id)
    if pull["status"] not in ("previewed", "collected"):
        raise PullError("Preview the pull first.")
    _check_spending()
    target = int(target)
    if target < 1:
        raise PullError("Enter how many people you want.")
    db.update_pull(pull_id, target=target, per_company_cap=int(cap or 0), stop_reason=None, error=None, pause_kind=None)
    if int(cap or 0) != (pull["per_company_cap"] or 0):
        _reapply_cap(pull_id, int(cap or 0))
    db.x("UPDATE contacts SET kept=1, selected=1, skip_reason=NULL, skip_kind=NULL WHERE pull_id=? "
         "AND skip_reason='Over your target' AND submitted_at IS NULL", (pull_id,))
    kept = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1", (pull_id,))["n"]
    if kept >= target or not pull["next_token"]:
        _finish_collect(pull_id, "Reached your target." if kept >= target else "No more matching people in Seamless.")
        return db.get_pull(pull_id)
    est = estimate_collect(pull_id, target)
    more = est["search_credits"] if est["search_credits"] is not None else math.ceil((target - kept) / 10) * 3
    spent = pull["search_credits"] or 0
    db.update_pull(pull_id, status="collecting", est_search_credits=more, collect_started_credits=spent,
                   spend_cap=spent + max(math.ceil(more * cfg.COLLECT_SPEND_CAP_PCT / 100), more + 5),
                   keep_ack_page=db.q1("SELECT COALESCE(MAX(id),0) m FROM collect_pages WHERE pull_id=?",
                                       (pull_id,))["m"])
    return db.get_pull(pull_id)


def _reapply_cap(pull_id, cap):
    if not cap:
        db.x("UPDATE contacts SET kept=1, selected=1, skip_reason=NULL, skip_kind=NULL WHERE pull_id=? "
             "AND skip_reason LIKE 'Company limit%'", (pull_id,))
        return
    counts = {}
    for r in db.q("SELECT id, company, domain, kept, skip_reason FROM contacts WHERE pull_id=? ORDER BY id", (pull_id,)):
        if not r["kept"] and not (r["skip_reason"] or "").startswith("Company limit"):
            continue
        ck = db.company_key(r["company"], r["domain"])
        counts[ck] = counts.get(ck, 0) + 1
        if ck and counts[ck] > cap:
            _skip(r["id"], f"Company limit reached ({cap} per company)")
        elif (r["skip_reason"] or "").startswith("Company limit"):
            db.x("UPDATE contacts SET kept=1, selected=1, skip_reason=NULL, skip_kind=NULL WHERE id=?", (r["id"],))


def _finish_collect(pull_id, reason):
    pull = db.get_pull(pull_id)
    ids = [r["id"] for r in db.q("SELECT id FROM contacts WHERE pull_id=? AND kept=1 ORDER BY id", (pull_id,))]
    if pull["target"] and len(ids) > pull["target"]:
        extra = ids[pull["target"]:]
        marks = ",".join("?" * len(extra))
        db.x(f"UPDATE contacts SET kept=0, selected=0, skip_reason='Over your target', skip_kind='over_target' "
             f"WHERE id IN ({marks})", extra)
    db.update_pull(pull_id, status="collected", stop_reason=reason)
    compute_email_chances(pull_id)


# ---------------------------------------------------------------- email chance (learned from your own pulls)
def _fresh(c):
    age = _date_age_months(c.get("last_modified_at"))
    return "unknown" if age is None else ("fresh" if age <= 12 else "old")


def compute_email_chances(pull_id):
    """Each person's chance of an email, from people of the same kind you researched before.
    Always at least the same country: uses the most specific group with enough history
    (EMAIL_CHANCE_MIN_SAMPLES). No guess from other countries: without enough history it stays empty (—)."""
    need = cfg.EMAIL_CHANCE_MIN_SAMPLES
    hist = db.q(f"""SELECT country, size, li_url, last_modified_at, email, email_status FROM contacts
                    WHERE submitted_at IS NOT NULL AND pull_id != ? AND COALESCE(filled_free,0)=0
                    AND LOWER(research_status) IN ('done','not found','missing','no answer')""", (pull_id,))
    has_li = lambda c: bool(db.norm_li(c["li_url"]))
    levels = [  # (key function, how to describe it)
        (lambda c: ("csl", c["country"], c["size"], has_li(c)),
         lambda c, n: f"{n:,} {c['country']} people at {c['size']}-employee companies{' with' if has_li(c) else ' without'} LinkedIn"),
        (lambda c: ("cs", c["country"], c["size"]),
         lambda c, n: f"{n:,} {c['country']} people at {c['size']}-employee companies"),
        (lambda c: ("cl", c["country"], has_li(c), _fresh(c)),
         lambda c, n: f"{n:,} {c['country']} people{' with' if has_li(c) else ' without'} LinkedIn"),
        (lambda c: ("c", c["country"]), lambda c, n: f"{n:,} {c['country']} people"),
    ]
    tables = []
    for fn, _ in levels:
        t = {}
        for h in hist:
            if not h["country"]:
                continue
            a = t.setdefault(fn(h), [0, 0])
            a[0] += 1
            a[1] += 1 if (h["email"] and (h["email_status"] or "").lower() != "invalid") else 0
        tables.append(t)
    known = False
    for c in db.q("SELECT id, country, size, li_url, last_modified_at FROM contacts WHERE pull_id=? AND kept=1",
                  (pull_id,)):
        chance, n_used, basis = None, None, None
        if c["country"]:
            for (fn, describe), t in zip(levels, tables):
                if fn(c)[0] in ("csl", "cs") and not c["size"]:
                    continue
                n, e = t.get(fn(c), (0, 0))
                if n >= need:
                    chance, n_used, basis = round(100.0 * e / n, 1), n, "based on " + describe(c, n)
                    break
        known = known or chance is not None
        db.x("UPDATE contacts SET email_chance=?, email_chance_n=?, email_chance_basis=? WHERE id=?",
             (chance, n_used, basis, c["id"]))
    return known


# ---------------------------------------------------------------- research choice
def _balance_now(pull=None):
    acct = accounts.get((pull or {}).get("account_id")) if pull else None
    if not acct:
        return None
    try:
        return seamless.test_connection(acct).get("balance")
    except seamless.SeamlessError:
        return None


def choice_ids(pull_id, choice, first_n=None, chance_min=None):
    """Who a research choice covers, among the people still ticked. Same rule for the count shown on screen
    and for what is actually sent, so the two can never differ."""
    rows = db.q("""SELECT id, fit, email_chance FROM contacts WHERE pull_id=? AND kept=1 AND selected=1
                   AND submitted_at IS NULL ORDER BY id""", (pull_id,))
    if choice == "high":
        return [r["id"] for r in rows if (r["fit"] or "high") == "high"]
    if choice == "high_medium":
        return [r["id"] for r in rows if r["fit"] != "low"]
    if choice == "first_n":
        return [r["id"] for r in rows][: max(0, int(first_n or 0))]
    if choice == "chance":
        m = cfg.EMAIL_CHANCE_MIN if chance_min in (None, "") else float(chance_min)
        return [r["id"] for r in rows if r["email_chance"] is not None and r["email_chance"] >= m]
    return [r["id"] for r in rows]


def confirm_research(pull_id, choice, first_n=None, chance_min=None, expected=None):
    pull = db.get_pull(pull_id)
    if pull["status"] != "collected":
        raise PullError("Collect the list first.")
    _check_spending()
    if choice == "chance" and not db.q1("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND selected=1
                                          AND email_chance IS NOT NULL""", (pull_id,))["n"]:
        raise PullError("Not enough history yet to judge email chance. Pick another option for now.")
    chosen = choice_ids(pull_id, choice, first_n, chance_min)
    n = len(chosen)
    if not n:
        raise PullError("Nobody is selected for research.")
    if expected is not None and int(expected) != n:
        raise PullError(f"The number of people changed to {n:,} (the screen showed {int(expected):,}). "
                        f"Check the list and press the button again.")
    acct = _account(pull, n)
    room = budget_left(acct)
    if room is not None and n > room:
        others = accounts.others_with_room(acct["id"], n)
        raise PullError(f"Researching {n:,} people needs {n:,} credits, but {acct['name']} can only spend {room:,} more "
                        f"(balance above ACCOUNT_RESERVE, and monthly budget)." +
                        (f" {others[0]['name']} has room: use “Continue with {others[0]['name']}”." if others else ""))
    bal = acct.get("balance")
    if bal is not None and n > bal:
        raise PullError(f"Researching {n:,} people needs {n:,} credits, but the Seamless balance of {acct['name']} is {bal:,}.")
    chosen_set = set(chosen)
    with db.tx():
        for r in db.q("SELECT id FROM contacts WHERE pull_id=? AND kept=1 AND selected=1 AND submitted_at IS NULL",
                      (pull_id,)):
            if r["id"] not in chosen_set:
                db.x("UPDATE contacts SET selected=0 WHERE id=?", (r["id"],))
    already = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (pull_id,))["n"]
    test = cfg.TEST_BATCH_SIZE if (cfg.TEST_BATCH_SIZE and n > cfg.TEST_BATCH_SIZE and not already) else 0
    db.update_pull(pull_id, status="researching", stop_reason=None, error=None, test_size=test, pause_kind=None,
                   test_state="running" if test else None, test_rate=None,
                   bal_before=_balance_now(pull) if not already else pull.get("bal_before"), bal_after=None)
    return db.get_pull(pull_id)


# ---------------------------------------------------------------- pause / resume / stop
def pause(pull_id):
    pull = db.get_pull(pull_id)
    if pull["status"] in ACTIVE:
        db.update_pull(pull_id, status="paused", paused_from=pull["status"])


def _pause(pull, kind, message):
    db.update_pull(pull["id"], status="paused", paused_from=pull["status"] if pull["status"] in ACTIVE else
                   pull.get("paused_from"), pause_kind=kind, error=message)


def resume(pull_id):
    pull = db.get_pull(pull_id)
    if pull["status"] != "paused":
        return
    _check_spending()
    fields = {"status": pull["paused_from"] or "collecting", "error": None, "stop_reason": None, "pause_kind": None}
    if pull.get("test_state") == "failed":
        fields["test_state"] = "accepted"
    kind = pull.get("pause_kind")
    if kind == "low_keep":
        fields["keep_ack_page"] = db.q1("SELECT COALESCE(MAX(id),0) m FROM collect_pages WHERE pull_id=?",
                                        (pull_id,))["m"]
    if kind == "cap":
        more = max(math.ceil((pull.get("est_search_credits") or 20) * 0.5), 10)
        fields["spend_cap"] = (pull["search_credits"] or 0) + more
        fields["keep_ack_page"] = db.q1("SELECT COALESCE(MAX(id),0) m FROM collect_pages WHERE pull_id=?",
                                        (pull_id,))["m"]
    if kind == "account":
        acct = accounts.get(pull.get("account_id"))
        room = budget_left(acct) if acct else 0
        if acct and acct.get("enabled"):
            try:
                seamless.test_connection(acct)          # free: refresh the balance first
                room = budget_left(accounts.get(acct["id"]))
            except seamless.SeamlessError:
                pass
        if not acct or not acct.get("enabled") or (room is not None and room < 1):
            raise PullError(f"{acct['name'] if acct else 'This account'} still has no credits to spare. "
                            f"Use “Continue with another account”, or raise its budget / top it up first.")
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
                  "error": None, "pause_kind": None}
        if pull.get("test_state") in ("running", "failed"):
            fields["test_state"] = "stopped"
        db.update_pull(pull_id, **fields)


def finish_now(pull_id):
    """Don't wait any longer for Seamless: send nobody else, finish the pull with what has come back.
    Answers that arrive late are still filled in (free) for LATE_ANSWER_HOURS."""
    pull = db.get_pull(pull_id)
    phase = pull["paused_from"] if pull["status"] == "paused" else pull["status"]
    if phase != "researching":
        raise PullError("Finish now only works while research is running.")
    cancel(pull_id)
    _give_up(pull_id, "Finished without waiting (you pressed Finish now).")
    db.update_pull(pull_id, status="researching", stop_reason="Finished by you without waiting for the last answers. "
                   "Late answers are still filled in for free.")
    step_research(db.get_pull(pull_id))


def _give_up(pull_id, message, older_than=None):
    now = time.time()
    where = "pull_id=? AND resolved_at IS NULL" + (" AND created_at<?" if older_than else "")
    args = (pull_id, older_than) if older_than else (pull_id,)
    for req in db.q(f"SELECT * FROM research_requests WHERE {where}", args):
        db.x("UPDATE research_requests SET status='timed out', resolved_at=?, gave_up=1 WHERE request_id=? "
             "AND resolved_at IS NULL", (now, req["request_id"]))
        c = db.q1("SELECT id FROM contacts WHERE pull_id=? AND (request_id=? OR search_result_id=?) AND "
                  "research_status IN ('queued','researching')", (pull_id, req["request_id"], req["guess_sid"]))
        if c:
            db.x("UPDATE contacts SET research_status='timed out', research_message=? WHERE id=?", (message, c["id"]))
            _record_outcome(pull_id, c["id"])


def move_account(pull_id, account_id):
    """The pull's account ran low. People already researched stay here. Everyone not researched yet is released
    and a new pull with the same filters starts on the other account (a short preview, about 3 credits).
    Search result IDs belong to the account that found them, so those people are found again by the new
    account; anyone already paid for is skipped (shared memory)."""
    pull = db.get_pull(pull_id)
    acct = accounts.get(int(account_id))
    if not acct or not acct["enabled"]:
        raise PullError("Choose an account that is switched on.")
    if acct["id"] == pull.get("account_id"):
        raise PullError("This pull already uses that account.")
    researched = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (pull_id,))["n"]
    phase = pull["paused_from"] if pull["status"] == "paused" else pull["status"]
    db.x("UPDATE contacts SET selected=0 WHERE pull_id=? AND submitted_at IS NULL", (pull_id,))
    pending = db.q1("SELECT COUNT(*) n FROM research_requests WHERE pull_id=? AND resolved_at IS NULL", (pull_id,))["n"]
    remaining = max(1, (pull["target"] or 0) - researched) if pull["target"] else 0
    new_id = db.create_pull(pull["name"] + f" ({acct['name']})", pull["prompt"], pull["filters"], pull["notes"],
                            target=remaining, per_company_cap=pull["per_company_cap"], cloned_from=pull_id,
                            cooldown=pull["company_cooldown"])
    db.update_pull(new_id, account_choice=str(acct["id"]), claude_filters_json=pull.get("claude_filters_json"))
    if phase == "researching" or researched:
        db.update_pull(pull_id, status="researching", pause_kind=None, error=None,
                       moved_to=new_id, stop_reason=f"The rest moved to pull #{new_id} on {acct['name']}.")
        if pull.get("test_state") in ("running", "failed"):
            db.update_pull(pull_id, test_state="stopped")
    else:
        db.update_pull(pull_id, status="cancelled", pause_kind=None, error=None, moved_to=new_id,
                       stop_reason=f"Moved to pull #{new_id} on {acct['name']}.")
    try:
        run_preview(new_id)
    except (PullError, seamless.SeamlessError) as e:
        db.update_pull(new_id, error=f"Preview not run yet: {e}")
    return new_id


def stop_all():
    stopped = 0
    for r in db.q("SELECT id FROM pulls WHERE status IN ('collecting','researching','paused')"):
        cancel(r["id"])
        stopped += 1
    return stopped


# ---------------------------------------------------------------- background: collect
def _recent_pages(pull_id, n, after_id=0):
    rows = db.q("SELECT * FROM collect_pages WHERE pull_id=? AND id>? ORDER BY id DESC LIMIT ?", (pull_id, after_id, n))
    return rows if len(rows) == n else []


def _main_skip(pull_id):
    top = [r for r in skip_report(pull_id) if r["kind"] != "over_target"]
    return top[0] if top else None


def step_collect(pull):
    pid = pull["id"]
    if not cfg.SPENDING_ENABLED:
        _pause(pull, "spending", f"Paused: {SPENDING_OFF}")
        return
    kept = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1", (pid,))["n"]
    target = pull["target"]
    if kept >= target:
        return _finish_collect(pid, "Reached your target.")
    if not pull["next_token"]:
        return _finish_collect(pid, "No more matching people in Seamless.")
    close = math.ceil(target * cfg.COLLECT_CLOSE_ENOUGH_PCT / 100) if cfg.COLLECT_CLOSE_ENOUGH_PCT else 0
    if close and kept and target - kept <= close:
        return _finish_collect(pid, f"Close enough: {kept:,} of {target:,} (within {cfg.COLLECT_CLOSE_ENOUGH_PCT}% of your "
                                    f"target), so no more search credits were spent on the last few.")
    ack = pull.get("keep_ack_page") or 0
    empty = _recent_pages(pid, cfg.COLLECT_EMPTY_PAGES_STOP, ack)
    if empty and all(p["kept"] == 0 for p in empty):
        m = _main_skip(pid)
        return _finish_collect(pid, f"Stopped early: the last {len(empty)} search pages kept nobody, so no more credits "
                                    f"were spent. You have {kept:,} of {target:,}." +
                                    (f" Main reason: {m['label']}. {m['tip']}" if m else ""))
    if cfg.COLLECT_LOW_KEEP_PCT:
        recent = _recent_pages(pid, cfg.COLLECT_LOW_KEEP_PAGES, ack)
        rows_n, kept_n = sum(p["rows"] for p in recent), sum(p["kept"] for p in recent)
        if recent and rows_n and 100.0 * kept_n / rows_n < cfg.COLLECT_LOW_KEEP_PCT:
            credits = sum(p["credits"] for p in recent)
            m = _main_skip(pid)
            per = f"{credits / kept_n:.1f} search credits per person kept" if kept_n else "nobody kept"
            return _pause(pull, "low_keep", (
                f"Paused: the last {len(recent)} pages kept only {kept_n} of {rows_n} people "
                f"({100.0 * kept_n / rows_n:.0f}%, {per}). "
                + (f"Main reason: {m['label']} ({m['n']:,} people, about {m['credits']:,.0f} credits). {m['tip']} " if m else "")
                + f"You have {kept:,} of {target:,}. Continue collecting, or Stop and keep what you have."))
    if pull.get("spend_cap") and (pull["search_credits"] or 0) >= pull["spend_cap"]:
        return _pause(pull, "cap", (
            f"Paused: {pull['search_credits']:,} search credits spent, past the {pull['spend_cap']:,} limit "
            f"({cfg.COLLECT_SPEND_CAP_PCT}% of the estimate shown before collecting). Most results are being skipped. "
            f"You have {kept:,} of {target:,}. Continue (another "
            f"{max(math.ceil((pull.get('est_search_credits') or 20) * 0.5), 10):,} credits allowed) or Stop and keep "
            f"what you have."))
    acct = accounts.get(pull.get("account_id"))
    if not acct:
        return _pause(pull, "account", "Paused: this pull's Seamless account was deleted.")
    need = target - kept
    rate = _keep_rate(pid) or 0.05
    limit = math.ceil(need / max(rate, 0.05) * 1.2 / 10) * 10
    limit = max(10, min(limit, cfg.PAGE_SIZE))
    cost = math.ceil(limit / 10)
    room = budget_left(acct)
    if room is not None and room < cost:
        return _pause(pull, "account", _account_low_message(acct, "collecting"))
    rows, sup = seamless.search_contacts(to_search_body(pull["filters"]), limit, pull["next_token"], pid, account=acct)
    kept_ids = _store_rows(pull, rows)          # stored even if stopped meanwhile: these were paid for
    more = sup.get("nextToken") if (rows and sup.get("isMore", True)) else None
    db.x("INSERT INTO collect_pages(pull_id, at, rows, kept, credits) VALUES(?,?,?,?,?)",
         (pid, time.time(), len(rows), len(kept_ids), math.ceil(len(rows) / 10) if rows else 0))
    db.update_pull(pid, next_token=more, pages_fetched=pull["pages_fetched"] + 1,
                   rows_seen=pull["rows_seen"] + len(rows), total_matches=sup.get("total", pull["total_matches"]))
    _remember_position(pull, more, len(rows))
    note = score_contacts(db.get_pull(pid), kept_ids)
    if note:
        db.update_pull(pid, error=note)
    now_status = db.get_pull(pid)["status"]
    if now_status == "collected":               # Stop was pressed while this page was loading: trim to target
        _finish_collect(pid, db.get_pull(pid)["stop_reason"] or "Stopped by you.")


def _account_low_message(acct, phase):
    room = budget_left(acct)
    why = []
    if acct.get("balance") is not None and acct["balance"] - cfg.ACCOUNT_RESERVE <= (room or 0) + 0.5:
        why.append(f"balance {acct['balance']:,}, keeping {cfg.ACCOUNT_RESERVE:,} in reserve (ACCOUNT_RESERVE)")
    if acct.get("monthly_budget"):
        why.append(f"its monthly budget of {acct['monthly_budget']:,}")
    if cfg.MONTHLY_CREDIT_BUDGET:
        why.append(f"the overall monthly budget of {cfg.MONTHLY_CREDIT_BUDGET:,} (MONTHLY_CREDIT_BUDGET)")
    others = accounts.others_with_room(acct["id"])
    return (f"Paused while {phase}: {acct['name']} has no credits to spare ({'; '.join(why) or 'no room'}). "
            + (f"Continue with {others[0]['name']}, or top up / raise the budget and Resume." if others else
               "No other Seamless account has credits to spare. Top up or raise the budget, then Resume."))


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
    c = db.q1("SELECT id, name, company, li_url, research_status FROM contacts WHERE pull_id=? AND search_result_id=?",
              (pull_id, sid))
    if not c:
        return False
    if guessed and not _same_person(c, row.get("contact") or {}):
        return False
    status = (row.get("status") or "").strip()
    st = status.lower()
    terminal = st in seamless.TERMINAL
    contact = row.get("contact") or {}
    late = False
    if req and req.get("resolved_at"):
        # Already handled -- unless we stopped waiting for it and this is the late answer.
        if not (req.get("gave_up") and terminal and (c["research_status"] or "").lower() in ("timed out", "no answer")):
            return terminal
        late = True
    fields = {"research_status": status, "research_message": "Arrived late; filled in (no extra charge)." if late
              else (row.get("message") or ""), "request_id": rid}
    if contact:
        fields.update(contact_fields(contact))
    cols = ", ".join(f"{k}=?" for k in fields)
    db.x(f"UPDATE contacts SET {cols} WHERE id=?", (*fields.values(), c["id"]))
    if rid and terminal:
        if late:
            db.x("UPDATE research_requests SET status=?, gave_up=0 WHERE request_id=?", (status, rid))
        else:
            db.x("UPDATE research_requests SET status=?, resolved_at=? WHERE request_id=? AND resolved_at IS NULL",
                 (status, time.time(), rid))
        if st in DUPLICATE_STATUSES:
            # Logged as 1 credit at submit; Seamless's de-duplication normally doesn't charge these.
            req_acct = (req or {}).get("account_id")
            db.log_credits(pull_id, "research", -1, None, "already researched before - normally not charged",
                           account_id=req_acct)
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
    """Your org's saved Seamless contacts (free to read), from every account, indexed for strict matching."""
    ttl = cfg.SAVED_CONTACTS_CACHE_MINUTES * 60
    if ttl and _saved_cache.get("at", 0) > time.time() - ttl:
        return _saved_cache["idx"]
    end = time.time()
    start = end - cfg.ORG_LOOKBACK_DAYS * 86400
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    by_li, by_nc, read = {}, {}, 0
    for acct in accounts.enabled():
        for page in range(1, cfg.ORG_LOOKUP_MAX_PAGES + 1):
            if _fill_stop.get("all"):
                break
            rows = seamless.org_contacts(iso(start), iso(end), page, account=acct)
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


def _record_email(r):
    return (r.get("email") or r.get("email1") or "").strip().lower()


def _saved_match(idx, c):
    """Strict: exact LinkedIn with one consistent email, else exact name+company where every record has the same
    email (the same record can come from two accounts)."""
    li = db.norm_li(c["li_url"])
    if li and idx["li"].get(li):
        emails = {_record_email(r) for r in idx["li"][li]}
        if len(emails) == 1:
            return idx["li"][li][0]
    cands = idx["nc"].get(db.norm(c["name"]) + "|" + db.norm(c["company"]), [])
    if cands and len({_record_email(r) for r in cands}) == 1:
        return cands[0]
    return None


def _fill_free(pull_id, c, record, now):
    f = contact_fields(record)
    if not f.get("email"):
        return False
    f.update(research_message="Filled in from your Seamless saved contacts (no charge).", filled_free=1)
    cols = ", ".join(f"{k}=?" for k in f)
    db.x(f"UPDATE contacts SET {cols} WHERE id=? AND pull_id=?", (*f.values(), c["id"], pull_id))
    return True


def _poll_pending(pid, rows):
    """Poll a list of research_requests rows, each with the account that sent it."""
    by_acct = {}
    for p in rows:
        by_acct.setdefault(p.get("account_id"), []).append(p)
    for acct_id, reqs in by_acct.items():
        acct = accounts.get(acct_id)
        if not acct or not acct["api_key"]:
            continue
        for i in range(0, len(reqs), POLL_CHUNK):
            chunk = reqs[i:i + POLL_CHUNK]
            for row in seamless.poll_research([p["request_id"] for p in chunk], account=acct):
                _apply_result(pid, row)


def step_research(pull):
    pid = pull["id"]
    acct = accounts.get(pull.get("account_id"))
    # 1. submit the next batch (unless spending is off, a test batch is full, or nobody is waiting)
    room = _test_batch_check(pull)
    limit = cfg.RESEARCH_BATCH if room is None else min(cfg.RESEARCH_BATCH, room)
    todo = []
    if limit > 0 and cfg.SPENDING_ENABLED and acct:
        todo = db.q("""SELECT id, search_result_id, name, company, domain, li_url, retry FROM contacts
                       WHERE pull_id=? AND kept=1 AND selected=1 AND request_id IS NULL AND submitted_at IS NULL
                       ORDER BY id LIMIT ?""", (pid, limit))
    if todo:
        # Re-check right before paying: another pull (with any account) may have taken the same person or
        # company meanwhile, or the block list may have changed.
        fresh = []
        for t in todo:
            reason, retry = people_reason(t["name"], t["company"], t["li_url"])
            reason = reason or blocked_company(t["company"], t["domain"]) or _cooldown_reason(t, pull)
            if reason:
                _skip(t["id"], reason)
            else:
                t["retry"] = 1 if retry else 0
                fresh.append(t)
        todo = fresh
    if todo:
        left = budget_left(acct)
        if left is not None and left < len(todo):
            todo = todo[:left]
            if not todo:
                pending_n = db.q1("SELECT COUNT(*) n FROM research_requests WHERE pull_id=? AND resolved_at IS NULL",
                                  (pid,))["n"]
                if not pending_n:
                    _pause(pull, "account", _account_low_message(acct, "researching"))
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
                request_ids = seamless.research_contacts(sids, pid, skip_dedup=skip_dedup, account=acct)
            except seamless.SeamlessError as e:
                if e.code == "unconfirmed":
                    _pause(pull, "unconfirmed", (
                        f"Paused: {e} while sending {len(sids)} people for research, so the tool did not resend them "
                        f"(that could charge twice). Check the Seamless balance. Resume sends them again; anyone "
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
                db.x("INSERT OR IGNORE INTO research_requests(request_id,pull_id,guess_sid,status,created_at,account_id) "
                     "VALUES(?,?,?,?,?,?)", (rid, pid, sids[i] if same_len else None, "queued", now, acct["id"]))
            if not request_ids:
                for t in group:
                    db.x("UPDATE contacts SET research_status='error', research_message=? WHERE id=?",
                         ("Seamless did not accept this person for research.", t["id"]))
                    _record_outcome(pid, t["id"])
    # 2. poll what is pending (free; continues even when spending is switched off), with the sending account
    every = cfg.POLL_SECONDS if POLL_EVERY is None else POLL_EVERY
    if time.time() - _last_poll.get(pid, 0) >= every:
        _last_poll[pid] = time.time()
        pending = db.q("SELECT request_id, created_at, account_id FROM research_requests WHERE pull_id=? "
                       "AND resolved_at IS NULL ORDER BY created_at LIMIT ?", (pid, POLL_CHUNK))
        if pending:
            _poll_pending(pid, pending)
            _give_up(pid, f"Seamless took more than {cfg.RESEARCH_WAIT_MINUTES} minutes to answer. If the answer comes "
                          f"later it is filled in for free.", older_than=time.time() - cfg.RESEARCH_WAIT_MINUTES * 60)
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
        _pause(pull, "spending", f"Paused: {SPENDING_OFF}")
        return
    if not pending_n and left_to_submit and not acct:
        _pause(pull, "account", "Paused: this pull's Seamless account was deleted.")
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
        db.update_pull(pid, status="done", finished_at=time.time(), bal_after=_balance_now(pull),
                       stop_reason=pull["stop_reason"] if pull["stop_reason"] else None)


def poll_late(now=None):
    """Free: keep checking for answers we stopped waiting for, and fill them in when they arrive."""
    if not cfg.LATE_ANSWER_HOURS:
        return 0
    now = now or time.time()
    due = db.q("""SELECT request_id, pull_id, account_id, created_at FROM research_requests WHERE gave_up=1
                  AND created_at>? AND (late_polled_at IS NULL OR late_polled_at<?) ORDER BY created_at LIMIT 500""",
               (now - cfg.LATE_ANSWER_HOURS * 3600, now - cfg.LATE_POLL_MINUTES * 60))
    by_pull = {}
    for r in due:
        by_pull.setdefault(r["pull_id"], []).append(r)
    for pid, rows in by_pull.items():
        for r in rows:
            db.x("UPDATE research_requests SET late_polled_at=? WHERE request_id=?", (now, r["request_id"]))
        try:
            _poll_pending(pid, rows)
        except seamless.SeamlessError:
            pass
    return len(due)


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
            if e.code in ACCOUNT_CODES:
                acct = accounts.get(pull.get("account_id"))
                if e.code == "insufficientCredits" and acct:
                    _pause(pull, "account", _account_low_message(acct, pull["status"]) + f" (Seamless said: {e})")
                else:
                    _pause(pull, "account", f"Paused: {e}")
            elif e.code == "rateLimited":
                continue
            else:
                _fail(pull, e)
        except Exception as e:  # never let one pull kill the loop
            log.exception("pull %s step failed", pull["id"])
            _fail(pull, e)
    try:
        poll_late()
    except Exception:
        log.exception("late poll failed")


def _fail(pull, e):
    n = _failures.get(pull["id"], 0) + 1
    _failures[pull["id"]] = n
    if n >= cfg.ERROR_PAUSE_AFTER:
        _pause(pull, "errors", f"Paused after repeated errors: {e}")
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
    # People Seamless called "already researched", plus people whose answer never came (timed out / no answer):
    # Seamless did research them, so their record is usually in the saved contacts already.
    statuses = DUPLICATE_STATUSES + ("timed out", "no answer")
    want = db.q(f"""SELECT id, name, first_name, last_name, company, domain, li_url FROM contacts WHERE pull_id=?
                    AND submitted_at IS NOT NULL AND LOWER(research_status) IN ({",".join("?" * len(statuses))})
                    AND (email IS NULL OR email='')""", (pull_id, *statuses))
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
