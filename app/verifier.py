"""MillionVerifier: checks whether a guessed email address really exists, for a tiny fraction of a Seamless credit.

Fails safe everywhere: only a clear "ok" counts as valid. A timeout, an error, an unknown answer or a missing key
is "not valid", so the person is simply paid for in Seamless as before. Answers are cached, so the same address
is never paid for twice within GUESS_CACHE_DAYS.
"""
import time

import httpx

from . import db
from .config import cfg

TRANSPORT = None       # tests swap in a fake
NOW = time.time

RESULTS = ("ok", "catch_all", "unknown", "invalid", "disposable", "error")


class VerifierError(Exception):
    pass


def _row():
    return db.q1("SELECT * FROM verifier WHERE id=1")


def key():
    r = _row()
    if r and r["api_key"]:
        return r["api_key"]
    return cfg.MILLIONVERIFIER_API_KEY or ""


def status():
    """For the Accounts page (the key is shown as its last 4 characters only)."""
    r = _row() or {}
    k = key()
    return {"has_key": bool(k), "key": ("•••• " + k[-4:]) if k else "", "from_env": bool(k) and not r.get("api_key"),
            "enabled": bool(r.get("enabled", 1)), "credits": r.get("credits"), "credits_at": r.get("credits_at"),
            "last_error": r.get("last_error"), "paused_until": r.get("paused_until") or 0,
            "guessing_on": cfg.EMAIL_GUESSING,
            "used_this_month": db.q1("SELECT COUNT(*) n FROM verify_log WHERE cached=0 AND at>=?",
                                     (db.month_start(),))["n"]}


def _ensure():
    if not _row():
        db.x("INSERT OR IGNORE INTO verifier(id, enabled) VALUES(1, 1)")


def _client():
    return httpx.Client(transport=TRANSPORT, timeout=httpx.Timeout(cfg.GUESS_TIMEOUT_SECONDS + 5, connect=10))


def fetch_credits(api_key):
    """Free call: how many checks are left on this key. Raises VerifierError if the key isn't accepted."""
    try:
        with _client() as c:
            r = c.get(cfg.MILLIONVERIFIER_BASE_URL.rstrip("/") + "/credits", params={"api": api_key})
    except httpx.HTTPError as e:
        raise VerifierError(f"MillionVerifier didn't answer ({type(e).__name__}). Try again in a minute.")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or body.get("error") or "credits" not in body:
        why = body.get("error") or f"HTTP {r.status_code}"
        raise VerifierError(f"MillionVerifier didn't accept this key ({why}).")
    return int(body.get("credits") or 0)


def set_key(api_key):
    api_key = (api_key or "").strip()
    if len(api_key) < 8:
        raise VerifierError("Paste the MillionVerifier API key (MillionVerifier → API).")
    credits = fetch_credits(api_key)                  # a key MillionVerifier doesn't accept is never saved
    _ensure()
    db.x("UPDATE verifier SET api_key=?, credits=?, credits_at=?, last_error=NULL, errors_in_row=0, paused_until=0 "
         "WHERE id=1", (api_key, credits, NOW()))
    return status()


def remove_key():
    _ensure()
    db.x("UPDATE verifier SET api_key=NULL, credits=NULL, credits_at=NULL, last_error=NULL WHERE id=1")


def set_enabled(on):
    _ensure()
    db.x("UPDATE verifier SET enabled=? WHERE id=1", (1 if on else 0,))


def refresh_credits():
    k = key()
    if not k:
        raise VerifierError("No MillionVerifier key yet.")
    _ensure()
    try:
        credits = fetch_credits(k)
    except VerifierError as e:
        db.x("UPDATE verifier SET last_error=? WHERE id=1", (str(e),))
        raise
    db.x("UPDATE verifier SET credits=?, credits_at=?, last_error=NULL WHERE id=1", (credits, NOW()))
    return credits


def ready():
    """(True, None) if guesses can be checked right now, else (False, why). Never raises."""
    if not cfg.EMAIL_GUESSING:
        return False, "Email guessing is off (EMAIL_GUESSING=0 in .env)."
    r = _row() or {}
    if not key():
        return False, "No MillionVerifier key (add it on the Accounts page)."
    if not r.get("enabled", 1):
        return False, "Email guessing is switched off on the Accounts page."
    if (r.get("paused_until") or 0) > NOW():
        return False, "MillionVerifier had errors; guessing resumes by itself shortly."
    if r.get("credits") is not None and r["credits"] < cfg.GUESS_MIN_CHECKS_LEFT:
        return False, f"MillionVerifier has fewer than {cfg.GUESS_MIN_CHECKS_LEFT} checks left (GUESS_MIN_CHECKS_LEFT)."
    return True, None


def _cached(email):
    since = NOW() - cfg.GUESS_CACHE_DAYS * 86400
    r = db.q1("SELECT result FROM verify_log WHERE email=? AND at>=? AND result IN ('ok','catch_all','invalid',"
              "'disposable') ORDER BY id DESC LIMIT 1", (email, since))
    return r["result"] if r else None


def _error(msg, pull_id, contact_id, email):
    _ensure()
    r = _row()
    n = (r["errors_in_row"] or 0) + 1
    pause = NOW() + 1800 if n >= cfg.GUESS_ERRORS_PAUSE else (r["paused_until"] or 0)
    db.x("UPDATE verifier SET errors_in_row=?, last_error=?, paused_until=? WHERE id=1", (n, msg, pause))
    db.x("INSERT INTO verify_log(at,pull_id,contact_id,email,result,note) VALUES(?,?,?,?,?,?)",
         (NOW(), pull_id, contact_id, email, "error", msg[:200]))
    return "error"


def check(email, pull_id=None, contact_id=None):
    """'ok' | 'catch_all' | 'invalid' | 'disposable' | 'unknown' | 'error'. Only 'ok' is ever treated as valid."""
    email = db.norm_email(email)
    if not email:
        return "invalid"
    hit = _cached(email)
    if hit:
        db.x("INSERT INTO verify_log(at,pull_id,contact_id,email,result,cached) VALUES(?,?,?,?,?,1)",
             (NOW(), pull_id, contact_id, email, hit))
        return hit
    ok, why = ready()
    if not ok:
        return "error"
    timeout = max(2, min(60, cfg.GUESS_TIMEOUT_SECONDS))
    try:
        with _client() as c:
            r = c.get(cfg.MILLIONVERIFIER_BASE_URL.rstrip("/") + "/",
                      params={"api": key(), "email": email, "timeout": timeout})
    except httpx.HTTPError as e:
        return _error(f"MillionVerifier didn't answer ({type(e).__name__})", pull_id, contact_id, email)
    try:
        body = r.json()
    except ValueError:
        return _error(f"MillionVerifier sent an unreadable answer (HTTP {r.status_code})", pull_id, contact_id, email)
    if r.status_code != 200 or body.get("error"):
        msg = str(body.get("error") or f"HTTP {r.status_code}")
        if "credit" in msg.lower():
            db.x("UPDATE verifier SET credits=0, credits_at=? WHERE id=1", (NOW(),))
        return _error(f"MillionVerifier: {msg}", pull_id, contact_id, email)
    result = str(body.get("result") or "").strip().lower().replace("-", "_")
    if result not in RESULTS:
        result = "unknown"
    if result == "ok" and str(body.get("resultcode", 1)) not in ("1", "1.0"):
        result = "unknown"                      # mixed signals: never treat as valid
    credits = body.get("credits")
    _ensure()
    db.x("UPDATE verifier SET errors_in_row=0, last_error=NULL" + (", credits=?, credits_at=?" if credits is not None
                                                                    else "") + " WHERE id=1",
         ((int(credits), NOW()) if credits is not None else ()))
    db.x("INSERT INTO verify_log(at,pull_id,contact_id,email,result,credits_after) VALUES(?,?,?,?,?,?)",
         (NOW(), pull_id, contact_id, email, result, credits))
    return result
