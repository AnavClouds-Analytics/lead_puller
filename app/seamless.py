"""Seamless.AI v1 API client with per-endpoint rate limiting, 429 back-off and credit tracking."""
import collections
import math
import os
import threading
import time

import httpx

from . import db

BASE_URL = os.environ.get("SEAMLESS_BASE_URL", "https://api.seamless.ai/api/client/v1")
# Tests swap this for an httpx.MockTransport.
TRANSPORT = None

# Seamless allows 60/min per endpoint for the whole org, shared with AnavScout on the same account.
# Take about half so a pull here never starves AnavScout's nightly run (a 1,000-person pull needs ~50 calls).
PER_MINUTE = int(os.environ.get("SEAMLESS_PER_MINUTE", 30))

TERMINAL = {"done", "error", "missing", "duplicate", "not found", "contact-already-researched",
            "no license or credits available"}


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

    def wait(self, endpoint, sleep=time.sleep):
        while True:
            with self.lock:
                now = time.time()
                dq = self.calls[endpoint]
                while dq and now - dq[0] > 60:
                    dq.popleft()
                wait_for = max(0.0, self.blocked_until[endpoint] - now)
                if not wait_for and len(dq) >= PER_MINUTE:
                    wait_for = 60 - (now - dq[0]) + 0.05
                if not wait_for:
                    dq.append(now)
                    return
            sleep(min(wait_for, 5))

    def block(self, endpoint, until):
        with self.lock:
            self.blocked_until[endpoint] = max(self.blocked_until[endpoint], until)


limiter = _Limiter()
SLEEP = time.sleep


def api_key():
    return os.environ.get("SEAMLESS_API_KEY") or db.get_setting("seamless_api_key") or ""


def _record_headers(resp):
    bal = resp.headers.get("X-PublicAPI-Credits")
    if bal is not None:
        try:
            db.set_setting("credit_balance", int(float(bal)))
            db.set_setting("credit_balance_at", time.time())
        except ValueError:
            pass
    return resp


def _request(method, path, endpoint, *, json=None, params=None, key=None, retries=6):
    key = key or api_key()
    if not key:
        raise SeamlessError("No Seamless API key yet. Add it in Settings.", code="noKey")
    headers = {"Token": key, "Content-Type": "application/json"}
    for attempt in range(retries):
        limiter.wait(endpoint, SLEEP)
        try:
            with httpx.Client(timeout=60, transport=TRANSPORT) as c:
                resp = c.request(method, BASE_URL + path, json=json, params=params, headers=headers)
        except httpx.HTTPError as e:
            if attempt == retries - 1:
                raise SeamlessError(f"Could not reach Seamless: {e}", code="network")
            SLEEP(min(2 ** attempt, 30))
            continue
        _record_headers(resp)
        if resp.status_code == 429:
            reset = resp.headers.get("X-RateLimit-Reset")
            try:
                until = float(reset)
            except (TypeError, ValueError):
                until = time.time() + 20
            limiter.block(endpoint, max(until, time.time() + 1))
            continue
        if resp.status_code >= 500:
            if attempt == retries - 1:
                raise SeamlessError(f"Seamless server error ({resp.status_code}).", status=resp.status_code)
            SLEEP(min(2 ** attempt, 30))
            continue
        if resp.status_code in (401, 403):
            raise SeamlessError("Seamless did not accept the API key. Check it in Settings.",
                                code="badKey", status=resp.status_code)
        if resp.status_code == 422:
            body = _safe_json(resp)
            code = body.get("code")
            if code == "missingLicense":
                msg = "Your Seamless account has no active Public API license."
            elif code == "insufficientCredits":
                need = (body.get("data") or {}).get("additionalCreditsNeeded")
                msg = "Not enough Seamless credits" + (f" ({need} more needed)." if need else ".")
            else:
                msg = body.get("msg") or body.get("message") or "Seamless rejected the request (422)."
            raise SeamlessError(msg, code=code or "422", status=422)
        if resp.status_code >= 400:
            body = _safe_json(resp)
            raise SeamlessError(body.get("message") or body.get("msg") or f"Seamless error {resp.status_code}",
                                status=resp.status_code)
        return resp
    raise SeamlessError("Seamless kept rate-limiting the request; will try again shortly.", code="rateLimited")


def _safe_json(resp):
    try:
        return resp.json() or {}
    except Exception:
        return {}


# ---------- endpoints ----------
def test_connection(key=None):
    """Free call: location lookup costs no credits but returns the credit balance header."""
    resp = _request("GET", "/search/locations", "locations", params={"q": "new york", "limit": 1}, key=key)
    bal = resp.headers.get("X-PublicAPI-Credits")
    return {"ok": True, "balance": int(float(bal)) if bal is not None else None}


def lookup_location(text, types):
    resp = _request("GET", "/search/locations", "locations", params={"q": text, "limit": 5, "types": types})
    return _safe_json(resp).get("data") or []


def search_contacts(body, limit, next_token=None, pull_id=None):
    """One page of contact search. Costs 1 credit per 10 rows returned (rounded up)."""
    payload = dict(body)
    payload["limit"] = limit
    payload["nextToken"] = next_token
    resp = _request("POST", "/search/contacts", "search_contacts", json=payload)
    data = _safe_json(resp)
    rows = data.get("data") or []
    sup = data.get("supplementalData") or {}
    credits = math.ceil(len(rows) / 10) if rows else 0
    if credits:
        db.log_credits(pull_id, "search", credits, db.get_setting("credit_balance"),
                       f"search page, {len(rows)} rows")
    return rows, sup


def research_contacts(search_result_ids, pull_id=None):
    """Submit up to 500 searchResultIds for research (1 credit each). Returns requestIds in order."""
    resp = _request("POST", "/contacts/research", "research_contacts",
                    json={"searchResultIds": list(search_result_ids)})
    data = _safe_json(resp)
    ids = data.get("requestIds") or []
    if ids:
        db.log_credits(pull_id, "research", len(ids), db.get_setting("credit_balance"),
                       f"research {len(ids)} contacts")
    return ids


def poll_research(request_ids):
    """Poll up to 500 requestIds. Free. Returns list of result rows (each carries its requestId)."""
    resp = _request("GET", "/contacts/research/poll", "poll",
                    params={"requestIds": ",".join(request_ids)})
    return _safe_json(resp).get("data") or []
