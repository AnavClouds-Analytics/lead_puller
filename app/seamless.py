"""Seamless.AI v1 API client with per-endpoint rate limiting, 429 back-off and credit tracking."""
import collections
import math
import os
import threading
import time

import httpx

from . import accounts, db
from .config import cfg

# Tests swap this for an httpx.MockTransport.
TRANSPORT = None


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
            sleep(min(wait_for, 5))

    def block(self, endpoint, until):
        with self.lock:
            self.blocked_until[endpoint] = max(self.blocked_until[endpoint], until)


NOW = time.time      # tests swap in a fake clock
limiter = _Limiter()
SLEEP = time.sleep


def api_key():
    """True when at least one Seamless account is set up (kept for older callers)."""
    return bool(accounts.enabled())


def any_account():
    """Some enabled account, for free calls (place lookups)."""
    acc = accounts.enabled()
    if not acc:
        raise SeamlessError("No Seamless account is set up. Add your API key on the Seamless accounts page.",
                            code="noKey")
    return acc[0]


def _balance_of(resp):
    bal = resp.headers.get("X-PublicAPI-Credits")
    try:
        return int(float(bal)) if bal is not None else None
    except ValueError:
        return None


def _record_headers(resp, account):
    bal = _balance_of(resp)
    if bal is not None and account and account.get("id"):
        accounts.record_balance(account["id"], bal)
        account["balance"] = bal
    return resp


def _request(method, path, endpoint, *, account, json=None, params=None, retries=6, safe_to_retry=True):
    """safe_to_retry=False for calls that spend credits per item (research): if Seamless times out or
    errors after receiving the request we can't know whether it charged, so we never resend automatically."""
    key = (account or {}).get("api_key")
    if not key:
        raise SeamlessError("No Seamless API key for this account. Add it on the Seamless accounts page.",
                            code="noKey")
    headers = {"Token": key, "Content-Type": "application/json"}
    for attempt in range(retries):
        limiter.wait(endpoint, SLEEP)
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
        _record_headers(resp, account)
        if resp.status_code == 429:
            reset = resp.headers.get("X-RateLimit-Reset")
            try:
                until = float(reset)
            except (TypeError, ValueError):
                until = NOW() + 20
            limiter.block(endpoint, max(until, NOW() + 1))
            continue
        if resp.status_code >= 500:
            if not safe_to_retry:
                raise SeamlessError(f"Seamless returned a server error ({resp.status_code}).", code="unconfirmed",
                                    status=resp.status_code)
            if attempt == retries - 1:
                raise SeamlessError(f"Seamless server error ({resp.status_code}).", status=resp.status_code)
            SLEEP(min(2 ** attempt, 30))
            continue
        if resp.status_code in (401, 403):
            name = account.get("name") or "this account"
            if account.get("id"):
                db.x("UPDATE accounts SET last_error=? WHERE id=?", ("Seamless did not accept this API key.",
                                                                    account["id"]))
            raise SeamlessError(f"Seamless did not accept the API key of {name}.",
                                code="badKey", status=resp.status_code)
        if resp.status_code == 422:
            body = _safe_json(resp)
            code = body.get("code")
            if code == "missingLicense":
                msg = "Your Seamless account has no active Public API license."
            elif code == "insufficientCredits":
                need = (body.get("data") or {}).get("additionalCreditsNeeded")
                msg = f"Not enough Seamless credits on {account.get('name') or 'this account'}" + \
                    (f" ({need} more needed)." if need else ".")
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
def test_connection(account):
    """Free call: location lookup costs no credits but returns the credit balance header."""
    resp = _request("GET", "/search/locations", "locations", params={"q": "new york", "limit": 1}, account=account)
    return {"ok": True, "balance": _balance_of(resp)}


def lookup_location(text, types, limit=5, account=None):
    params = {"q": text, "limit": limit}
    if types:
        params["types"] = types
    resp = _request("GET", "/search/locations", "locations", params=params, account=account or any_account())
    return _safe_json(resp).get("data") or []


def search_contacts(body, limit, next_token=None, pull_id=None, account=None):
    """One page of contact search. Costs 1 credit per 10 rows returned (rounded up)."""
    payload = dict(body)
    payload["limit"] = limit
    payload["nextToken"] = next_token
    resp = _request("POST", "/search/contacts", "search_contacts", json=payload, account=account)
    data = _safe_json(resp)
    rows = data.get("data") or []
    sup = data.get("supplementalData") or {}
    credits = math.ceil(len(rows) / 10) if rows else 0
    if credits:
        db.log_credits(pull_id, "search", credits, _balance_of(resp), f"search page, {len(rows)} rows",
                       account_id=account.get("id"))
    return rows, sup


def research_contacts(search_result_ids, pull_id=None, skip_dedup=False, account=None):
    """Submit up to 500 searchResultIds for research (1 credit each). Returns requestIds.
    skip_dedup=True asks Seamless to look again at people your org researched before (charged)."""
    body = {"searchResultIds": list(search_result_ids)}
    if skip_dedup:
        body["skipDeduplicationCheck"] = True
    resp = _request("POST", "/contacts/research", "research_contacts", json=body, safe_to_retry=False, account=account)
    data = _safe_json(resp)
    ids = data.get("requestIds") or []
    if ids:
        db.log_credits(pull_id, "research", len(ids), _balance_of(resp),
                       f"research {len(ids)} contacts" + (" (retry, fresh look)" if skip_dedup else ""),
                       account_id=account.get("id"))
    return ids


def poll_research(request_ids, account=None):
    """Poll up to 500 requestIds. Free. Returns list of result rows (each carries its requestId).
    Must use the account that sent the research request."""
    resp = _request("GET", "/contacts/research/poll", "poll",
                    params={"requestIds": ",".join(request_ids)}, account=account)
    return _safe_json(resp).get("data") or []


def org_contacts(start_iso, end_iso, page, limit=500, account=None):
    """Contacts already researched in your Seamless org (free). Used to fill in people Seamless reports as
    already researched, instead of paying again."""
    resp = _request("GET", "/contacts", "org_contacts",
                    params={"startDate": start_iso, "endDate": end_iso, "page": page, "limit": limit},
                    account=account)
    return _safe_json(resp).get("data") or []
