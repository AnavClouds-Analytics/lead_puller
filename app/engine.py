"""The pull engine. One pull runs at a time (others wait in line), in small steps that are saved as they go,
so a restart continues exactly where it was.

Where credits go, and how each is saved:
  search    1 credit per 10 people Seamless shows (including people the tool then skips).
            - pages are sized to what's still needed, never bigger;
            - each search is split by company size, and every slice remembers where it stopped, per account,
              so a re-run never pays to see the same pages again;
            - a fully scanned slice is later asked only for people updated since then (FRESH_RESCAN);
            - a slice where new people have become expensive is left for later (MOVE_ON_*);
            - people found but not needed are kept ("banked") and used first next time, without searching.
  research  1 credit per person sent.
            - email guessing first: where a company's email format is known from a real email, the address is
              built and checked with MillionVerifier; only a "valid" answer is used (no Seamless credit), anything
              else is paid for in Seamless as usual. At a new company one person is paid for first to learn the
              format, the others wait for that answer;
            - nobody already pulled (any account, by email, LinkedIn or name + company), nobody already
              researched in your Seamless accounts, nobody on do-not-pull, no company without a website or
              without an email server, no more than N per company;
            - only as many as are needed to reach the target (at the email rate seen so far);
            - checked again right before paying; never resent automatically after an error;
            - max credits per pull and the monthly budgets are never exceeded.
"""
import hashlib
import json
import math
import re
import threading
import time

from . import accounts, companies as CO, db, dnscheck, filters as F, guess, memory, seamless, verifier
from .config import cfg

NOW = time.time            # tests swap in a fake clock


class PullError(Exception):
    pass


DELIVERABLE = "state='done' AND email IS NOT NULL AND email!='' AND LOWER(COALESCE(email_status,''))!='invalid' AND dup_email=0"
NOT_GUESSED = "COALESCE(email_source,'')!='guessed'"
OPEN_STATES = ("kept", "queued", "done", "unconfirmed", "timed_out", "no_email", "failed")


# ================================================================ creating and controlling pulls
def create_pull(name, prompt, filters, target, max_credits=None, account_choice="auto", max_per_company=None,
                rerun_of=None, company_list=None):
    f = F.clean(filters)
    items = None
    if company_list is not None:
        items = CO.check(company_list)
        if not any(i["status"] == "ready" for i in items):
            raise PullError("None of those companies has a website Lead Puller can search. Add websites (e.g. acme.com).")
        f = CO.people_filters(f)
        if max_per_company in (None, "", 0, "0"):
            max_per_company = cfg.DEFAULT_PER_COMPANY
    elif not F.meaningful(f):
        raise PullError("Pick at least one search filter (for example a job title, country or industry).")
    target = int(target or 0)
    if target < 1:
        raise PullError("Enter how many contacts with email you want.")
    if items is not None:
        target = min(target, sum(1 for i in items if i["status"] == "ready") * max(1, int(max_per_company)))
    if max_credits in (None, "", 0):
        max_credits = math.ceil(target * cfg.DEFAULT_MAX_CREDITS_PER_EMAIL)
    max_credits = int(max_credits)
    if max_credits < 1:
        raise PullError("Max credits must be at least 1.")
    if account_choice not in (None, "", "auto") and not accounts.get(int(account_choice)):
        raise PullError("That Seamless account doesn't exist.")
    if not accounts.ordered_for(account_choice or "auto"):
        raise PullError("No Seamless account is switched on. Add your API key on the Accounts page.")
    cap = cfg.MAX_PER_COMPANY if max_per_company in (None, "") else max(0, int(max_per_company))
    now = NOW()
    default_name = F.summary(f) if items is None else f"{sum(1 for i in items if i['status'] == 'ready')} companies" + (
        f" · {F.summary(f)}" if F.summary(f) != "No filters" else "")
    pid = db.x("""INSERT INTO pulls(name,prompt,filters_json,target,max_credits,max_per_company,account_choice,status,
                  created_at,updated_at,rerun_of,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
               ((name or default_name)[:90], prompt or "", json.dumps(f), target, max_credits, cap,
                str(account_choice or "auto"), "queued", now, now, rerun_of, "companies" if items is not None else "filters"))
    if items is not None:
        CO.save_for_pull(pid, items)
    return pid


def pause(pid):
    p = db.get_pull(pid)
    if p and p["status"] in ("running", "queued"):
        db.update_pull(pid, status="paused", pause_kind="you", error="Paused by you.")


def resume(pid):
    p = db.get_pull(pid)
    if not p or p["status"] != "paused":
        return
    if not cfg.SPENDING_ENABLED:
        raise PullError("Spending is switched off (SPENDING_ENABLED=0 in .env).")
    db.update_pull(pid, status="running" if p["started_at"] else "queued", pause_kind=None, error=None, errors_in_row=0,
                   rate_ack=1 if p["pause_kind"] == "low_rate" else p["rate_ack"])


def stop(pid):
    """Spend nothing more. People already sent for research (already paid for) still come back."""
    p = db.get_pull(pid)
    if not p or p["status"] in ("done", "failed"):
        return
    if p["status"] == "queued" or not p["started_at"]:
        return _finish(p, "Stopped by you before it started.")
    db.update_pull(pid, stopping=1, status="running" if p["status"] == "paused" else p["status"],
                   pause_kind=None, error=None)


def finish_now(pid):
    """Don't wait for slow answers: stop, and finish with what has come back (late answers still fill in free)."""
    stop(pid)
    p = db.get_pull(pid)
    if p["status"] == "done":
        return
    _give_up(pid, "Finished without waiting for Seamless (you pressed Finish now).")
    _finish(db.get_pull(pid), "Stopped by you. Answers still on their way are filled in free if they arrive.")


def stop_all():
    n = 0
    for r in db.q("SELECT id FROM pulls WHERE status IN ('running','queued','paused')"):
        stop(r["id"])
        n += 1
    return n


# ================================================================ helpers
def _hash(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:20]


def _ceil10(n):
    return int(math.ceil(max(1, n) / 10.0) * 10)


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def counts(pid):
    one = lambda sql, a=(): db.q1(sql, (pid, *a))["n"]
    return {
        "emails": one(f"SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND {DELIVERABLE}"),
        "pending": one("SELECT COUNT(*) n FROM research_requests WHERE pull_id=? AND resolved_at IS NULL"),
        "kept": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND state='kept'"),
        "researched": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL"),
        "seen": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND state!='banked_in'"),
        "skipped": one("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND state='skipped'"),
    }


def hit_rate(pid):
    """Share of people researched who come back with a usable email: this pull, else your history, else .env."""
    r = db.q1(f"""SELECT COUNT(*) n, SUM(CASE WHEN {DELIVERABLE} THEN 1 ELSE 0 END) e FROM contacts
                  WHERE pull_id=? AND state IN ('done','no_email','failed') AND filled_free=0 AND {NOT_GUESSED}""", (pid,))
    if r["n"] >= 20:
        return min(1.0, max(0.05, (r["e"] or 0) / r["n"]))
    r = db.q1(f"""SELECT COUNT(*) n, SUM(CASE WHEN {DELIVERABLE} THEN 1 ELSE 0 END) e FROM
                  (SELECT * FROM contacts WHERE state IN ('done','no_email','failed') AND filled_free=0 AND {NOT_GUESSED}
                   ORDER BY id DESC LIMIT 2000)""")
    if r["n"] >= 50:
        return min(1.0, max(0.05, (r["e"] or 0) / r["n"]))
    return min(1.0, max(0.05, cfg.EXPECTED_EMAIL_RATE / 100.0))


def credits_left(p):
    return p["max_credits"] - db.pull_spend(p["id"])


def _room(p, acct):
    u = accounts.usable(acct)
    left = credits_left(p)
    return left if u is None else min(u, left)


def _pick_account(p, need):
    """The pull's current account if it can still spend `need`, else the next one in your order that can."""
    order = accounts.ordered_for(p["account_choice"])
    cur = next((a for a in order if a["id"] == p["current_account_id"]), None)
    if cur:
        order = [cur] + [a for a in order if a["id"] != cur["id"]]
    for a in order:
        u = accounts.usable(a)
        if u is None or u >= need:
            return a
    return None


# ================================================================ slices
def _plan_companies(p):
    """Company lists: one search per batch of up to COMPANY_BATCH websites (Seamless allows 100)."""
    doms = CO.ready_domains(p["id"])
    n = max(1, min(100, cfg.COMPANY_BATCH))
    base = F.search_body(p["filters"])
    with db.tx() as c:
        for i in range(0, len(doms), n):
            chunk = doms[i:i + n]
            body = dict(base, companyDomain=chunk)
            c.execute("INSERT INTO pull_slices(pull_id,idx,label,size_value,skey) VALUES(?,?,?,?,?)",
                      (p["id"], i // n * 10, f"Companies {i + 1}–{i + len(chunk)}", json.dumps({"domains": chunk}),
                       _hash(body)))


def _slice_domains(sl):
    try:
        v = json.loads(sl["size_value"] or "null")
    except ValueError:
        return None
    return v.get("domains") if isinstance(v, dict) else None


def _plan(p):
    """Split the search into company-size slices (each remembered separately, per account)."""
    if p.get("mode") == "companies":
        return _plan_companies(p)
    f = p["filters"]
    base = F.search_body(f)
    base.pop("companySize", None)
    plan = []
    if cfg.SLICE_BY_COMPANY_SIZE:
        sizes, chosen = F.size_slices(f)
        plan = [(s, s) for s in sizes]
        if not chosen:
            plan.append(("Company size not on record", None))
    else:
        inc = F.search_body(f).get("companySize")
        plan = [("Whole search", json.dumps(inc) if inc else None)]
    rows = []
    for label, size in plan:
        body = dict(base)
        if size is not None:
            body["companySize"] = json.loads(size) if size.startswith("[") else [size]
        elif cfg.SLICE_BY_COMPANY_SIZE:
            body["_slice"] = "rest"
        skey = _hash(body)
        hist = db.q("SELECT * FROM scan_state WHERE skey=?", (skey,))
        live = any(h["next_token"] for h in hist)
        done = any(h["exhausted_at"] for h in hist)
        rows_seen = sum(h["rows"] for h in hist)
        kept = sum(h["kept"] for h in hist)
        rate = kept / rows_seen if rows_seen else 1.0
        group = 0 if live else (1 if not done else 2)
        if size is None and cfg.SLICE_BY_COMPANY_SIZE:
            group = 9                                  # the catch-all goes last
        rows.append((group, -rate, label, size, skey))
    rows.sort(key=lambda r: (r[0], r[1]))
    with db.tx() as c:
        for i, (_, _, label, size, skey) in enumerate(rows):
            c.execute("INSERT INTO pull_slices(pull_id,idx,label,size_value,skey) VALUES(?,?,?,?,?)",
                      (p["id"], i, label, size, skey))


def _slice_body(p, sl):
    body = F.search_body(p["filters"])
    doms = _slice_domains(sl)
    if doms is not None:
        body["companyDomain"] = doms
        return body
    body.pop("companySize", None)
    if sl["size_value"] is not None:
        sv = sl["size_value"]
        body["companySize"] = json.loads(sv) if sv.startswith("[") else [sv]
    elif cfg.SLICE_BY_COMPANY_SIZE:
        # The catch-all: no size filter at all (people with no size on record). Sizes already covered by the
        # other slices show up again here, are skipped (already pulled) and cost 0.1 each to see, so this
        # slice only runs last and is watched by the same move-on rule.
        pass
    return body


def _current_slice(pid):
    return db.q1("SELECT * FROM pull_slices WHERE pull_id=? AND state IN ('todo','scanning') ORDER BY idx LIMIT 1",
                 (pid,))


# ================================================================ the step
_errors = {}


def tick():
    """One small step: poll answers for every pull that has some (free), then advance the one running pull."""
    for r in db.q("SELECT DISTINCT pull_id FROM research_requests WHERE resolved_at IS NULL"):
        p = db.get_pull(r["pull_id"])
        if p and p["status"] in ("running", "paused"):
            try:
                _poll(p)
            except seamless.SeamlessError:
                pass
    poll_late()
    running = db.q1("SELECT id FROM pulls WHERE status='running' ORDER BY id LIMIT 1")
    if not running:
        nxt = db.q1("SELECT id FROM pulls WHERE status='queued' ORDER BY id LIMIT 1")
        if not nxt:
            return False
        running = nxt
        db.update_pull(nxt["id"], status="running")
    p = db.get_pull(running["id"])
    try:
        step(p)
        if p["errors_in_row"]:
            db.update_pull(p["id"], errors_in_row=0)
    except seamless.SeamlessError as e:
        n = (db.get_pull(p["id"])["errors_in_row"] or 0) + 1
        db.update_pull(p["id"], errors_in_row=n, error=f"Seamless: {e}")
        if e.code in ("badKey", "missingLicense") or n >= cfg.ERROR_PAUSE_AFTER:
            db.update_pull(p["id"], status="paused", pause_kind="errors", error=f"Paused: {e}")
    except PullError as e:
        db.update_pull(p["id"], status="paused", pause_kind="error", error=f"Paused: {e}")
    return True


def step(p):
    pid = p["id"]
    if p["status"] != "running":
        return
    if not p["started_at"]:
        _start(p)
        p = db.get_pull(pid)
    _poll(p)
    p = db.get_pull(pid)
    _free_fill_unconfirmed(p)
    st = counts(pid)
    hit = hit_rate(pid)
    expected = st["emails"] + st["pending"] * hit
    if st["emails"] >= p["target"]:
        if st["pending"]:
            return _wait(p, st)
        return _finish(p, f"Reached your target: {st['emails']:,} emails.")
    if p["stopping"]:
        if st["pending"]:
            return _wait(p, st)
        return _finish(p, "Stopped by you.")
    if expected >= p["target"]:
        return _wait(p, st)                       # enough is on its way: don't pay for more yet
    if not cfg.SPENDING_ENABLED:
        db.update_pull(pid, status="paused", pause_kind="spending",
                       error="Paused: spending is switched off (SPENDING_ENABLED=0 in .env).")
        return
    if credits_left(p) < 1:
        if st["pending"]:
            return _wait(p, st)
        return _finish(p, f"Reached your max of {p['max_credits']:,} credits with {st['emails']:,} emails.")
    if cfg.PAUSE_BELOW_EMAIL_RATE and not p["rate_ack"] and st["researched"]:
        # The first batch is a small test: check the email rate before spending more on research.
        if st["pending"]:
            return _wait(p, st)
        r = db.q1(f"""SELECT COUNT(*) n, SUM(CASE WHEN {DELIVERABLE} THEN 1 ELSE 0 END) e FROM contacts
                      WHERE pull_id=? AND state IN ('done','no_email','failed') AND filled_free=0 AND {NOT_GUESSED}""",
                    (pid,))
        db.update_pull(pid, rate_ack=1)
        if r["n"] >= 10 and 100.0 * (r["e"] or 0) / r["n"] < cfg.PAUSE_BELOW_EMAIL_RATE:
            db.update_pull(pid, status="paused", pause_kind="low_rate", error=(
                f"Paused: only {r['e'] or 0} of the first {r['n']} people researched had an email "
                f"({100.0 * (r['e'] or 0) / r['n']:.0f}%, below PAUSE_BELOW_EMAIL_RATE={cfg.PAUSE_BELOW_EMAIL_RATE}%). "
                f"Each person researched costs 1 credit whether or not an email is found. Resume to carry on anyway, "
                f"or Stop and change the filters (fresher records, other countries or sizes)."))
            return
    need_people = max(1, math.ceil((p["target"] - expected) / hit))
    batch = min(cfg.RESEARCH_BATCH, need_people)
    if not st["researched"] and cfg.FIRST_BATCH:
        batch = min(batch, cfg.FIRST_BATCH)
    sl = _current_slice(pid)
    if st["kept"] >= batch or (st["kept"] and not sl):
        if _research(p, min(batch, st["kept"])) or not sl:
            return
    if sl:
        return _search(p, sl, need_people - st["kept"], hit)
    if st["pending"]:
        return _wait(p, st)
    return _finish(p, f"Seamless has no more new people for these filters: you have {st['emails']:,} of "
                      f"{p['target']:,}. Widen the filters (more titles, countries or sizes) to get more.")


def _start(p):
    pid = p["id"]
    _plan(p)
    guess.backfill()                                     # free: email formats from everything you already have
    if verifier.key() and cfg.EMAIL_GUESSING:
        try:
            verifier.refresh_credits()                   # free
        except verifier.VerifierError:
            pass
    balances = {}
    for a in accounts.ordered_for(p["account_choice"]):
        try:
            bal = seamless.test_connection(a)            # free: fresh balance before spending anything
            memory.sync_saved(accounts.get(a["id"]))     # free: people already researched in Seamless
        except seamless.SeamlessError as e:
            db.x("UPDATE accounts SET last_error=? WHERE id=?", (str(e), a["id"]))
            continue
        balances[str(a["id"])] = {"name": a["name"], "before": bal, "after": None}
    db.update_pull(pid, started_at=NOW(), balances=balances)


def _wait(p, st):
    """Only answers are outstanding. Give up on slow ones after RESEARCH_WAIT_MINUTES (late answers still count)."""
    oldest = db.q1("SELECT MIN(created_at) t FROM research_requests WHERE pull_id=? AND resolved_at IS NULL",
                   (p["id"],))["t"]
    if oldest and NOW() - oldest > cfg.RESEARCH_WAIT_MINUTES * 60:
        _give_up(p["id"], f"Seamless took more than {cfg.RESEARCH_WAIT_MINUTES} minutes to answer.")


def _finish(p, reason):
    pid = p["id"]
    now = NOW()
    _settle_unmatched(pid)
    left = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND state='kept'", (pid,))["n"]
    # Found but not needed: keep them for the next pull with the same filters (no search needed then).
    db.x("UPDATE contacts SET state='banked', banked_at=? WHERE pull_id=? AND state='kept'", (now, pid))
    bal = dict(p.get("balances") or {})
    for aid, b in bal.items():
        a = accounts.get(int(aid))
        if a:
            b["after"] = a["balance"]
    db.update_pull(pid, status="done", finished_at=now, stop_reason=reason + (
        f" {left:,} people found but not needed are kept for your next pull with these filters." if left else ""),
        balances=bal, stopping=0, error=None, pause_kind=None)
    db.x("UPDATE pull_slices SET state=CASE WHEN state IN ('todo','scanning') THEN 'left' ELSE state END WHERE pull_id=?",
         (pid,))


def _settle_unmatched(pid):
    """People paid for whose answer couldn't be matched safely, or never came: free lookup in saved contacts."""
    rows = db.q("""SELECT c.* FROM contacts c WHERE c.pull_id=? AND (c.state='timed_out' OR (c.state='queued' AND NOT
                   EXISTS (SELECT 1 FROM research_requests r WHERE r.contact_id=c.id AND r.resolved_at IS NULL)))""",
                (pid,))
    if not rows:
        return
    for aid in {r["account_id"] for r in rows}:
        acct = accounts.get(aid)
        if acct:
            try:
                memory.sync_saved(acct, force=True)
            except seamless.SeamlessError:
                pass
    for c in rows:
        rec = memory.saved_match(c["account_id"], c["name"], c["company"], c["li_url"])
        if rec:
            _apply_contact(c, rec, "done", "Filled in free from your Seamless saved contacts.", free=True)
        elif c["state"] == "queued":
            db.x("UPDATE contacts SET state='no_email', research_message=? WHERE id=?",
                 ("Seamless's answer couldn't be matched to this person safely, and no saved contact was found.",
                  c["id"]))


# ================================================================ searching
def _keep_rate(pid, sl, scan):
    if sl["rows"] >= 20:
        return max(0.05, sl["kept"] / sl["rows"])
    if scan and scan["rows"] >= 20:
        return max(0.05, scan["kept"] / scan["rows"])
    r = db.q1("SELECT COUNT(*) n, SUM(CASE WHEN state!='skipped' THEN 1 ELSE 0 END) k FROM contacts WHERE pull_id=?",
              (pid,))
    if r["n"] >= 20:
        return max(0.05, (r["k"] or 0) / r["n"])
    return 0.6


def _claim_bank(p, acct, sl):
    """People an earlier pull found (same filters, same account) but didn't need: use them first, free."""
    since = NOW() - cfg.BANK_DAYS * 86400
    rows = db.q("""SELECT * FROM contacts WHERE state='banked' AND account_id=? AND skey=? AND banked_at>=?
                   ORDER BY id LIMIT 500""", (acct["id"], sl["skey"], since))
    if not rows:
        return 0
    block = memory.Blocklist()
    taken = 0
    for c in rows:
        reason = _recheck(c, block)
        if reason:
            db.x("UPDATE contacts SET state='skipped', skip_reason=?, skip_kind='recheck', pull_id=? WHERE id=?",
                 (reason, p["id"], c["id"]))
            continue
        cap_reason = _company_cap(p, c["company"], c["domain"])
        if cap_reason:
            continue                                    # stays in the bank for another pull
        db.x("UPDATE contacts SET pull_id=?, slice_id=?, state='kept', banked_at=NULL WHERE id=?",
             (p["id"], sl["id"], c["id"]))
        taken += 1
    if taken:
        db.x("UPDATE pull_slices SET note=? WHERE id=?",
             (f"{taken} found earlier used first (no search credits)", sl["id"]))
    return taken


def _search(p, sl, want_people, hit):
    pid = p["id"]
    acct = _pick_account(p, 1)
    if not acct:
        db.update_pull(pid, status="paused", pause_kind="account",
                       error="Paused: no Seamless account has credits to spare (balance above ACCOUNT_RESERVE and "
                             "within its monthly budget). Top up, raise the budget in .env, or add another account.")
        return
    if acct["id"] != p["current_account_id"]:
        db.update_pull(pid, current_account_id=acct["id"])
    if sl["state"] == "todo":
        db.x("UPDATE pull_slices SET state='scanning' WHERE id=?", (sl["id"],))
    # People found earlier (same filters, this account) but not needed: use them first, in every slice.
    taken = 0
    for other in db.q("SELECT * FROM pull_slices WHERE pull_id=? ORDER BY idx", (pid,)):
        taken += _claim_bank(p, acct, other)
    if taken:
        return
    room = _room(p, acct)
    if room is not None and room < 1:
        return
    body = _slice_body(p, sl)
    scan = db.q1("SELECT * FROM scan_state WHERE account_id=? AND skey=?", (acct["id"], sl["skey"]))
    fresh_note = None
    if cfg.FRESH_RESCAN and scan and scan["exhausted_at"] and not scan["next_token"]:
        since = _iso(scan["exhausted_at"] - 86400)
        if since > body.get("lastModifiedAfter", ""):
            body["lastModifiedAfter"] = since
            fresh_note = f"only people updated since {time.strftime('%d %b', time.gmtime(scan['exhausted_at']))}"
    body_hash = _hash(body)
    token = scan["next_token"] if scan and scan["body_hash"] == body_hash else None
    if not scan:
        db.x("""INSERT INTO scan_state(account_id,skey,body_hash,started_at,updated_at) VALUES(?,?,?,?,?)""",
             (acct["id"], sl["skey"], body_hash, NOW(), NOW()))
    elif scan["body_hash"] != body_hash:
        db.x("UPDATE scan_state SET body_hash=?, next_token=NULL, started_at=?, updated_at=? WHERE account_id=? AND skey=?",
             (body_hash, NOW(), NOW(), acct["id"], sl["skey"]))
    scan = db.q1("SELECT * FROM scan_state WHERE account_id=? AND skey=?", (acct["id"], sl["skey"]))
    keep = _keep_rate(pid, sl, scan)
    limit = min(cfg.PAGE_SIZE, max(10, _ceil10(want_people / keep * 1.15)))
    if room is not None:
        limit = min(limit, room * 10)
    limit = max(1, int(limit))
    try:
        rows, sup, credits = seamless.search_contacts(body, limit, token, acct, pid,
                                                      note=f"search: {sl['label']}")
    except seamless.SeamlessError as e:
        if e.code == "bad_request" and token:
            # Seamless no longer accepts the saved position: start this slice from the top (memory skips repeats).
            db.x("UPDATE scan_state SET next_token=NULL, body_hash=? WHERE account_id=? AND skey=?",
                 ("restart", acct["id"], sl["skey"]))
            db.x("UPDATE pull_slices SET note=? WHERE id=?", ("saved position expired; started from the top", sl["id"]))
            return
        if e.code == "insufficientCredits":
            seamless.test_connection(acct)           # refresh the balance; the next step picks another account
            return
        raise
    kept = _store_rows(p, acct, sl, rows)
    more = bool(sup.get("isMore") or sup.get("nextToken")) and bool(rows)
    nxt = sup.get("nextToken") if more else None
    now = NOW()
    db.x("""UPDATE scan_state SET next_token=?, exhausted_at=CASE WHEN ? THEN exhausted_at ELSE ? END,
            pages=pages+1, rows=rows+?, kept=kept+?, updated_at=? WHERE account_id=? AND skey=?""",
         (nxt, 1 if more else 0, now, len(rows), kept, now, acct["id"], sl["skey"]))
    recent = json.loads(sl["recent_json"] or "[]")[-(cfg.MOVE_ON_PAGES - 1):] if cfg.MOVE_ON_PAGES > 1 else []
    recent.append({"rows": len(rows), "kept": kept, "credits": credits})
    state, note = "scanning", fresh_note
    total = sup.get("total")
    if not more:
        state = "done"
        note = "scanned to the end" + (f" ({fresh_note})" if fresh_note else "")
    elif cfg.MOVE_ON_PAGES and len(recent) >= cfg.MOVE_ON_PAGES:
        k = sum(r["kept"] for r in recent)
        cr = sum(r["credits"] for r in recent)
        cost = cr / (k * hit) if k else float("inf")
        if cost > cfg.MOVE_ON_SEARCH_COST:
            state = "expensive"
            note = (f"left for later: the last {len(recent)} pages kept {k} of {sum(r['rows'] for r in recent)} "
                    f"(mostly people you already have)")
    doms = _slice_domains(sl)
    new_slice = None
    if doms is not None and state == "scanning":
        cap = p["max_per_company"] or 0
        full = [d for d in doms if cap and _listed_count(pid, d) >= cap]
        rest = [d for d in doms if d not in full]
        if full and not rest:
            state, note = "done", "every company in this batch has enough people"
        elif full and 100.0 * len(full) / len(doms) >= cfg.RESLICE_FULL_SHARE and _slice_gen(sl) < 3:
            state = "done"
            others = "the other one is" if len(rest) == 1 else f"the other {len(rest)} are"
            note = f"{len(full)} of {len(doms)} companies have enough people; {others} searched again on their own"
            new_slice = rest
    db.x("""UPDATE pull_slices SET state=?, pages=pages+1, rows=rows+?, kept=kept+?, search_credits=search_credits+?,
            recent_json=?, note=COALESCE(?, note), total=COALESCE(?, total) WHERE id=?""",
         (state, len(rows), kept, credits, json.dumps(recent), note, total, sl["id"]))
    if new_slice:
        body2 = dict(F.search_body(p["filters"]), companyDomain=new_slice)
        db.x("INSERT INTO pull_slices(pull_id,idx,label,size_value,skey,note) VALUES(?,?,?,?,?,?)",
             (pid, sl["idx"] + 1, f"{sl['label'].split(' · ')[0]} · {len(new_slice)} still to fill",
              json.dumps({"domains": new_slice, "gen": _slice_gen(sl) + 1}), _hash(body2),
              "searched again without the companies that already have enough people"))


def _slice_gen(sl):
    try:
        return int((json.loads(sl["size_value"] or "{}") or {}).get("gen", 0))
    except (ValueError, AttributeError, TypeError):
        return 0


def _listed_count(pid, domain):
    return db.q1("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND state IN ('kept','queued','done','unconfirmed',
                    'timed_out','no_email','failed') AND (domain=? OR domain LIKE ?)""", (pid, domain, "%." + domain))["n"]


def _row_email(r):
    for k in ("email", "email1", "workEmail", "contactEmail", "businessEmail"):
        e = db.norm_email(r.get(k))
        if e:
            return e
    return None


def _parse_row(r):
    name = (r.get("name") or r.get("fullName") or f"{r.get('firstName') or ''} {r.get('lastName') or ''}").strip()
    first, last = r.get("firstName"), r.get("lastName")
    if not first and name:
        bits = name.split()
        first, last = bits[0], " ".join(bits[1:])
    ind = r.get("industries") or r.get("industry") or ""
    if isinstance(ind, list):
        ind = ", ".join(str(i) for i in ind)
    return {"sid": r.get("searchResultId"), "name": name, "first_name": first, "last_name": last,
            "title": r.get("title") or r.get("jobTitle") or "", "seniority": r.get("seniority") or "",
            "department": r.get("department") or "", "company": r.get("company") or r.get("companyName") or "",
            "domain": db.norm_domain(r.get("domain") or r.get("companyDomain") or ""),
            "li_url": r.get("liUrl") or r.get("linkedInProfileUrl") or r.get("lIProfileUrl") or "",
            "country": r.get("country") or "", "region": r.get("state") or "", "city": r.get("city") or "",
            "industry": ind, "size": r.get("employeeSizeRange") or r.get("companySize") or "",
            "row_email": _row_email(r)}


def _has_words(text, word):
    t = " " + db.norm(text) + " "
    w = db.norm(word)
    return bool(w) and (" " + w + " ") in t


def _tool_exclude(c, rules):
    for key, field, vals, label in rules:
        v = c.get({"industries": "industry"}.get(field, field)) or ""
        if field in ("title", "company"):
            hit = next((x for x in vals if _has_words(v, x)), None)
        elif field == "domain":
            d = db.norm_domain(v)
            hit = next((x for x in vals if d and (d == db.norm_domain(x) or d.endswith("." + db.norm_domain(x)))), None)
        elif field == "industries":
            have = {db.norm(i) for i in v.split(",")}
            hit = next((x for x in vals if db.norm(x) in have), None)
        else:
            hit = next((x for x in vals if db.norm(x) == db.norm(v)), None)
        if hit:
            return f"Excluded {label.lower()}: {hit}"
    return None


def _company_key(company, domain):
    d = db.norm_domain(domain)
    return "d:" + d if d else ("n:" + db.norm_company(company) if db.norm_company(company) else None)


def _company_cap(p, company, domain):
    cap = p["max_per_company"] or 0
    if not cap:
        return None
    ck = _company_key(company, domain)
    if not ck:
        return None
    if ck.startswith("d:"):
        n = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND state IN ('kept','queued','done','unconfirmed',"
                  "'timed_out','no_email','failed') AND domain=?", (p["id"], ck[2:]))["n"]
    else:
        n = sum(1 for r in db.q("SELECT company FROM contacts WHERE pull_id=? AND state IN ('kept','queued','done',"
                                "'unconfirmed','timed_out','no_email','failed') AND (domain IS NULL OR domain='')",
                                (p["id"],)) if db.norm_company(r["company"]) == ck[2:])
    return f"More than {cap} at this company" if n >= cap else None


def _recheck(c, block):
    """Right before paying (and when using people found earlier): already pulled? on do-not-pull?"""
    keys = db.person_keys(c["name"], c["company"], c["li_url"], c.get("row_email"))
    hit = memory.find(keys)
    if hit:
        what, how = memory.why_seen(hit)
        return f"{what.capitalize()} ({how})"
    return block.reason(c["name"], c["company"], c["domain"], c["li_url"], c.get("row_email"))


def _store_rows(p, acct, sl, rows):
    """Check every person on the page and keep only the ones worth paying for. Returns how many were kept."""
    pid = p["id"]
    f = p["filters"]
    rules = F.tool_excludes(f)
    block = memory.Blocklist()
    kept = 0
    now = NOW()
    page_keys = _pull_keys(pid)
    listed_mode = p.get("mode") == "companies"
    parsed = [_parse_row(r) for r in rows]
    if listed_mode:
        parsed.sort(key=lambda c: guess.seniority_rank(c["seniority"], c["title"]))
    for c in parsed:
        if not c["sid"]:
            continue
        keys = db.person_keys(c["name"], c["company"], c["li_url"], c["row_email"])
        reason = kind = None
        if keys and page_keys & set(keys):
            reason, kind = "Same person twice in Seamless's results", "dup"
        listed_dom = CO.listed(pid, c["domain"]) if listed_mode else None
        if not reason and listed_mode and not listed_dom:
            reason, kind = "Not one of your companies", "not_listed"
        if not reason:
            hit = memory.find(keys)
            if hit:
                what, how = memory.why_seen(hit)
                reason = f"{what.capitalize()} ({how})"
                kind = "seamless" if hit["source"] == "seamless" else ("mem_email" if hit["key"].startswith("em:")
                                                                     else "mem_other")
        if not reason:
            reason = block.reason(c["name"], c["company"], c["domain"], c["li_url"], c["row_email"])
            kind = "dnp" if reason else None
        if not reason:
            reason = _tool_exclude(c, rules)
            kind = "exclude" if reason else None
        if not reason and f.get("requireLinkedIn") and not db.norm_li(c["li_url"]):
            reason, kind = "No LinkedIn profile", "no_li"
        if not reason and cfg.REQUIRE_COMPANY_WEBSITE and not c["domain"]:
            reason, kind = "No company website", "no_site"
        if not reason:
            if listed_dom:
                cap = p["max_per_company"] or 0
                reason = f"More than {cap} at this company" if cap and _listed_count(pid, listed_dom) >= cap else None
            else:
                reason = _company_cap(p, c["company"], c["domain"])
            kind = "cap" if reason else None
        if not reason and cfg.SKIP_NO_EMAIL_SERVER and c["domain"] and dnscheck.has_mail_server(c["domain"]) == "no":
            reason, kind = "Company domain can't receive email", "no_mx"
        page_keys.update(keys)
        if c["row_email"] and c["domain"]:
            guess.learn(c["first_name"], c["last_name"], c["row_email"], c["domain"], "search")   # free
        state = "skipped" if reason else "kept"
        kept += 0 if reason else 1
        db.x("""INSERT INTO contacts(pull_id,account_id,slice_id,skey,sid,state,skip_reason,skip_kind,name,first_name,
                last_name,title,seniority,department,company,domain,li_url,country,region,city,industry,size,row_email,
                created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (pid, acct["id"], sl["id"], sl["skey"], c["sid"], state, reason, kind, c["name"], c["first_name"],
              c["last_name"], c["title"], c["seniority"], c["department"], c["company"], c["domain"], c["li_url"],
              c["country"], c["region"], c["city"], c["industry"], c["size"], c["row_email"], now))
    return kept


def _pull_keys(pid):
    """Every memory key of the people already in this pull (found on earlier pages), loaded once per page."""
    keys = set()
    for r in db.q("SELECT name, company, li_url, row_email, email FROM contacts WHERE pull_id=? AND state!='skipped'",
                  (pid,)):
        keys.update(db.person_keys(r["name"], r["company"], r["li_url"], r["row_email"] or r["email"]))
    return keys


# ================================================================ researching
def _research(p, n):
    """Get emails for up to n kept people: a verified guess where the company's format is known (free in Seamless),
    otherwise Seamless research (1 credit). Returns how many people were handled (0 = nothing could move yet)."""
    pid = p["id"]
    by_acct = db.q("SELECT account_id, COUNT(*) n FROM contacts WHERE pull_id=? AND state='kept' GROUP BY account_id",
                   (pid,))
    order = [a["id"] for a in accounts.ordered_for(p["account_choice"])]
    by_acct.sort(key=lambda r: (r["account_id"] != p["current_account_id"],
                                order.index(r["account_id"]) if r["account_id"] in order else 99))
    for grp in by_acct:
        acct = accounts.get(grp["account_id"])
        room = _room(p, acct) if acct and acct["enabled"] else 0
        if room is None:
            room = n
        if credits_left(p) < 1:
            return 0
        if room < 1:
            # This account can't pay: keep these people for later (their search IDs belong to this account).
            db.x("UPDATE contacts SET state='banked', banked_at=? WHERE pull_id=? AND state='kept' AND account_id=?",
                 (NOW(), pid, grp["account_id"]))
            continue
        k = min(n, grp["n"])
        cands = db.q("SELECT * FROM contacts WHERE pull_id=? AND state='kept' AND account_id=? ORDER BY id LIMIT ?",
                     (pid, acct["id"], max(k * 3, 60)))
        cands.sort(key=lambda c: (guess.seniority_rank(c["seniority"], c["title"]), c["id"]))
        guessing, _why = verifier.ready()
        inflight = _domains_in_flight(pid) if guessing else set()
        block = memory.Blocklist()
        to_pay, done, checks, learners = [], 0, 0, set()
        for c in cands:
            if done + len(to_pay) >= k or len(to_pay) >= room:
                break
            reason = _recheck(c, block)
            if reason:
                db.x("UPDATE contacts SET state='skipped', skip_reason=?, skip_kind='recheck' WHERE id=?",
                     (reason, c["id"]))
                continue
            d = db.norm_domain(c["domain"])
            if not guessing or c["guess_done"] or not d:
                to_pay.append(c)
                continue
            addrs, why_not = guess.plan(c, _namesakes(pid, c))
            if addrs:
                if checks >= max(1, cfg.GUESS_PER_STEP):
                    continue                              # checked next step, so Pause/Stop answer quickly
                checks += len(addrs)
                email, note = guess.try_person(c, addrs, pid)
                if email and _recheck(c, memory.Blocklist()) is None:
                    _apply_guess(c, email, note)
                    done += 1
                    continue
                db.x("UPDATE contacts SET guess_done=1, guess_note=? WHERE id=?", (note, c["id"]))
                to_pay.append(c)
                guessing, _why = verifier.ready()        # e.g. MillionVerifier just ran out: stop guessing
            elif why_not == "Company's email format not known yet.":
                if d in inflight or d in learners:
                    continue                              # wait for the person already paid for at this company
                learners.add(d)
                db.x("UPDATE contacts SET guess_note=? WHERE id=?",
                     ("Paid for in Seamless to learn this company's email format.", c["id"]))
                to_pay.append(c)
            else:
                db.x("UPDATE contacts SET guess_done=1, guess_note=? WHERE id=?", (why_not, c["id"]))
                to_pay.append(c)
        if to_pay:
            _send(p, acct, to_pay[:max(0, min(k - done, room))])
        return done + len(to_pay)
    return 0


def _domains_in_flight(pid):
    """Companies where someone in this pull is paid for and their answer (with the email format) is on its way."""
    if not db.q1("SELECT 1 x FROM research_requests WHERE pull_id=? AND resolved_at IS NULL LIMIT 1", (pid,)):
        return set()                     # nothing is on its way, so nobody should wait
    return {db.norm_domain(r["domain"]) for r in db.q(
        """SELECT DISTINCT c.domain FROM contacts c WHERE c.pull_id=? AND c.state='queued' AND c.domain IS NOT NULL
           AND c.domain!='' AND (c.request_id IS NULL OR EXISTS (SELECT 1 FROM research_requests r WHERE
           r.request_id=c.request_id AND r.resolved_at IS NULL))""", (pid,))}


def _namesakes(pid, c):
    """Is there evidence of a different person with the same name at this company? (Same name, different LinkedIn,
    anywhere in your results, or a known email of someone with this name there.) Then a guess could reach the wrong
    person, so it isn't made."""
    parts = guess.name_parts(c["first_name"], c["last_name"])
    if not parts:
        return False
    d = db.norm_domain(c["domain"])
    mine = db.norm_li(c["li_url"])
    for r in db.q("SELECT first_name, last_name, li_url FROM contacts WHERE domain=? AND id!=?", (d, c["id"])):
        if guess.name_parts(r["first_name"], r["last_name"]) != parts:
            continue
        other = db.norm_li(r["li_url"])
        if mine and other and mine != other:
            return True
    return bool(db.q1("SELECT 1 x FROM email_examples WHERE domain=? AND first=? AND last=?", (d, *parts)))


def _apply_guess(c, email, note):
    now = NOW()
    db.x("""UPDATE contacts SET state='done', email=?, email_status='valid', email_source='guessed', guess_done=1,
            guess_note=?, research_status='guessed', research_message=?, resolved_at=? WHERE id=?""",
         (email, note, "Guessed from the company's email format and checked valid by MillionVerifier (no Seamless "
                       "credit).", now, c["id"]))
    memory.remember(db.person_keys(c["name"], c["company"], c["li_url"], c["row_email"]) + ["em:" + email], "pull",
                    c["pull_id"], None, "email")
    guess.learn(c["first_name"], c["last_name"], email, c["domain"], "guess")


def _send(p, acct, people):
    pid = p["id"]
    sids = [c["sid"] for c in people]
    before = accounts.get(acct["id"])["balance"]
    try:
        rids = seamless.research_contacts(sids, acct, pid)
    except seamless.SeamlessError as e:
        if e.code == "unconfirmed":
            return _unconfirmed(p, acct, people, before, e)
        if e.code == "insufficientCredits":
            seamless.test_connection(acct)
            return
        if e.code == "bad_request":
            # e.g. search IDs Seamless no longer knows (people kept from an earlier pull): nothing was charged.
            db.x(f"UPDATE contacts SET state='skipped', skip_reason=?, skip_kind='expired' WHERE id IN "
                 f"({','.join('?' * len(people))})", ("Seamless no longer knows this search result", *[c["id"] for c in people]))
            return
        raise
    now = NOW()
    same = len(rids) == len(people)
    with db.tx() as con:
        for i, c in enumerate(people):
            con.execute("UPDATE contacts SET state='queued', submitted_at=?, request_id=? WHERE id=?",
                        (now, rids[i] if same else None, c["id"]))
        for i, rid in enumerate(rids):
            con.execute("""INSERT OR IGNORE INTO research_requests(request_id,pull_id,account_id,contact_id,guess_sid,
                           created_at) VALUES(?,?,?,?,?,?)""",
                        (rid, pid, acct["id"], people[i]["id"] if same else None, sids[i] if same else None, now))
    for c in people:
        memory.remember(db.person_keys(c["name"], c["company"], c["li_url"], c["row_email"]), "pull", pid,
                        acct["id"], "pending")


def _unconfirmed(p, acct, people, before, err):
    """The research call failed in a way that may or may not have charged. Never resend automatically: read the
    balance (free) to find out."""
    pid = p["id"]
    n = len(people)
    ids = [c["id"] for c in people]
    marks = ",".join("?" * n)
    try:
        after = seamless.test_connection(acct)
    except seamless.SeamlessError:
        after = None
    charged = None if (before is None or after is None) else before - after
    if charged == 0:
        # Not charged: the people stay in line and are sent again on the next step.
        db.update_pull(pid, error=f"Seamless didn't take the last research request ({err}); nothing was charged. "
                                  f"Trying again.")
        return
    db.x(f"UPDATE contacts SET state='unconfirmed', submitted_at=? WHERE id IN ({marks})", (NOW(), *ids))
    for c in people:
        memory.remember(db.person_keys(c["name"], c["company"], c["li_url"], c["row_email"]), "pull", pid,
                        acct["id"], "pending")
    if charged == n:
        db.log_credits(pid, acct["id"], "research", n, after,
                       f"research {n} people (Seamless charged but didn't answer; details fetched free)")
        db.update_pull(pid, error=f"Seamless charged for {n} people but didn't answer. They won't be sent again; "
                                  f"their details are fetched free from your Seamless saved contacts.")
        return
    db.log_credits(pid, acct["id"], "research", charged if charged is not None else n, after,
                   f"research {n} people: Seamless didn't answer; balance change {charged if charged is not None else 'unknown'}")
    db.update_pull(pid, status="paused", pause_kind="unconfirmed", error=(
        f"Paused: Seamless didn't answer a research request for {n} people and the balance doesn't show clearly "
        f"whether it charged. They will NOT be sent again (that could charge twice). Check your Seamless balance, "
        f"then press Resume to carry on with other people."))


def _free_fill_unconfirmed(p):
    """People paid for whose answer never came: fetch their details free from your Seamless saved contacts."""
    rows = db.q("SELECT * FROM contacts WHERE pull_id=? AND state='unconfirmed' AND filled_free=0", (p["id"],))
    if not rows:
        return
    acct = accounts.get(rows[0]["account_id"])
    if acct:
        try:
            memory.sync_saved(acct, force=True)
        except seamless.SeamlessError:
            return
    for c in rows:
        rec = memory.saved_match(c["account_id"], c["name"], c["company"], c["li_url"])
        if rec:
            _apply_contact(c, rec, "done", "Filled in free from your Seamless saved contacts.", free=True)
        else:
            db.x("UPDATE contacts SET state='no_email', filled_free=1, research_message=? WHERE id=?",
                 ("Seamless didn't answer and no saved contact was found.", c["id"]))


# ================================================================ answers
STATUS_RANK = {"valid": 3, "verified": 3, "deliverable": 3, "risky": 2, "catch-all": 2, "catchall": 2,
               "accept-all": 2, "unknown": 1, "": 1, "invalid": 0, "undeliverable": 0}


def _pct(v):
    try:
        return float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return -1.0


def pick_email(rec):
    """One email per person. best: best status, then highest confidence, then Seamless's primary."""
    primary = (rec.get("email") or "").strip()
    cands = []
    for i in (1, 2, 3):
        e = (rec.get(f"email{i}") or "").strip()
        if e and "@" in e and e.lower() not in [c[0].lower() for c in cands]:
            cands.append((e, rec.get(f"email{i}EmailAI") or rec.get(f"email{i}Status"), rec.get(f"email{i}TotalAI")))
    if primary and "@" in primary and primary.lower() not in [c[0].lower() for c in cands]:
        cands.insert(0, (primary, rec.get("emailStatus"), None))
    if not cands:
        return None, None, None, []
    is_primary = lambda c: bool(primary) and c[0].lower() == primary.lower()
    if cfg.EMAIL_PICK == "seamless" and primary:
        best = next(c for c in cands if is_primary(c))
    else:
        best = max(cands, key=lambda c: (STATUS_RANK.get((c[1] or "").strip().lower(), 1), _pct(c[2]), is_primary(c)))
    others = [c[0] for c in cands if c[0].lower() != best[0].lower()]
    return best[0], best[1], best[2], others


def _same_person(c, rec):
    """Does this answer belong to this contact? (Used only when the answer has no searchResultId.)"""
    li_a, li_b = db.norm_li(c["li_url"]), db.norm_li(rec.get("lIProfileUrl") or rec.get("liUrl") or "")
    if li_a and li_b:
        return li_a == li_b
    na = db.norm(c["name"])
    nb = db.norm(rec.get("fullName") or f"{rec.get('firstName') or ''} {rec.get('lastName') or ''}")
    if not na or na != nb:
        return False
    # Same name is not enough (two Anna Duboises at two companies): the company must match too.
    dom_a, dom_b = db.norm_domain(c["domain"]), db.norm_domain(rec.get("companyDomain") or "")
    if dom_a and dom_b:
        return dom_a == dom_b
    co_a, co_b = db.norm_company(c["company"]), db.norm_company(rec.get("companyName") or rec.get("company") or "")
    return bool(co_a) and co_a == co_b


def _apply_contact(c, rec, status, message, free=False):
    email, estatus, conf, others = pick_email(rec)
    techs = rec.get("companyTechnologies") or rec.get("topTechnologies") or rec.get("technologies") or ""
    if isinstance(techs, list):
        techs = ", ".join(str(t) for t in techs)
    em = db.norm_email(email)
    dup = 0
    if em:
        owner = db.q1("SELECT pull_id, source FROM memory WHERE key=?", ("em:" + em,))
        mine = set(db.person_keys(c["name"], c["company"], c["li_url"], c["row_email"]))
        if owner and "em:" + em not in mine:
            # The same email was already pulled for someone else (e.g. a shared info@ address).
            other = db.q1("SELECT id FROM contacts WHERE id!=? AND LOWER(email)=? AND state='done' LIMIT 1", (c["id"], em))
            if other or owner["source"] in ("seamless", "pull"):
                dup = 1
    fields = {"state": "done" if email else "no_email", "research_status": status, "research_message": message,
              "resolved_at": NOW(), "email": email, "email_status": estatus, "email_confidence": conf,
              "email_2": others[0] if others else None, "email_3": others[1] if len(others) > 1 else None,
              "phone": rec.get("contactPhone1"), "phone_type": rec.get("contactPhone1DataType"),
              "company_phone": rec.get("companyPhone1"), "technologies": techs or None,
              "contact_json": json.dumps(rec), "filled_free": 1 if free else 0, "dup_email": dup,
              "email_source": "seamless" if email else None}
    for col, keys in (("first_name", ("firstName",)), ("last_name", ("lastName",)), ("title", ("jobTitle",)),
                      ("city", ("city",)), ("region", ("state",))):
        for k in keys:
            if rec.get(k):
                fields[col] = rec[k]
    if rec.get("companyDomain") and not c["domain"]:
        fields["domain"] = db.norm_domain(rec["companyDomain"])
    cols = ", ".join(f"{k}=?" for k in fields)
    db.x(f"UPDATE contacts SET {cols} WHERE id=?", (*fields.values(), c["id"]))
    keys = db.person_keys(c["name"], c["company"], c["li_url"], c["row_email"])
    memory.set_outcome(keys, "email" if email else "no_email")
    if em and not dup:
        memory.remember(["em:" + em], "pull", c["pull_id"], c["account_id"], "email")
        guess.learn(fields.get("first_name") or c["first_name"], fields.get("last_name") or c["last_name"], em,
                    fields.get("domain") or c["domain"], "pull")
    for t in [t.strip() for t in (techs or "").split(",") if t.strip()][:40]:
        db.x("INSERT INTO techs(name,seen) VALUES(?,1) ON CONFLICT(name) DO UPDATE SET seen=seen+1", (t[:80],))


def _apply_result(row, account_id):
    rid = row.get("requestId")
    req = db.q1("SELECT * FROM research_requests WHERE request_id=? AND account_id=?", (rid, account_id)) if rid else None
    if not req:
        return
    status = (row.get("status") or "").lower()
    if status not in seamless.TERMINAL:
        return
    if req["resolved_at"] and not req["gave_up"]:
        return                                      # already handled (Seamless sometimes repeats a row)
    rec = row.get("contact") or {}
    sid = row.get("searchResultId")
    c = None
    if sid:
        c = db.q1("SELECT * FROM contacts WHERE account_id=? AND sid=? AND submitted_at IS NOT NULL", (account_id, sid))
    elif req["contact_id"] and rec:
        # No searchResultId in the answer: matched by the order Seamless gave the request IDs, trusted only if the
        # answer is visibly the same person. Otherwise it stays unmatched (never given to the wrong person) and
        # is looked up free in your Seamless saved contacts when the pull finishes.
        cand = db.q1("SELECT * FROM contacts WHERE id=?", (req["contact_id"],))
        c = cand if cand and _same_person(cand, rec) else None
    late = bool(req["gave_up"])
    now = NOW()
    first_time = not req["status"]
    db.x("UPDATE research_requests SET status=?, resolved_at=COALESCE(resolved_at, ?), gave_up=0 WHERE request_id=?",
         (status, now, rid))
    if first_time and status in seamless.NOT_CHARGED:
        db.log_credits(req["pull_id"], account_id, "research", -1, None,
                       "already researched before in Seamless: not charged" if status != "error" else
                       "Seamless error on one person: not charged")
    if not c:
        return
    if c["state"] not in ("queued", "timed_out"):
        return
    if status == "done" and rec:
        _apply_contact(c, rec, status, "Arrived late; filled in (no extra charge)." if late else "")
        return
    if status in ("duplicate", "contact-already-researched"):
        saved = memory.saved_match(account_id, c["name"], c["company"], c["li_url"])
        if not saved:
            acct = accounts.get(account_id)
            try:
                memory.sync_saved(acct, force=True)
            except seamless.SeamlessError:
                pass
            saved = memory.saved_match(account_id, c["name"], c["company"], c["li_url"])
        if saved:
            _apply_contact(c, saved, status, "Already researched in Seamless: filled in free from your saved contacts.",
                           free=True)
            return
        db.x("UPDATE contacts SET state='no_email', research_status=?, research_message=?, resolved_at=? WHERE id=?",
             (status, "Already researched in Seamless before; not charged.", now, c["id"]))
        memory.set_outcome(db.person_keys(c["name"], c["company"], c["li_url"], c["row_email"]), "no_email")
        return
    db.x("UPDATE contacts SET state=?, research_status=?, research_message=?, resolved_at=? WHERE id=?",
         ("no_email" if status in ("not found", "missing", "done") else "failed", status,
          row.get("message") or "", now, c["id"]))
    memory.set_outcome(db.person_keys(c["name"], c["company"], c["li_url"], c["row_email"]), "no_email")


def _poll(p, force=False):
    now = NOW()
    if not force and now - (p.get("last_poll") or 0) < cfg.POLL_SECONDS:
        return
    db.update_pull(p["id"], last_poll=now)
    pending = db.q("SELECT request_id, account_id FROM research_requests WHERE pull_id=? AND resolved_at IS NULL "
                   "ORDER BY created_at LIMIT 500", (p["id"],))
    by = {}
    for r in pending:
        by.setdefault(r["account_id"], []).append(r["request_id"])
    for aid, rids in by.items():
        acct = accounts.get(aid)
        if not acct or not acct["api_key"]:
            continue
        for i in range(0, len(rids), 100):
            for row in seamless.poll_research(rids[i:i + 100], acct):
                _apply_result(row, aid)


def _give_up(pid, message):
    now = NOW()
    for req in db.q("SELECT * FROM research_requests WHERE pull_id=? AND resolved_at IS NULL", (pid,)):
        db.x("UPDATE research_requests SET resolved_at=?, gave_up=1, late_polled_at=? WHERE request_id=?",
             (now, now, req["request_id"]))
    db.x("UPDATE contacts SET state='timed_out', research_message=? WHERE pull_id=? AND state='queued'", (message, pid))


def poll_late():
    """Answers that arrive after the tool stopped waiting are still filled in (free) for LATE_ANSWER_HOURS."""
    now = NOW()
    due = db.q("""SELECT request_id, account_id FROM research_requests WHERE gave_up=1 AND created_at>? AND
                  (late_polled_at IS NULL OR late_polled_at<?) LIMIT 500""",
               (now - cfg.LATE_ANSWER_HOURS * 3600, now - cfg.LATE_POLL_MINUTES * 60))
    if not due:
        return
    by = {}
    for r in due:
        by.setdefault(r["account_id"], []).append(r["request_id"])
    for aid, rids in by.items():
        acct = accounts.get(aid)
        db.x(f"UPDATE research_requests SET late_polled_at=? WHERE request_id IN ({','.join('?' * len(rids))})",
             (now, *rids))
        if not acct:
            continue
        try:
            for i in range(0, len(rids), 100):
                for row in seamless.poll_research(rids[i:i + 100], acct):
                    _apply_result(row, aid)
        except seamless.SeamlessError:
            pass


# ================================================================ free and cheap helpers
def check_count(filters, account_choice="auto"):
    """How many people Seamless has for these filters. Costs 1 credit (one result is returned)."""
    f = F.clean(filters)
    if not F.meaningful(f):
        raise PullError("Pick at least one search filter first.")
    acct = next((a for a in accounts.ordered_for(account_choice) if (accounts.usable(a) or 1) >= 1), None)
    if not acct:
        raise PullError("No Seamless account with credits to spare.")
    rows, sup, credits = seamless.search_contacts(F.search_body(f), 1, None, acct, None, note="check how many match")
    return {"total": sup.get("total"), "credits": credits, "account": acct["name"]}


def estimate(target):
    """Credits for N emails, from your own finished pulls (1.2 per email until there are 50+ emails of history)."""
    c = db.q1("SELECT COALESCE(SUM(credits),0) c FROM ledger WHERE pull_id IN (SELECT id FROM pulls WHERE status='done')")["c"]
    e = db.q1(f"SELECT COUNT(*) n FROM contacts WHERE pull_id IN (SELECT id FROM pulls WHERE status='done') "
              f"AND {DELIVERABLE}")["n"]
    per = (c / e) if e >= 50 and c else 1.2
    return {"per_email": round(per, 2), "credits": math.ceil(int(target or 0) * per), "based_on": e if e >= 50 else 0}


# ================================================================ worker thread
class Worker(threading.Thread):
    def __init__(self, interval=0.5):
        super().__init__(daemon=True)
        self.interval = interval
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                busy = tick()
            except Exception:              # noqa: BLE001 - the loop must survive anything
                import logging
                logging.exception("worker tick failed")
                busy = False
            self._halt.wait(self.interval if busy else 2.0)

    def stop(self):
        self._halt.set()
