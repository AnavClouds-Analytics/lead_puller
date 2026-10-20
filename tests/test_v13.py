"""v1.3: two Seamless accounts with shared memory, collect spend guards, finish now / late answers,
downloads while researching, edit filters, prompt rebuild, location default, email chance basis, paged
tables, balance check, upgrade from v1.2."""
import os
import shutil
import time

import pytest

from app import accounts, db, filters, worker
from tests.conftest import check_batches, check_books, check_people, new_pull, run_until

US = {"contactCountry": ["United States"], "seniority": ["C-Level", "VP"]}
CA_IL = {"contactCountry": ["Canada", "Israel"], "seniority": ["C-Level", "VP"]}


# ---------------------------------------------------------------- helpers
def add_account(client, fake, key="key-bravo-22", balance=50_000, name="Account B", budget=0):
    fake.add_key(key, balance)
    r = client.post("/api/accounts", json={"name": name, "api_key": key, "monthly_budget": budget})
    assert r.status_code == 200, r.text
    return next(a for a in r.json()["rows"] if a["name"] == name)


def env_account():
    return db.q1("SELECT * FROM accounts WHERE source='env'")


def collect(client, f, target=60, cap=0, cooldown=0, from_top=True, account=None, status=("collected",)):
    p = new_pull(client)
    patch = {"filters": f, "company_cooldown": cooldown}
    if account:
        patch["account_choice"] = str(account)
    r = client.patch(f"/api/pulls/{p['id']}", json=patch)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/pulls/{p['id']}/preview", json={"from_top": from_top})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/pulls/{p['id']}/collect", json={"target": target, "per_company_cap": cap})
    assert r.status_code == 200, r.text
    return run_until(client, p["id"], set(status))


def research(client, pid, choice="all", status=("done", "paused"), **kw):
    r = client.post(f"/api/pulls/{pid}/research", json={"choice": choice, **kw})
    assert r.status_code == 200, r.text
    return run_until(client, pid, set(status))


def pids_of(fake, pull_id, where="submitted_at IS NOT NULL"):
    return {fake.sid_to_pid[c["search_result_id"]] for c in
            db.q(f"SELECT search_result_id FROM contacts WHERE pull_id=? AND {where}", (pull_id,))}


def check_accounts(fake):
    """Per account: the tool's ledger equals what that account was charged; nothing ever went to the wrong key."""
    for a in db.q("SELECT * FROM accounts"):
        logged = db.q1("SELECT COALESCE(SUM(credits),0) s FROM ledger WHERE account_id=?", (a["id"],))["s"]
        assert logged == fake.charged_by_key.get(a["api_key"], 0), (a["name"], logged, fake.charged_by_key)
    assert db.q1("SELECT COUNT(*) n FROM ledger WHERE account_id IS NULL AND credits!=0")["n"] == 0
    assert fake.foreign_sid_rejected == 0, "sent one account's search results to another account"
    # every research request was polled only with the key that sent it
    sender = {rid: k for rid, k in fake.rid_key.items()}
    for r in db.q("SELECT r.request_id, a.api_key FROM research_requests r JOIN accounts a ON a.id=r.account_id"):
        assert sender[r["request_id"]] == r["api_key"]


# ================================================================ Section D: two accounts, shared memory
def test_people_found_with_one_key_never_appear_with_the_other(env):
    """G's rule: whoever was pulled with account A is skipped when pulling with account B, and the other way round."""
    client, fake, _, _ = env
    a = env_account()
    b = add_account(client, fake)
    p1 = collect(client, US, target=80, account=a["id"])
    p1 = research(client, p1["id"])
    assert p1["account"]["name"] == a["name"]
    paid_a = pids_of(fake, p1["id"])
    # the same search with account B, from the top: every person A paid for is skipped before any credit
    p2 = collect(client, US, target=80, account=b["id"])
    assert p2["account"]["id"] == b["id"]
    kept_b = pids_of(fake, p2["id"], "kept=1")
    assert not (kept_b & paid_a)
    skipped = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_kind='pulled_before'", (p2["id"],))["n"]
    assert skipped >= len(paid_a) * 0.9
    p2 = research(client, p2["id"])
    paid_b = pids_of(fake, p2["id"])
    assert paid_b and not (paid_a & paid_b)
    # and back: A never gets B's people
    p3 = collect(client, US, target=40, account=a["id"])
    assert not (pids_of(fake, p3["id"], "kept=1") & (paid_a | paid_b))
    assert all(n <= 1 for n in fake.researched_pids.values())
    check_books(fake)
    check_accounts(fake)
    check_people(client, fake)


def test_people_held_in_an_open_pull_dont_appear_in_the_other_accounts_pull(env, monkeypatch):
    monkeypatch.setenv("OPEN_PULL_HOLD_DAYS", "14")
    client, fake, _, _ = env
    a, b = env_account(), add_account(client, fake)
    p1 = collect(client, US, target=60, account=a["id"])          # collected, not researched yet
    held = pids_of(fake, p1["id"], "kept=1")
    p2 = collect(client, US, target=60, account=b["id"])
    assert not (pids_of(fake, p2["id"], "kept=1") & held)
    n = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_kind='other_pull'", (p2["id"],))["n"]
    assert n >= len(held) * 0.9
    # both research: nobody charged twice, each pull on its own key
    research(client, p1["id"])
    research(client, p2["id"])
    assert all(v <= 1 for v in fake.researched_pids.values())
    check_books(fake)
    check_accounts(fake)
    check_people(client, fake)


def test_new_pulls_use_the_next_account_when_the_first_is_low(env, monkeypatch):
    monkeypatch.setenv("ACCOUNT_RESERVE", "50")
    client, fake, _, _ = env
    a = env_account()
    fake.balance = 52                     # account A: only 2 credits above the reserve
    b = add_account(client, fake, balance=10_000)
    client.post(f"/api/accounts/{a['id']}/test")
    p = collect(client, US, target=40)
    assert p["account"]["id"] == b["id"], "a new pull should start on the account with credits to spare"
    research(client, p["id"])
    assert fake.charged_by_key.get("good-key", 0) == 0
    assert fake.balance_of("good-key") == 52                      # A was not touched, reserve kept
    check_accounts(fake)


def test_account_runs_out_mid_research_then_continue_with_the_other(env):
    client, fake, _, _ = env
    a = env_account()
    b = add_account(client, fake, balance=10_000)
    p = collect(client, US, target=150, account=a["id"])
    fake.balance = 60                     # A runs dry during research
    p = research(client, p["id"])
    assert p["status"] == "paused" and p["pause_kind"] == "account"
    assert [x["id"] for x in p["move_to"]] == [b["id"]]
    paid_a = pids_of(fake, p["id"])
    r = client.post(f"/api/pulls/{p['id']}/move-account", json={"account_id": b["id"]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["account"]["id"] == b["id"] and new["status"] == "previewed"
    old = run_until(client, p["id"], {"done"})
    assert old["moved_to"] == new["id"]
    assert not (pids_of(fake, new["id"], "kept=1") & paid_a)
    r = client.post(f"/api/pulls/{new['id']}/collect", json={"target": new["target"], "per_company_cap": 0})
    assert r.status_code == 200, r.text
    run_until(client, new["id"], {"collected"})
    research(client, new["id"])
    assert not (pids_of(fake, new["id"]) & paid_a)
    assert all(v <= 1 for v in fake.researched_pids.values())
    check_books(fake)
    check_accounts(fake)
    check_people(client, fake)


def test_per_account_monthly_budget(env):
    client, fake, _, _ = env
    a = env_account()
    client.patch(f"/api/accounts/{a['id']}", json={"monthly_budget": 30})
    p = collect(client, US, target=100, account=a["id"], status=("collected", "paused"))
    if p["status"] == "collected":
        r = client.post(f"/api/pulls/{p['id']}/research", json={"choice": "all"})
        assert r.status_code == 400 and "can only spend" in r.json()["error"]
    assert db.month_spend(a["id"]) <= 30
    assert fake.charged_by_key.get("good-key", 0) <= 30


def test_fill_in_reads_saved_contacts_of_every_account(env):
    """Fill-in (free) covers people whose answer never came, and reads every account's saved contacts."""
    client, fake, _, _ = env
    add_account(client, fake)
    p, slow = _start_research_with_slow(client, fake)
    client.post(f"/api/pulls/{p['id']}/finish-now")
    p = run_until(client, p["id"], {"done"})
    charged = fake.charged
    r = client.post(f"/api/pulls/{p['id']}/fill-saved").json()["fill_result"]
    keys = {k for k, m, path in fake.calls_by_key if path == "/contacts" and m == "GET"}
    assert keys == {"good-key", "key-bravo-22"}
    with_email = [s for s in slow if fake.saved.get(s)]
    assert r["checked"] == 3 and r["filled"] == len(with_email)
    assert fake.charged == charged
    check_people(client, fake)


def test_accounts_page_rules(env):
    client, fake, _, _ = env
    r = client.post("/api/accounts", json={"name": "Wrong", "api_key": "not-a-real-key-123"})
    assert r.status_code == 400 and "did not accept" in r.json()["error"]
    b = add_account(client, fake)
    out = client.get("/api/accounts").json()
    assert b["key"] == "•••• o-22"
    assert "key-bravo-22" not in client.get("/api/accounts").text            # the key never goes back to the browser
    assert client.post("/api/accounts", json={"name": "Again", "api_key": "key-bravo-22"}).status_code == 400
    a = env_account()
    assert client.delete(f"/api/accounts/{a['id']}").status_code == 400      # .env key: switch off instead
    p = collect(client, US, target=20, account=b["id"])
    client.post(f"/api/pulls/{p['id']}/research", json={"choice": "all"})
    assert client.delete(f"/api/accounts/{b['id']}").status_code == 400      # in use
    run_until(client, p["id"], {"done"})
    assert client.patch(f"/api/accounts/{b['id']}", json={"enabled": False}).status_code == 200
    p2 = collect(client, US, target=20)
    assert p2["account"]["id"] == a["id"]                                     # switched-off account not used
    r = client.post("/api/accounts/order", json={"ids": [b["id"], a["id"]]})
    assert [x["id"] for x in r.json()["rows"]] == [b["id"], a["id"]]


def test_shared_pool_is_detected_and_counted_once(env):
    client, fake, _, _ = env
    fake.shared_pool = True
    add_account(client, fake)
    client.post("/api/settings/test-seamless")
    c = client.get("/api/meta").json()["credits"]
    assert c["shared_pools"] and len(c["shared_pools"][0]["names"]) == 2
    assert c["balance"] == fake.balance_of("good-key")                 # not doubled


def test_balance_check_finds_credits_used_outside_the_tool(env):
    client, fake, _, _ = env
    a = env_account()
    client.post(f"/api/accounts/{a['id']}/test")
    p = collect(client, US, target=40)
    research(client, p["id"])
    s = accounts.summary(accounts.get(a["id"]))
    assert s["check"]["difference"] == 0 and not s["check"]["warn"]
    fake.balance -= 300                                                  # someone used the Seamless website
    client.post(f"/api/accounts/{a['id']}/test")
    s = accounts.summary(accounts.get(a["id"]))
    assert s["check"]["warn"] and s["check"]["difference"] == 300 and "outside this tool" in s["check"]["message"]
    fake.balance += 10_000                                               # monthly refill: not counted as spending
    client.post(f"/api/accounts/{a['id']}/test")
    s = accounts.summary(accounts.get(a["id"]))
    assert s["check"]["added"] == 10_000 and s["check"]["difference"] == 300


# ================================================================ Section A: spend fewer search credits
def test_estimate_before_collecting(env):
    client, fake, _, _ = env
    p = new_pull(client)
    client.patch(f"/api/pulls/{p['id']}", json={"filters": US})
    client.post(f"/api/pulls/{p['id']}/preview", json={"from_top": True})
    e = client.get(f"/api/pulls/{p['id']}/estimate?target=200").json()
    assert e["keep_rate"] and e["search_credits"] > 0 and e["research_credits"] == 200
    assert e["total"] == e["search_credits"] + 200 and e["cap"] >= e["search_credits"]


def test_low_keep_rate_pauses_and_asks(env, monkeypatch):
    client, fake, _, _ = env
    p1 = collect(client, US, target=200)
    research(client, p1["id"])
    monkeypatch.setenv("COLLECT_LOW_KEEP_PCT", "20")
    monkeypatch.setenv("COLLECT_LOW_KEEP_PAGES", "3")
    monkeypatch.setenv("CONTINUE_SAME_SEARCH", "0")
    p2 = collect(client, US, target=200, status=("collected", "paused"))
    assert p2["status"] == "paused" and p2["pause_kind"] == "low_keep"
    assert "Main reason: Already pulled before" in p2["error"] and "credits" in p2["error"]
    spent = fake.charged
    for _ in range(10):
        worker.tick()
    assert fake.charged == spent                                   # paused: nothing more spent
    client.post(f"/api/pulls/{p2['id']}/cancel")                   # "Stop and keep what you have"
    p2 = run_until(client, p2["id"], {"collected"})
    assert fake.charged == spent
    check_books(fake)


def test_pages_that_keep_nobody_stop_collecting(env, monkeypatch):
    client, fake, _, _ = env
    p1 = collect(client, US, target=200)
    research(client, p1["id"])
    monkeypatch.setenv("COLLECT_EMPTY_PAGES_STOP", "3")
    monkeypatch.setenv("CONTINUE_SAME_SEARCH", "0")
    p2 = collect(client, US, target=400, status=("collected",))
    assert "kept nobody" in p2["stop_reason"]
    pages = db.q("SELECT kept FROM collect_pages WHERE pull_id=? ORDER BY id DESC LIMIT 3", (p2["id"],))
    assert [x["kept"] for x in pages] == [0, 0, 0]
    assert db.q1("SELECT COUNT(*) n FROM collect_pages WHERE pull_id=?", (p2["id"],))["n"] >= 3
    check_books(fake)


def test_close_enough_to_target_finishes(env, monkeypatch):
    monkeypatch.setenv("COLLECT_CLOSE_ENOUGH_PCT", "5")
    client, fake, _, _ = env
    p = collect(client, US, target=96, cap=0)
    assert p["stats"]["kept"] == 96 and p["next_token"], "needs more people left in Seamless"
    # The extras trimmed as "Over your target" would refill the gap for free; take them out of play.
    db.x("UPDATE contacts SET skip_reason='Duplicate inside this pull', skip_kind='dup_pull' WHERE pull_id=? "
         "AND skip_reason='Over your target'", (p["id"],))
    spent = p["search_credits"]
    r = client.post(f"/api/pulls/{p['id']}/collect", json={"target": 100, "per_company_cap": 0})
    assert r.status_code == 200, r.text
    p = run_until(client, p["id"], {"collected"})
    # 96 of 100 is within 5%: it finishes without spending another search credit.
    assert p["stats"]["kept"] == 96
    assert "Close enough" in p["stop_reason"], p["stop_reason"]
    assert p["search_credits"] == spent
    check_books(fake)


def test_spend_cap_pauses_then_continue_allows_more(env, monkeypatch):
    client, fake, _, _ = env
    monkeypatch.setenv("COLLECT_SPEND_CAP_PCT", "150")
    monkeypatch.setenv("COLLECT_LOW_KEEP_PCT", "0")          # only the spend cap may stop it here
    monkeypatch.setenv("COLLECT_EMPTY_PAGES_STOP", "0")
    p = new_pull(client)
    pid = p["id"]
    client.patch(f"/api/pulls/{pid}", json={"filters": US, "company_cooldown": 0})
    client.post(f"/api/pulls/{pid}/preview", json={"from_top": True})
    est = client.get(f"/api/pulls/{pid}/estimate?target=60").json()
    # After the preview (good keep rate), block almost every company: the estimate turns out far too low.
    client.post("/api/exclusions", json={"companies": "Acme Software"})
    client.post(f"/api/pulls/{pid}/collect", json={"target": 60, "per_company_cap": 0})
    p = run_until(client, pid, {"collected", "paused"})
    assert p["status"] == "paused" and p["pause_kind"] == "cap", (p["status"], p.get("error"), p.get("stop_reason"))
    assert "limit" in p["error"]
    page_credits = 5
    assert p["spend_cap"] - p["collect_started_credits"] == est["cap"]
    assert p["search_credits"] <= p["spend_cap"] + page_credits
    spent = fake.charged
    for _ in range(10):
        worker.tick()
    assert fake.charged == spent                                   # paused: nothing more spent
    cap_before = p["spend_cap"]
    client.post(f"/api/pulls/{pid}/resume")                       # "Keep collecting": a limited extra allowance
    p = run_until(client, pid, {"collected", "paused"})
    assert p["status"] == "paused" and p["spend_cap"] > cap_before
    assert p["search_credits"] <= p["spend_cap"] + page_credits
    client.post(f"/api/pulls/{pid}/cancel")                       # "Stop and keep what's collected"
    p = run_until(client, pid, {"collected"})
    assert p["stats"]["kept"] < 60
    check_books(fake)


def test_skip_report_adds_up_with_credits_and_tips(env):
    client, fake, _, _ = env
    p = collect(client, US, target=100, cap=1)
    rep = p["stats"]["skip_reasons"]
    cap = next(r for r in rep if r["kind"] == "company_cap")
    assert cap["n"] > 0 and cap["credits"] > 0 and "Max people per company" in cap["tip"]
    total = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=0", (p["id"],))["n"]
    assert sum(r["n"] for r in rep) == total
    r = client.get(f"/api/pulls/{p['id']}/contacts?view=skipped&kind=company_cap&limit=25").json()
    assert r["total"] == cap["n"] and len(r["rows"]) == min(25, cap["n"])
    assert all(x["skip_kind"] == "company_cap" for x in r["rows"])


def test_location_default_is_persons_location(env, monkeypatch):
    body = filters.to_search_body({"contactCountry": ["Israel"]})
    assert body["locationType"] == "contact"
    body = filters.to_search_body({"contactCountry": ["Israel"], "locationType": "bothOR"})
    assert body["locationType"] == "bothOR"                         # your choice wins
    assert "locationType" not in filters.to_search_body({"seniority": ["VP"]})
    monkeypatch.setenv("LOCATION_APPLIES_TO", "company")
    assert filters.to_search_body({"contactCountry": ["Israel"]})["locationType"] == "company"
    client, fake, _, _ = env
    collect(client, {"contactCountry": ["Israel"]}, target=10)
    assert fake.searches[-1]["locationType"] == "company"


# ================================================================ Section B: results without waiting
def _start_research_with_slow(client, fake, n_slow=3, target=60):
    p = collect(client, US, target=target)
    slow = sorted(pids_of(fake, p["id"], "kept=1"))[-n_slow:]
    fake.slow_pids.update(slow)
    client.post(f"/api/pulls/{p['id']}/research", json={"choice": "all"})
    for _ in range(30):
        worker.tick()
    p = client.get(f"/api/pulls/{p['id']}").json()
    assert p["status"] == "researching"
    return p, slow


def test_finish_now_then_late_answers_are_filled_in_free(env):
    client, fake, _, _ = env
    p, slow = _start_research_with_slow(client, fake)
    assert db.q1("SELECT COUNT(*) n FROM research_requests WHERE pull_id=? AND resolved_at IS NULL", (p["id"],))["n"] == 3
    research_calls = len([c for c in fake.calls if c[2] == "/contacts/research"])
    charged = fake.charged
    r = client.post(f"/api/pulls/{p['id']}/finish-now")
    assert r.status_code == 200, r.text
    p = run_until(client, p["id"], {"done"})
    assert p["stats"]["timed_out"] == 3 and p["stats"]["late_waiting"] == 3
    # results are downloadable straight away
    r = client.post(f"/api/pulls/{p['id']}/batches", json={"count": 1000})
    assert r.status_code == 200
    first = r.json()["batch_id"]
    # Seamless answers later: filled in, nothing extra charged, nothing re-sent
    fake.slow_pids.clear()
    db.x("UPDATE research_requests SET late_polled_at=NULL")
    worker.poll_late()
    got = db.q("SELECT email, research_message FROM contacts WHERE pull_id=? AND research_message LIKE 'Arrived late%'",
               (p["id"],))
    assert len(got) == 3
    assert fake.charged == charged                                         # nothing extra charged
    assert len([c for c in fake.calls if c[2] == "/contacts/research"]) == research_calls   # nobody re-sent
    # the late people go into the next batch, never into the first one again
    r = client.post(f"/api/pulls/{p['id']}/batches", json={"count": 1000})
    late_with_email = [g for g in got if g["email"]]
    if late_with_email:
        assert r.status_code == 200 and r.json()["batch_id"] != first
    check_books(fake)
    check_people(client, fake)
    check_batches(client, fake)


def test_finish_now_sends_nobody_else(env, monkeypatch):
    """Finish now in the middle of research: people not sent yet are never sent or charged."""
    monkeypatch.setenv("RESEARCH_BATCH", "10")
    monkeypatch.setenv("TEST_BATCH_SIZE", "0")
    client, fake, _, _ = env
    p = collect(client, US, target=60)
    fake.slow_pids.update(sorted(pids_of(fake, p["id"], "kept=1"))[:10])     # the first batch stays "researching"
    client.post(f"/api/pulls/{p['id']}/research", json={"choice": "all"})
    worker.tick()
    sent = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (p["id"],))["n"]
    assert 0 < sent < 60
    r = client.post(f"/api/pulls/{p['id']}/finish-now")
    assert r.status_code == 200, r.text
    p = run_until(client, p["id"], {"done"})
    assert p["stats"]["submitted"] == sent and p["research_credits"] == sent
    unsent = db.q("SELECT search_result_id FROM contacts WHERE pull_id=? AND kept=1 AND submitted_at IS NULL", (p["id"],))
    assert len(unsent) == 60 - sent
    for _ in range(20):
        worker.tick()
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (p["id"],))["n"] == sent
    fake.slow_pids.clear()
    db.x("UPDATE research_requests SET late_polled_at=NULL")
    worker.poll_late()
    check_books(fake)
    check_people(client, fake)


def test_budget_is_rechecked_before_every_research_batch(env, monkeypatch):
    """The budget can shrink while research runs (another pull spends, or .env is lowered): every batch is
    re-checked, so the month's spending never goes past the budget."""
    monkeypatch.setenv("RESEARCH_BATCH", "10")
    monkeypatch.setenv("TEST_BATCH_SIZE", "0")
    client, fake, _, _ = env
    p = collect(client, US, target=60)
    client.post(f"/api/pulls/{p['id']}/research", json={"choice": "all"})
    worker.tick()
    spent = db.month_spend()
    monkeypatch.setenv("MONTHLY_CREDIT_BUDGET", str(spent + 15))
    p = run_until(client, p["id"], {"paused", "done"})
    assert p["status"] == "paused" and "budget" in (p["error"] or "").lower(), p.get("error")
    assert db.month_spend() <= spent + 15
    assert fake.charged <= spent + 15
    for _ in range(10):
        worker.tick()
    assert fake.charged <= spent + 15
    client.post(f"/api/pulls/{p['id']}/cancel")
    p = run_until(client, p["id"], {"done"})
    check_books(fake)
    check_people(client, fake)


def test_slow_answers_stop_waiting_after_the_env_minutes(env, monkeypatch):
    monkeypatch.setenv("RESEARCH_WAIT_MINUTES", "15")
    client, fake, _, _ = env
    p, slow = _start_research_with_slow(client, fake)
    for _ in range(5):
        worker.tick()
    assert client.get(f"/api/pulls/{p['id']}").json()["status"] == "researching"   # not yet 15 minutes
    db.x("UPDATE research_requests SET created_at=created_at-16*60 WHERE pull_id=?", (p["id"],))
    p = run_until(client, p["id"], {"done"}, max_ticks=20)
    assert p["stats"]["timed_out"] == 3
    msg = db.q1("SELECT research_message m FROM contacts WHERE pull_id=? AND research_status='timed out'", (p["id"],))["m"]
    assert "15 minutes" in msg and "free" in msg


def test_download_batches_while_research_is_running(env):
    client, fake, _, _ = env
    p, slow = _start_research_with_slow(client, fake, n_slow=5, target=80)
    r = client.post(f"/api/pulls/{p['id']}/batches", json={"count": 1000})
    assert r.status_code == 200, r.text
    b1 = r.json()["batch_id"]
    in_b1 = {fake.sid_to_pid[c["search_result_id"]] for c in db.q("SELECT search_result_id FROM contacts WHERE batch_id=?", (b1,))}
    assert not (in_b1 & set(slow))
    fake.slow_pids.clear()
    run_until(client, p["id"], {"done"})
    r = client.post(f"/api/pulls/{p['id']}/batches", json={"count": 1000})
    assert r.status_code == 200
    in_b2 = {fake.sid_to_pid[c["search_result_id"]] for c in
             db.q("SELECT search_result_id FROM contacts WHERE batch_id=?", (r.json()["batch_id"],))}
    assert in_b2 and in_b2 <= set(slow) and not (in_b1 & in_b2)
    check_books(fake)
    check_people(client, fake)


# ================================================================ Section C: edit filters, prompt, tables
def test_edit_filters_before_research_clears_the_list(env):
    client, fake, _, _ = env
    p = collect(client, US, target=40)
    assert p["stats"]["kept"] > 0
    r = client.post(f"/api/pulls/{p['id']}/back-to-filters")
    assert r.status_code == 200 and r.json()["status"] == "draft"
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=?", (p["id"],))["n"] == 0
    client.patch(f"/api/pulls/{p['id']}", json={"filters": CA_IL})
    client.post(f"/api/pulls/{p['id']}/preview", json={"from_top": True})
    client.post(f"/api/pulls/{p['id']}/collect", json={"target": 30, "per_company_cap": 0})
    p = run_until(client, p["id"], {"collected"})
    assert {c["country"] for c in db.q("SELECT country FROM contacts WHERE pull_id=? AND kept=1", (p["id"],))} <= {
        "Canada", "Israel"}
    check_books(fake)


def test_edit_filters_after_research_makes_a_copy_and_skips_paid_people(env):
    client, fake, _, _ = env
    p = collect(client, US, target=40)
    p = research(client, p["id"])
    assert client.post(f"/api/pulls/{p['id']}/back-to-filters").status_code == 400
    r = client.post(f"/api/pulls/{p['id']}/edit-copy")
    assert r.status_code == 200
    c = r.json()
    assert c["id"] != p["id"] and c["status"] == "draft" and c["edited_from"] == p["id"]
    assert c["filters"]["contactCountry"] == ["United States"]
    client.post(f"/api/pulls/{c['id']}/preview", json={"from_top": True})
    client.post(f"/api/pulls/{c['id']}/collect", json={"target": 40, "per_company_cap": 0})
    c = run_until(client, c["id"], {"collected"})
    assert not (pids_of(fake, c["id"], "kept=1") & pids_of(fake, p["id"]))
    assert db.get_pull(p["id"])["status"] == "done"                 # the original is untouched


def test_rebuild_filters_from_the_edited_prompt(env):
    client, fake, claude_fake, _ = env
    p = new_pull(client, "CTOs in the US")
    client.patch(f"/api/pulls/{p['id']}", json={"filters": dict(p["filters"], contactCountry=["Germany"])})
    p = client.get(f"/api/pulls/{p['id']}").json()
    assert any("Countries" in e for e in p["hand_edits"])
    claude_fake.filters = {"jobTitle": ["CTO"], "contactCountry": ["Israel"]}
    r = client.post("/api/pulls/interpret", json={"prompt": "CTOs in Israel", "pull_id": p["id"], "replace": True})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["prompt"] == "CTOs in Israel" and p["filters"]["contactCountry"] == ["Israel"] and p["hand_edits"] == []


def test_tables_are_paged_and_select_all_matching_covers_every_page(env):
    client, fake, _, _ = env
    p = collect(client, US, target=120)
    pid = p["id"]
    r1 = client.get(f"/api/pulls/{pid}/contacts?view=kept&limit=25&offset=0").json()
    r2 = client.get(f"/api/pulls/{pid}/contacts?view=kept&limit=25&offset=25").json()
    assert r1["total"] == p["stats"]["kept"] and len(r1["rows"]) == 25 and len(r2["rows"]) == 25
    assert not ({x["id"] for x in r1["rows"]} & {x["id"] for x in r2["rows"]})
    # untick everyone on every page, then tick all matching a search
    r = client.post(f"/api/pulls/{pid}/contacts/select", json={"all_matching": True, "selected": False}).json()
    assert r["ticked"] == 0
    r = client.post(f"/api/pulls/{pid}/contacts/select", json={"all_matching": True, "selected": True, "q": "Acme"}).json()
    acme = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1 AND company LIKE '%Acme%'", (pid,))["n"]
    assert r["ticked"] == acme > 25
    # what the button shows == what is sent == what is charged
    shown = client.get(f"/api/pulls/{pid}/choice-count?choice=all").json()
    assert shown["n"] == acme and shown["credits"] == acme
    bad = client.post(f"/api/pulls/{pid}/research", json={"choice": "all", "expected": acme + 1})
    assert bad.status_code == 400 and "changed" in bad.json()["error"]
    before = fake.charged
    p = research(client, pid, expected=acme)
    assert p["stats"]["submitted"] == acme
    research_charged = sum(n for k, n in fake.charges if k == "research")
    assert research_charged == acme
    # sorted, paged and searchable lists elsewhere
    assert client.get("/api/pulls?limit=1").json()["total"] >= 1
    led = client.get("/api/ledger?limit=2&offset=0").json()
    assert len(led["rows"]) == 2 and led["total"] > 2
    assert client.get("/api/companies?limit=10").json()["total"] > 10
    check_books(fake)


def test_email_chance_says_what_it_is_based_on_or_shows_nothing(env, monkeypatch):
    monkeypatch.setenv("EMAIL_CHANCE_MIN_SAMPLES", "20")
    client, fake, _, _ = env
    p0 = collect(client, US, target=60)
    research(client, p0["id"])
    # A little Israel history, but fewer than EMAIL_CHANCE_MIN_SAMPLES: still not enough to show a chance.
    pi = collect(client, {"contactCountry": ["Israel"], "seniority": ["C-Level", "VP"]}, target=8)
    research(client, pi["id"])
    il_hist = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (pi["id"],))["n"]
    assert 0 < il_hist < 20
    p = collect(client, {"contactCountry": ["United States", "Israel"], "seniority": ["C-Level", "VP"]}, target=80)
    rows = db.q("SELECT country, email_chance, email_chance_basis FROM contacts WHERE pull_id=? AND kept=1", (p["id"],))
    us = [r for r in rows if r["country"] == "United States"]
    il = [r for r in rows if r["country"] == "Israel"]
    assert us and all(r["email_chance"] is not None and "United States" in r["email_chance_basis"] for r in us)
    assert il and all(r["email_chance"] is None for r in il)           # too little Israel history: —, never a US guess
    import re
    assert all(int(re.search(r"based on ([\d,]+)", r["email_chance_basis"]).group(1).replace(",", "")) >= 20 for r in us)


def test_email_chance_never_counts_invalid_emails(env, monkeypatch):
    monkeypatch.setenv("EMAIL_CHANCE_MIN_SAMPLES", "20")
    client, fake, _, _ = env
    p0 = collect(client, US, target=60)
    research(client, p0["id"])
    hist = db.q("""SELECT id FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL AND email IS NOT NULL
                   AND email!=''""", (p0["id"],))
    assert len(hist) >= 20
    db.x("UPDATE contacts SET email_status='invalid' WHERE pull_id=? AND email IS NOT NULL AND email!=''", (p0["id"],))
    p = collect(client, US, target=40)
    rows = db.q("SELECT email_chance FROM contacts WHERE pull_id=? AND kept=1 AND country='United States'", (p["id"],))
    assert rows and all(r["email_chance"] == 0 for r in rows), {r["email_chance"] for r in rows}


# ================================================================ upgrade
def test_upgrade_from_v12_database_with_a_stuck_pull(tmp_path, monkeypatch):
    """G's real situation: a v1.2 database with a pull waiting 50 minutes for 3 Seamless answers."""
    src = os.path.join(os.path.dirname(__file__), "data", "leadpuller-v1.2.db")
    path = tmp_path / "leadpuller.db"
    shutil.copy(src, path)
    for k, v in {"APP_PASSWORD": "x", "SEAMLESS_API_KEY": "good-key", "DATA_DIR": str(tmp_path),
                 "RESEARCH_WAIT_MINUTES": "15", "MONTHLY_CREDIT_BUDGET": "0"}.items():
        monkeypatch.setenv(k, v)
    db.reset_for_tests(str(path))
    acc = db.q("SELECT * FROM accounts")
    assert len(acc) == 1 and acc[0]["source"] == "env" and acc[0]["api_key"] == "good-key"
    assert db.q1("SELECT COUNT(*) n FROM ledger WHERE account_id IS NULL")["n"] == 0
    assert db.q1("SELECT COUNT(*) n FROM research_requests WHERE account_id IS NULL")["n"] == 0
    stuck = db.q1("SELECT * FROM pulls WHERE status='researching'")
    assert stuck and stuck["account_id"] == acc[0]["id"]
    seen_before = db.q1("SELECT COUNT(*) n FROM seen")["n"]
    assert seen_before > 0
    import httpx
    from app import seamless
    from tests.fakes import FakeSeamless
    fake = FakeSeamless()
    seamless.TRANSPORT = httpx.MockTransport(fake.handler)
    seamless.SLEEP = lambda s: None
    worker.POLL_EVERY = 0
    worker._last_poll.clear()
    for _ in range(5):
        worker.tick()
    p = db.get_pull(stuck["id"])
    assert p["status"] == "done"                                     # no more waiting: results open
    timed = db.q("SELECT research_message FROM contacts WHERE pull_id=? AND research_status='timed out'", (p["id"],))
    assert len(timed) == 3
    assert fake.charged == 0                                         # finishing never costs anything
    assert db.q1("SELECT COUNT(*) n FROM seen")["n"] == seen_before
