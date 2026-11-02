"""Prospeo (2.3): the second source for emails Seamless doesn't have.

Fails safe everywhere: only an email Prospeo marks VERIFIED, for the same person (same name, same company website or
LinkedIn) and not already somebody else's, is ever used. Errors, timeouts and unreadable answers count as "not
found" for now (the person is simply tried again later or left without an email); they never become an email.
Prospeo charges 1 credit per email found and nothing when it finds nothing; asking about the same person again
within 90 days is free. Its credits are kept apart from the Seamless ledger (prospeo_log), so Seamless's books stay
exact.
"""
import json
import time

import httpx

from . import db
from .config import cfg

TRANSPORT = None       # tests swap in a fake
NOW = time.time


class ProspeoError(Exception):
    pass


def _row():
    return db.q1("SELECT * FROM prospeo WHERE id=1")


def _ensure():
    if not _row():
        db.x("INSERT OR IGNORE INTO prospeo(id, enabled) VALUES(1, 1)")


def key():
    r = _row()
    if r and r["api_key"]:
        return r["api_key"]
    return cfg.PROSPEO_API_KEY or ""


def _client(timeout=None):
    t = timeout or max(5, min(120, cfg.PROSPEO_TIMEOUT_SECONDS))
    return httpx.Client(transport=TRANSPORT, timeout=httpx.Timeout(t, connect=10))


def _url(path):
    return cfg.PROSPEO_BASE_URL.rstrip("/") + path


def _clean_key(raw):
    k = "".join(ch for ch in (raw or "") if ch.isprintable() and not ch.isspace())
    return k.strip("'\"`")


def fetch_account(api_key):
    """Free call: plan and credits left. Raises ProspeoError if Prospeo doesn't accept the key."""
    try:
        with _client(20) as c:
            r = c.get(_url("/account-information"), headers={"X-KEY": api_key})
    except httpx.HTTPError as e:
        raise ProspeoError(f"Prospeo didn't answer ({type(e).__name__}). Try again in a minute.")
    try:
        body = r.json()
    except ValueError:
        body = {}
    resp = body.get("response") if isinstance(body.get("response"), dict) else body
    if r.status_code != 200 or body.get("error") or "remaining_credits" not in (resp or {}):
        why = body.get("error_code") or body.get("message") or f"HTTP {r.status_code}"
        raise ProspeoError(f"Prospeo didn't accept this key ({why}).")
    return int(resp.get("remaining_credits") or 0), str(resp.get("current_plan") or "")


def set_key(api_key):
    api_key = _clean_key(api_key)
    if len(api_key) < 8:
        raise ProspeoError("Paste the Prospeo API key (Prospeo → API).")
    credits, plan = fetch_account(api_key)          # a key Prospeo doesn't accept is never saved
    _ensure()
    db.x("UPDATE prospeo SET api_key=?, credits=?, credits_at=?, plan=?, last_error=NULL, errors_in_row=0, "
         "paused_until=0 WHERE id=1", (api_key, credits, NOW(), plan))
    return status()


def remove_key():
    _ensure()
    db.x("UPDATE prospeo SET api_key=NULL, credits=NULL, credits_at=NULL, plan=NULL, last_error=NULL WHERE id=1")


def set_enabled(on):
    _ensure()
    db.x("UPDATE prospeo SET enabled=? WHERE id=1", (1 if on else 0,))


def refresh_credits():
    k = key()
    if not k:
        raise ProspeoError("No Prospeo key yet.")
    _ensure()
    try:
        credits, plan = fetch_account(k)
    except ProspeoError as e:
        db.x("UPDATE prospeo SET last_error=? WHERE id=1", (str(e),))
        raise
    db.x("UPDATE prospeo SET credits=?, credits_at=?, plan=?, last_error=NULL WHERE id=1", (credits, NOW(), plan))
    return credits


def used_this_month():
    return db.q1("SELECT COALESCE(SUM(cost),0) s FROM prospeo_log WHERE at>=?", (db.month_start(),))["s"]


def status():
    """For the Accounts page (the key is shown as its last 4 characters only)."""
    r = _row() or {}
    k = key()
    return {"has_key": bool(k), "key": ("•••• " + k[-4:]) if k else "", "from_env": bool(k) and not r.get("api_key"),
            "enabled": bool(r.get("enabled", 1)), "credits": r.get("credits"), "credits_at": r.get("credits_at"),
            "plan": r.get("plan"), "last_error": r.get("last_error"), "paused_until": r.get("paused_until") or 0,
            "on": cfg.PROSPEO_ENABLED, "order": order(), "used_this_month": used_this_month()}


def order():
    """'seamless_first' (default) or 'prospeo_first'."""
    first = (cfg.SOURCE_ORDER or "").replace(" ", "").lower().split(",")[0]
    return "prospeo_first" if first == "prospeo" else "seamless_first"


def ready():
    """(True, None) if Prospeo can be asked right now, else (False, why). Never raises."""
    if not cfg.PROSPEO_ENABLED:
        return False, "Prospeo is off (PROSPEO_ENABLED=0 in .env)."
    r = _row() or {}
    if not key():
        return False, "No Prospeo key (add it on the Accounts page)."
    if not r.get("enabled", 1):
        return False, "Prospeo is switched off on the Accounts page."
    if (r.get("paused_until") or 0) > NOW():
        return False, "Prospeo had errors; it's asked again by itself shortly."
    if r.get("credits") is not None and r["credits"] < max(1, cfg.PROSPEO_MIN_CREDITS_LEFT):
        return False, f"Prospeo has fewer than {cfg.PROSPEO_MIN_CREDITS_LEFT} credits left (PROSPEO_MIN_CREDITS_LEFT)."
    return True, None


def spent(pull_id):
    return db.q1("SELECT COALESCE(SUM(cost),0) s FROM prospeo_log WHERE pull_id=?", (pull_id,))["s"]


def room(pull_id):
    """Prospeo credits this pull may still use (None = no limit)."""
    cap = cfg.PROSPEO_MAX_CREDITS_PER_PULL
    if not cap:
        return None
    return max(0, cap - spent(pull_id))


def _error(msg):
    _ensure()
    r = _row()
    n = (r["errors_in_row"] or 0) + 1
    pause = NOW() + 1800 if n >= max(1, cfg.PROSPEO_ERRORS_PAUSE) else (r["paused_until"] or 0)
    db.x("UPDATE prospeo SET errors_in_row=?, last_error=?, paused_until=? WHERE id=1", (n, msg[:300], pause))


def _ok():
    _ensure()
    db.x("UPDATE prospeo SET errors_in_row=0, last_error=NULL WHERE id=1")


def lookup_input(c):
    """What Prospeo needs to find this person: LinkedIn, or full name + company website. None = not enough."""
    out = {}
    li = (c.get("li_url") or "").strip()
    if db.norm_li(li):
        out["linkedin_url"] = li if li.lower().startswith("http") else "https://" + li.lstrip("/")
    first, last = (c.get("first_name") or "").strip(), (c.get("last_name") or "").strip()
    name = (c.get("name") or "").strip()
    dom = db.norm_domain(c.get("domain"))
    if first and last:
        out["first_name"], out["last_name"] = first, last
    elif name and len(name.split()) >= 2:
        out["full_name"] = name
    if dom:
        out["company_website"] = dom
    if "linkedin_url" in out or (("first_name" in out or "full_name" in out) and dom):
        return out
    return None


def recently_asked(keys):
    """A Prospeo answer for this person in the last 90 days (asking again would be free anyway, but this saves the
    call and never pays twice if Prospeo's own free window doesn't apply)."""
    since = NOW() - 90 * 86400
    for k in keys:
        r = db.q1("SELECT * FROM prospeo_log WHERE instr(char(10) || person_key || char(10), ?) > 0 AND at>=? AND result IN "
                  "('found','none','mismatch') ORDER BY id DESC LIMIT 1", ("\n" + k + "\n", since))
        if r:
            return r
    return None


def _same_person(c, person, company):
    """The answer must be this person: same LinkedIn, or same name and same company website."""
    li_a = db.norm_li(c.get("li_url"))
    li_b = db.norm_li((person or {}).get("linkedin_url") or "")
    if li_a and li_b:
        return li_a == li_b
    want = db.norm(c.get("name") or f"{c.get('first_name') or ''} {c.get('last_name') or ''}")
    got = db.norm((person or {}).get("full_name") or
                  f"{(person or {}).get('first_name') or ''} {(person or {}).get('last_name') or ''}")
    if not want or want != got:
        return False
    dom_a = db.norm_domain(c.get("domain"))
    dom_b = db.norm_domain((company or {}).get("domain") or (company or {}).get("website") or "")
    if dom_a and dom_b:
        return dom_a == dom_b or dom_b.endswith("." + dom_a) or dom_a.endswith("." + dom_b)
    return False                        # can't tell it's the same company: not used


def _log(pull_id, c, res, cost, email, note):
    keys = db.person_keys(c.get("name"), c.get("company"), c.get("li_url"), c.get("row_email"))
    db.x("INSERT INTO prospeo_log(at,pull_id,contact_id,person_key,result,cost,email,note) VALUES(?,?,?,?,?,?,?,?)",
         (NOW(), pull_id, c["id"], "\n".join(keys) or None, res, cost, email, (note or "")[:300]))


def _fail_all(batch, out, pull_id, msg):
    for c, _ in batch:
        out[c["id"]] = ("error", None, msg)
        _log(pull_id, c, "error", 0, None, msg)
    return out


def enrich(people, pull_id=None):
    """Ask Prospeo about up to 50 people in one call. people = contact dicts with 'id'.
    Returns {contact_id: (result, email_or_None, note)} where result is
    'found' | 'none' | 'mismatch' | 'invalid' | 'error'. Every call is logged with what it cost."""
    out = {}
    batch = []
    for c in people[:50]:
        inp = lookup_input(c)
        if not inp:
            out[c["id"]] = ("invalid", None, "Not enough to look this person up (needs LinkedIn, or full name + "
                                             "company website).")
            continue
        batch.append((c, dict(inp, identifier=str(c["id"]))))
    if not batch:
        return out
    ok, why = ready()
    if not ok:
        for c, _ in batch:
            out[c["id"]] = ("error", None, why)
        return out                                      # nothing was asked: not logged
    # Emails only: mobile numbers cost 10 credits each, so they are never asked for (switched off explicitly).
    body = {"only_verified_email": bool(cfg.PROSPEO_ONLY_VERIFIED), "enrich_mobile": False,
            "only_verified_mobile": False, "data": [d for _, d in batch]}
    try:
        with _client() as cl:
            r = cl.post(_url("/bulk-enrich-person"), headers={"X-KEY": key(), "Content-Type": "application/json"},
                        content=json.dumps(body))
    except httpx.HTTPError as e:
        _error(f"Prospeo didn't answer ({type(e).__name__})")
        return _fail_all(batch, out, pull_id, f"Prospeo didn't answer ({type(e).__name__}); tried again later.")
    try:
        resp = r.json()
    except ValueError:
        resp = None
    if not isinstance(resp, dict):
        _error(f"Prospeo sent an unreadable answer (HTTP {r.status_code})")
        return _fail_all(batch, out, pull_id, "Prospeo sent an unreadable answer; tried again later.")
    if r.status_code != 200 or resp.get("error"):
        code = str(resp.get("error_code") or resp.get("message") or f"HTTP {r.status_code}")
        if "CREDIT" in code.upper():
            _ensure()
            db.x("UPDATE prospeo SET credits=0, credits_at=? WHERE id=1", (NOW(),))
            msg = "Prospeo has no credits left."
        elif "KEY" in code.upper():
            msg = "Prospeo didn't accept the API key (Accounts page)."
        elif r.status_code == 429:
            msg = "Prospeo says too many requests; tried again shortly."
        else:
            msg = f"Prospeo error: {code}"
        _error(msg)
        return _fail_all(batch, out, pull_id, msg)
    _ok()
    by_id = {str(c["id"]): c for c, _ in batch}
    matched = resp.get("matched") or []
    cost_total = resp.get("total_cost")
    seen = set()
    for m in matched:
        ident = str((m or {}).get("identifier") or "")
        c = by_id.get(ident)
        if not c or ident in seen:
            continue
        seen.add(ident)
        person = m.get("person") or {}
        company = m.get("company") or {}
        em = person.get("email") or {}
        email = db.norm_email(em.get("email") if isinstance(em, dict) else em)
        estatus = str((em.get("status") if isinstance(em, dict) else "") or "").upper()
        if not email or (cfg.PROSPEO_ONLY_VERIFIED and estatus != "VERIFIED"):
            out[c["id"]] = ("none", None, "Prospeo found the person but no verified email.")
            continue
        if not _same_person(c, person, company):
            out[c["id"]] = ("mismatch", None, "Prospeo's answer looked like a different person, so it wasn't used.")
            continue
        out[c["id"]] = ("found", email, "Verified by Prospeo.")
    for c, _ in batch:
        out.setdefault(c["id"], ("none", None, "Prospeo has no verified email for this person (no charge)."))
    # What each one cost: Prospeo charges per email revealed; total_cost (if given) is the truth for the call.
    revealed = [cid for cid, v in out.items() if v[0] in ("found", "mismatch") and str(cid) in by_id]
    per = {cid: 1 for cid in revealed}
    if cost_total is not None:
        try:
            total = int(cost_total)
        except (TypeError, ValueError):
            total = len(revealed)
        # spread the real total over the revealed people (never below 0); free re-asks make it smaller
        per = {cid: 0 for cid in revealed}
        for idx, cid in enumerate(revealed):
            if idx < total:
                per[cid] = 1
        extra = total - min(total, len(revealed))
        if extra > 0:
            db.x("INSERT INTO prospeo_log(at,pull_id,contact_id,person_key,result,cost,note) VALUES(?,?,?,?,?,?,?)",
                 (NOW(), pull_id, None, None, "charge", extra, "Prospeo charged more than the emails it returned"))
    for c, _ in batch:
        res, email, note = out[c["id"]]
        _log(pull_id, c, res, per.get(c["id"], 0), email, note)
    used = sum(per.values())
    r0 = _row()
    if r0 and r0["credits"] is not None and used:
        db.x("UPDATE prospeo SET credits=MAX(0, credits-?), credits_at=? WHERE id=1", (used, NOW()))
    return out
