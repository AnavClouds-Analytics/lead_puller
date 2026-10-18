"""Pull pipeline: preview -> collect (search pages) -> research (enrich) -> done.

Collecting and researching run in a background thread in small steps (one search page, one research
batch, one poll), with all state in the database, so jobs survive closing the browser or restarting
the server, and pause/cancel take effect within seconds.
"""
import hashlib
import json
import logging
import math
import threading
import time

from . import claude, db, seamless
from .config import cfg
from .filters import search_filter_count, to_search_body

log = logging.getLogger("leadpuller")

# Sizes and timings come from .env (see app/config.py). POLL_EVERY can be overridden by tests.
POLL_EVERY = None
POLL_CHUNK = 100
PENDING_TIMEOUT = 45 * 60
US_ALIASES = {"united states", "us", "usa", "united states of america"}

ACTIVE = ("collecting", "researching")


# ---------------------------------------------------------------- helpers
def _split_name(name):
    parts = (name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def contact_from_search(row):
    first, last = row.get("firstName"), row.get("lastName")
    if not first:
        first, last = _split_name(row.get("name"))
    industries = row.get("industries") or []
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
    }


def _excluded(c):
    keys = []
    if c["domain"]:
        keys.append("domain:" + c["domain"])
    for k in db.person_keys(c["name"], c["company"], c["li_url"]):
        keys.append(k)
    if not keys:
        return None
    marks = ",".join("?" * len(keys))
    if db.q1(f"SELECT key FROM exclusions WHERE key IN ({marks})", keys):
        return "On your do-not-pull list"
    person = [k for k in keys if not k.startswith("domain:")]
    if person:
        marks = ",".join("?" * len(person))
        row = db.q1(f"SELECT pull_id FROM seen WHERE key IN ({marks})", person)
        if row:
            return f"Already pulled before (pull #{row['pull_id']})"
    return None


def _cooldown_reason(c, pull):
    days = pull.get("company_cooldown") or 0
    key = db.company_key(c["company"], c["domain"])
    if not days or not key:
        return None
    row = db.q1("SELECT * FROM companies WHERE key=?", (key,))
    if not row or row["last_pull_id"] == pull["id"]:
        return None
    if row["released_at"] and row["released_at"] >= row["last_at"]:
        return None
    until = row["last_at"] + days * 86400
    if time.time() < until:
        return (f"Company pulled {time.strftime('%d %b', time.localtime(row['last_at']))} (pull #{row['last_pull_id']}); "
                f"skipped until {time.strftime('%d %b %Y', time.localtime(until))}")
    return None


def _skip_reason(c, pull, company_counts, in_pull_keys):
    filters, cap = pull["filters"], pull["per_company_cap"] or 0
    title, company = c["title"].lower(), c["company"].lower()
    for w in filters.get("excludeTitleWords", []):
        if w.lower() in title:
            return f"Title contains “{w}”"
    for w in filters.get("excludeCompanyWords", []):
        if w.lower() in company:
            return f"Company contains “{w}”"
    if c["industry"] and c["industry"] in filters.get("excludeIndustries", []):
        return f"Industry {c['industry']}"
    if c["domain"] and c["domain"] in {db.norm_domain(d) for d in filters.get("excludeDomains", [])}:
        return "Domain on this pull's skip list"
    keys = db.person_keys(c["name"], c["company"], c["li_url"])
    if any(k in in_pull_keys for k in keys):
        return "Duplicate inside this pull"
    reason = _excluded(c) or _cooldown_reason(c, pull)
    if reason:
        return reason
    if cap:
        ck = db.company_key(c["company"], c["domain"])
        if ck and company_counts.get(ck, 0) >= cap:
            return f"Company limit reached ({cap} per company)"
    return None


def _pull_state(pull_id):
    company_counts, in_pull_keys = {}, set()
    for r in db.q("SELECT name, company, domain, li_url FROM contacts WHERE pull_id=? AND kept=1", (pull_id,)):
        ck = db.company_key(r["company"], r["domain"])
        if ck:
            company_counts[ck] = company_counts.get(ck, 0) + 1
    for r in db.q("SELECT name, company, li_url FROM contacts WHERE pull_id=?", (pull_id,)):
        in_pull_keys.update(db.person_keys(r["name"], r["company"], r["li_url"]))
    return company_counts, in_pull_keys


def _store_rows(pull, rows):
    """Insert a page of search rows, applying skip rules. Returns ids of newly kept contacts."""
    company_counts, in_pull_keys = _pull_state(pull["id"])
    kept_ids = []
    now = time.time()
    for row in rows:
        c = contact_from_search(row)
        if not c["search_result_id"]:
            continue
        reason = _skip_reason(c, pull, company_counts, in_pull_keys)
        cid = db.x("""INSERT INTO contacts(pull_id, search_result_id, name, first_name, last_name, title, company,
                      domain, country, state, city, company_country, li_url, seniority, department, size, industry,
                      kept, skip_reason, selected, raw_search, created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (pull["id"], c["search_result_id"], c["name"], c["first_name"], c["last_name"], c["title"],
                    c["company"], c["domain"], c["country"], c["state"], c["city"], c["company_country"],
                    c["li_url"], c["seniority"], c["department"], c["size"], c["industry"],
                    0 if reason else 1, reason, 0 if reason else 1, json.dumps(row), now))
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
    """Check non-US country/state values against Seamless's location vocabulary (free).
    A misspelt location silently returns zero results, so fix it or flag it."""
    notes = []
    for key, types in (("contactCountry", "country"), ("contactState", "state-country")):
        fixed = []
        for val in filters.get(key, []):
            if val.strip().lower() in US_ALIASES or (key == "contactState" and len(val.strip()) == 2):
                fixed.append(val)
                continue
            try:
                hits = seamless.lookup_location(val, types)
            except seamless.SeamlessError:
                fixed.append(val)
                continue
            if not hits:
                notes.append(f"Seamless doesn't recognise the location “{val}”, so it was removed.")
                continue
            best = hits[0]["location"]
            if best.split(",")[0].strip().lower() != val.strip().lower():
                notes.append(f"Location “{val}” matched to Seamless's “{best}”.")
            fixed.append(best if key == "contactCountry" else best)
        if key in filters:
            if fixed:
                filters[key] = fixed
            else:
                filters.pop(key)
    return filters, notes


# ---------------------------------------------------------------- user actions
class PullError(Exception):
    pass


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


def _day(t):
    return time.strftime("%d %b %Y", time.localtime(t)) if t else ""


def run_preview(pull_id, from_top=False):
    pull = db.get_pull(pull_id)
    if not pull:
        raise PullError("Pull not found.")
    if pull["status"] not in ("draft", "previewed", "collected", "cancelled"):
        raise PullError("This pull is already running.")
    if not search_filter_count(pull["filters"]):
        raise PullError("Add at least one search filter (title, seniority, industry, country...) first.")
    left = budget_left()
    if left is not None and left < 3:
        raise PullError(BUDGET_MSG[0].upper() + BUDGET_MSG[1:] + ".")
    filters, loc_notes = resolve_locations(dict(pull["filters"]))
    notes = [n for n in pull["notes"] if not n.startswith(("Location “", "Seamless doesn't recognise"))]
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
            if e.code in ("insufficientCredits", "missingLicense", "badKey", "noKey"):
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
    target = int(target)
    if target < 1:
        raise PullError("Enter how many people you want.")
    kept = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1", (pull_id,))["n"]
    db.update_pull(pull_id, target=target, per_company_cap=int(cap or 0), stop_reason=None, error=None)
    if cap != pull["per_company_cap"]:
        _reapply_cap(pull_id, int(cap or 0))
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


def confirm_research(pull_id, choice, first_n=None):
    pull = db.get_pull(pull_id)
    if pull["status"] != "collected":
        raise PullError("Collect the list first.")
    rows = db.q("""SELECT id, fit FROM contacts WHERE pull_id=? AND kept=1 AND selected=1
                   AND submitted_at IS NULL ORDER BY id""", (pull_id,))
    if choice == "high":
        chosen = [r["id"] for r in rows if (r["fit"] or "high") == "high"]
    elif choice == "high_medium":
        chosen = [r["id"] for r in rows if r["fit"] != "low"]
    elif choice == "first_n":
        chosen = [r["id"] for r in rows][: max(0, int(first_n or 0))]
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
    db.update_pull(pull_id, status="researching", stop_reason=None, error=None)
    return db.get_pull(pull_id)


def pause(pull_id):
    pull = db.get_pull(pull_id)
    if pull["status"] in ACTIVE:
        db.update_pull(pull_id, status="paused", paused_from=pull["status"])


def resume(pull_id):
    pull = db.get_pull(pull_id)
    if pull["status"] == "paused":
        db.update_pull(pull_id, status=pull["paused_from"] or "collecting", error=None, stop_reason=None)


def cancel(pull_id):
    pull = db.get_pull(pull_id)
    phase = pull["paused_from"] if pull["status"] == "paused" else pull["status"]
    if phase == "collecting":
        _finish_collect(pull_id, "Stopped by you.")
        return
    if phase == "researching":
        # Stop submitting new research; already-submitted people are still collected (already paid for).
        db.x("UPDATE contacts SET selected=0 WHERE pull_id=? AND request_id IS NULL AND kept=1", (pull_id,))
        db.update_pull(pull_id, status="researching", stop_reason="Stopped by you. Finishing people already sent.")


# ---------------------------------------------------------------- background steps
def step_collect(pull):
    pid = pull["id"]
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
    kept_ids = _store_rows(pull, rows)
    more = sup.get("nextToken") if (rows and sup.get("isMore", True)) else None
    db.update_pull(pid, next_token=more, pages_fetched=pull["pages_fetched"] + 1,
                   rows_seen=pull["rows_seen"] + len(rows), total_matches=sup.get("total", pull["total_matches"]))
    _remember_position(pull, more, len(rows))
    note = score_contacts(db.get_pull(pid), kept_ids)
    if note:
        db.update_pull(pid, error=note)


_last_poll = {}
DUPLICATE_STATUSES = ("duplicate", "contact-already-researched")


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


def contact_fields(contact):
    """Map a Seamless contact record (research poll or org contacts) to our columns."""
    email = contact.get("email") or contact.get("email1")
    estatus, conf = None, None
    for i in (1, 2, 3):
        if contact.get(f"email{i}") and email and contact.get(f"email{i}").lower() == email.lower():
            estatus, conf = contact.get(f"email{i}EmailAI"), contact.get(f"email{i}TotalAI")
            break
    fields = {"email": email, "email_status": estatus, "email_confidence": conf,
              "personal_email": contact.get("personalEmail"),
              "phone": contact.get("contactPhone1"), "phone_type": contact.get("contactPhone1DataType"),
              "company_phone": contact.get("companyPhone1"), "raw_research": json.dumps(contact)}
    for col, keys in (("first_name", ("firstName",)), ("last_name", ("lastName",)),
                      ("title", ("jobTitle", "title")), ("company", ("companyName", "company")),
                      ("li_url", ("lIProfileUrl", "liProfileUrl")), ("country", ("country",))):
        for key in keys:
            if contact.get(key):
                fields[col] = contact[key]
                break
    if contact.get("companyDomain"):
        fields["domain"] = db.norm_domain(contact["companyDomain"])
    return fields


def remember_company(name, domain, pull_id, at):
    """Every company someone was researched at goes into the company memory (drives the cooldown)."""
    key = db.company_key(name, domain)
    if not key:
        return
    db.x("""INSERT INTO companies(key, name, domain, first_at, last_at, first_pull_id, last_pull_id, people)
            VALUES(?,?,?,?,?,?,?,1)
            ON CONFLICT(key) DO UPDATE SET last_at=excluded.last_at, last_pull_id=excluded.last_pull_id,
              people=companies.people+1, name=COALESCE(NULLIF(excluded.name,''), companies.name),
              domain=COALESCE(NULLIF(excluded.domain,''), companies.domain)""",
         (key, name or "", db.norm_domain(domain), at, at, pull_id, pull_id))


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
    fields = {"research_status": status, "research_message": row.get("message") or "", "request_id": rid}
    if contact:
        fields.update(contact_fields(contact))
    cols = ", ".join(f"{k}=?" for k in fields)
    db.x(f"UPDATE contacts SET {cols} WHERE id=?", (*fields.values(), c["id"]))
    if fields.get("email"):
        db.x("INSERT OR IGNORE INTO seen(key,pull_id,created_at) VALUES(?,?,?)",
             ("em:" + fields["email"].lower(), pull_id, time.time()))
    if rid and terminal:
        first_time = db.q1("SELECT resolved_at FROM research_requests WHERE request_id=?", (rid,))
        db.x("UPDATE research_requests SET status=?, resolved_at=? WHERE request_id=? AND resolved_at IS NULL",
             (status, time.time(), rid))
        if st in DUPLICATE_STATUSES and first_time and first_time["resolved_at"] is None:
            # Logged as 1 credit at submit; Seamless's de-duplication normally doesn't charge these.
            db.log_credits(pull_id, "research", -1, None, "already researched before - normally not charged")
    return terminal


def step_research(pull):
    pid = pull["id"]
    # 1. submit the next batch
    todo = db.q("""SELECT id, search_result_id, name, company, domain, li_url FROM contacts WHERE pull_id=? AND kept=1
                   AND selected=1 AND request_id IS NULL AND submitted_at IS NULL ORDER BY id LIMIT ?""",
                (pid, cfg.RESEARCH_BATCH))
    if todo:
        # Re-check right before paying: another pull may have researched the same person or company meanwhile.
        fresh = []
        for t in todo:
            keys = db.person_keys(t["name"], t["company"], t["li_url"])
            hit = db.q1(f"SELECT pull_id FROM seen WHERE key IN ({','.join('?' * len(keys))})", keys) if keys else None
            reason = f"Already researched in pull #{hit['pull_id']}" if hit else _cooldown_reason(t, pull)
            if not reason:
                ck = db.company_key(t["company"], t["domain"])
                blocked = db.q1("SELECT key FROM exclusions WHERE key=?", ("domain:" + db.norm_domain(t["domain"]),)) \
                    if t["domain"] else None
                if blocked:
                    reason = "On your do-not-pull list"
            if reason:
                db.x("UPDATE contacts SET kept=0, selected=0, skip_reason=? WHERE id=?", (reason, t["id"]))
            else:
                fresh.append(t)
        todo = fresh
        if not todo:
            return
    if todo:
        left = budget_left()
        if left is not None and left < len(todo):
            todo = todo[:left]
            if not todo:
                db.update_pull(pid, status="paused", paused_from="researching",
                               error=f"Paused: {BUDGET_MSG}. Raise it, restart the app, then Resume.")
                return
        sids = [t["search_result_id"] for t in todo]
        try:
            request_ids = seamless.research_contacts(sids, pid)
        except seamless.SeamlessError as e:
            if e.code == "unconfirmed":
                db.update_pull(pid, status="paused", paused_from="researching", error=(
                    f"Paused: {e} while sending {len(sids)} people for research, so the tool did not resend them "
                    f"(that could charge twice). Check your Seamless balance. Resume sends them again; anyone Seamless "
                    f"already researched comes back as “already researched” and is normally not charged again — "
                    f"use “Fill in from Seamless saved contacts” afterwards to get their details free."))
                return
            raise
        now = time.time()
        for t in todo:
            db.x("UPDATE contacts SET submitted_at=?, research_status='queued' WHERE id=?", (now, t["id"]))
            for k in db.person_keys(t["name"], t["company"], t["li_url"]):
                db.x("INSERT OR IGNORE INTO seen(key,pull_id,created_at) VALUES(?,?,?)", (k, pid, now))
            remember_company(t["company"], t["domain"], pid, now)
        # Provisional link by position, used only if a poll row comes back without its searchResultId.
        same_len = len(request_ids) == len(sids)
        for i, rid in enumerate(request_ids):
            db.x("INSERT OR IGNORE INTO research_requests(request_id,pull_id,guess_sid,status,created_at) "
                 "VALUES(?,?,?,?,?)", (rid, pid, sids[i] if same_len else None, "queued", now))
        if not request_ids:
            for t in todo:
                db.x("UPDATE contacts SET research_status='error', research_message=? WHERE id=?",
                     ("Seamless did not accept this person for research.", t["id"]))
    # 2. poll what is pending
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
                    db.x("UPDATE research_requests SET status='timed out', resolved_at=? WHERE request_id=? "
                         "AND resolved_at IS NULL", (now, p["request_id"]))
                    db.x("""UPDATE contacts SET research_status='timed out',
                            research_message='Seamless took too long to answer.' WHERE pull_id=? AND
                            search_result_id=(SELECT guess_sid FROM research_requests WHERE request_id=?)
                            AND research_status IN ('queued','researching')""", (pid, p["request_id"]))
    # 3. finished?
    left_to_submit = db.q1("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND selected=1
                              AND submitted_at IS NULL""", (pid,))["n"]
    pending_n = db.q1("SELECT COUNT(*) n FROM research_requests WHERE pull_id=? AND resolved_at IS NULL", (pid,))["n"]
    if not left_to_submit and not pending_n:
        db.x("""UPDATE contacts SET research_status='no answer', research_message='Seamless returned no result.'
                WHERE pull_id=? AND submitted_at IS NOT NULL AND research_status IN ('queued','researching')""", (pid,))
        db.update_pull(pid, status="done", finished_at=time.time(),
                       stop_reason=pull["stop_reason"] if pull["stop_reason"] else None)


# ---------------------------------------------------------------- loop
_failures = {}
STOP_CODES = {"insufficientCredits", "missingLicense", "badKey", "noKey"}


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
                n = _failures.get(pull["id"], 0) + 1
                _failures[pull["id"]] = n
                if n >= 5:
                    db.update_pull(pull["id"], status="paused", paused_from=pull["status"],
                                   error=f"Paused after repeated errors: {e}")
                    _failures.pop(pull["id"], None)
        except Exception as e:  # never let one pull kill the loop
            log.exception("pull %s step failed", pull["id"])
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
def fill_from_saved(pull_id, lookback_days=None, max_pages=None):
    """Seamless doesn't return people your org researched before. Their records are in your org's saved
    contacts (free to read). Match strictly so no one gets someone else's details:
      1. exact LinkedIn profile, or
      2. exact name + company, only when exactly one saved contact has it."""
    lookback_days = lookback_days or cfg.ORG_LOOKBACK_DAYS
    max_pages = max_pages or cfg.ORG_LOOKUP_MAX_PAGES
    want = db.q(f"""SELECT id, name, first_name, last_name, company, li_url FROM contacts WHERE pull_id=?
                    AND submitted_at IS NOT NULL AND LOWER(research_status) IN ({",".join("?" * len(DUPLICATE_STATUSES))})
                    AND (email IS NULL OR email='')""", (pull_id, *DUPLICATE_STATUSES))
    if not want:
        return {"checked": 0, "filled": 0, "pages": 0, "saved_contacts_read": 0}
    end = time.time()
    start = end - lookback_days * 86400
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    by_li, by_nc = {}, {}
    pages = read = 0
    for page in range(1, max_pages + 1):
        rows = seamless.org_contacts(iso(start), iso(end), page)
        pages += 1
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
    filled = 0
    for c in want:
        match = None
        li = db.norm_li(c["li_url"])
        if li and len(by_li.get(li, [])) >= 1:
            emails = {(r.get("email") or r.get("email1") or "").lower() for r in by_li[li]}
            if len(emails) == 1:
                match = by_li[li][0]
        if match is None:
            cands = by_nc.get(db.norm(c["name"]) + "|" + db.norm(c["company"]), [])
            if len(cands) == 1:
                match = cands[0]
        if not match:
            continue
        f = contact_fields(match)
        if not f.get("email"):
            continue
        f["research_message"] = "Filled in from your Seamless saved contacts (no charge)."
        cols = ", ".join(f"{k}=?" for k in f)
        db.x(f"UPDATE contacts SET {cols} WHERE id=? AND pull_id=?", (*f.values(), c["id"], pull_id))
        db.x("INSERT OR IGNORE INTO seen(key,pull_id,created_at) VALUES(?,?,?)",
             ("em:" + f["email"].lower(), pull_id, time.time()))
        filled += 1
    return {"checked": len(want), "filled": filled, "pages": pages, "saved_contacts_read": read}
