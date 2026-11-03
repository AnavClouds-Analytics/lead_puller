"""Icypeas (2.5): the second source after Seamless.

- Email lookups: people Seamless had no usable email for are sent to Icypeas in bulk (name + company website, up to
  ICYPEAS_BATCH a time). Icypeas works in the background; Lead Puller reads the answers a few seconds later.
- People search ("Find people"): on company lists, the companies that are still short when Seamless runs out are
  searched in Icypeas's database with the same job titles; those people then get an email lookup.

Fails safe everywhere: only an email Icypeas rates ultra_sure / very_sure (or probable, if ICYPEAS_ACCEPT_PROBABLE=1),
at the person's own company website, and not already somebody else's, is ever used. Errors, timeouts and unreadable
answers never become an email. Icypeas's credits are kept apart from the Seamless ledger (icypeas_log), so Seamless's
books stay exact. Mobile numbers are never requested.

Icypeas's answers are read tolerantly (several possible field names), and the last answer of each kind is kept
(icypeas samples, Accounts page) so a field that comes back under an unexpected name can be seen and fixed quickly.
"""
import json
import re
import time

import httpx

from . import db
from .config import cfg

TRANSPORT = None       # tests swap in a fake
NOW = time.time

DONE = {"NOT_FOUND", "DEBITED_NOT_FOUND", "FOUND", "DEBITED", "BAD_INPUT", "INSUFFICIENT_FUNDS", "ABORTED"}
CHARGED = {"DEBITED", "DEBITED_NOT_FOUND"}
SURE = {"ultra_sure", "very_sure", "sure"}

COUNTRY_CODES = {"united states": "US", "usa": "US", "canada": "CA", "israel": "IL", "united kingdom": "GB",
                 "germany": "DE", "netherlands": "NL", "france": "FR", "australia": "AU", "india": "IN",
                 "ireland": "IE", "spain": "ES", "italy": "IT", "sweden": "SE", "norway": "NO", "denmark": "DK",
                 "finland": "FI", "switzerland": "CH", "austria": "AT", "belgium": "BE", "poland": "PL",
                 "portugal": "PT", "singapore": "SG", "united arab emirates": "AE", "new zealand": "NZ",
                 "south africa": "ZA", "mexico": "MX", "brazil": "BR", "japan": "JP"}


class IcypeasError(Exception):
    pass


# ---------------------------------------------------------------- key, status
def _row():
    return db.q1("SELECT * FROM icypeas WHERE id=1")


def _ensure():
    if not _row():
        db.x("INSERT OR IGNORE INTO icypeas(id, enabled) VALUES(1, 1)")


def key():
    r = _row()
    if r and r["api_key"]:
        return r["api_key"]
    return cfg.ICYPEAS_API_KEY or ""


def account_email():
    r = _row()
    return (r and r["account_email"]) or cfg.ICYPEAS_ACCOUNT_EMAIL or ""


def _client(timeout=None):
    t = timeout or max(5, min(120, cfg.ICYPEAS_TIMEOUT_SECONDS))
    return httpx.Client(transport=TRANSPORT, timeout=httpx.Timeout(t, connect=10))


def _url(path):
    return cfg.ICYPEAS_BASE_URL.rstrip("/") + path


def _headers(k=None):
    return {"Authorization": k or key(), "Content-Type": "application/json"}


def _clean_key(raw):
    k = "".join(ch for ch in (raw or "") if ch.isprintable() and not ch.isspace())
    return k.strip("'\"`")


def _sample(kind, status, body):
    """Keep the last answer of each kind (shortened) so an unexpected field name can be seen on the Accounts page."""
    try:
        txt = json.dumps(body)[:4000]
    except (TypeError, ValueError):
        txt = str(body)[:4000]
    s = db.get_meta("icypeas_samples") or {}
    s[kind] = {"at": NOW(), "http": status, "body": txt}
    db.set_meta("icypeas_samples", s)


def _json(r):
    try:
        return r.json()
    except ValueError:
        return None


def _find_number(obj, words, inside=False):
    """First number in an answer whose key contains one of these words (e.g. 'credit'), or any number inside such a
    key ({"credits": {"remaining": 950}})."""
    if isinstance(obj, (int, float)) and not isinstance(obj, bool) and inside:
        return obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            hit = inside or any(w in str(k).lower() for w in words)
            if hit and isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
            if hit and isinstance(v, (dict, list)):
                n = _find_number(v, words, True)
                if n is not None:
                    return n
        for v in obj.values():
            n = _find_number(v, words, inside)
            if n is not None:
                return n
    elif isinstance(obj, list):
        for v in obj:
            n = _find_number(v, words, inside)
            if n is not None:
                return n
    return None


def check_key(api_key):
    """Free call (a count of people, which Icypeas doesn't charge). Raises IcypeasError if the key isn't accepted."""
    try:
        with _client(20) as c:
            r = c.post(_url("/find-people/count"), headers=_headers(api_key),
                       content=json.dumps({"query": {"currentJobTitle": {"include": ["CTO"]}}}))
    except httpx.HTTPError as e:
        raise IcypeasError(f"Icypeas didn't answer ({type(e).__name__}). Try again in a minute.")
    body = _json(r)
    _sample("count", r.status_code, body)
    if r.status_code in (401, 403):
        raise IcypeasError("Icypeas didn't accept this API key.")
    if r.status_code == 429:
        return True                                  # the key works; Icypeas just wants us to slow down
    if r.status_code != 200 or not isinstance(body, dict) or body.get("success") is False:
        why = (body or {}).get("message") if isinstance(body, dict) else None
        if isinstance(body, dict) and body.get("validationErrors"):
            why = "; ".join(str(v.get("humanReadableMessage") or v.get("message") or v) for v in body["validationErrors"])
        raise IcypeasError(f"Icypeas didn't accept this API key ({why or 'HTTP ' + str(r.status_code)}).")
    return True


def fetch_credits():
    """Credits left, from Icypeas's subscription information (needs your Icypeas account email). None if unknown."""
    k, em = key(), account_email()
    if not k or not em:
        return None
    try:
        with _client(20) as c:
            r = c.post(_url("/a/actions/subscription-information"), headers=_headers(k), content=json.dumps({"email": em}))
    except httpx.HTTPError:
        return None
    body = _json(r)
    _sample("subscription", r.status_code, body)
    if r.status_code != 200 or not isinstance(body, dict):
        return None
    n = _find_number(body, ("credit",))
    return float(n) if n is not None else None


def set_key(api_key, email=None):
    api_key = _clean_key(api_key)
    if len(api_key) < 8:
        raise IcypeasError("Paste the Icypeas API key (Icypeas → your profile → API).")
    check_key(api_key)                                 # a key Icypeas doesn't accept is never saved
    _ensure()
    db.x("UPDATE icypeas SET api_key=?, last_error=NULL, errors_in_row=0, paused_until=0, pause_reason=NULL WHERE id=1",
         (api_key,))
    if email is not None:
        set_email(email)
    refresh_credits(quiet=True)
    return status()


def set_email(email):
    em = (email or "").strip()
    if em and not db.norm_email(em):
        raise IcypeasError("That doesn't look like an email address.")
    _ensure()
    db.x("UPDATE icypeas SET account_email=? WHERE id=1", (em or None,))


def remove_key():
    _ensure()
    db.x("UPDATE icypeas SET api_key=NULL, credits=NULL, credits_at=NULL, last_error=NULL WHERE id=1")


def set_enabled(on):
    _ensure()
    db.x("UPDATE icypeas SET enabled=? WHERE id=1", (1 if on else 0,))


def refresh_credits(quiet=False):
    if not key():
        if quiet:
            return None
        raise IcypeasError("No Icypeas key yet.")
    n = fetch_credits()
    if n is not None:
        _ensure()
        db.x("UPDATE icypeas SET credits=?, credits_at=? WHERE id=1", (n, NOW()))
    elif not quiet and not account_email():
        raise IcypeasError("Add your Icypeas account email to see the credits left.")
    return n


def used_this_month():
    return round(db.q1("SELECT COALESCE(SUM(cost),0) s FROM icypeas_log WHERE at>=?", (db.month_start(),))["s"], 2)


def status():
    r = _row() or {}
    k = key()
    return {"has_key": bool(k), "key": ("•••• " + k[-4:]) if k else "", "from_env": bool(k) and not r.get("api_key"),
            "account_email": account_email(), "enabled": bool(r.get("enabled", 1)), "credits": r.get("credits"),
            "credits_at": r.get("credits_at"), "last_error": r.get("last_error"),
            "paused_until": r.get("paused_until") or 0,
            "pause_reason": r.get("pause_reason") if (r.get("paused_until") or 0) > NOW() else None,
            "on": cfg.ICYPEAS_ENABLED, "search_on": cfg.ICYPEAS_SEARCH, "used_this_month": used_this_month(),
            "accept_probable": cfg.ICYPEAS_ACCEPT_PROBABLE, "samples": db.get_meta("icypeas_samples") or {}}


def ready():
    """(True, None) if Icypeas can be asked right now, else (False, why). Never raises."""
    if not cfg.ICYPEAS_ENABLED:
        return False, "Icypeas is off (ICYPEAS_ENABLED=0 in .env)."
    r = _row() or {}
    if not key():
        return False, "No Icypeas key (add it on the Accounts page)."
    if not r.get("enabled", 1):
        return False, "Icypeas is switched off on the Accounts page."
    if (r.get("paused_until") or 0) > NOW():
        return False, r.get("pause_reason") or "Icypeas had errors; it's asked again by itself shortly."
    if r.get("credits") is not None and r["credits"] < max(1, cfg.ICYPEAS_MIN_CREDITS_LEFT):
        return False, f"Icypeas has fewer than {cfg.ICYPEAS_MIN_CREDITS_LEFT} credits left (ICYPEAS_MIN_CREDITS_LEFT)."
    return True, None


def spent(pull_id):
    return round(db.q1("SELECT COALESCE(SUM(cost),0) s FROM icypeas_log WHERE pull_id=?", (pull_id,))["s"], 2)


def room(pull_id):
    cap = cfg.ICYPEAS_MAX_CREDITS_PER_PULL
    if not cap:
        return None
    return max(0.0, cap - spent(pull_id))


def _error(msg):
    _ensure()
    r = _row()
    n = (r["errors_in_row"] or 0) + 1
    pause = NOW() + 1800 if n >= max(1, cfg.ICYPEAS_ERRORS_PAUSE) else (r["paused_until"] or 0)
    db.x("UPDATE icypeas SET errors_in_row=?, last_error=?, paused_until=?, pause_reason=? WHERE id=1",
         (n, msg[:300], pause, "Icypeas had errors; it's asked again by itself shortly." if pause > NOW() else None))


def _ok():
    _ensure()
    db.x("UPDATE icypeas SET errors_in_row=0, last_error=NULL WHERE id=1")


def _slow_down(seconds=60):
    """A 429: wait, never an error; nobody is given up on."""
    _ensure()
    db.x("UPDATE icypeas SET paused_until=?, pause_reason=?, last_error=NULL WHERE id=1",
         (NOW() + seconds, "Icypeas asked Lead Puller to slow down (rate limit); waiting a minute."))


def _paced(kind):
    """Icypeas's per-route limits: bulk search 1 a second, reading results 30 a minute (we keep under 25)."""
    m = db.get_meta("icypeas_calls") or {}
    now = NOW()
    if kind == "read":
        recent = [t for t in m.get("read", []) if now - t < 60]
        if len(recent) >= max(1, cfg.ICYPEAS_READS_PER_MINUTE):
            return False
        m["read"] = recent + [now]
    else:
        if now - (m.get(kind) or 0) < max(0.0, cfg.ICYPEAS_SECONDS_BETWEEN_CALLS):
            return False
        m[kind] = now
    db.set_meta("icypeas_calls", m)
    return True


def _log(pull_id, c, result, cost, email, note):
    keys = db.person_keys(c.get("name"), c.get("company"), c.get("li_url"), c.get("row_email")) if c else []
    db.x("INSERT INTO icypeas_log(at,pull_id,contact_id,person_key,result,cost,email,note) VALUES(?,?,?,?,?,?,?,?)",
         (NOW(), pull_id, c["id"] if c else None, "\n".join(keys) or None, result, cost, email, (note or "")[:300]))


def recently_asked(keys):
    since = NOW() - 90 * 86400
    for k in keys:
        r = db.q1("SELECT * FROM icypeas_log WHERE instr(char(10) || person_key || char(10), ?) > 0 AND at>=? AND "
                  "result IN ('found','none','mismatch') ORDER BY id DESC LIMIT 1", ("\n" + k + "\n", since))
        if r:
            return r
    return None


# ---------------------------------------------------------------- email lookups (bulk, in the background)
def lookup_input(c):
    """[firstname, lastname, website] for one person, or None when there isn't enough to look them up."""
    first, last = (c.get("first_name") or "").strip(), (c.get("last_name") or "").strip()
    if not (first and last):
        parts = (c.get("name") or "").split()
        if len(parts) >= 2:
            first, last = first or parts[0], last or parts[-1]
    dom = db.norm_domain(c.get("domain"))
    if first and last and dom:
        return [first, last, dom]
    return None


def submit(people, pull_id, name):
    """Start one bulk email lookup for these people. Returns (job_id or None, why)."""
    rows = [(c, lookup_input(c)) for c in people]
    rows = [(c, r) for c, r in rows if r]
    if not rows:
        return None, "nothing to look up"
    ok, why = ready()
    if not ok:
        return None, why
    if not _paced("bulk"):
        return None, "later"
    body = {"task": "email-search", "name": name[:80], "data": [r for _, r in rows],
            "custom": {"externalIds": [str(c["id"]) for c, _ in rows]}}
    try:
        with _client() as cl:
            r = cl.post(_url("/bulk-search"), headers=_headers(), content=json.dumps(body))
    except httpx.HTTPError as e:
        _error(f"Icypeas didn't answer ({type(e).__name__})")
        return None, "error"
    resp = _json(r)
    _sample("bulk-search", r.status_code, resp)
    if r.status_code == 429:
        _slow_down()
        return None, "later"
    if r.status_code in (401, 403):
        _error("Icypeas didn't accept the API key (Accounts page).")
        return None, "error"
    if r.status_code != 200 or not isinstance(resp, dict) or resp.get("success") is False:
        msg = _validation(resp) or f"HTTP {r.status_code}"
        if "credit" in msg.lower() or "fund" in msg.lower():
            _ensure()
            db.x("UPDATE icypeas SET credits=0, credits_at=? WHERE id=1", (NOW(),))
            msg = "Icypeas has no credits left."
        elif "subscription" in msg.lower():
            msg = "Icypeas says the subscription isn't paid (the API needs a paid plan)."
        _error(f"Icypeas: {msg}")
        return None, "error"
    job = _job_id(resp)
    if not job:
        _error("Icypeas started the lookup but didn't say its id (see the last answer on the Accounts page).")
        return None, "error"
    _ok()
    now = NOW()
    db.x("INSERT OR REPLACE INTO icypeas_jobs(job,pull_id,created_at,rows_n,polls,last_poll_at) VALUES(?,?,?,?,0,0)",
         (job, pull_id, now, len(rows)))
    ids = [c["id"] for c, _ in rows]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        db.x(f"UPDATE contacts SET icy_state='asked', icy_job=?, icy_asked_at=? WHERE id IN ({','.join('?' * len(chunk))})",
             (job, now, *chunk))
    return job, None


def _validation(resp):
    if not isinstance(resp, dict):
        return ""
    errs = resp.get("validationErrors") or resp.get("errors") or []
    if isinstance(errs, list) and errs:
        return "; ".join(str((e or {}).get("humanReadableMessage") or (e or {}).get("message") or e) for e in errs)[:200]
    return str(resp.get("message") or resp.get("error") or "")[:200]


def _job_id(resp):
    for path in (("file",), ("item", "_id"), ("item", "file"), ("_id",), ("id",), ("fileId",), ("data", "file"),
                 ("data", "_id")):
        v = resp
        for p in path:
            v = v.get(p) if isinstance(v, dict) else None
        if isinstance(v, str) and v:
            return v
    return None


def _items(resp):
    if isinstance(resp, list):
        return [x for x in resp if isinstance(x, dict)]
    if not isinstance(resp, dict):
        return []
    for k in ("items", "results", "data", "searches", "rows"):
        v = resp.get(k)
        if isinstance(v, list) and all(isinstance(x, dict) for x in v):
            return v
    for v in resp.values():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
    return []


def _external_id(item):
    for path in (("userData", "externalId"), ("custom", "externalId"), ("externalId",), ("userData", "external_id")):
        v = item
        for p in path:
            v = v.get(p) if isinstance(v, dict) else None
        if v not in (None, ""):
            return str(v)
    return None


def _emails(item):
    """[(email, certainty)] from one search item, best first."""
    res = item.get("results") if isinstance(item.get("results"), dict) else {}
    lst = res.get("emails") if isinstance(res.get("emails"), list) else (item.get("emails") or [])
    out = []
    for e in lst if isinstance(lst, list) else []:
        if isinstance(e, dict):
            out.append((db.norm_email(e.get("email")), str(e.get("certainty") or "").lower()))
        elif isinstance(e, str):
            out.append((db.norm_email(e), ""))
    rank = {"ultra_sure": 0, "very_sure": 0, "sure": 1, "probable": 2}
    return sorted([o for o in out if o[0]], key=lambda o: rank.get(o[1], 9))


def read(job, pull_id, contacts_by_id):
    """Read a bulk lookup's answers (free). Returns ({contact_id: (result, email, note)}, finished, unreadable)
    where result is 'found' | 'none' | 'error' and finished says every row has its final answer."""
    if not _paced("read"):
        return {}, False, 0
    body = {"mode": "bulk", "file": job, "limit": 100}
    try:
        with _client() as cl:
            r = cl.post(_url("/bulk-single-searchs/read"), headers=_headers(), content=json.dumps(body))
    except httpx.HTTPError:
        return {}, False, 0
    resp = _json(r)
    _sample("read", r.status_code, resp)
    if r.status_code == 429:
        _slow_down()
        return {}, False, 0
    if r.status_code != 200 or not isinstance(resp, dict) or resp.get("success") is False:
        return {}, False, 0
    items = _items(resp)
    out, unreadable, pending, charged = {}, 0, 0, 0
    for idx, it in enumerate(items):
        st = str(it.get("status") or "").upper()
        ext = _external_id(it)
        if ext and ext.isdigit() and int(ext) not in contacts_by_id:
            continue                                     # someone already settled by an earlier read
        cid = int(ext) if ext and ext.isdigit() else None   # never matched by position: a wrong match = wrong email
        if cid is None:
            unreadable += 1
            continue
        if st not in DONE:
            pending += 1
            continue
        c = contacts_by_id[cid]
        cost = 1 if st in CHARGED else 0
        charged += cost
        if st in ("BAD_INPUT", "ABORTED", "INSUFFICIENT_FUNDS"):
            out[cid] = ("error" if st == "INSUFFICIENT_FUNDS" else "none", None, f"Icypeas: {st.lower()}")
            _log(pull_id, c, out[cid][0], cost, None, out[cid][2])
            if st == "INSUFFICIENT_FUNDS":
                _ensure()
                db.x("UPDATE icypeas SET credits=0, credits_at=? WHERE id=1", (NOW(),))
            continue
        best = _emails(it)
        ok = [e for e in best if e[1] in SURE or (cfg.ICYPEAS_ACCEPT_PROBABLE and e[1] == "probable")]
        dom = db.norm_domain(c.get("domain"))
        ok = [e for e in ok if dom and (e[0].endswith("@" + dom) or e[0].split("@")[1].endswith("." + dom))]
        if st in ("FOUND", "DEBITED") and ok:
            out[cid] = ("found", ok[0][0], f"Found by Icypeas ({ok[0][1].replace('_', ' ')}).")
        elif st in ("FOUND", "DEBITED") and best and not [e for e in best if e[1] in SURE or (
                cfg.ICYPEAS_ACCEPT_PROBABLE and e[1] == "probable")]:
            out[cid] = ("none", None, f"Icypeas only had a {(best[0][1] or 'uncertain').replace('_', ' ')} email "
                                      f"(not used).")
        elif st in ("FOUND", "DEBITED") and best:
            out[cid] = ("none", None, f"Icypeas's email ({best[0][0].split('@')[-1]}) isn't at this person's company "
                                      f"(not used).")
        else:
            out[cid] = ("none", None, "Icypeas has no email for this person.")
        _log(pull_id, c, out[cid][0], cost, out[cid][1], out[cid][2])
    _ensure()
    if items:
        _ok()
    note_spent_credits(charged)
    finished = bool(items) and not pending and len(out) + unreadable >= len(contacts_by_id)
    return out, finished, unreadable


def note_spent_credits(n):
    r = _row()
    if r and r["credits"] is not None and n:
        db.x("UPDATE icypeas SET credits=MAX(0, credits-?), credits_at=? WHERE id=1", (n, NOW()))


# ---------------------------------------------------------------- people search ("Find people")
def search_query(websites, titles, title_out, countries):
    q = {"currentCompanyWebsite": {"include": list(websites)[:100]}}
    if titles:
        q["currentJobTitle"] = {"include": list(titles)[:50]}
        if title_out:
            q["currentJobTitle"]["exclude"] = list(title_out)[:50]
    codes = [COUNTRY_CODES.get((c or "").strip().lower()) for c in (countries or [])]
    if countries and all(codes):
        q["location"] = {"include": sorted(set(codes))}
    return q


def find_people(websites, titles, title_out, countries, token=None, pull_id=None):
    """One page (up to 100 people) of Icypeas's people search. 0.02 credits per person returned.
    Returns (leads, next_token, cost, stop) where stop is None, 'later', or a reason to stop searching."""
    ok, why = ready()
    if not ok:
        return [], None, 0, why
    if not _paced("find"):
        return [], token, 0, "later"
    body = {"query": search_query(websites, titles, title_out, countries), "pagination": {"size": 100}}
    if token:
        body["pagination"]["token"] = token
    try:
        with _client() as cl:
            r = cl.post(_url("/find-people"), headers=_headers(), content=json.dumps(body))
    except httpx.HTTPError as e:
        _error(f"Icypeas search didn't answer ({type(e).__name__})")
        return [], token, 0, "later"
    resp = _json(r)
    _sample("find-people", r.status_code, resp)
    if r.status_code == 429:
        _slow_down()
        return [], token, 0, "later"
    if r.status_code != 200 or not isinstance(resp, dict) or resp.get("success") is False:
        msg = _validation(resp) or f"HTTP {r.status_code}"
        if "subscription" in msg.lower():
            return [], None, 0, "Icypeas says the subscription isn't paid (the API needs a paid plan)."
        _error(f"Icypeas search: {msg}")
        return [], None, 0, f"Icypeas didn't accept the search: {msg}"
    _ok()
    leads = resp.get("leads") if isinstance(resp.get("leads"), list) else _items(resp)
    cost = round(0.02 * len(leads), 2)
    db.x("INSERT INTO icypeas_log(at,pull_id,contact_id,person_key,result,cost,email,note) VALUES(?,?,?,?,?,?,?,?)",
         (NOW(), pull_id, None, None, "search", cost, None, f"people search: {len(leads)} people at {len(websites)} companies"))
    note_spent_credits(cost)
    pag = resp.get("pagination") if isinstance(resp.get("pagination"), dict) else {}
    nxt = pag.get("token") if len(leads) >= 100 else None
    return leads, nxt, cost, None


def _pick(d, *names):
    for n in names:
        v = d
        for p in n.split("."):
            v = v.get(p) if isinstance(v, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def read_lead(lead):
    """One person from a people search, in Lead Puller's terms (tolerant of field names). {} if unreadable."""
    if not isinstance(lead, dict):
        return {}
    first = _pick(lead, "firstname", "firstName", "first_name")
    last = _pick(lead, "lastname", "lastName", "last_name")
    full = _pick(lead, "fullname", "fullName", "full_name", "name") or f"{first} {last}".strip()
    if not (first and last) and len(full.split()) >= 2:
        first, last = first or full.split()[0], last or full.split()[-1]
    title = _pick(lead, "lastJobTitle", "currentJobTitle", "jobTitle", "title", "headline",
                  "lastJob.title", "currentJob.title", "experiences.0.title")
    site = _pick(lead, "lastCompanyWebsite", "currentCompanyWebsite", "companyWebsite", "companyDomain", "website",
                 "lastCompany.website", "currentCompany.website", "company.website", "company.domain")
    company = _pick(lead, "lastCompanyName", "currentCompanyName", "companyName", "lastCompany.name",
                    "currentCompany.name", "company.name")
    li = _pick(lead, "profileUrl", "linkedinUrl", "linkedin_url", "url", "lid")
    if li and "linkedin.com" not in li.lower():
        li = ""
    country = _pick(lead, "country", "address.country", "location.country", "countryCode", "location")
    if not (first and last and site):
        return {}
    return {"first_name": first, "last_name": last, "name": f"{first} {last}", "title": title,
            "domain": db.norm_domain(site), "company": company, "li_url": li, "country": country}


def country_ok(lead_country, wanted, unwanted):
    lc = (lead_country or "").strip().lower()
    if not lc:
        return not wanted                             # unknown country: only when you didn't choose countries
    codes = {v.lower(): k for k, v in COUNTRY_CODES.items()}
    names = {lc, codes.get(lc, ""), (COUNTRY_CODES.get(lc) or "").lower()}
    want = {w.lower() for w in wanted}
    want |= {COUNTRY_CODES.get(w.lower(), "").lower() for w in wanted}
    no = {w.lower() for w in unwanted} | {COUNTRY_CODES.get(w.lower(), "").lower() for w in unwanted}
    if wanted and not (names & want):
        return False
    return not (names & no - {""})
