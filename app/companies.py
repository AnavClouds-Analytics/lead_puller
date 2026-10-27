"""Company lists ("People at my companies"): read a pasted list or a CSV (Hiring Signal Finder, Funding Finder or any
file with a website column), check every company before anything is spent, and report per company during a pull."""
import csv
import io
import re

from . import db, guess, memory
from .config import cfg

WEBSITE_COLS = ("website", "domain", "company_domain", "company_website", "website_url", "url", "company_url",
                "homepage", "site", "web", "company_site")
NAME_COLS = ("company", "company_name", "organization", "organisation", "account_name", "name", "employer")
SIGNAL_COLS = ("signal", "signals", "hiring_signal", "signal_detail", "signal_summary", "reason", "why", "trigger",
               "job_title", "job", "jobs", "role", "roles", "open_roles", "position", "hiring_for", "funding",
               "funding_round", "round", "latest_funding", "amount", "notes")
EMAIL_COLS = ("email", "work_email", "contact_email")

# Company-level filters don't apply: the list decides the companies.
COMPANY_ONLY = {"industry", "companySize", "companyRevenue", "technologies", "technologiesIsOr", "companyName",
                "companyDomain", "industrySicCodes", "industryNaicsCodes", "companyType", "companyFoundedOn",
                "companyLatestFundingClassifications", "companyLatestFundingDates", "companyLatestFundingTotals",
                "newsTypes", "newsTypeDates"}


def people_filters(f):
    return {k: v for k, v in (f or {}).items() if k not in COMPANY_ONLY}


def _norm_header(h):
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def _domain(v):
    v = (v or "").strip()
    if "@" in v and " " not in v:
        v = v.split("@")[-1]
    d = db.norm_domain(v)
    d = d.split(":")[0]
    return d if re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", d or "") and \
        re.search(r"\.[a-z]{2,}$", d) else ""


def parse(text=None, csv_text=None):
    """[{name, domain, signal}] in file order, before checking."""
    rows = []
    if csv_text:
        reader = csv.reader(io.StringIO(csv_text))
        header = [_norm_header(h) for h in next(reader, [])]
        pick = lambda names: next((header.index(n) for n in names if n in header), None)
        iw, iname, isig, iem = pick(WEBSITE_COLS), pick(NAME_COLS), pick(SIGNAL_COLS), pick(EMAIL_COLS)
        if iw is None and iname is None and iem is None:
            # No header row we recognise: treat the first column as websites.
            rows += [{"name": "", "domain_raw": h, "signal": ""} for h in header[:1]]
            iw = 0
        for r in reader:
            get = lambda i: r[i].strip() if i is not None and i < len(r) else ""
            raw = get(iw) or (get(iem).split("@")[-1] if get(iem) else "")
            if not (raw or get(iname)):
                continue
            rows.append({"name": get(iname), "domain_raw": raw, "signal": get(isig)[:200]})
    for line in (text or "").splitlines():
        line = line.strip().strip(",;")
        if not line:
            continue
        bits = [b.strip() for b in re.split(r"[\t,;]", line) if b.strip()]
        dom = next((b for b in bits if _domain(b)), "")
        name = next((b for b in bits if b != dom and not _domain(b)), "")
        rows.append({"name": name, "domain_raw": dom or (line if not name else ""), "signal": ""})
    return rows


def check(rows):
    """Status per company: ready / no_website / dnp / duplicate / too_many. Nothing here costs anything."""
    block = memory.Blocklist()
    seen = set()
    out = []
    ready = 0
    for r in rows:
        d = _domain(r.get("domain_raw") or r.get("domain") or "")
        name = (r.get("name") or "").strip()[:120]
        item = {"name": name or d, "domain": d, "signal": (r.get("signal") or "").strip()[:200]}
        if not d:
            item["status"] = "no_website"
        elif d in seen:
            item["status"] = "duplicate"
        elif block.reason(name, name, d, None, None):
            item["status"] = "dnp"
        elif ready >= cfg.COMPANY_LIST_MAX:
            item["status"] = "too_many"
        else:
            item["status"] = "ready"
            ready += 1
        if d:
            seen.add(d)
        out.append(item)
    return out


def summary(items):
    s = {}
    for i in items:
        s[i["status"]] = s.get(i["status"], 0) + 1
    known = sum(1 for i in items if i["status"] == "ready" and guess.format_label(i["domain"]) not in (None, "catch_all"))
    catch = sum(1 for i in items if i["status"] == "ready" and guess.format_label(i["domain"]) == "catch_all")
    return {"ready": s.get("ready", 0), "no_website": s.get("no_website", 0), "dnp": s.get("dnp", 0),
            "duplicate": s.get("duplicate", 0), "too_many": s.get("too_many", 0), "known_format": known,
            "catch_all": catch, "total": len(items)}


def estimate(n_ready, per_company, target, known_format, catch_all, guessing=True):
    """Rough ranges for the New pull screen, not promises."""
    t = max(0, min(int(target or 0), n_ready * per_company))
    learners = max(0, n_ready - known_format)
    rows_low, rows_high = t * 0.35, t * 1.0
    if guessing:
        # Measured on test data: about 1 paid per company to learn its format, then roughly half of the rest are
        # guessable (full-name formats, not catch-all) and most of those guesses come back valid.
        extra = catch_all * max(0, per_company - 1)
        paid_low = min(t, round(learners * 0.9 + extra + 0.3 * max(0, t - learners)))
        paid_high = min(t, round(learners + extra + 0.6 * max(0, t - learners)))
    else:
        paid_low = paid_high = t
    checks = max(0, round((t - learners) * 0.5)) if guessing else 0
    return {"target": t, "search_low": max(1, -(-int(rows_low) // 10)), "search_high": max(1, -(-int(rows_high) // 10)),
            "research_without": t, "research_low": paid_low, "research_high": paid_high, "checks": checks,
            "guessing": guessing}


def save_for_pull(pid, items):
    with db.tx() as c:
        for i, it in enumerate(items):
            c.execute("INSERT INTO pull_companies(pull_id,idx,name,domain,signal,status) VALUES(?,?,?,?,?,?)",
                      (pid, i, it["name"], it["domain"], it["signal"], it["status"]))


def ready_domains(pid):
    return [r["domain"] for r in db.q("SELECT domain FROM pull_companies WHERE pull_id=? AND status='ready' ORDER BY idx",
                                      (pid,))]


def listed(pid, domain):
    """Is this website one of the pull's companies (or a subdomain of one)?"""
    d = db.norm_domain(domain)
    if not d:
        return None
    parts = d.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if db.q1("SELECT 1 x FROM pull_companies WHERE pull_id=? AND status='ready' AND domain=?", (pid, cand)):
            return cand
    return None


def rows_for_pull(pid, offset=0, limit=50, q=""):
    p = db.q1("SELECT max_per_company FROM pulls WHERE id=?", (pid,))
    cap = (p or {}).get("max_per_company") or 0
    where, args = "pull_id=?", [pid]
    if q:
        where += " AND (name LIKE ? OR domain LIKE ? OR signal LIKE ?)"
        args += [f"%{q}%"] * 3
    total = db.q1(f"SELECT COUNT(*) n FROM pull_companies WHERE {where}", args)["n"]
    rows = db.q(f"SELECT * FROM pull_companies WHERE {where} ORDER BY idx LIMIT ? OFFSET ?", (*args, limit, offset))
    for r in rows:
        if r["status"] != "ready":
            continue
        st = db.q1("""SELECT COUNT(*) found,
                      SUM(CASE WHEN state='done' AND email IS NOT NULL AND email!='' AND dup_email=0 AND
                          LOWER(COALESCE(email_status,''))!='invalid' THEN 1 ELSE 0 END) emails,
                      SUM(CASE WHEN submitted_at IS NOT NULL THEN 1 ELSE 0 END) paid,
                      SUM(CASE WHEN email_source='guessed' AND state='done' THEN 1 ELSE 0 END) guessed,
                      SUM(CASE WHEN state IN ('kept','queued') THEN 1 ELSE 0 END) waiting
                      FROM contacts WHERE pull_id=? AND state NOT IN ('skipped','banked','banked_in') AND
                      (domain=? OR domain LIKE ?)""", (pid, r["domain"], "%." + r["domain"]))
        r.update({k: st[k] or 0 for k in ("found", "emails", "paid", "guessed", "waiting")})
        r["format"] = guess.format_label(r["domain"])
        r["full"] = bool(cap) and r["found"] >= cap
    return {"rows": rows, "total": total}
