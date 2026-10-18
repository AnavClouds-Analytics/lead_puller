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

from . import claude, db, exports, seamless, worker
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
    bal = db.get_setting("credit_balance")
    return {"balance": bal, "balance_at": db.get_setting("credit_balance_at"),
            "monthly_budget": budget, "spent_this_month": spent,
            "budget_left": max(0, budget - spent) if budget else None,
            "low_balance": bool(cfg.LOW_BALANCE_WARNING and bal is not None and bal < cfg.LOW_BALANCE_WARNING),
            "low_balance_level": cfg.LOW_BALANCE_WARNING, "spending_enabled": cfg.SPENDING_ENABLED}


@app.get("/api/meta", dependencies=[Depends(require_login)])
def meta():
    return {"filters": FILTERS, "credits": _credit_summary(), "columns": exports.catalog(),
            "has_seamless_key": bool(seamless.api_key()), "has_claude_key": bool(claude.api_key()),
            "version": APP_VERSION,
            "defaults": {"per_company_cap": cfg.MAX_PER_COMPANY, "company_cooldown": cfg.COMPANY_COOLDOWN_DAYS,
                         "preview_size": cfg.PREVIEW_SIZE, "page_size": cfg.PAGE_SIZE,
                         "batch_size": cfg.DEFAULT_BATCH_SIZE, "columns": exports.default_columns(),
                         "no_email_columns": exports.NO_EMAIL_DEFAULT,
                         "chance_min": cfg.EMAIL_CHANCE_MIN, "test_batch_size": cfg.TEST_BATCH_SIZE,
                         "test_batch_min_rate": cfg.TEST_BATCH_MIN_EMAIL_RATE,
                         "retry_days": cfg.NO_EMAIL_RETRY_DAYS}}


@app.get("/api/settings", dependencies=[Depends(require_login)])
def get_settings():
    """Read-only: every setting lives in .env. Change it there and restart the app."""
    return {"groups": shown_settings(), "credits": _credit_summary(), "version": APP_VERSION}


@app.post("/api/settings/test-seamless", dependencies=[Depends(require_login)])
def test_seamless():
    return seamless.test_connection()


@app.post("/api/settings/test-claude", dependencies=[Depends(require_login)])
def test_claude():
    out = claude.prompt_to_filters("CTOs at software companies in the United States")
    return {"ok": True, "sample": out}


@app.get("/api/ledger", dependencies=[Depends(require_login)])
def ledger(limit: int = 200):
    rows = db.q("""SELECT l.*, p.name AS pull_name FROM ledger l LEFT JOIN pulls p ON p.id=l.pull_id
                   ORDER BY l.id DESC LIMIT ?""", (min(limit, 1000),))
    return {"rows": rows, "credits": _credit_summary()}


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
        "skip_reasons": db.q("""SELECT skip_reason AS reason, COUNT(*) n FROM contacts WHERE pull_id=? AND kept=0
                                GROUP BY skip_reason ORDER BY n DESC""", (pid,)),
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
    return p


@app.get("/api/pulls", dependencies=[Depends(require_login)])
def list_pulls():
    rows = db.q("""SELECT p.id, p.name, p.status, p.target, p.search_credits, p.research_credits, p.created_at,
                   p.finished_at, p.total_matches, p.cloned_from,
                   (SELECT COUNT(*) FROM contacts c WHERE c.pull_id=p.id AND c.kept=1) AS kept,
                   (SELECT COUNT(*) FROM contacts c WHERE c.pull_id=p.id AND c.email IS NOT NULL AND c.email!='') AS with_email
                   FROM pulls p ORDER BY p.id DESC""")
    return {"rows": rows}


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
        out = claude.prompt_to_filters(prompt, pull["filters"])
        db.update_pull(pid, filters=out["filters"], notes=out["notes"], status="draft",
                       prompt=(pull["prompt"] or "") + "\n" + prompt)
        db.x("DELETE FROM contacts WHERE pull_id=? AND submitted_at IS NULL", (pid,))
        return _pull_out(pid)
    out = claude.prompt_to_filters(prompt)
    pid = db.create_pull(out["name"], prompt, db.with_defaults(out["filters"]), out["notes"], target=out["target"])
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


@app.post("/api/pulls/{pid}/collect", dependencies=[Depends(require_login)])
async def collect(pid: int, request: Request):
    body = await request.json()
    worker.start_collect(pid, body.get("target") or 0, body.get("per_company_cap") or 0)
    return _pull_out(pid)


@app.post("/api/pulls/{pid}/research", dependencies=[Depends(require_login)])
async def research(pid: int, request: Request):
    body = await request.json()
    worker.confirm_research(pid, body.get("choice") or "all", body.get("first_n"), body.get("chance_min"))
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
def list_batches(pid: int):
    rows = db.q("SELECT * FROM batches WHERE pull_id=? ORDER BY id DESC", (pid,))
    for r in rows:
        r["countries"] = json.loads(r.pop("countries_json") or "{}")
        r["columns"] = json.loads(r.pop("columns_json") or "[]")
    return {"rows": rows}


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


@app.post("/api/pulls/{pid}/{action}", dependencies=[Depends(require_login)])
def pull_action(pid: int, action: str):
    if action == "pause":
        worker.pause(pid)
    elif action == "resume":
        worker.resume(pid)
    elif action == "cancel":
        worker.cancel(pid)
    elif action == "rerun":
        pull = db.get_pull(pid)
        new_id = db.create_pull(pull["name"] + " (new people)", pull["prompt"], pull["filters"], pull["notes"],
                                target=pull["target"], per_company_cap=pull["per_company_cap"], cloned_from=pid,
                                cooldown=pull["company_cooldown"])
        return _pull_out(new_id)
    elif action == "back-to-filters":
        pull = db.get_pull(pid)
        if pull["status"] in ("previewed",):
            db.update_pull(pid, status="draft")
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
                  department, size, industry, fit, fit_reason, kept, skip_reason, selected, research_status,
                  research_message, submitted_at, email, email_status, email_confidence, phone, phone_type,
                  company_phone, timezone, funding, news, email_chance, retry, batch_id, filled_free"""


@app.get("/api/pulls/{pid}/contacts", dependencies=[Depends(require_login)])
def pull_contacts(pid: int, view: str = "kept", offset: int = 0, limit: int = 100, fit: str = "", q: str = ""):
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
    if fit:
        where.append("COALESCE(fit,'unrated')=?")
        args.append(fit)
    if q:
        where.append("(name LIKE ? OR company LIKE ? OR title LIKE ? OR email LIKE ?)")
        args += [f"%{q}%"] * 4
    w = " AND ".join(where)
    total = db.q1(f"SELECT COUNT(*) n FROM contacts WHERE {w}", args)["n"]
    rows = db.q(f"SELECT {CONTACT_COLS} FROM contacts WHERE {w} ORDER BY id LIMIT ? OFFSET ?",
                (*args, min(limit, 500), max(offset, 0)))
    return {"total": total, "rows": rows}


@app.post("/api/pulls/{pid}/contacts/select", dependencies=[Depends(require_login)])
async def select_contacts(pid: int, request: Request):
    body = await request.json()
    ids = [int(i) for i in body.get("ids") or []]
    sel = 1 if body.get("selected") else 0
    pull = db.get_pull(pid)
    if pull["status"] not in ("collected", "previewed"):
        raise worker.PullError("Selection can only change before research starts.")
    for cid in ids:
        db.x("UPDATE contacts SET selected=? WHERE id=? AND pull_id=? AND kept=1 AND submitted_at IS NULL",
             (sel, cid, pid))
    return _pull_out(pid)


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
def presets():
    rows = db.q("SELECT * FROM presets ORDER BY name")
    for r in rows:
        r["filters"] = json.loads(r.pop("filters_json") or "{}")
    return {"rows": rows}


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
def list_exclusions(q: str = "", offset: int = 0, limit: int = 50):
    where, args = "", []
    if q:
        where, args = "WHERE value LIKE ?", [f"%{q}%"]
    total = db.q1(f"SELECT COUNT(*) n FROM exclusions {where}", args)["n"]
    rows = db.q(f"SELECT * FROM exclusions {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*args, min(limit, 200), max(offset, 0)))
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
def list_companies(q: str = "", status: str = "all", offset: int = 0, limit: int = 50):
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
    rows = db.q(f"""SELECT * FROM companies WHERE {w} ORDER BY COALESCE(last_at, tried_last_at, first_at) DESC
                    LIMIT ? OFFSET ?""", (*args, min(limit, 200), max(offset, 0)))
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
