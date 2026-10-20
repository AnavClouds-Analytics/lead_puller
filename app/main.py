"""AnavClouds Lead Puller — web app (FastAPI)."""
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import accounts, claude, db, exports, seamless, worker
from .config import APP_VERSION, cfg, shown_settings
from .filters import FILTERS, clean_filters, search_filter_count

STATIC = os.path.join(os.path.dirname(__file__), "static")
_worker = None


@asynccontextmanager
async def lifespan(app):
    global _worker
    db.connect()
    if not os.environ.get("DISABLE_WORKER"):
        _worker = worker.Worker()
        _worker.start()
    yield
    if _worker:
        _worker.stop()


app = FastAPI(title="AnavClouds Lead Puller", lifespan=lifespan, docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- errors
@app.exception_handler(worker.PullError)
async def _pull_err(request, exc):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(seamless.SeamlessError)
async def _seam_err(request, exc):
    return JSONResponse({"error": str(exc), "code": exc.code}, status_code=400)


@app.exception_handler(accounts.NoAccount)
async def _acct_err(request, exc):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(ValueError)
async def _value_err(request, exc):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(claude.ClaudeError)
async def _claude_err(request, exc):
    return JSONResponse({"error": str(exc)}, status_code=400)


# ---------------------------------------------------------------- auth
def _check_password(password):
    return bool(cfg.APP_PASSWORD) and hmac.compare_digest(password.encode(), cfg.APP_PASSWORD.encode())


def require_login(request: Request):
    token = request.cookies.get("lp_session")
    if token:
        row = db.q1("SELECT created_at FROM sessions WHERE token=?", (token,))
        if row and time.time() - row["created_at"] < cfg.SESSION_DAYS * 86400:
            return True
    raise HTTPException(401, "Please log in.")


def _start_session(response):
    token = secrets.token_urlsafe(32)
    db.x("INSERT INTO sessions(token, created_at) VALUES(?,?)", (token, time.time()))
    response.set_cookie("lp_session", token, max_age=cfg.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=cfg.COOKIE_SECURE)


_login_fails = []


@app.get("/api/auth/status")
def auth_status(request: Request):
    try:
        require_login(request)
        ok = True
    except HTTPException:
        ok = False
    return {"logged_in": ok, "no_password": not cfg.APP_PASSWORD}


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response):
    now = time.time()
    _login_fails[:] = [t for t in _login_fails if now - t < 300]
    if len(_login_fails) >= 10:
        raise HTTPException(429, "Too many wrong passwords. Wait 5 minutes.")
    body = await request.json()
    if not cfg.APP_PASSWORD:
        raise HTTPException(400, "No password set. Ask your developer to set APP_PASSWORD in .env.")
    if not _check_password(body.get("password") or ""):
        _login_fails.append(now)
        raise HTTPException(401, "Wrong password.")
    _start_session(response)
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    token = request.cookies.get("lp_session")
    if token:
        db.x("DELETE FROM sessions WHERE token=?", (token,))
    response.delete_cookie("lp_session")
    return {"ok": True}


# ---------------------------------------------------------------- meta & settings
def _mask(key):
    if not key:
        return ""
    return "•••• " + key[-4:]


def _credit_summary():
    budget = cfg.MONTHLY_CREDIT_BUDGET
    spent = db.month_spend()
    accs = [accounts.summary(a) for a in accounts.all_accounts()]
    on = [a for a in accs if a["enabled"]]
    bal = accounts.total_balance()
    at = max([a["balance_at"] for a in on if a["balance_at"]] or [None]) if on else None
    low = [a["name"] for a in on if a["low"]]
    return {"balance": bal, "balance_at": at, "accounts": accs, "shared_pools": accounts.shared_pool_groups(),
            "monthly_budget": budget, "spent_this_month": spent,
            "budget_left": max(0, budget - spent) if budget else None,
            "low_balance": bool(low), "low_accounts": low,
            "low_balance_level": cfg.LOW_BALANCE_WARNING, "spending_enabled": cfg.SPENDING_ENABLED,
            "mismatch": [{"account": a["name"], **a["check"]} for a in on if a["check"] and a["check"]["warn"]]}


@app.get("/api/meta", dependencies=[Depends(require_login)])
def meta():
    return {"filters": FILTERS, "credits": _credit_summary(), "columns": exports.catalog(),
            "has_seamless_key": bool(seamless.api_key()), "has_claude_key": bool(claude.api_key()),
            "version": APP_VERSION, "location_default": cfg.LOCATION_APPLIES_TO,
            "defaults": {"per_company_cap": cfg.MAX_PER_COMPANY, "company_cooldown": cfg.COMPANY_COOLDOWN_DAYS,
                         "preview_size": cfg.PREVIEW_SIZE, "page_size": cfg.PAGE_SIZE,
                         "batch_size": cfg.DEFAULT_BATCH_SIZE, "columns": exports.default_columns(),
                         "no_email_columns": exports.NO_EMAIL_DEFAULT,
                         "chance_min": cfg.EMAIL_CHANCE_MIN, "chance_samples": cfg.EMAIL_CHANCE_MIN_SAMPLES, "test_batch_size": cfg.TEST_BATCH_SIZE,
                         "test_batch_min_rate": cfg.TEST_BATCH_MIN_EMAIL_RATE,
                         "retry_days": cfg.NO_EMAIL_RETRY_DAYS, "table_page_size": cfg.TABLE_PAGE_SIZE,
                         "account_reserve": cfg.ACCOUNT_RESERVE, "wait_minutes": cfg.RESEARCH_WAIT_MINUTES,
                         "late_hours": cfg.LATE_ANSWER_HOURS, "spend_cap_pct": cfg.COLLECT_SPEND_CAP_PCT}}


@app.get("/api/settings", dependencies=[Depends(require_login)])
def get_settings():
    """Read-only: every setting lives in .env. Change it there and restart the app."""
    return {"groups": shown_settings(), "credits": _credit_summary(), "version": APP_VERSION}


@app.post("/api/settings/test-seamless", dependencies=[Depends(require_login)])
def test_seamless():
    """Free: check every switched-on account and refresh its balance."""
    out, total = [], 0
    for a in accounts.enabled():
        try:
            r = seamless.test_connection(a)
            out.append({"name": a["name"], "ok": True, "balance": r["balance"]})
        except seamless.SeamlessError as e:
            out.append({"name": a["name"], "ok": False, "error": str(e)})
    if not out:
        raise worker.PullError("No Seamless account is set up. Add your API key on the Seamless accounts page.")
    return {"ok": all(r["ok"] for r in out), "accounts": out, "balance": accounts.total_balance()}


# ---------------------------------------------------------------- Seamless accounts (keys entered on screen)
def _accounts_out():
    return {"rows": [accounts.summary(a) for a in accounts.all_accounts()],
            "shared_pools": accounts.shared_pool_groups(), "reserve": cfg.ACCOUNT_RESERVE,
            "credits": _credit_summary()}


@app.get("/api/accounts", dependencies=[Depends(require_login)])
def list_accounts():
    return _accounts_out()


@app.post("/api/accounts", dependencies=[Depends(require_login)])
async def add_account(request: Request):
    body = await request.json()
    key = (body.get("api_key") or "").strip()
    name = (body.get("name") or "").strip()[:60]
    if not key or len(key) < 8:
        raise HTTPException(400, "Paste the Seamless API key (Seamless → Settings → Public API).")
    if db.q1("SELECT id FROM accounts WHERE api_key=?", (key,)):
        raise HTTPException(400, "That API key is already added.")
    probe = {"id": None, "name": name or "this account", "api_key": key}
    r = seamless.test_connection(probe)             # free; fails with a clear message on a bad key
    n = db.q1("SELECT COALESCE(MAX(priority),0) m FROM accounts")["m"]
    aid = db.x("INSERT INTO accounts(name, api_key, priority, enabled, monthly_budget, source, created_at) "
               "VALUES(?,?,?,?,?,?,?)", (name or f"Account {n + 1}", key, n + 1, 1,
                                         max(0, int(body.get("monthly_budget") or 0)), "ui", time.time()))
    if r.get("balance") is not None:
        accounts.record_balance(aid, r["balance"])
    return _accounts_out()


@app.patch("/api/accounts/{aid}", dependencies=[Depends(require_login)])
async def edit_account(aid: int, request: Request):
    body = await request.json()
    a = accounts.get(aid)
    if not a:
        raise HTTPException(404)
    fields = {}
    if "name" in body:
        fields["name"] = (body["name"] or a["name"]).strip()[:60]
    if "enabled" in body:
        fields["enabled"] = 1 if body["enabled"] else 0
    if "monthly_budget" in body:
        fields["monthly_budget"] = max(0, int(body["monthly_budget"] or 0))
    if body.get("api_key"):
        if a["source"] == "env":
            raise worker.PullError("This key comes from .env (SEAMLESS_API_KEY). Change it there.")
        key = body["api_key"].strip()
        seamless.test_connection({"id": None, "name": a["name"], "api_key": key})
        fields["api_key"] = key
        fields["balance"] = None
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        db.x(f"UPDATE accounts SET {cols} WHERE id=?", (*fields.values(), aid))
    return _accounts_out()


@app.post("/api/accounts/order", dependencies=[Depends(require_login)])
async def order_accounts(request: Request):
    ids = [int(i) for i in (await request.json()).get("ids") or []]
    for i, aid in enumerate(ids, start=1):
        db.x("UPDATE accounts SET priority=? WHERE id=?", (i, aid))
    return _accounts_out()


@app.post("/api/accounts/{aid}/test", dependencies=[Depends(require_login)])
def test_account(aid: int):
    a = accounts.get(aid)
    if not a:
        raise HTTPException(404)
    seamless.test_connection(a)
    return _accounts_out()


@app.delete("/api/accounts/{aid}", dependencies=[Depends(require_login)])
def delete_account(aid: int):
    a = accounts.get(aid)
    if not a:
        raise HTTPException(404)
    if a["source"] == "env":
        raise worker.PullError("This key comes from .env (SEAMLESS_API_KEY). Switch it off here, or remove it from .env.")
    busy = db.q1("""SELECT COUNT(*) n FROM pulls WHERE account_id=? AND status IN ('collecting','researching','paused')""",
                 (aid,))["n"]
    waiting = db.q1("SELECT COUNT(*) n FROM research_requests WHERE account_id=? AND (resolved_at IS NULL OR gave_up=1)",
                    (aid,))["n"]
    if busy or waiting:
        raise worker.PullError("Pulls are still using this account (or waiting for its answers). Switch it off instead; "
                               "delete it once they're finished.")
    db.x("DELETE FROM accounts WHERE id=?", (aid,))
    return _accounts_out()


# ---------------------------------------------------------------- lookups (free)
@app.get("/api/lookup/locations", dependencies=[Depends(require_login)])
def lookup_locations(q: str, types: str = ""):
    q = (q or "").strip().lstrip("-")
    if len(q) < 2:
        return {"rows": []}
    rows = seamless.lookup_location(q, types or None, limit=10)
    return {"rows": [{"value": r.get("location"), "type": r.get("type"), "count": r.get("count")} for r in rows
                     if r.get("location")]}


@app.post("/api/settings/test-claude", dependencies=[Depends(require_login)])
def test_claude():
    out = claude.prompt_to_filters("CTOs at software companies in the United States")
    return {"ok": True, "sample": out}


def _page(offset, limit):
    return max(0, int(offset or 0)), max(1, min(int(limit or cfg.TABLE_PAGE_SIZE), 250))


def _order(sort, allowed, default, dir_):
    col = allowed.get(sort) or default
    return f"{col} {'ASC' if (dir_ or '').lower() == 'asc' else 'DESC'}"


@app.get("/api/ledger", dependencies=[Depends(require_login)])
def ledger(offset: int = 0, limit: int = 0, account: int = 0, pull: int = 0, q: str = "", sort: str = "",
           dir: str = "desc"):
    offset, limit = _page(offset, limit)
    where, args = ["1=1"], []
    if account:
        where.append("l.account_id=?")
        args.append(account)
    if pull:
        where.append("l.pull_id=?")
        args.append(pull)
    if q:
        where.append("(l.note LIKE ? OR p.name LIKE ?)")
        args += [f"%{q}%"] * 2
    w = " AND ".join(where)
    total = db.q1(f"SELECT COUNT(*) n FROM ledger l LEFT JOIN pulls p ON p.id=l.pull_id WHERE {w}", args)["n"]
    order = _order(sort, {"at": "l.id", "credits": "l.credits", "pull": "p.name"}, "l.id", dir)
    rows = db.q(f"""SELECT l.*, p.name AS pull_name, a.name AS account_name FROM ledger l
                    LEFT JOIN pulls p ON p.id=l.pull_id LEFT JOIN accounts a ON a.id=l.account_id
                    WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?""", (*args, limit, offset))
    return {"rows": rows, "total": total, "credits": _credit_summary()}


# ---------------------------------------------------------------- pulls
def _pull_stats(pid):
    one = lambda sql: db.q1(sql, (pid,))["n"]
    stats = {
        "seen": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=?"),
        "kept": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1"),
        "skipped": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=0"),
        "selected": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND selected=1"),
        "to_research": one("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND selected=1
                              AND submitted_at IS NULL"""),
        "submitted": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL"),
        "pending": one("SELECT COUNT(*) n FROM research_requests WHERE pull_id=? AND resolved_at IS NULL"),
        "with_email": one("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                             AND email IS NOT NULL AND email!=''"""),
        "fit": {r["fit"] or "unrated": r["n"] for r in db.q(
            "SELECT fit, COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 GROUP BY fit", (pid,))},
        "skip_reasons": worker.skip_report(pid),
        "by_country": db.q("""SELECT COALESCE(NULLIF(country,''),'Unknown') AS country, COUNT(*) n
                              FROM contacts WHERE pull_id=? AND kept=1 GROUP BY 1 ORDER BY n DESC LIMIT 12""", (pid,)),
        "by_size": db.q("""SELECT COALESCE(NULLIF(size,''),'Unknown') AS size, COUNT(*) n
                           FROM contacts WHERE pull_id=? AND kept=1 GROUP BY 1 ORDER BY n DESC""", (pid,)),
        "hit_rate": db.q("""SELECT COALESCE(NULLIF(country,''),'Unknown') AS country, COUNT(*) researched,
                            SUM(CASE WHEN email IS NOT NULL AND email!='' THEN 1 ELSE 0 END) AS with_email
                            FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                            AND research_status NOT IN ('queued','researching') GROUP BY 1 ORDER BY researched DESC""",
                         (pid,)),
        "dup_missing": one("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                              AND LOWER(research_status) IN ('duplicate','contact-already-researched')
                              AND (email IS NULL OR email='')"""),
        "research_outcomes": db.q("""SELECT research_status AS status, COUNT(*) n FROM contacts
                                     WHERE pull_id=? AND submitted_at IS NOT NULL GROUP BY 1 ORDER BY n DESC""", (pid,)),
        "no_email": one("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                           AND (email IS NULL OR email='') AND research_status NOT IN ('queued','researching')"""),
        "no_email_li": one("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                              AND (email IS NULL OR email='') AND research_status NOT IN ('queued','researching')
                              AND li_url IS NOT NULL AND li_url!=''"""),
        "no_email_phone": one("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                                 AND (email IS NULL OR email='') AND research_status NOT IN ('queued','researching')
                                 AND ((phone IS NOT NULL AND phone!='') OR (company_phone IS NOT NULL AND company_phone!=''))"""),
        "downloaded": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND batch_id IS NOT NULL"),
        "retries": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND selected=1 AND retry=1"),
        "filled_free": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND filled_free=1"),
        "chance_known": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND email_chance IS NOT NULL"),
        "timed_out": one("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                            AND research_status IN ('timed out','no answer')"""),
        "late_waiting": one("SELECT COUNT(*) n FROM research_requests WHERE pull_id=? AND gave_up=1"),
        "oldest_pending_at": db.q1("SELECT MIN(created_at) t FROM research_requests WHERE pull_id=? AND resolved_at IS NULL",
                                   (pid,))["t"],
        "skipped_blocked": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_reason LIKE 'Blocked company%'"),
        "skipped_no_server": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_reason LIKE 'Company domain can%'"),
    }
    stats["left_to_download"] = len(exports.eligible(pid, True))
    stats["left_to_download_valid"] = len(exports.eligible(pid, False))
    stats["charge_check"] = _charge_check(pid, stats)
    return stats


def _charge_check(pid, stats):
    """Did Seamless charge for people it found no email for? Compare the live balance before and after research."""
    p = db.q1("SELECT bal_before, bal_after, status FROM pulls WHERE id=?", (pid,))
    if not p or p["bal_before"] is None or p["bal_after"] is None or p["status"] != "done":
        return None
    dups = stats["dup_missing"] + db.q1("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND email IS NOT NULL
                                           AND LOWER(research_status) IN ('duplicate','contact-already-researched')""",
                                        (pid,))["n"]
    sent = stats["submitted"] - dups - stats["filled_free"]
    found = stats["with_email"] - stats["filled_free"]
    no_email = max(0, sent - found)
    drop = p["bal_before"] - p["bal_after"]
    if drop == sent and no_email:
        verdict = f"Seamless charged for every person sent, including the {no_email:,} with no email."
        code = "charged"
    elif drop == found and no_email:
        verdict = f"Seamless did NOT charge for the {no_email:,} people with no email."
        code = "free"
    elif not no_email:
        verdict = "Everyone sent had an email, so this pull can't tell whether no-email results are charged."
        code = "unknown"
    else:
        verdict = ("Couldn't tell: the balance changed by a different amount (someone else may have used Seamless "
                   "at the same time).")
        code = "unknown"
    return {"before": p["bal_before"], "after": p["bal_after"], "drop": drop, "sent": sent, "with_email": found,
            "no_email": no_email, "verdict": verdict, "code": code}


def _pull_out(pid):
    p = db.get_pull(pid)
    if not p:
        raise HTTPException(404, "Pull not found.")
    p["stats"] = _pull_stats(pid)
    p["credits"] = _credit_summary()
    a = accounts.get(p.get("account_id"))
    p["account"] = accounts.summary(a) if a else None
    p["account_options"] = [{"id": x["id"], "name": x["name"], "balance": x["balance"], "usable": accounts.usable(x)}
                            for x in accounts.enabled()]
    p["move_to"] = accounts.others_with_room(a["id"]) if (a and p.get("pause_kind") == "account") else []
    p.pop("claude_filters_json", None)
    cf = db.q1("SELECT claude_filters_json FROM pulls WHERE id=?", (pid,))["claude_filters_json"]
    p["hand_edits"] = _hand_edits(json.loads(cf), p["filters"]) if cf else []
    return p


def _hand_edits(claude_filters, filters):
    """What you changed by hand since Claude read the request (shown before 'Rebuild from this')."""
    labels = {f["key"]: f["label"] for f in FILTERS}
    out = []
    for k in sorted(set(claude_filters) | set(filters)):
        a, b = claude_filters.get(k), filters.get(k)
        if a == b:
            continue
        if b is None:
            out.append(f"Removed {labels.get(k, k)}")
        elif a is None:
            out.append(f"Added {labels.get(k, k)}: {', '.join(map(str, b)) if isinstance(b, list) else b}")
        else:
            out.append(f"Changed {labels.get(k, k)} to {', '.join(map(str, b)) if isinstance(b, list) else b}")
    return out


@app.get("/api/pulls", dependencies=[Depends(require_login)])
def list_pulls(offset: int = 0, limit: int = 0, q: str = "", status: str = "", sort: str = "", dir: str = "desc"):
    offset, limit = _page(offset, limit)
    where, args = ["1=1"], []
    if q:
        where.append("(p.name LIKE ? OR p.prompt LIKE ?)")
        args += [f"%{q}%"] * 2
    if status:
        where.append("p.status=?")
        args.append(status)
    w = " AND ".join(where)
    total = db.q1(f"SELECT COUNT(*) n FROM pulls p WHERE {w}", args)["n"]
    order = _order(sort, {"id": "p.id", "name": "p.name", "credits": "(p.search_credits + p.research_credits)",
                          "created": "p.created_at", "status": "p.status"}, "p.id", dir)
    rows = db.q(f"""SELECT p.id, p.name, p.status, p.target, p.search_credits, p.research_credits, p.created_at,
                   p.finished_at, p.total_matches, p.cloned_from, a.name AS account_name,
                   (SELECT COUNT(*) FROM contacts c WHERE c.pull_id=p.id AND c.kept=1) AS kept,
                   (SELECT COUNT(*) FROM contacts c WHERE c.pull_id=p.id AND c.email IS NOT NULL AND c.email!='') AS with_email
                   FROM pulls p LEFT JOIN accounts a ON a.id=p.account_id WHERE {w} ORDER BY {order}
                   LIMIT ? OFFSET ?""", (*args, limit, offset))
    return {"rows": rows, "total": total}


@app.post("/api/pulls/interpret", dependencies=[Depends(require_login)])
async def interpret(request: Request):
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Describe who you want to find.")
    pid = body.get("pull_id")
    if pid:
        pull = db.get_pull(pid)
        if pull["status"] not in ("draft", "previewed", "collected"):
            raise worker.PullError("Filters can't change while the pull is running.")
        if _pull_stats(pid)["submitted"]:
            raise worker.PullError("Research has started on this pull. Use “Edit filters (new copy)”.")
        if body.get("replace"):
            # "Rebuild filters from this": start again from the (edited) request, not from the current filters.
            out = claude.prompt_to_filters(prompt)
            filters = db.with_defaults(out["filters"])
            db.update_pull(pid, filters=filters, notes=out["notes"], status="draft", prompt=prompt,
                           claude_filters_json=json.dumps(filters), target=out["target"] or pull["target"])
        else:
            out = claude.prompt_to_filters(prompt, pull["filters"])
            db.update_pull(pid, filters=out["filters"], notes=out["notes"], status="draft",
                           prompt=(pull["prompt"] or "") + "\n" + prompt, claude_filters_json=json.dumps(out["filters"]))
        db.x("DELETE FROM contacts WHERE pull_id=? AND submitted_at IS NULL", (pid,))
        return _pull_out(pid)
    out = claude.prompt_to_filters(prompt)
    filters = db.with_defaults(out["filters"])
    pid = db.create_pull(out["name"], prompt, filters, out["notes"], target=out["target"])
    db.update_pull(pid, claude_filters_json=json.dumps(filters))
    return _pull_out(pid)


@app.post("/api/pulls", dependencies=[Depends(require_login)])
async def create_pull(request: Request):
    body = await request.json()
    filters = db.with_defaults(clean_filters(body.get("filters") or {}))
    pid = db.create_pull((body.get("name") or "New pull")[:80], body.get("prompt") or "", filters, [],
                         target=int(body.get("target") or 0))
    return _pull_out(pid)


@app.get("/api/pulls/{pid}", dependencies=[Depends(require_login)])
def get_pull(pid: int):
    return _pull_out(pid)


@app.patch("/api/pulls/{pid}", dependencies=[Depends(require_login)])
async def edit_pull(pid: int, request: Request):
    body = await request.json()
    pull = db.get_pull(pid)
    if not pull:
        raise HTTPException(404)
    fields = {}
    if "name" in body:
        fields["name"] = (body["name"] or "Untitled")[:80]
    for k in ("company_cooldown", "per_company_cap"):
        if k in body and body[k] not in (None, ""):
            if pull["status"] != "draft":
                raise worker.PullError("Change this before Preview (or use “Edit filters”).")
            fields[k] = max(0, int(body[k]))
    if "account_choice" in body:
        if pull["status"] != "draft":
            raise worker.PullError("Choose the account before Preview.")
        v = str(body["account_choice"] or "auto")
        if v != "auto" and not accounts.get(int(v)):
            raise worker.PullError("That Seamless account doesn't exist.")
        fields["account_choice"] = v
        fields["account_id"] = None
    if "filters" in body:
        if pull["status"] not in ("draft", "previewed", "collected"):
            raise worker.PullError("Filters can't change while the pull is running.")
        if pull["status"] == "collected" and _pull_stats(pid)["submitted"]:
            raise worker.PullError("Research has started on this pull. Use “Run again” to change the filters.")
        fields["filters"] = clean_filters(body["filters"])
        fields["status"] = "draft"
        db.x("DELETE FROM contacts WHERE pull_id=?", (pid,))
    if fields:
        db.update_pull(pid, **fields)
    return _pull_out(pid)


@app.post("/api/pulls/{pid}/preview", dependencies=[Depends(require_login)])
async def preview(pid: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    worker.run_preview(pid, from_top=bool(body.get("from_top")))
    return _pull_out(pid)


@app.get("/api/pulls/{pid}/estimate", dependencies=[Depends(require_login)])
def estimate(pid: int, target: int = 0):
    if not db.get_pull(pid):
        raise HTTPException(404)
    return worker.estimate_collect(pid, target)


@app.get("/api/pulls/{pid}/choice-count", dependencies=[Depends(require_login)])
def choice_count(pid: int, choice: str = "all", first_n: int = 0, chance_min: float = -1):
    ids = worker.choice_ids(pid, choice, first_n, None if chance_min < 0 else chance_min)
    a = accounts.get((db.get_pull(pid) or {}).get("account_id"))
    room = worker.budget_left(a) if a else None
    return {"n": len(ids), "credits": len(ids), "room": room, "account": a["name"] if a else None,
            "selected": db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND selected=1 "
                              "AND submitted_at IS NULL", (pid,))["n"]}


@app.post("/api/pulls/{pid}/collect", dependencies=[Depends(require_login)])
async def collect(pid: int, request: Request):
    body = await request.json()
    worker.start_collect(pid, body.get("target") or 0, body.get("per_company_cap") or 0)
    return _pull_out(pid)


@app.post("/api/pulls/{pid}/research", dependencies=[Depends(require_login)])
async def research(pid: int, request: Request):
    body = await request.json()
    worker.confirm_research(pid, body.get("choice") or "all", body.get("first_n"), body.get("chance_min"),
                            body.get("expected"))
    return _pull_out(pid)


@app.post("/api/pulls/{pid}/fill-saved", dependencies=[Depends(require_login)])
def fill_saved(pid: int):
    pull = db.get_pull(pid)
    if not pull:
        raise HTTPException(404)
    if pull["status"] in ("collecting", "researching"):
        raise worker.PullError("Wait until research has finished.")
    result = worker.fill_from_saved(pid)
    out = _pull_out(pid)
    out["fill_result"] = result
    return out


@app.post("/api/fill-stop", dependencies=[Depends(require_login)])
def fill_stop():
    worker.stop_fill()
    return {"ok": True}


@app.post("/api/stop-all", dependencies=[Depends(require_login)])
def stop_all():
    return {"stopped": worker.stop_all()}


@app.post("/api/pulls/{pid}/chances", dependencies=[Depends(require_login)])
def refresh_chances(pid: int):
    worker.compute_email_chances(pid)
    return _pull_out(pid)


@app.post("/api/pulls/{pid}/batches", dependencies=[Depends(require_login)])
async def new_batch(pid: int, request: Request):
    body = await request.json()
    pull = db.get_pull(pid)
    if not pull:
        raise HTTPException(404)
    bid = exports.create_batch(pid, body.get("count"), "country" if body.get("split") == "country" else "one",
                               bool(body.get("include_risky", True)), body.get("columns"))
    return {"batch_id": bid, "pull": _pull_out(pid)}


@app.get("/api/pulls/{pid}/batches", dependencies=[Depends(require_login)])
def list_batches(pid: int, offset: int = 0, limit: int = 0):
    offset, limit = _page(offset, limit)
    total = db.q1("SELECT COUNT(*) n FROM batches WHERE pull_id=?", (pid,))["n"]
    rows = db.q("SELECT * FROM batches WHERE pull_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (pid, limit, offset))
    for r in rows:
        r["countries"] = json.loads(r.pop("countries_json") or "{}")
        r["columns"] = json.loads(r.pop("columns_json") or "[]")
    return {"rows": rows, "total": total}


@app.get("/api/column-sets", dependencies=[Depends(require_login)])
def column_sets():
    rows = db.q("SELECT * FROM column_sets ORDER BY name")
    for r in rows:
        r["columns"] = json.loads(r.pop("columns_json") or "[]")
    return {"rows": rows}


@app.post("/api/column-sets", dependencies=[Depends(require_login)])
async def save_column_set(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(400, "Give the column set a name.")
    cols = exports.parse_columns(body.get("columns"), [])
    if not cols:
        raise HTTPException(400, "Pick at least one column.")
    old = db.q1("SELECT id FROM column_sets WHERE name=?", (name,))
    if old:
        db.x("UPDATE column_sets SET columns_json=? WHERE id=?", (json.dumps(cols), old["id"]))
    else:
        db.x("INSERT INTO column_sets(name, columns_json, created_at) VALUES(?,?,?)", (name, json.dumps(cols), time.time()))
    return column_sets()


@app.delete("/api/column-sets/{sid}", dependencies=[Depends(require_login)])
def delete_column_set(sid: int):
    db.x("DELETE FROM column_sets WHERE id=?", (sid,))
    return column_sets()


@app.get("/api/batches/{bid}/download", dependencies=[Depends(require_login)])
def download_batch(bid: int):
    out = exports.batch_file(bid)
    if not out:
        raise HTTPException(404)
    data, name, mime = out
    return _download(data, name, mime)


@app.post("/api/pulls/{pid}/move-account", dependencies=[Depends(require_login)])
async def move_account(pid: int, request: Request):
    body = await request.json()
    new_id = worker.move_account(pid, body.get("account_id"))
    return _pull_out(new_id)


@app.post("/api/pulls/{pid}/{action}", dependencies=[Depends(require_login)])
def pull_action(pid: int, action: str):
    if action == "pause":
        worker.pause(pid)
    elif action == "resume":
        worker.resume(pid)
    elif action == "cancel":
        worker.cancel(pid)
    elif action in ("rerun", "edit-copy"):
        pull = db.get_pull(pid)
        suffix = " (new people)" if action == "rerun" else " (edited)"
        new_id = db.create_pull(pull["name"] + suffix, pull["prompt"], pull["filters"], pull["notes"],
                                target=pull["target"], per_company_cap=pull["per_company_cap"], cloned_from=pid,
                                cooldown=pull["company_cooldown"])
        cf = db.q1("SELECT claude_filters_json FROM pulls WHERE id=?", (pid,))["claude_filters_json"]
        db.update_pull(new_id, claude_filters_json=cf, account_choice=pull.get("account_choice") or "auto",
                       edited_from=pid if action == "edit-copy" else None)
        return _pull_out(new_id)
    elif action == "back-to-filters":
        pull = db.get_pull(pid)
        if _pull_stats(pid)["submitted"] or pull["status"] in ("researching", "done"):
            raise worker.PullError("Research has started on this pull. Use “Edit filters (new copy)”.")
        if pull["status"] == "collecting":
            raise worker.PullError("Stop collecting first, then edit the filters.")
        if pull["status"] == "cancelled":
            raise worker.PullError("This pull was stopped. Use “Edit filters (new copy)”.")
        if pull["status"] in ("previewed", "collected", "paused"):
            # The collected list is cleared (its search credits are already spent); the filters stay.
            db.x("DELETE FROM contacts WHERE pull_id=? AND submitted_at IS NULL", (pid,))
            db.x("DELETE FROM collect_pages WHERE pull_id=?", (pid,))
            db.update_pull(pid, status="draft", next_token=None, stop_reason=None, error=None, pause_kind=None)
    elif action == "finish-now":
        worker.finish_now(pid)
    else:
        raise HTTPException(404)
    return _pull_out(pid)


@app.delete("/api/pulls/{pid}", dependencies=[Depends(require_login)])
def delete_pull(pid: int):
    pull = db.get_pull(pid)
    if not pull:
        raise HTTPException(404)
    if pull["status"] in ("collecting", "researching"):
        raise worker.PullError("Pause or stop the pull before deleting it.")
    # “seen” keys stay, so people you already paid for are never pulled again.
    db.x("DELETE FROM contacts WHERE pull_id=?", (pid,))
    db.x("DELETE FROM research_requests WHERE pull_id=?", (pid,))
    db.x("DELETE FROM pulls WHERE id=?", (pid,))
    return {"ok": True}


CONTACT_COLS = """id, name, first_name, last_name, title, company, domain, country, state, city, li_url, seniority,
                  department, size, industry, fit, fit_reason, kept, skip_reason, skip_kind, selected, research_status,
                  research_message, submitted_at, email, email_status, email_confidence, phone, phone_type,
                  company_phone, timezone, funding, news, email_chance, email_chance_n, email_chance_basis, retry,
                  batch_id, filled_free"""


def _contact_where(pid, view, fit, q, kind=""):
    where = ["pull_id=?"]
    args = [pid]
    if view == "kept":
        where.append("kept=1")
    elif view == "skipped":
        where.append("kept=0")
    elif view == "results":
        where.append("submitted_at IS NOT NULL")
    elif view == "with_email":
        where.append("submitted_at IS NOT NULL AND email IS NOT NULL AND email!=''")
    elif view == "not_downloaded":
        where.append("submitted_at IS NOT NULL AND email IS NOT NULL AND email!='' AND batch_id IS NULL")
    elif view == "downloaded":
        where.append("batch_id IS NOT NULL")
    elif view == "no_email":
        where.append("submitted_at IS NOT NULL AND (email IS NULL OR email='') "
                     "AND research_status NOT IN ('queued','researching')")
    elif view == "selectable":
        where.append("kept=1 AND submitted_at IS NULL")
    if fit:
        where.append("COALESCE(fit,'unrated')=?")
        args.append(fit)
    if kind:
        where.append("COALESCE(skip_kind,'other')=?")
        args.append(kind)
    if q:
        where.append("(name LIKE ? OR company LIKE ? OR title LIKE ? OR email LIKE ? OR country LIKE ?)")
        args += [f"%{q}%"] * 5
    return " AND ".join(where), args


CONTACT_SORTS = {"name": "name COLLATE NOCASE", "title": "title COLLATE NOCASE", "company": "company COLLATE NOCASE",
                 "country": "country", "fit": "CASE fit WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END",
                 "chance": "COALESCE(email_chance,-1)", "email": "email", "result": "research_status",
                 "reason": "skip_reason", "id": "id"}


@app.get("/api/pulls/{pid}/contacts", dependencies=[Depends(require_login)])
def pull_contacts(pid: int, view: str = "kept", offset: int = 0, limit: int = 0, fit: str = "", q: str = "",
                  kind: str = "", sort: str = "", dir: str = "asc"):
    offset, limit = _page(offset, limit)
    w, args = _contact_where(pid, view, fit, q, kind)
    total = db.q1(f"SELECT COUNT(*) n FROM contacts WHERE {w}", args)["n"]
    order = _order(sort, CONTACT_SORTS, "id", dir)
    rows = db.q(f"SELECT {CONTACT_COLS} FROM contacts WHERE {w} ORDER BY {order}, id LIMIT ? OFFSET ?",
                (*args, limit, offset))
    out = {"total": total, "rows": rows, "offset": offset, "limit": limit}
    if view in ("kept", "selectable"):
        out["ticked"] = db.q1(f"SELECT COUNT(*) n FROM contacts WHERE {w} AND selected=1", args)["n"]
    return out


@app.post("/api/pulls/{pid}/contacts/select", dependencies=[Depends(require_login)])
async def select_contacts(pid: int, request: Request):
    body = await request.json()
    sel = 1 if body.get("selected") else 0
    pull = db.get_pull(pid)
    if pull["status"] not in ("collected", "previewed"):
        raise worker.PullError("Selection can only change before research starts.")
    if body.get("all_matching"):
        # Every person matching the table's search / fit filter, on every page.
        w, args = _contact_where(pid, "selectable", body.get("fit") or "", body.get("q") or "")
        db.x(f"UPDATE contacts SET selected=? WHERE {w}", (sel, *args))
    else:
        with db.tx():
            for cid in [int(i) for i in body.get("ids") or []]:
                db.x("UPDATE contacts SET selected=? WHERE id=? AND pull_id=? AND kept=1 AND submitted_at IS NULL",
                     (sel, cid, pid))
    ticked = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND selected=1 AND submitted_at IS NULL",
                   (pid,))["n"]
    return {"ok": True, "ticked": ticked}


# ---------------------------------------------------------------- export
_cell = exports.cell
_risky = exports.risky


@app.get("/api/pulls/{pid}/export", dependencies=[Depends(require_login)])
def export(pid: int, format: str = "full", include_risky: int = 1, columns: str = ""):
    """format: full (CSV, every researched person), xlsx, no_email, anavmail (everyone with an email, NOT tracked
    as downloaded -- for re-downloads; new campaigns should use batches)."""
    pull = db.get_pull(pid)
    if not pull:
        raise HTTPException(404)
    safe = exports.safe_name(pull["name"], f"pull-{pid}")
    if format == "no_email":
        cols = exports.parse_columns(columns, exports.NO_EMAIL_DEFAULT)
        return _download(exports.rows_to_csv(exports.no_email_rows(pid), cols), f"{safe}-no-email.csv", "text/csv")
    if format == "anavmail":
        rows = [r for r in exports.all_rows(pid) if r["email"]
                and (r["email_status"] or "").lower() != "invalid"
                and (include_risky or not _risky(r["email_status"]))]
        seen, unique = set(), []
        for r in rows:
            e = r["email"].strip().lower()
            if e not in seen:
                seen.add(e)
                unique.append(r)
        cols = exports.parse_columns(columns, ["email", "first_name", "last_name", "company", "title", "linkedin"],
                                     need_email=True)
        return _download(exports.rows_to_csv(unique, cols), f"{safe}-anavmail.csv", "text/csv")
    cols = exports.parse_columns(columns, exports.ALL_KEYS)
    rows = exports.all_rows(pid)
    if format == "xlsx":
        return _download(exports.rows_to_xlsx(rows, cols), f"{safe}.xlsx",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return _download(exports.rows_to_csv(rows, cols), f"{safe}-full.csv", "text/csv")


def _download(data, filename, mime):
    return StreamingResponse(iter([data]), media_type=mime,
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------- presets
@app.get("/api/presets", dependencies=[Depends(require_login)])
def presets(offset: int = 0, limit: int = 0, q: str = ""):
    offset, limit = _page(offset, limit)
    where, args = ("WHERE name LIKE ?", [f"%{q}%"]) if q else ("", [])
    total = db.q1(f"SELECT COUNT(*) n FROM presets {where}", args)["n"]
    rows = db.q(f"SELECT * FROM presets {where} ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?", (*args, limit, offset))
    for r in rows:
        r["filters"] = json.loads(r.pop("filters_json") or "{}")
    return {"rows": rows, "total": total}


@app.post("/api/presets", dependencies=[Depends(require_login)])
async def save_preset(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Give the preset a name.")
    filters = clean_filters(body.get("filters") or {})
    db.x("INSERT INTO presets(name,prompt,filters_json,created_at) VALUES(?,?,?,?)",
         (name[:80], body.get("prompt") or "", json.dumps(filters), time.time()))
    return presets()


@app.post("/api/presets/{preset_id}/use", dependencies=[Depends(require_login)])
def use_preset(preset_id: int):
    p = db.q1("SELECT * FROM presets WHERE id=?", (preset_id,))
    if not p:
        raise HTTPException(404)
    pid = db.create_pull(p["name"], p["prompt"], json.loads(p["filters_json"]), [])
    db.update_pull(pid, claude_filters_json=p["filters_json"])
    return _pull_out(pid)


@app.delete("/api/presets/{preset_id}", dependencies=[Depends(require_login)])
def delete_preset(preset_id: int):
    db.x("DELETE FROM presets WHERE id=?", (preset_id,))
    return presets()


# ---------------------------------------------------------------- exclusions (do-not-pull list)
def _add_exclusion(kind, value, source):
    value = (value or "").strip()
    if not value:
        return False
    if kind == "email":
        if "@" not in value:
            return False
        key = "em:" + value.lower()
    elif kind == "domain":
        value = db.norm_domain(value.split("@")[-1])
        if "." not in value:
            return False
        key = "domain:" + value
    elif kind == "linkedin":
        key = "li:" + db.norm_li(value)
    elif kind == "name_company":
        key = value
    elif kind == "company_name":
        n = db.norm(value)
        if len(n) < 2:
            return False
        key = "cn:" + n
    else:
        return False
    db.x("INSERT OR IGNORE INTO exclusions(key,kind,value,source,created_at) VALUES(?,?,?,?,?)",
         (key, kind, value, source, time.time()))
    return True


@app.get("/api/exclusions", dependencies=[Depends(require_login)])
def list_exclusions(q: str = "", offset: int = 0, limit: int = 0):
    offset, limit = _page(offset, limit)
    where, args = "", []
    if q:
        where, args = "WHERE value LIKE ?", [f"%{q}%"]
    total = db.q1(f"SELECT COUNT(*) n FROM exclusions {where}", args)["n"]
    rows = db.q(f"SELECT * FROM exclusions {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*args, limit, offset))
    counts = {r["kind"]: r["n"] for r in db.q("SELECT kind, COUNT(*) n FROM exclusions GROUP BY kind")}
    return {"total": total, "rows": rows, "counts": counts}


def _pick(header, *names):
    low = [h.strip().lower().replace(" ", "_") for h in header]
    for n in names:
        if n in low:
            return low.index(n)
    return None


@app.post("/api/exclusions/upload", dependencies=[Depends(require_login)])
async def upload_exclusions(file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(raw))
    header = next(reader, [])
    ie = _pick(header, "email", "email_address", "e-mail")
    idom = _pick(header, "domain", "website", "company_domain")
    ili = _pick(header, "linkedin", "linkedin_url", "li_url", "linkedin_profile")
    ifn, iln = _pick(header, "first_name", "firstname", "first"), _pick(header, "last_name", "lastname", "last")
    iname = _pick(header, "name", "full_name")
    ico = _pick(header, "company", "company_name", "organization")
    if all(i is None for i in (ie, idom, ili, ico)):
        raise HTTPException(400, "Couldn't find an email, domain, linkedin or company column in that file.")
    added = 0
    src = file.filename or "upload"
    for row in reader:
        get = lambda i: row[i].strip() if i is not None and i < len(row) else ""
        if get(ie):
            added += _add_exclusion("email", get(ie), src)
        if get(idom) and not get(ie):
            added += _add_exclusion("domain", get(idom), src)
        if get(ili):
            added += _add_exclusion("linkedin", get(ili), src)
        name = get(iname) or f"{get(ifn)} {get(iln)}".strip()
        if name and get(ico):
            added += _add_exclusion("name_company", "nc:" + db.norm(name) + "|" + db.norm(get(ico)), src)
        elif get(ico) and not get(ie) and not get(ili):
            # A company-only row (no person): block the whole company everywhere.
            added += _add_exclusion("company_name", get(ico), src)
    return {"added": added, **list_exclusions()}


@app.post("/api/exclusions", dependencies=[Depends(require_login)])
async def add_exclusions(request: Request):
    body = await request.json()
    added = 0
    for line in (body.get("companies") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "." in line and " " not in line and "@" not in line:
            added += _add_exclusion("domain", line, "typed (company)")
        else:
            added += _add_exclusion("company_name", line, "typed (company)")
    for line in (body.get("text") or "").replace(",", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        if "linkedin.com" in line.lower():
            added += _add_exclusion("linkedin", line, "typed")
        elif "@" in line and not line.startswith("@"):
            added += _add_exclusion("email", line, "typed")
        else:
            added += _add_exclusion("domain", line.lstrip("@"), "typed")
    return {"added": added, **list_exclusions()}


@app.delete("/api/exclusions", dependencies=[Depends(require_login)])
def clear_exclusions(key: str = ""):
    if key:
        db.x("DELETE FROM exclusions WHERE key=?", (key,))
    else:
        db.x("DELETE FROM exclusions")
    return list_exclusions()


# ---------------------------------------------------------------- company memory
@app.get("/api/companies", dependencies=[Depends(require_login)])
def list_companies(q: str = "", status: str = "all", offset: int = 0, limit: int = 0, sort: str = "", dir: str = "desc"):
    offset, limit = _page(offset, limit)
    days = cfg.COMPANY_COOLDOWN_DAYS
    now = time.time()
    since = now - days * 86400
    where, args = ["1=1"], []
    if q:
        where.append("(name LIKE ? OR domain LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    cool = "(last_at IS NOT NULL AND last_at > ? AND (released_at IS NULL OR released_at < last_at))"
    if status == "cooldown":
        where.append(cool)
        args.append(since)
    elif status == "available":
        where.append("last_at IS NOT NULL AND NOT " + cool)
        args.append(since)
    elif status == "noemail":
        where.append("last_at IS NULL")
    w = " AND ".join(where)
    total = db.q1(f"SELECT COUNT(*) n FROM companies WHERE {w}", args)["n"]
    order = _order(sort, {"name": "COALESCE(name, domain) COLLATE NOCASE", "people": "people",
                          "last": "COALESCE(last_at, tried_last_at, first_at)"},
                   "COALESCE(last_at, tried_last_at, first_at)", dir)
    rows = db.q(f"""SELECT * FROM companies WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?""", (*args, limit, offset))
    for r in rows:
        released = r["released_at"] and r["last_at"] and r["released_at"] >= r["last_at"]
        r["available_at"] = None if (released or not r["last_at"]) else r["last_at"] + days * 86400
        r["on_cooldown"] = bool(days and r["available_at"] and r["available_at"] > now)
        r["blocked"] = bool(worker.blocked_company(r["name"], r["domain"]))
        r["no_email"] = not r["last_at"]
    counts = {
        "all": db.q1("SELECT COUNT(*) n FROM companies")["n"],
        "emailed": db.q1("SELECT COUNT(*) n FROM companies WHERE last_at IS NOT NULL")["n"],
        "cooldown": db.q1(f"SELECT COUNT(*) n FROM companies WHERE {cool}", (since,))["n"] if days else 0,
        "noemail": db.q1("SELECT COUNT(*) n FROM companies WHERE last_at IS NULL")["n"],
    }
    return {"total": total, "rows": rows, "counts": counts, "cooldown_days": days}


@app.post("/api/companies/release", dependencies=[Depends(require_login)])
async def release_company(request: Request):
    body = await request.json()
    db.x("UPDATE companies SET released_at=? WHERE key=?", (time.time(), body.get("key")))
    return {"ok": True}


@app.post("/api/companies/block", dependencies=[Depends(require_login)])
async def block_company(request: Request):
    body = await request.json()
    row = db.q1("SELECT * FROM companies WHERE key=?", (body.get("key"),))
    if not row:
        raise HTTPException(404)
    if row["domain"]:
        _add_exclusion("domain", row["domain"], "company memory")
    elif row["name"]:
        _add_exclusion("company_name", row["name"], "company memory")
    else:
        raise worker.PullError("This company has no domain or name on record.")
    return {"ok": True}


@app.get("/api/credits/countries", dependencies=[Depends(require_login)])
def cost_by_country():
    """Credits per email by country, across all pulls. Search credits are shared out by each pull's results."""
    out = {}
    for p in db.q("SELECT id, search_credits FROM pulls"):
        seen = db.q("SELECT COALESCE(NULLIF(country,''),'Unknown') c, COUNT(*) n FROM contacts WHERE pull_id=? GROUP BY 1",
                    (p["id"],))
        total = sum(r["n"] for r in seen) or 1
        for r in seen:
            out.setdefault(r["c"], {"country": r["c"], "search": 0.0, "researched": 0, "emails": 0})
            out[r["c"]]["search"] += (p["search_credits"] or 0) * r["n"] / total
    for r in db.q("""SELECT COALESCE(NULLIF(country,''),'Unknown') c,
                     SUM(CASE WHEN LOWER(research_status) IN ('duplicate','contact-already-researched') OR filled_free=1
                         THEN 0 ELSE 1 END) researched,
                     SUM(CASE WHEN email IS NOT NULL AND email!='' THEN 1 ELSE 0 END) emails
                     FROM contacts WHERE submitted_at IS NOT NULL GROUP BY 1"""):
        d = out.setdefault(r["c"], {"country": r["c"], "search": 0.0, "researched": 0, "emails": 0})
        d["researched"], d["emails"] = r["researched"] or 0, r["emails"] or 0
    rows = []
    for d in out.values():
        if not d["researched"]:
            continue
        credits = d["search"] + d["researched"]
        rows.append({**d, "search": round(d["search"], 1), "credits": round(credits, 1),
                     "hit_rate": round(100.0 * d["emails"] / d["researched"], 1),
                     "per_email": round(credits / d["emails"], 2) if d["emails"] else None})
    rows.sort(key=lambda r: -r["researched"])
    return {"rows": rows}


# ---------------------------------------------------------------- page
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def healthz():
    return {"ok": True}
