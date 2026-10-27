"""Lead Puller 2.0: web app (FastAPI) + background worker."""
import csv
import hmac
import io
import json
import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import accounts, claude, companies, db, engine, exports, guess, importer, memory, seamless, verifier
from . import filters as F
from .config import APP_VERSION, cfg, shown_settings

STATIC = os.path.join(os.path.dirname(__file__), "static")
_worker = None


@asynccontextmanager
async def lifespan(app):
    global _worker
    db.connect()
    importer.auto_import()
    accounts.sync_env_key()
    if not os.environ.get("DISABLE_WORKER"):
        _worker = engine.Worker()
        _worker.start()
    yield
    if _worker:
        _worker.stop()


app = FastAPI(title="Lead Puller", lifespan=lifespan, docs_url=None, redoc_url=None)


def _err(status):
    async def handler(request, exc):
        return JSONResponse({"error": str(exc), "code": getattr(exc, "code", None)}, status_code=status)
    return handler


for _exc in (engine.PullError, seamless.SeamlessError, claude.ClaudeError, verifier.VerifierError, ValueError):
    app.add_exception_handler(_exc, _err(400))


# ---------------------------------------------------------------- login
def require_login(request: Request):
    token = request.cookies.get("lp_session")
    if token:
        row = db.q1("SELECT created_at FROM sessions WHERE token=?", (token,))
        if row and time.time() - row["created_at"] < cfg.SESSION_DAYS * 86400:
            return True
    raise HTTPException(401, "Please log in.")


_fails = []


@app.get("/api/auth/status")
def auth_status(request: Request):
    try:
        ok = require_login(request)
    except HTTPException:
        ok = False
    return {"logged_in": ok, "no_password": not cfg.APP_PASSWORD}


@app.post("/api/auth/login")
async def login(request: Request, response: Response):
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < 300]
    if len(_fails) >= 10:
        raise HTTPException(429, "Too many wrong passwords. Wait 5 minutes.")
    if not cfg.APP_PASSWORD:
        raise HTTPException(400, "No password set. Ask your developer to set APP_PASSWORD in .env.")
    body = await request.json()
    if not hmac.compare_digest((body.get("password") or "").encode(), cfg.APP_PASSWORD.encode()):
        _fails.append(now)
        raise HTTPException(401, "Wrong password.")
    token = secrets.token_urlsafe(32)
    db.x("INSERT INTO sessions(token,created_at) VALUES(?,?)", (token, now))
    response.set_cookie("lp_session", token, max_age=cfg.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=cfg.COOKIE_SECURE)
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    db.x("DELETE FROM sessions WHERE token=?", (request.cookies.get("lp_session") or "",))
    response.delete_cookie("lp_session")
    return {"ok": True}


AUTH = [Depends(require_login)]


# ---------------------------------------------------------------- overview
def _shared_pools():
    on = [a for a in accounts.enabled() if a["balance"] is not None and a["balance_at"]]
    groups = {}
    for a in on:
        groups.setdefault(a["balance"], []).append(a)
    return [{"balance": b, "names": [a["name"] for a in g]} for b, g in groups.items()
            if len(g) > 1 and max(a["balance_at"] for a in g) - min(a["balance_at"] for a in g) < 900]


def _overview():
    accs = [accounts.summary(a) for a in accounts.all_accounts()]
    budget = cfg.MONTHLY_CREDIT_BUDGET
    spent = db.month_spend()
    return {"accounts": accs, "spent_this_month": spent, "monthly_budget": budget,
            "budget_left": max(0, budget - spent) if budget else None, "spending_enabled": cfg.SPENDING_ENABLED,
            "shared_pools": _shared_pools(), "low": [a["name"] for a in accs if a["enabled"] and a["low"]],
            "people_pulled": db.q1("SELECT COUNT(*) n FROM contacts WHERE submitted_at IS NOT NULL OR "
                                   "email_source='guessed'")["n"],
            "verifier": verifier.status(),
            "seamless_saved": db.q1("SELECT COUNT(*) n FROM saved_contacts")["n"],
            "emails_in_memory": db.q1("SELECT COUNT(*) n FROM memory WHERE key LIKE 'em:%'")["n"]}


@app.get("/api/meta", dependencies=AUTH)
def meta():
    return {"version": APP_VERSION, "fields": F.FIELDS, "defaults": {
        "filters": F.default_filters(), "target": cfg.DEFAULT_TARGET,
        "max_per_email": cfg.DEFAULT_MAX_CREDITS_PER_EMAIL, "max_per_company": cfg.MAX_PER_COMPANY,
        "columns": exports.default_columns(), "page_size": cfg.TABLE_PAGE_SIZE,
        "location_default": cfg.LOCATION_APPLIES_TO, "move_on": cfg.MOVE_ON_SEARCH_COST,
        "wait_minutes": cfg.RESEARCH_WAIT_MINUTES, "late_hours": cfg.LATE_ANSWER_HOURS,
        "per_company": cfg.DEFAULT_PER_COMPANY, "company_max": cfg.COMPANY_LIST_MAX,
        "company_only": sorted(companies.COMPANY_ONLY)},
        "columns": [{"key": k, "label": l} for k, l, _ in exports.COLUMNS], "tech_bundles": F.TECH_BUNDLES,
        "has_claude": bool(cfg.ANTHROPIC_API_KEY), "overview": _overview()}


@app.get("/api/overview", dependencies=AUTH)
def overview():
    return _overview()


@app.get("/api/settings", dependencies=AUTH)
def settings():
    return {"groups": shown_settings(), "version": APP_VERSION, "old_import": db.get_meta("old_import")}


# ---------------------------------------------------------------- helpers for the new-pull screen
@app.post("/api/describe", dependencies=AUTH)
async def describe(request: Request):
    text = ((await request.json()).get("text") or "").strip()
    if not text:
        raise engine.PullError("Describe who you want first.")
    return claude.describe(text)


@app.get("/api/lookup/places", dependencies=AUTH)
def places(q: str, types: str = ""):
    q = (q or "").strip()
    if len(q) < 2:
        return {"rows": []}
    acct = next(iter(accounts.enabled()), None)
    if not acct:
        return {"rows": []}
    rows = seamless.lookup_location(q, types or None, acct)
    return {"rows": [{"value": r.get("location"), "type": r.get("type"), "count": r.get("count")} for r in rows
                     if r.get("location")]}


@app.get("/api/lookup/tech", dependencies=AUTH)
def tech(q: str = ""):
    ql = (q or "").strip().lower()
    seen = {r["name"]: r["seen"] for r in db.q("SELECT name, seen FROM techs")}
    names = list(dict.fromkeys(list(seen) + F.TECH_COMMON))
    hits = [n for n in names if ql in n.lower()] if ql else names
    hits.sort(key=lambda n: (not n.lower().startswith(ql), -seen.get(n, 0), n.lower()))
    return {"rows": [{"value": n, "seen": seen.get(n, 0)} for n in hits[:15]]}


@app.post("/api/check-count", dependencies=AUTH)
async def check_count(request: Request):
    body = await request.json()
    return engine.check_count(body.get("filters") or {}, str(body.get("account") or "auto"))


@app.get("/api/estimate", dependencies=AUTH)
def estimate(target: int = 0):
    return engine.estimate(target)


# ---------------------------------------------------------------- pulls
def _page(offset, limit):
    return max(0, int(offset or 0)), max(1, min(int(limit or cfg.TABLE_PAGE_SIZE), 250))


def _pull_out(pid):
    p = db.get_pull(pid)
    if not p:
        raise HTTPException(404, "Pull not found.")
    st = engine.counts(pid)
    spend_s = db.pull_spend(pid, "search")
    spend_r = db.pull_spend(pid, "research")
    skips = db.q("""SELECT COALESCE(skip_kind,'other') kind, MIN(skip_reason) example, COUNT(*) n FROM contacts
                    WHERE pull_id=? AND state='skipped' GROUP BY 1 ORDER BY n DESC""", (pid,))
    no_email = db.q1("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL AND
                        (email IS NULL OR email='') AND state!='queued'""", (pid,))["n"]
    slices = db.q("SELECT * FROM pull_slices WHERE pull_id=? ORDER BY idx", (pid,))
    hit = engine.hit_rate(pid)
    for s in slices:
        s.pop("recent_json", None)
        s["new_pct"] = round(100.0 * s["kept"] / s["rows"]) if s["rows"] else None
        s["per_new_email"] = round(s["search_credits"] / (s["kept"] * hit) + 1 / hit, 2) if s["kept"] else None
    acct = accounts.get(p["current_account_id"])
    emails = st["emails"]
    spent = spend_s + spend_r
    oldest = db.q1("SELECT MIN(created_at) t FROM research_requests WHERE pull_id=? AND resolved_at IS NULL",
                   (pid,))["t"]
    g = db.q1(f"SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND email_source='guessed' AND {engine.DELIVERABLE}",
              (pid,))["n"]
    vl = {r["result"]: r["n"] for r in db.q("SELECT result, COUNT(*) n FROM verify_log WHERE pull_id=? AND cached=0 "
                                            "GROUP BY result", (pid,))}
    paid_email = db.q1(f"SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL AND "
                       f"{engine.DELIVERABLE}", (pid,))["n"]
    guessing = {"guessed": g, "paid": st["researched"], "paid_email": paid_email, "saved": g,
                "checks": sum(vl.values()), "valid": vl.get("ok", 0), "catch_all": vl.get("catch_all", 0),
                "not_valid": sum(n for k, n in vl.items() if k not in ("ok", "catch_all")),
                "ready": verifier.ready()[0], "why_not": verifier.ready()[1]}
    if p.get("mode") == "companies":
        cs = db.q1("SELECT COUNT(*) n, SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) r FROM pull_companies "
                   "WHERE pull_id=?", (pid,))
        p["companies"] = {"total": cs["n"], "ready": cs["r"] or 0}
    p.update({"guessing": guessing, "stats": {**st, "no_email": no_email, "search_credits": spend_s, "research_credits": spend_r,
                        "credits": spent, "per_email": round(spent / emails, 2) if emails else None,
                        "hit_rate": round(hit * 100), "late": db.q1(
                            "SELECT COUNT(*) n FROM research_requests WHERE pull_id=? AND gave_up=1", (pid,))["n"],
                        "oldest_pending_at": oldest, "skips": skips,
                        "downloadable": len(exports.anavmail_rows(pid)) if p["status"] == "done" else emails},
              "slices": slices, "account": acct["name"] if acct else None, "summary": F.summary(p["filters"]),
              "overview": _overview()})
    return p


@app.post("/api/pulls", dependencies=AUTH)
async def new_pull(request: Request):
    b = await request.json()
    clist = b.get("companies")
    if clist is not None:
        clist = [{"name": c.get("name") or "", "domain_raw": c.get("domain") or "", "signal": c.get("signal") or ""}
                 for c in clist if isinstance(c, dict)]
    pid = engine.create_pull(b.get("name"), b.get("prompt"), b.get("filters") or {}, b.get("target"),
                             b.get("max_credits"), str(b.get("account") or "auto"), b.get("max_per_company"),
                             company_list=clist)
    return _pull_out(pid)


# ---------------------------------------------------------------- company lists
def _companies_out(items, per_company, target):
    sm = companies.summary(items)
    return {"items": items, "summary": sm,
            "estimate": companies.estimate(sm["ready"], max(1, int(per_company or cfg.DEFAULT_PER_COMPANY)),
                                           target or sm["ready"] * max(1, int(per_company or cfg.DEFAULT_PER_COMPANY)),
                                           sm["known_format"], sm["catch_all"], verifier.ready()[0]),
            "guessing_ready": verifier.ready()[0], "guessing_why": verifier.ready()[1]}


@app.post("/api/companies/check", dependencies=AUTH)
async def companies_check(request: Request):
    """Paste or re-check a list (free): status per company, and the estimate."""
    b = await request.json()
    rows = companies.parse(text=b.get("text") or "")
    rows += [{"name": c.get("name") or "", "domain_raw": c.get("domain") or "", "signal": c.get("signal") or ""}
             for c in (b.get("items") or []) if isinstance(c, dict)]
    return _companies_out(companies.check(rows), b.get("per_company"), b.get("target"))


@app.post("/api/companies/upload", dependencies=AUTH)
async def companies_upload(file: UploadFile = File(...), per_company: int = 0, target: int = 0):
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = companies.parse(csv_text=raw)
    if not rows:
        raise engine.PullError("Couldn't find any companies in that file. It needs a website or domain column.")
    out = _companies_out(companies.check(rows), per_company, target)
    out["file"] = (file.filename or "upload.csv")[:120]
    return out


@app.get("/api/pulls/{pid}/companies", dependencies=AUTH)
def pull_companies(pid: int, offset: int = 0, limit: int = 0, q: str = "", all: int = 0):
    if all:
        return {"rows": db.q("SELECT name, domain, signal, status FROM pull_companies WHERE pull_id=? ORDER BY idx",
                             (pid,))}
    offset, limit = _page(offset, limit)
    return companies.rows_for_pull(pid, offset, limit, q)


@app.get("/api/pulls", dependencies=AUTH)
def list_pulls(offset: int = 0, limit: int = 0, q: str = ""):
    offset, limit = _page(offset, limit)
    where, args = ("WHERE name LIKE ?", [f"%{q}%"]) if q else ("", [])
    total = db.q1(f"SELECT COUNT(*) n FROM pulls {where}", args)["n"]
    rows = db.q(f"SELECT id FROM pulls {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
    out = []
    for r in rows:
        p = db.get_pull(r["id"])
        st = engine.counts(p["id"])
        cr = db.pull_spend(p["id"])
        acc = db.q("SELECT DISTINCT a.name FROM contacts c JOIN accounts a ON a.id=c.account_id WHERE c.pull_id=? "
                   "AND c.submitted_at IS NOT NULL", (p["id"],))
        out.append({"id": p["id"], "name": p["name"], "status": p["status"], "stopping": p["stopping"],
                    "created_at": p["created_at"], "target": p["target"], "emails": st["emails"], "credits": cr,
                    "per_email": round(cr / st["emails"], 2) if st["emails"] else None,
                    "accounts": ", ".join(a["name"] for a in acc), "stop_reason": p["stop_reason"]})
    return {"rows": out, "total": total}


@app.get("/api/pulls/{pid}", dependencies=AUTH)
def get_pull(pid: int):
    return _pull_out(pid)


@app.post("/api/pulls/{pid}/{action}", dependencies=AUTH)
def pull_action(pid: int, action: str):
    if not db.get_pull(pid):
        raise HTTPException(404)
    if action == "pause":
        engine.pause(pid)
    elif action == "resume":
        engine.resume(pid)
    elif action == "stop":
        engine.stop(pid)
    elif action == "finish-now":
        engine.finish_now(pid)
    elif action == "rerun":
        p = db.get_pull(pid)
        clist = None
        if p.get("mode") == "companies":
            clist = [{"name": r["name"], "domain_raw": r["domain"], "signal": r["signal"]} for r in
                     db.q("SELECT * FROM pull_companies WHERE pull_id=? ORDER BY idx", (pid,)) if r["domain"]]
        new = engine.create_pull(p["name"], p["prompt"], p["filters"], p["target"], p["max_credits"],
                                 p["account_choice"], p["max_per_company"], rerun_of=pid, company_list=clist)
        return _pull_out(new)
    else:
        raise HTTPException(404)
    return _pull_out(pid)


@app.delete("/api/pulls/{pid}", dependencies=AUTH)
def delete_pull(pid: int):
    p = db.get_pull(pid)
    if not p:
        raise HTTPException(404)
    if p["status"] in ("running", "queued") or db.q1(
            "SELECT 1 x FROM research_requests WHERE pull_id=? AND resolved_at IS NULL", (pid,)):
        raise engine.PullError("Stop the pull (and let its answers arrive) before deleting it.")
    # Memory stays: people you paid for are still never pulled again.
    db.x("DELETE FROM contacts WHERE pull_id=? AND state!='banked'", (pid,))
    db.x("DELETE FROM pull_slices WHERE pull_id=?", (pid,))
    db.x("DELETE FROM pull_companies WHERE pull_id=?", (pid,))
    db.x("DELETE FROM pulls WHERE id=?", (pid,))
    return {"ok": True}


@app.post("/api/stop-all", dependencies=AUTH)
def stop_all():
    return {"stopped": engine.stop_all()}


PEOPLE_COLS = """id, name, first_name, last_name, title, company, domain, li_url, country, city, size, industry, state,
                 skip_reason, skip_kind, email, email_status, phone, research_status, research_message, filled_free,
                 dup_email, email_source, guess_note"""
PEOPLE_VIEWS = {
    "emails": "state='done' AND email IS NOT NULL AND email!='' AND LOWER(COALESCE(email_status,''))!='invalid' AND dup_email=0",
    "no_email": "submitted_at IS NOT NULL AND (email IS NULL OR email='') AND state!='queued'",
    "researched": "submitted_at IS NOT NULL",
    "skipped": "state='skipped'",
    "waiting": "state IN ('kept','queued')",
}


@app.get("/api/pulls/{pid}/people", dependencies=AUTH)
def people(pid: int, view: str = "emails", q: str = "", kind: str = "", offset: int = 0, limit: int = 0,
           sort: str = "id", dir: str = "asc"):
    offset, limit = _page(offset, limit)
    where = ["pull_id=?", PEOPLE_VIEWS.get(view, PEOPLE_VIEWS["emails"])]
    args = [pid]
    if kind:
        where.append("COALESCE(skip_kind,'other')=?")
        args.append(kind)
    if q:
        where.append("(name LIKE ? OR company LIKE ? OR title LIKE ? OR email LIKE ? OR country LIKE ?)")
        args += [f"%{q}%"] * 5
    w = " AND ".join(where)
    order = {"name": "name COLLATE NOCASE", "company": "company COLLATE NOCASE", "title": "title COLLATE NOCASE",
             "email": "email", "country": "country", "id": "id"}.get(sort, "id")
    total = db.q1(f"SELECT COUNT(*) n FROM contacts WHERE {w}", args)["n"]
    rows = db.q(f"SELECT {PEOPLE_COLS} FROM contacts WHERE {w} ORDER BY {order} {'DESC' if dir == 'desc' else 'ASC'}, id "
                f"LIMIT ? OFFSET ?", (*args, limit, offset))
    return {"rows": rows, "total": total}


def _download(data, name, mime):
    return StreamingResponse(iter([data]), media_type=mime,
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/pulls/{pid}/download", dependencies=AUTH)
def download(pid: int, kind: str = "anavmail", format: str = "csv", columns: str = ""):
    p = db.get_pull(pid)
    if not p:
        raise HTTPException(404)
    base = exports.safe_name(p["name"], f"pull-{pid}")
    if kind == "anavmail":
        cols = exports.parse_columns(columns, exports.default_columns(), need_email=True)
        rows, name = exports.anavmail_rows(pid), f"{base}-anavmail"
    elif kind == "no_email":
        cols = exports.parse_columns(columns, exports.NO_EMAIL_COLUMNS)
        rows, name = exports.no_email_rows(pid), f"{base}-no-email"
    else:
        cols = exports.parse_columns(columns, [k for k, _, _ in exports.COLUMNS])
        rows, name = exports.all_rows(pid), f"{base}-everything"
    if format == "xlsx":
        return _download(exports.to_xlsx(rows, cols), name + ".xlsx",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return _download(exports.to_csv(rows, cols), name + ".csv", "text/csv")


# ---------------------------------------------------------------- accounts
@app.get("/api/accounts", dependencies=AUTH)
def list_accounts():
    return _overview()


@app.post("/api/accounts", dependencies=AUTH)
async def add_account(request: Request):
    b = await request.json()
    key = seamless.clean_key(b.get("api_key"))
    if len(key) < 8:
        raise engine.PullError("Paste the Seamless API key (Seamless → Settings → Public API).")
    if db.q1("SELECT id FROM accounts WHERE api_key=?", (key,)):
        raise engine.PullError("That API key is already added.")
    name = (b.get("name") or "").strip()[:60]
    n = db.q1("SELECT COALESCE(MAX(priority),0) m FROM accounts")["m"]
    probe = {"id": None, "name": name or "this account", "api_key": key}
    bal = seamless.test_connection(probe)              # free; a bad key is never saved
    aid = db.x("INSERT INTO accounts(name,api_key,priority,enabled,monthly_budget,source,balance,balance_at,created_at)"
               " VALUES(?,?,?,?,?,?,?,?,?)", (name or f"Account {n + 1}", key, n + 1, 1,
                                              max(0, int(b.get("monthly_budget") or 0)), "ui", bal, time.time(),
                                              time.time()))
    try:
        memory.sync_saved(accounts.get(aid))
    except seamless.SeamlessError:
        pass
    return _overview()


@app.patch("/api/accounts/{aid}", dependencies=AUTH)
async def edit_account(aid: int, request: Request):
    b = await request.json()
    a = accounts.get(aid)
    if not a:
        raise HTTPException(404)
    if "name" in b:
        db.x("UPDATE accounts SET name=? WHERE id=?", ((b["name"] or a["name"]).strip()[:60], aid))
    if "enabled" in b:
        db.x("UPDATE accounts SET enabled=? WHERE id=?", (1 if b["enabled"] else 0, aid))
    if "monthly_budget" in b:
        db.x("UPDATE accounts SET monthly_budget=? WHERE id=?", (max(0, int(b["monthly_budget"] or 0)), aid))
    if b.get("api_key"):
        if a["source"] == "env":
            raise engine.PullError("This key comes from .env (SEAMLESS_API_KEY). Change it there.")
        key = seamless.clean_key(b["api_key"])
        bal = seamless.test_connection({"id": None, "name": a["name"], "api_key": key})
        db.x("UPDATE accounts SET api_key=?, balance=?, balance_at=?, last_error=NULL WHERE id=?",
             (key, bal, time.time(), aid))
    if "priority" in b:
        ids = [r["id"] for r in accounts.all_accounts() if r["id"] != aid]
        ids.insert(max(0, min(int(b["priority"]), len(ids))), aid)
        for i, x in enumerate(ids, 1):
            db.x("UPDATE accounts SET priority=? WHERE id=?", (i, x))
    return _overview()


@app.post("/api/accounts/{aid}/check", dependencies=AUTH)
def check_account(aid: int):
    a = accounts.get(aid)
    if not a:
        raise HTTPException(404)
    seamless.test_connection(a)
    return _overview()


@app.delete("/api/accounts/{aid}", dependencies=AUTH)
def delete_account(aid: int):
    a = accounts.get(aid)
    if not a:
        raise HTTPException(404)
    if a["source"] == "env":
        raise engine.PullError("This key comes from .env (SEAMLESS_API_KEY). Switch it off here or remove it there.")
    if db.q1("SELECT 1 x FROM research_requests WHERE account_id=? AND (resolved_at IS NULL OR gave_up=1)", (aid,)) or \
            db.q1("SELECT 1 x FROM pulls WHERE current_account_id=? AND status IN ('running','queued','paused')", (aid,)):
        raise engine.PullError("A pull is still using this account. Switch it off instead; delete it later.")
    db.x("DELETE FROM accounts WHERE id=?", (aid,))
    return _overview()


# ---------------------------------------------------------------- MillionVerifier (email guessing)
@app.get("/api/verifier", dependencies=AUTH)
def verifier_status():
    return verifier.status()


@app.post("/api/verifier", dependencies=AUTH)
async def verifier_set(request: Request):
    b = await request.json()
    if b.get("api_key"):
        verifier.set_key(b["api_key"])
    if "enabled" in b:
        verifier.set_enabled(bool(b["enabled"]))
    if b.get("check"):
        verifier.refresh_credits()
    return verifier.status()


@app.delete("/api/verifier", dependencies=AUTH)
def verifier_delete():
    verifier.remove_key()
    return verifier.status()


# ---------------------------------------------------------------- do-not-pull
@app.get("/api/exclusions", dependencies=AUTH)
def list_exclusions(q: str = "", offset: int = 0, limit: int = 0):
    offset, limit = _page(offset, limit)
    where, args = ("WHERE value LIKE ?", [f"%{q}%"]) if q else ("", [])
    total = db.q1(f"SELECT COUNT(*) n FROM exclusions {where}", args)["n"]
    rows = db.q(f"SELECT * FROM exclusions {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*args, limit, offset))
    counts = {r["kind"]: r["n"] for r in db.q("SELECT kind, COUNT(*) n FROM exclusions GROUP BY kind")}
    return {"rows": rows, "total": total, "counts": counts}


@app.post("/api/exclusions", dependencies=AUTH)
async def add_exclusions(request: Request):
    text = (await request.json()).get("text") or ""
    added = 0
    for line in text.replace(",", "\n").splitlines():
        if line.strip():
            added += memory.add_exclusion(memory.guess_kind(line), line, "typed")
    return {"added": added, **list_exclusions()}


def _col(header, *names):
    low = [h.strip().lower().replace(" ", "_") for h in header]
    return next((low.index(n) for n in names if n in low), None)


@app.post("/api/exclusions/upload", dependencies=AUTH)
async def upload_exclusions(file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(raw))
    header = next(reader, [])
    ie = _col(header, "email", "email_address", "e-mail", "work_email")
    idom = _col(header, "domain", "website", "company_domain", "company_website")
    ili = _col(header, "linkedin", "linkedin_url", "li_url", "linkedin_profile", "person_linkedin_url")
    ico = _col(header, "company", "company_name", "organization", "account_name")
    if all(i is None for i in (ie, idom, ili, ico)):
        raise engine.PullError("Couldn't find an email, website, company or LinkedIn column in that file.")
    src = (file.filename or "upload")[:80]
    added = 0
    for row in reader:
        get = lambda i: row[i].strip() if i is not None and i < len(row) else ""
        if get(ie):
            added += memory.add_exclusion("email", get(ie), src)
        if get(ili):
            added += memory.add_exclusion("linkedin", get(ili), src)
        if not get(ie) and not get(ili):
            if get(idom):
                added += memory.add_exclusion("domain", get(idom), src)
            elif get(ico):
                added += memory.add_exclusion("company", get(ico), src)
    return {"added": added, **list_exclusions()}


@app.delete("/api/exclusions", dependencies=AUTH)
def delete_exclusion(key: str = "", everything: int = 0):
    if everything:
        db.x("DELETE FROM exclusions")
    elif key:
        db.x("DELETE FROM exclusions WHERE key=?", (key,))
    return list_exclusions()


# ---------------------------------------------------------------- credits history
@app.get("/api/ledger", dependencies=AUTH)
def ledger(offset: int = 0, limit: int = 0, account: int = 0):
    offset, limit = _page(offset, limit)
    where, args = ("WHERE l.account_id=?", [account]) if account else ("", [])
    total = db.q1(f"SELECT COUNT(*) n FROM ledger l {where}", args)["n"]
    rows = db.q(f"""SELECT l.*, p.name pull_name, a.name account_name FROM ledger l LEFT JOIN pulls p ON p.id=l.pull_id
                    LEFT JOIN accounts a ON a.id=l.account_id {where} ORDER BY l.id DESC LIMIT ? OFFSET ?""",
                (*args, limit, offset))
    return {"rows": rows, "total": total}


# ---------------------------------------------------------------- page
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": APP_VERSION}
