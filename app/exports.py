"""Downloads: AnavMail batches (never the same email twice, across all pulls), country split, no-email file,
full CSV / Excel. Every text cell is protected against spreadsheet formulas."""
import csv
import io
import json
import time
import zipfile

from . import db
from .config import cfg

# ---------------------------------------------------------------- column catalog
# Every column a download can have: (header, label, group, getter(row, research, search)).
# The header is what appears in the file; it never changes, so AnavMail/AnavAgent mappings stay stable.
def _j(text):
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def _g(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return ""
        d = d.get(p)
    if d is None:
        return ""
    if isinstance(d, list):
        return ", ".join(str(x) for x in d)
    return d


def _hist(R, i, key):
    h = R.get("jobHistory") or []
    return (h[i] or {}).get(key) or "" if len(h) > i and isinstance(h[i], dict) else ""


COLUMNS = [
    # Person
    ("first_name", "First name", "Person", lambda r, R, S: r["first_name"]),
    ("middle_name", "Middle name", "Person", lambda r, R, S: _g(R, "middleName") or _g(S, "middleName")),
    ("last_name", "Last name", "Person", lambda r, R, S: r["last_name"]),
    ("full_name", "Full name", "Person", lambda r, R, S: r["name"]),
    ("title", "Job title", "Person", lambda r, R, S: r["title"]),
    ("seniority", "Seniority", "Person", lambda r, R, S: r["seniority"]),
    ("department", "Department", "Person", lambda r, R, S: r["department"]),
    ("time_in_role", "Time in role", "Person", lambda r, R, S: _g(R, "timeAtRole") or _g(S, "timeAtRole")),
    ("time_at_company", "Time at company", "Person", lambda r, R, S: _g(R, "timeAtCompany") or _g(S, "timeAtCompany")),
    ("role_started", "Role start date", "Person", lambda r, R, S: r["title_started_at"]),
    # Emails
    ("email", "Email", "Emails", lambda r, R, S: r["email"]),
    ("email_status", "Email status (Seamless)", "Emails", lambda r, R, S: r["email_status"]),
    ("email_confidence", "Email confidence", "Emails", lambda r, R, S: r["email_confidence"]),
    ("email_picked_from", "Which email was picked", "Emails", lambda r, R, S: r.get("email_pick") or ""),
    ("seamless_primary_email", "Seamless primary email", "Emails", lambda r, R, S: _g(R, "email")),
    ("email_1", "Email 1", "Emails", lambda r, R, S: _g(R, "email1")),
    ("email_1_status", "Email 1 status", "Emails", lambda r, R, S: _g(R, "email1EmailAI")),
    ("email_1_confidence", "Email 1 confidence", "Emails", lambda r, R, S: _g(R, "email1TotalAI")),
    ("email_2", "Email 2", "Emails", lambda r, R, S: _g(R, "email2")),
    ("email_2_status", "Email 2 status", "Emails", lambda r, R, S: _g(R, "email2EmailAI")),
    ("email_2_confidence", "Email 2 confidence", "Emails", lambda r, R, S: _g(R, "email2TotalAI")),
    ("email_3", "Email 3", "Emails", lambda r, R, S: _g(R, "email3")),
    ("email_3_status", "Email 3 status", "Emails", lambda r, R, S: _g(R, "email3EmailAI")),
    ("email_3_confidence", "Email 3 confidence", "Emails", lambda r, R, S: _g(R, "email3TotalAI")),
    ("personal_email", "Personal email", "Emails", lambda r, R, S: r["personal_email"]),
    # Phones
    ("phone", "Direct phone", "Phones", lambda r, R, S: r["phone"]),
    ("phone_type", "Direct phone type", "Phones", lambda r, R, S: r["phone_type"]),
    ("phone_confidence", "Direct phone confidence", "Phones", lambda r, R, S: _g(R, "contactPhone1TotalAI")),
    ("phone_2", "Direct phone 2", "Phones", lambda r, R, S: _g(R, "contactPhone2")),
    ("phone_2_type", "Direct phone 2 type", "Phones", lambda r, R, S: _g(R, "contactPhone2DataType")),
    ("company_phone", "Company phone", "Phones", lambda r, R, S: r["company_phone"]),
    ("company_phone_confidence", "Company phone confidence", "Phones", lambda r, R, S: _g(R, "companyPhone1TotalAI")),
    ("company_phone_2", "Company phone 2", "Phones", lambda r, R, S: _g(R, "companyPhone2")),
    ("company_phone_3", "Company phone 3", "Phones", lambda r, R, S: _g(R, "companyPhone3")),
    # Links
    ("linkedin", "LinkedIn", "Links", lambda r, R, S: r["li_url"]),
    ("sales_navigator", "Sales Navigator", "Links", lambda r, R, S: _g(R, "lISalesNavUrl") or _g(S, "contactLISalesNavUrl")),
    ("linkedin_recruiter", "LinkedIn Recruiter", "Links", lambda r, R, S: _g(R, "lIRecruiterUrl") or _g(S, "contactLIRecruiterUrl")),
    ("company_linkedin", "Company LinkedIn", "Links", lambda r, R, S: _g(R, "companyLIProfileUrl") or _g(S, "companyLIProfileUrl")),
    # Person location
    ("city", "City", "Location", lambda r, R, S: r["city"]),
    ("state", "State / region", "Location", lambda r, R, S: r["state"]),
    ("country", "Country", "Location", lambda r, R, S: r["country"]),
    ("postcode", "Postcode", "Location", lambda r, R, S: _g(R, "contactLocation", "postCode")),
    ("time_zone", "Time zone", "Location", lambda r, R, S: r["timezone"]),
    ("local_time", "Local time (at research)", "Location", lambda r, R, S: _g(R, "localTime")),
    # Company
    ("company", "Company", "Company", lambda r, R, S: r["company"]),
    ("domain", "Website", "Company", lambda r, R, S: r["domain"]),
    ("company_description", "Company description", "Company", lambda r, R, S: _g(R, "companyDescription")),
    ("industry", "Industry", "Company", lambda r, R, S: r["industry"] or _g(R, "companyIndustry")),
    ("sic", "SIC code", "Company", lambda r, R, S: _g(R, "sicCode") or _g(S, "sicCode")),
    ("naics", "NAICS code", "Company", lambda r, R, S: _g(R, "naicsCode")),
    ("employees", "Employees (approx.)", "Company", lambda r, R, S: _g(R, "companyStaffCount")),
    ("company_size", "Company size", "Company", lambda r, R, S: r["size"]),
    ("revenue_range", "Revenue range", "Company", lambda r, R, S: _g(R, "companyRevenue") or _g(S, "companyRevenue")),
    ("revenue_estimate", "Revenue estimate (USD)", "Company", lambda r, R, S: _g(R, "companyAnnualRevenue")),
    ("founded", "Year founded", "Company", lambda r, R, S: _g(R, "companyFounded") or str(_g(S, "companyFoundedOn"))[:4]),
    ("company_type", "Public / private", "Company", lambda r, R, S: _g(R, "companyType") or _g(S, "companyType")),
    ("stock_ticker", "Stock ticker", "Company", lambda r, R, S: _g(R, "stockTicker") or _g(S, "stockTicker")),
    # Company HQ
    ("hq_street", "HQ street", "Company HQ", lambda r, R, S: _g(R, "companyLocation", "street1")),
    ("hq_city", "HQ city", "Company HQ", lambda r, R, S: _g(R, "companyLocation", "city") or _g(S, "companyCity")),
    ("hq_state", "HQ state", "Company HQ", lambda r, R, S: _g(R, "companyLocation", "state") or _g(S, "companyState")),
    ("hq_postcode", "HQ postcode", "Company HQ", lambda r, R, S: _g(R, "companyLocation", "postCode")),
    ("hq_country", "HQ country", "Company HQ", lambda r, R, S: _g(R, "companyLocation", "country") or r["company_country"]),
    # Signals
    ("funding", "Latest funding", "Signals", lambda r, R, S: r["funding"]),
    ("funding_total", "Total funding (USD)", "Signals", lambda r, R, S: _g(R, "companyFundingTotal") or _g(S, "companyFundingTotal")),
    ("news", "Recent company news", "Signals", lambda r, R, S: r["news"]),
    ("job_change", "Recent job change", "Signals", lambda r, R, S: _g(R, "jobChangeAlert") or _g(S, "jobChangeAlert")),
    # History
    ("previous_company", "Previous company", "History", lambda r, R, S: _g(R, "formerCompany") or _g(S, "formerCompany")),
    ("previous_title", "Previous title", "History", lambda r, R, S: _g(R, "formerTitle") or _g(S, "formerTitle")),
    ("previous_start", "Previous job start", "History", lambda r, R, S: _g(R, "formerStartedAt") or _g(S, "formerStartedAt")),
    ("previous_end", "Previous job end", "History", lambda r, R, S: _g(R, "formerEndedAt") or _g(S, "formerEndedAt")),
    ("previous_company_2", "Job before that: company", "History", lambda r, R, S: _hist(R, 1, "companyName")),
    ("previous_title_2", "Job before that: title", "History", lambda r, R, S: _hist(R, 1, "title")),
    # This tool
    ("fit", "Fit (Claude)", "This tool", lambda r, R, S: r["fit"]),
    ("fit_reason", "Fit note", "This tool", lambda r, R, S: r["fit_reason"]),
    ("email_chance", "Email chance %", "This tool", lambda r, R, S: "" if r["email_chance"] is None else r["email_chance"]),
    ("seamless_result", "Seamless result", "This tool", lambda r, R, S: FRIENDLY.get((r["research_status"] or "").lower(),
                                                                                    r["research_status"])),
    ("result_note", "Result note", "This tool", lambda r, R, S: r["research_message"]),
    ("batch", "Download batch", "This tool", lambda r, R, S: r["batch_id"] or ""),
    ("pull", "Pull #", "This tool", lambda r, R, S: r["pull_id"]),
]
FRIENDLY = {
    "done": "Found", "not found": "Not found in Seamless", "missing": "Not found in Seamless",
    "duplicate": "Already researched in your Seamless account before (no new charge)",
    "contact-already-researched": "Already researched in your Seamless account before (no new charge)",
    "error": "Seamless error", "no license or credits available": "No credits / license at the time",
    "timed out": "Seamless took too long", "no answer": "Seamless returned no result",
}
COLUMN_BY_KEY = {c[0]: c for c in COLUMNS}
ALL_KEYS = [c[0] for c in COLUMNS]
NO_EMAIL_DEFAULT = ["first_name", "last_name", "title", "company", "domain", "linkedin", "phone", "phone_type",
                    "company_phone", "country", "city", "time_zone", "seamless_result"]


def catalog():
    return [{"key": k, "label": label, "group": g} for k, label, g, _ in COLUMNS]


def parse_columns(cols, fallback, need_email=False):
    """A list or comma-separated string of column keys -> known keys, in order, no repeats."""
    if isinstance(cols, str):
        cols = [c.strip() for c in cols.split(",")]
    out = []
    for c in cols or []:
        c = (c or "").strip().lower()
        if c in COLUMN_BY_KEY and c not in out:
            out.append(c)
    if not out:
        out = list(fallback)
    if need_email and "email" not in out:
        out.insert(0, "email")
    return out


def default_columns():
    return parse_columns(cfg.DEFAULT_EXPORT_COLUMNS, ["email", "first_name", "last_name", "company"], need_email=True)


def rows_to_csv(rows, columns):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        R, S = _j(r.get("raw_research")), _j(r.get("raw_search"))
        w.writerow([cell(str(COLUMN_BY_KEY[k][3](r, R, S) or "")) for k in columns])
    return buf.getvalue().encode("utf-8-sig")


def rows_to_xlsx(rows, columns):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Contacts"
    ws.append([COLUMN_BY_KEY[k][1] for k in columns])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        R, S = _j(r.get("raw_research")), _j(r.get("raw_search"))
        ws.append([cell(str(COLUMN_BY_KEY[k][3](r, R, S) or "")) for k in columns])
    ws.freeze_panes = "A2"
    for i, k in enumerate(columns, start=1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = 30 if k in (
            "email", "linkedin", "company", "title", "news", "company_description") else 16
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def cell(v):
    """Stop spreadsheet formula injection: text starting with = + - @ (or tab/CR) is prefixed with '."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def risky(status):
    s = (status or "").lower()
    return bool(s) and s not in ("valid", "verified", "deliverable")


def _excluded_email(email, excl):
    e = email.lower()
    dom = e.split("@")[-1]
    if "em:" + e in excl:
        return True
    parts = dom.split(".")
    return any("domain:" + ".".join(parts[i:]) in excl for i in range(len(parts) - 1))


def eligible(pull_id, include_risky=True):
    """People in this pull who can go into the next AnavMail batch, in order."""
    rows = db.q("""SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL AND batch_id IS NULL
                   AND email IS NOT NULL AND email!='' ORDER BY id""", (pull_id,))
    excl = {r["key"] for r in db.q("SELECT key FROM exclusions WHERE kind IN ('email','domain')")}
    names = [db.norm(r["value"]) for r in db.q("SELECT value FROM exclusions WHERE kind='company_name'")]
    taken = {r["email"] for r in db.q("SELECT email FROM downloaded")}
    out, seen = [], set()
    for r in rows:
        e = r["email"].strip().lower()
        if (r["email_status"] or "").lower() == "invalid":
            continue
        if not include_risky and risky(r["email_status"]):
            continue
        if e in seen or e in taken or _excluded_email(e, excl):
            continue
        comp = " " + db.norm(r["company"]) + " "
        if any(n and (" " + n + " ") in comp for n in names):
            continue
        seen.add(e)
        out.append(r)
    return out


def create_batch(pull_id, count, split="one", include_risky=True, columns=None):
    columns = parse_columns(columns, default_columns(), need_email=True)
    count = int(count or 0)
    if count < 1:
        raise ValueError("Enter how many people to download.")
    with db.tx():
        rows = eligible(pull_id, include_risky)[:count]
        if not rows:
            raise ValueError("Nobody left to download (everyone with an email is already in a batch).")
        now = time.time()
        countries = {}
        for r in rows:
            countries[r["country"] or "Unknown"] = countries.get(r["country"] or "Unknown", 0) + 1
        bid = db.x("""INSERT INTO batches(pull_id, created_at, count, split, include_risky, extras, countries_json,
                      columns_json) VALUES(?,?,?,?,?,?,?,?)""", (pull_id, now, len(rows), split,
                                                                1 if include_risky else 0, 0, json.dumps(countries),
                                                                json.dumps(columns)))
        for r in rows:
            db.x("UPDATE contacts SET batch_id=? WHERE id=? AND batch_id IS NULL", (bid, r["id"]))
            db.x("INSERT INTO downloaded(email, batch_id, at) VALUES(?,?,?)", (r["email"].strip().lower(), bid, now))
    return bid


def safe_name(s, fallback):
    out = "".join(ch if ch.isalnum() else "-" for ch in (s or "")).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:40] or fallback


def batch_file(batch_id):
    b = db.q1("SELECT * FROM batches WHERE id=?", (batch_id,))
    if not b:
        return None
    pull = db.get_pull(b["pull_id"])
    rows = db.q("SELECT * FROM contacts WHERE batch_id=? ORDER BY id", (batch_id,))
    columns = parse_columns(_j(b.get("columns_json")) or [], default_columns(), need_email=True)
    base = f"{safe_name(pull['name'] if pull else '', 'pull')}-batch{batch_id}"
    if b["split"] == "country":
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            by = {}
            for r in rows:
                by.setdefault(r["country"] or "Unknown", []).append(r)
            for country, rs in sorted(by.items()):
                zf.writestr(f"{base}-{safe_name(country, 'Unknown')}.csv", rows_to_csv(rs, columns))
        return z.getvalue(), f"{base}-by-country.zip", "application/zip"
    return rows_to_csv(rows, columns), f"{base}.csv", "text/csv"


def no_email_rows(pull_id):
    return db.q("""SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL AND (email IS NULL OR email='')
                   AND research_status NOT IN ('queued','researching') ORDER BY id""", (pull_id,))


def all_rows(pull_id):
    """Everyone researched in the pull, minus anything on the do-not-pull list (emails / blocked domains)."""
    excl = {r["key"] for r in db.q("SELECT key FROM exclusions WHERE kind IN ('email','domain')")}
    out = []
    for r in db.q("SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL ORDER BY id", (pull_id,)):
        if r["email"] and _excluded_email(r["email"].strip(), excl):
            continue
        out.append(r)
    return out
