"""Seamless.AI v1 API client: per-endpoint rate limiting, 429 back-off, the live balance from every answer,
and never an automatic resend of a call that may already have been charged."""
import collections
import math
import threading
import time

import httpx

from . import db
from .config import cfg

TRANSPORT = None          # tests swap in an httpx.MockTransport
NOW = time.time           # tests swap in a fake clock
SLEEP = time.sleep

TERMINAL = {"done", "error", "missing", "duplicate", "not found", "contact-already-researched",
            "no license or credits available"}
NOT_CHARGED = {"duplicate", "contact-already-researched", "error"}


class SeamlessError(Exception):
    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class _Limiter:
    def __init__(self):
        self.calls = collections.defaultdict(collections.deque)
        self.blocked_until = collections.defaultdict(float)
        self.lock = threading.Lock()

    def wait(self, endpoint):
        while True:
            with self.lock:
                now = NOW()
                dq = self.calls[endpoint]
                while dq and now - dq[0] > 60:
                    dq.popleft()
                wait_for = max(0.0, self.blocked_until[endpoint] - now)
                if not wait_for and len(dq) >= cfg.RATE_LIMIT_PER_MIN:
                    wait_for = 60 - (now - dq[0]) + 0.05
                if not wait_for:
                    dq.append(now)
                    return
            SLEEP(min(wait_for, 5))

    def block(self, endpoint, until):
        with self.lock:
            self.blocked_until[endpoint] = max(self.blocked_until[endpoint], until)


limiter = _Limiter()


def clean_key(raw):
    """A pasted key often carries extras: spaces, line breaks, invisible characters, quotes or a 'Token' prefix."""
    k = "".join(ch for ch in (raw or "") if ch.isprintable() and not ch.isspace())
    k = k.strip("'\"`")
    for prefix in ("Token:", "Token", "Bearer"):
        if k.lower().startswith(prefix.lower()) and len(k) > len(prefix) + 8:
            k = k[len(prefix):].lstrip(":")
    return k


def _says(resp):
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "").strip()[:160]
    if isinstance(body, dict):
        for k in ("message", "msg", "error", "detail", "code"):
            if isinstance(body.get(k), str) and body[k].strip():
                return body[k].strip()[:160]
    return ""


def _json(resp):
    try:
        return resp.json() or {}
    except Exception:
        return {}


def balance_of(resp):
    v = resp.headers.get("X-PublicAPI-Credits")
    try:
        return int(float(v)) if v is not None else None
    except ValueError:
        return None


def _record_balance(resp, account):
    bal = balance_of(resp)
    if bal is not None and account and account.get("id"):
        db.x("UPDATE accounts SET balance=?, balance_at=?, last_error=NULL WHERE id=?", (bal, time.time(), account["id"]))
        account["balance"] = bal
    return bal


def _request(method, path, endpoint, *, account, json=None, params=None, retries=6, safe_to_retry=True):
    """safe_to_retry=False for research: if Seamless errors or times out after receiving the request we can't
    know whether it charged, so the call is never resent automatically (code 'unconfirmed')."""
    key = (account or {}).get("api_key")
    if not key:
        raise SeamlessError("No Seamless API key. Add one on the Accounts page.", code="noKey")
    headers = {"Token": key, "Content-Type": "application/json", "Accept": "application/json"}
    for attempt in range(retries):
        limiter.wait(endpoint)
        try:
            with httpx.Client(timeout=60, transport=TRANSPORT) as c:
                resp = c.request(method, cfg.SEAMLESS_BASE_URL + path, json=json, params=params, headers=headers)
        except httpx.HTTPError as e:
            if not safe_to_retry and not isinstance(e, httpx.ConnectError):
                raise SeamlessError(f"Seamless did not answer ({type(e).__name__}).", code="unconfirmed")
            if attempt == retries - 1:
                raise SeamlessError(f"Could not reach Seamless: {e}", code="network")
            SLEEP(min(2 ** attempt, 30))
            continue
        _record_balance(resp, account)
        st = resp.status_code
        if st == 429:
            try:
                until = float(resp.headers.get("X-RateLimit-Reset"))
            except (TypeError, ValueError):
                until = NOW() + 20
            limiter.block(endpoint, max(until, NOW() + 1))
            continue
        if st >= 500:
            if not safe_to_retry:
                raise SeamlessError(f"Seamless returned a server error ({st}).", code="unconfirmed", status=st)
            if attempt == retries - 1:
                raise SeamlessError(f"Seamless server error ({st}).", code="server", status=st)
            SLEEP(min(2 ** attempt, 30))
            continue
        if st in (401, 403):
            said = _says(resp)
            why = ("the key is wrong, incomplete or was replaced in Seamless" if st == 401 else
                   "the key is valid but this Seamless user isn't allowed to use the Public API")
            msg = (f"Seamless did not accept the API key of {account.get('name') or 'this account'} (error {st}: {why}"
                   + (f"; Seamless said: “{said}”" if said else "") + "). Copy the key again from that user's "
                   "Seamless → Settings → Public API, or ask Seamless to switch the Public API on for that user.")
            if account.get("id"):
                db.x("UPDATE accounts SET last_error=? WHERE id=?", (msg, account["id"]))
            raise SeamlessError(msg, code="badKey", status=st)
        if st == 422:
            body = _json(resp)
            code = body.get("code") or "422"
            if code == "insufficientCredits":
                msg = f"Not enough Seamless credits on {account.get('name') or 'this account'}."
            elif code == "missingLicense":
                msg = f"{account.get('name') or 'This account'} has no active Seamless Public API license."
            else:
                msg = _says(resp) or "Seamless rejected the request (422)."
            raise SeamlessError(msg, code=code, status=422)
        if st >= 400:
            raise SeamlessError(_says(resp) or f"Seamless error {st}", code="bad_request", status=st)
        return resp
    raise SeamlessError("Seamless kept rate-limiting the request; trying again shortly.", code="rateLimited")


# ---------------------------------------------------------------- endpoints
def test_connection(account):
    """Free: a place lookup costs nothing and its answer carries the live credit balance."""
    resp = _request("GET", "/search/locations", "locations", params={"q": "new york", "limit": 1}, account=account)
    return balance_of(resp)


def lookup_location(text, types, account, limit=10):
    params = {"q": text, "limit": limit}
    if types:
        params["types"] = types
    resp = _request("GET", "/search/locations", "locations", params=params, account=account)
    return _json(resp).get("data") or []


def search_contacts(body, limit, next_token, account, pull_id=None, note="search page"):
    """One page of contact search. Seamless charges 1 credit per 10 rows returned (rounded up)."""
    payload = dict(body)
    payload["limit"] = int(limit)
    if next_token:
        payload["nextToken"] = next_token
    resp = _request("POST", "/search/contacts", "search_contacts", json=payload, account=account)
    data = _json(resp)
    rows = data.get("data") or []
    sup = data.get("supplementalData") or {}
    credits = math.ceil(len(rows) / 10) if rows else 0
    if credits:
        db.log_credits(pull_id, account.get("id"), "search", credits, balance_of(resp), f"{note}, {len(rows)} rows")
    return rows, sup, credits


def research_contacts(search_result_ids, account, pull_id=None):
    """Submit searchResultIds for research (1 credit each). Returns requestIds (same order as sent)."""
    body = {"searchResultIds": list(search_result_ids)}
    resp = _request("POST", "/contacts/research", "research_contacts", json=body, safe_to_retry=False, account=account)
    ids = _json(resp).get("requestIds") or []
    if ids:
        db.log_credits(pull_id, account.get("id"), "research", len(ids), balance_of(resp), f"research {len(ids)} people")
    return ids


def poll_research(request_ids, account):
    """Free. Must use the account that sent the research request."""
    resp = _request("GET", "/contacts/research/poll", "poll", params={"requestIds": ",".join(request_ids)},
                    account=account)
    return _json(resp).get("data") or []


def saved_contacts(start_iso, end_iso, page, account, limit=500):
    """Free: contacts already researched in this Seamless account."""
    resp = _request("GET", "/contacts", "org_contacts",
                    params={"startDate": start_iso, "endDate": end_iso, "page": page, "limit": limit}, account=account)
    return _json(resp).get("data") or []
