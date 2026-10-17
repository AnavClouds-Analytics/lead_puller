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

from . import claude, db, seamless, worker
from .filters import FILTERS, clean_filters, search_filter_count

STATIC = os.path.join(os.path.dirname(__file__), "static")
SESSION_DAYS = 30
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


@app.exception_handler(claude.ClaudeError)
async def _claude_err(request, exc):
    return JSONResponse({"error": str(exc)}, status_code=400)


# ---------------------------------------------------------------- auth
def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def _env_users():
    """ADMIN_USERS=email:password,email2:password2 (same format as AnavMail)."""
    out = {}
    for pair in os.environ.get("ADMIN_USERS", "").split(","):
        if ":" in pair:
            email, pw = pair.split(":", 1)
            if email.strip() and pw.strip():
                out[email.strip().lower()] = pw.strip()
    return out


def _known_user(email):
    """With ADMIN_USERS set, only those accounts exist; otherwise the account created on first visit."""
    env = _env_users()
    if env:
        return email in env
    return email in (db.get_setting("users") or {})


def _check_login(email, password):
    email = (email or "").strip().lower()
    env = _env_users()
    if env:
        expected = env.get(email)
        return expected is not None and hmac.compare_digest(password.encode(), expected.encode())
    stored = (db.get_setting("users") or {}).get(email)
    if not stored:
        return False
    salt, h = stored.split("$", 1)
    return hmac.compare_digest(_hash(password, salt), h)


def _needs_setup():
    return not _env_users() and not db.get_setting("users")


def require_login(request: Request):
    """Returns the logged-in email. A session ends when its user is removed from ADMIN_USERS."""
    token = request.cookies.get("lp_session")
    if token:
        row = db.q1("SELECT created_at, email FROM sessions WHERE token=?", (token,))
        if (row and row["email"] and time.time() - row["created_at"] < SESSION_DAYS * 86400
                and _known_user(row["email"])):
            return row["email"]
    raise HTTPException(401, "Please log in.")


def _start_session(response, email):
    token = secrets.token_urlsafe(32)
    db.x("INSERT INTO sessions(token, created_at, email) VALUES(?,?,?)", (token, time.time(), email))
    response.set_cookie("lp_session", token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=os.environ.get("COOKIE_SECURE", "0") == "1")


_login_fails = []


@app.get("/api/auth/status")
def auth_status(request: Request):
    try:
        email = require_login(request)
    except HTTPException:
        email = None
    return {"logged_in": bool(email), "email": email, "needs_setup": _needs_setup()}


@app.post("/api/auth/setup")
async def auth_setup(request: Request, response: Response):
    if not _needs_setup():
        raise HTTPException(400, "An account already exists.")
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = (body.get("password") or "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Enter a valid email address.")
    if len(pw) < 8:
        raise HTTPException(400, "Use at least 8 characters.")
    salt = secrets.token_hex(8)
    db.set_setting("users", {email: f"{salt}${_hash(pw, salt)}"})
    _start_session(response, email)
    return {"ok": True}


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response):
    now = time.time()
    _login_fails[:] = [t for t in _login_fails if now - t < 300]
    if len(_login_fails) >= 10:
        raise HTTPException(429, "Too many wrong passwords. Wait 5 minutes.")
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not _check_login(email, body.get("password") or ""):
        _login_fails.append(now)
        raise HTTPException(401, "Wrong email or password.")
    _start_session(response, email)
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
    budget = db.get_setting("monthly_budget") or 0
    spent = db.month_spend()
    return {"balance": db.get_setting("credit_balance"), "balance_at": db.get_setting("credit_balance_at"),
            "monthly_budget": budget, "spent_this_month": spent,
            "budget_left": max(0, budget - spent) if budget else None}


@app.get("/api/meta")
def meta(user: str = Depends(require_login)):
    return {"user": user, "filters": FILTERS, "credits": _credit_summary(),
            "has_seamless_key": bool(seamless.api_key()), "has_claude_key": bool(claude.api_key()),
            "defaults": {"per_company_cap": db.get_setting("per_company_cap", 3),
                         "preview_cost": 3, "page_size": worker.PAGE_LIMIT}}


@app.get("/api/settings", dependencies=[Depends(require_login)])
def get_settings():
    return {
        "seamless_api_key": _mask(seamless.api_key()),
        "seamless_key_from_env": bool(os.environ.get("SEAMLESS_API_KEY")),
        "anthropic_api_key": _mask(claude.api_key()),
        "anthropic_key_from_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "monthly_budget": db.get_setting("monthly_budget") or 0,
        "per_company_cap": db.get_setting("per_company_cap", 3),
        "fit_scoring": db.get_setting("fit_scoring", True),
        "claude_model": db.get_setting("claude_model") or claude.DEFAULT_MODEL,
        "claude_fast_model": db.get_setting("claude_fast_model") or claude.DEFAULT_FAST_MODEL,
        "credits": _credit_summary(),
    }


@app.post("/api/settings", dependencies=[Depends(require_login)])
async def save_settings(request: Request):
    body = await request.json()
    if body.get("seamless_api_key"):
        key = body["seamless_api_key"].strip()
        seamless.test_connection(key)  # refuse to save a key Seamless rejects
        db.set_setting("seamless_api_key", key)
    if body.get("anthropic_api_key"):
        db.set_setting("anthropic_api_key", body["anthropic_api_key"].strip())
    for k in ("monthly_budget", "per_company_cap"):
        if k in body and body[k] not in (None, ""):
            db.set_setting(k, max(0, int(body[k])))
    if "fit_scoring" in body:
        db.set_setting("fit_scoring", bool(body["fit_scoring"]))
    for k in ("claude_model", "claude_fast_model"):
        if body.get(k):
            db.set_setting(k, body[k].strip())
    return get_settings()


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
        "research_outcomes": db.q("""SELECT research_status AS status, COUNT(*) n FROM contacts
                                     WHERE pull_id=? AND submitted_at IS NOT NULL GROUP BY 1 ORDER BY n DESC""", (pid,)),
    }
    return stats


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
    cap = db.get_setting("per_company_cap", 3)
    pid = db.create_pull(out["name"], prompt, out["filters"], out["notes"], target=out["target"], per_company_cap=cap)
    return _pull_out(pid)


@app.post("/api/pulls", dependencies=[Depends(require_login)])
async def create_pull(request: Request):
    body = await request.json()
    filters = clean_filters(body.get("filters") or {})
    cap = db.get_setting("per_company_cap", 3)
    pid = db.create_pull((body.get("name") or "New pull")[:80], body.get("prompt") or "", filters, [],
                         target=int(body.get("target") or 0), per_company_cap=cap)
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
def preview(pid: int):
    worker.run_preview(pid)
    return _pull_out(pid)


@app.post("/api/pulls/{pid}/collect", dependencies=[Depends(require_login)])
async def collect(pid: int, request: Request):
    body = await request.json()
    worker.start_collect(pid, body.get("target") or 0, body.get("per_company_cap") or 0)
    return _pull_out(pid)


@app.post("/api/pulls/{pid}/research", dependencies=[Depends(require_login)])
async def research(pid: int, request: Request):
    body = await request.json()
    worker.confirm_research(pid, body.get("choice") or "all", body.get("first_n"))
    return _pull_out(pid)


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
                                target=pull["target"], per_company_cap=pull["per_company_cap"], cloned_from=pid)
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
                  research_message, submitted_at, email, email_status, email_confidence, phone, phone_type"""


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
FRIENDLY_STATUS = {
    "done": "Found", "not found": "Not found in Seamless", "missing": "Not found in Seamless",
    "duplicate": "Already researched in your Seamless account before (no new charge)",
    "contact-already-researched": "Already researched in your Seamless account before (no new charge)",
    "error": "Seamless error", "no license or credits available": "No credits / license at the time",
    "timed out": "Seamless took too long", "no answer": "Seamless returned no result",
}


def _export_rows(pid, only_email):
    rows = db.q("""SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL ORDER BY id""", (pid,))
    excl = {r["key"] for r in db.q("SELECT key FROM exclusions WHERE kind IN ('email','domain')")}
    out = []
    for r in rows:
        email = (r["email"] or "").strip()
        if only_email and not email:
            continue
        if email and ("em:" + email.lower() in excl or "domain:" + email.split("@")[-1].lower() in excl):
            continue
        out.append(r)
    return out


def _risky(status):
    s = (status or "").lower()
    return s and s not in ("valid", "verified", "deliverable")


@app.get("/api/pulls/{pid}/export", dependencies=[Depends(require_login)])
def export(pid: int, format: str = "anavmail", include_risky: int = 1):
    pull = db.get_pull(pid)
    if not pull:
        raise HTTPException(404)
    safe = "".join(ch if ch.isalnum() else "-" for ch in pull["name"]).strip("-")[:40] or f"pull-{pid}"
    if format == "anavmail":
        rows = [r for r in _export_rows(pid, True)
                if include_risky or not _risky(r["email_status"])]
        rows = [r for r in rows if (r["email_status"] or "").lower() != "invalid"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["email", "first_name", "last_name", "company", "title", "linkedin"])
        for r in rows:
            w.writerow([r["email"], r["first_name"], r["last_name"], r["company"], r["title"], r["li_url"]])
        return _download(buf.getvalue().encode("utf-8-sig"), f"{safe}-anavmail.csv", "text/csv")
    cols = [("first_name", "First name"), ("last_name", "Last name"), ("email", "Email"),
            ("email_status", "Email status"), ("email_confidence", "Email confidence"),
            ("personal_email", "Personal email"), ("phone", "Phone"), ("phone_type", "Phone type"),
            ("company_phone", "Company phone"), ("title", "Title"), ("seniority", "Seniority"),
            ("department", "Department"), ("company", "Company"), ("domain", "Domain"), ("industry", "Industry"),
            ("size", "Company size"), ("city", "City"), ("state", "State"), ("country", "Country"),
            ("li_url", "LinkedIn"), ("fit", "Fit"), ("fit_reason", "Fit note"), ("research_status", "Result")]
    rows = _export_rows(pid, False)

    def val(r, k):
        if k == "research_status":
            return FRIENDLY_STATUS.get((r[k] or "").lower(), r[k])
        return r[k]

    if format == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        ws = wb.active
        ws.title = "Contacts"
        ws.append([c[1] for c in cols])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append([val(r, c[0]) for c in cols])
        ws.freeze_panes = "A2"
        for i, (k, _) in enumerate(cols, start=1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = 28 if k in ("email", "li_url", "company", "title") else 16
        b = io.BytesIO()
        wb.save(b)
        return _download(b.getvalue(), f"{safe}.xlsx",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[1] for c in cols])
    for r in rows:
        w.writerow([val(r, c[0]) for c in cols])
    return _download(buf.getvalue().encode("utf-8-sig"), f"{safe}-full.csv", "text/csv")


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
    cap = db.get_setting("per_company_cap", 3)
    pid = db.create_pull(p["name"], p["prompt"], json.loads(p["filters_json"]), [], per_company_cap=cap)
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
    return {"added": added, **list_exclusions()}


@app.post("/api/exclusions", dependencies=[Depends(require_login)])
async def add_exclusions(request: Request):
    body = await request.json()
    added = 0
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


# ---------------------------------------------------------------- page
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def healthz():
    return {"ok": True}
