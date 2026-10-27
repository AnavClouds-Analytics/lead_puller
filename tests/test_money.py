"""Money and data safety: every scenario ends with the books checked against what 'Seamless' charged, per
account, and with proof that nobody was paid for twice and every email belongs to its person."""
import os
import json

import pytest

from app import db, engine, memory
from tests.conftest import US, add_account, check_books, check_people, run, start
from tests.fakes import email_for

ISRAEL = {"contactCountry": {"in": ["Israel", "Germany", "Canada"]}}


def research_charges(fake, key=None):
    return sum(n for k, kind, n in fake.charges if kind == "research" and (key is None or k == key))


def search_bodies(fake, key=None):
    return [b for k, b in fake.searches if key is None or k == key]


# ---------------------------------------------------------------- the basics
def test_pull_reaches_target_and_books_balance(env):
    client, fake, clock = env
    pid = start(client, US, target=60, max_credits=500)
    p = run(client, pid)
    s = p["stats"]
    assert s["emails"] >= 60 and "Reached your target" in p["stop_reason"]
    # Researched only about as many as needed: no big overshoot.
    assert s["researched"] <= 60 / 0.75 + 15, s
    assert s["credits"] == s["search_credits"] + s["research_credits"]
    check_books(fake)
    check_people(fake)


def test_max_credits_is_never_exceeded(env):
    client, fake, clock = env
    for target, cap in ((200, 37), (50, 12), (30, 1)):
        pid = start(client, US, target=target, max_credits=cap)
        p = run(client, pid)
        assert db.pull_spend(pid) <= cap, (cap, db.pull_spend(pid))
        assert "max" in p["stop_reason"].lower() or p["stats"]["emails"] >= target
    check_books(fake)
    check_people(fake)


def test_monthly_budgets_are_never_exceeded(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("MONTHLY_CREDIT_BUDGET", "45")
    pid = start(client, US, target=500, max_credits=5000)
    p = run(client, pid, until=("done", "paused"))
    assert db.month_spend() <= 45
    assert p["status"] == "paused" and p["pause_kind"] == "account"
    for _ in range(20):
        engine.tick()
    assert db.month_spend() <= 45
    check_books(fake)


def test_per_account_budget_then_next_account(env):
    client, fake, clock = env
    a = db.q1("SELECT id FROM accounts WHERE name='Account A'")["id"]
    client.patch(f"/api/accounts/{a}", json={"monthly_budget": 40})
    add_account(client, fake)
    pid = start(client, US, target=120, max_credits=1000)
    p = run(client, pid)
    assert db.month_spend(a) <= 40
    assert p["stats"]["emails"] >= 120
    assert fake.charged_by_key.get("key-bravo-2222", 0) > 0
    check_books(fake)
    check_people(fake)


# ---------------------------------------------------------------- never twice
def test_never_pay_twice_across_pulls_and_search_continues(env):
    client, fake, clock = env
    run(client, start(client, US, target=80, max_credits=500))
    n_search = len(fake.searches)
    pid2 = start(client, US, target=80, max_credits=500)
    p2 = run(client, pid2)
    assert p2["stats"]["emails"] >= 80
    later = search_bodies(fake)[n_search:]
    # The second pull continued where the first stopped (or used people found earlier) instead of paying to
    # see the same pages again.
    assert any(b.get("nextToken") for b in later) or p2["stats"]["search_credits"] < 5
    check_books(fake)
    check_people(fake)


def test_people_found_with_one_key_never_appear_with_the_other(env):
    client, fake, clock = env
    b = add_account(client, fake)
    a = db.q1("SELECT id FROM accounts WHERE name='Account A'")["id"]
    p1 = run(client, start(client, US, target=100, max_credits=600, account=a))
    first = {c["email"] for c in db.q("SELECT email FROM contacts WHERE pull_id=? AND email IS NOT NULL", (p1["id"],))}
    p2 = run(client, start(client, US, target=100, max_credits=600, account=b["id"]))
    second = {c["email"] for c in db.q("SELECT email FROM contacts WHERE pull_id=? AND email IS NOT NULL", (p2["id"],))}
    assert not first & second
    # Account B saw some of A's people in its search results and skipped them without paying.
    skipped = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_kind LIKE 'mem%'", (p2["id"],))["n"]
    assert skipped > 0
    assert research_charges(fake, "key-bravo-2222") == p2["stats"]["researched"]
    check_books(fake)
    check_people(fake)


def test_people_researched_in_seamless_itself_are_never_paid_for(env):
    client, fake, clock = env
    # 40 US people were researched by hand in the Seamless app before this tool existed.
    org = fake.org("key-alpha-1111")
    before = [p for p in fake.people if p["country"] == "United States"][:40]
    for p in before:
        fake.researched_in(org)[p["pid"]] = 1
        fake.saved.setdefault(org, {})[p["pid"]] = fake.record(p)
    db.x("DELETE FROM saved_sync")
    pid = start(client, US, target=60, max_credits=500)
    p = run(client, pid)
    sent = {fake.sid_to_pid[c["sid"]] for c in db.q("SELECT sid FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL",
                                                    (pid,))}
    assert not sent & {x["pid"] for x in before}
    kinds = {r["kind"] for r in p["stats"]["skips"]}
    assert kinds & {"seamless", "mem_email"}
    fake.researched[org] = {k: v for k, v in fake.researched[org].items() if k not in {x["pid"] for x in before}}
    check_books(fake)
    check_people(fake)


def test_email_shown_in_search_results_is_checked_against_memory(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("PAUSE_BELOW_EMAIL_RATE", "0")
    run(client, start(client, US, target=50, max_credits=400))
    # Same filters from the top, as if Seamless lost the position: people already pulled come back with their
    # email shown in the search results, and are skipped on that email alone.
    db.x("UPDATE scan_state SET next_token=NULL, exhausted_at=NULL, body_hash='x'")
    db.x("UPDATE contacts SET state='skipped' WHERE state='banked'")
    db.x("DELETE FROM memory WHERE key NOT LIKE 'em:%'")
    pid = start(client, US, target=40, max_credits=400)
    p = run(client, pid)
    kinds = {r["kind"]: r["n"] for r in p["stats"]["skips"]}
    assert kinds.get("mem_email", 0) > 0
    check_books(fake)
    check_people(fake)


# ---------------------------------------------------------------- saving search credits
def test_people_found_but_not_needed_are_used_first_next_time(env):
    client, fake, clock = env
    banked = 0
    for target in (40, 30, 20, 35, 25):
        db.x("DELETE FROM contacts WHERE state='banked'")
        p1 = run(client, start(client, US, target=target, max_credits=500))
        banked = db.q1("SELECT COUNT(*) n FROM contacts WHERE state='banked'")["n"]
        if banked >= 8:
            break
    assert banked >= 8, "the test needs people left over"
    n_search = len(fake.searches)
    p2 = run(client, start(client, US, target=max(1, banked // 3), max_credits=500))
    # The second pull got its people from the bank: no new search page at all.
    assert len(fake.searches) == n_search
    assert p2["stats"]["search_credits"] == 0 and p2["stats"]["emails"] >= 1
    check_books(fake)
    check_people(fake)


def test_fully_scanned_slices_are_later_asked_only_for_fresh_people(env, monkeypatch):
    client, fake, clock = env
    small = {"contactCountry": {"in": ["Israel"]}}
    p1 = run(client, start(client, small, target=5000, max_credits=5000))
    assert "no more new people" in p1["stop_reason"]
    n = len(fake.searches)
    clock.t += 3 * 86400
    p2 = run(client, start(client, small, target=50, max_credits=500))
    later = search_bodies(fake)[n:]
    assert later and all(b.get("lastModifiedAfter") for b in later)
    # Nothing new was updated since: Seamless returns (and charges for) nobody old.
    assert p2["stats"]["search_credits"] == 0
    check_books(fake)
    check_people(fake)


def test_fresh_slices_go_first_and_a_slice_full_of_old_people_is_left_after_one_page(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("MOVE_ON_PAGES", "1")
    mid = {"contactCountry": {"in": ["United States"]}, "companySize": {"in": ["51 - 200"]}}
    run(client, start(client, mid, target=5000, max_credits=5000))       # takes everyone in that slice
    # Seamless loses the saved position: the slice would show only people you already have.
    db.x("UPDATE scan_state SET next_token=NULL, exhausted_at=NULL, body_hash='x'")
    db.x("UPDATE contacts SET state='skipped' WHERE state='banked'")
    both = {"contactCountry": {"in": ["United States"]}, "companySize": {"in": ["51 - 200", "201 - 500"]}}
    pid = start(client, both, target=5000, max_credits=5000)
    p = run(client, pid)
    by = {s["label"]: s for s in p["slices"]}
    assert p["slices"][0]["label"] == "201 - 500"            # the fresh slice went first
    assert by["51 - 200"]["state"] == "expensive"             # the old one was left after one page…
    assert by["51 - 200"]["pages"] == 1                       # …instead of paying to see all of it again
    assert by["201 - 500"]["state"] == "done"
    check_books(fake)
    check_people(fake)


def test_a_first_batch_without_emails_pauses_before_spending_more(env):
    client, fake, clock = env
    il = [p for p in fake.people if p["country"] == "Israel"]
    for p in il:
        fake.email_override[p["pid"]] = ""            # nobody in Israel has an email
    pid = start(client, {"contactCountry": {"in": ["Israel"]}}, target=60, max_credits=500)
    p = run(client, pid, until=("paused", "done"))
    assert p["status"] == "paused" and p["pause_kind"] == "low_rate"
    assert p["stats"]["researched"] <= 25                 # only the small first batch was paid for
    spent = fake.charged
    for _ in range(20):
        engine.tick()
    assert fake.charged == spent                          # nothing more until you decide
    client.post(f"/api/pulls/{pid}/stop")
    p = run(client, pid)
    assert fake.charged == spent
    check_books(fake)


def test_page_size_follows_what_is_still_needed(env):
    client, fake, clock = env
    run(client, start(client, US, target=8, max_credits=200))
    limits = [b["limit"] for b in search_bodies(fake)]
    assert limits and max(limits) <= 20, limits


# ---------------------------------------------------------------- accounts
def test_account_runs_low_mid_pull_and_the_next_one_continues(env):
    client, fake, clock = env
    fake.pools["key-alpha-1111"] = 70
    client.post(f"/api/accounts/{db.q1('SELECT id FROM accounts')['id']}/check")
    add_account(client, fake)
    pid = start(client, US, target=150, max_credits=2000)
    p = run(client, pid)
    assert p["stats"]["emails"] >= 150
    assert fake.balance_of("key-alpha-1111") >= 0
    assert fake.charged_by_key.get("key-bravo-2222", 0) > 0
    check_books(fake)
    check_people(fake)


def test_shared_pool_is_detected(env):
    client, fake, clock = env
    fake.shared_pool = True
    add_account(client, fake)
    for a in db.q("SELECT id FROM accounts"):
        client.post(f"/api/accounts/{a['id']}/check")
    ov = client.get("/api/overview").json()
    assert ov["shared_pools"] and len(ov["shared_pools"][0]["names"]) == 2


def test_bad_key_is_never_saved_and_says_why(env):
    client, fake, clock = env
    r = client.post("/api/accounts", json={"name": "Wrong", "api_key": "not-a-real-key-123"})
    assert r.status_code == 400 and "error 401" in r.json()["error"]
    fake.add_key("key-charlie-3333", 1000)
    r = client.post("/api/accounts", json={"name": "Pasted", "api_key": ' "key-charlie-3333​"\n'})
    assert r.status_code == 200
    names = [a["name"] for a in r.json()["accounts"]]
    assert "Pasted" in names and "Wrong" not in names


# ---------------------------------------------------------------- research errors
@pytest.mark.parametrize("mode", ["500_after_charge", "timeout_after_charge", "500_before_charge"])
def test_research_that_may_have_charged_is_never_resent(env, mode):
    client, fake, clock = env
    pid = start(client, US, target=80, max_credits=600)
    for _ in range(4):
        engine.tick()
    fake.research_fail_next = mode
    p = run(client, pid, until=("done", "paused"))
    if p["status"] == "paused":
        assert p["pause_kind"] == "unconfirmed"
        client.post(f"/api/pulls/{pid}/resume")
        p = run(client, pid)
    check_people(fake)                     # nobody charged twice: the failed batch was never resent blindly
    check_books(fake)
    if mode != "500_before_charge":
        assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND state IN ('unconfirmed') "
                     "OR filled_free=1", (pid,))["n"] > 0


def test_answers_out_of_order_partial_and_repeated_never_mix_people(env):
    client, fake, clock = env
    fake.chaos_poll = True
    fake.shuffle_request_ids = True
    fake.flaky_rate = 0.1
    p = run(client, start(client, US, target=120, max_credits=900), clock=clock)
    assert p["stats"]["emails"] > 60
    check_people(fake)
    check_books(fake)


def test_expired_search_position_starts_over_without_paying_twice(env):
    client, fake, clock = env
    run(client, start(client, US, target=40, max_credits=400))
    fake.token_ttl = 10
    clock.t += 1000
    p = run(client, start(client, US, target=40, max_credits=400))
    assert p["stats"]["emails"] >= 40
    check_people(fake)
    check_books(fake)


def test_slow_answers_finish_then_late_ones_fill_in_free(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("RESEARCH_WAIT_MINUTES", "5")
    # Every third US person answers slowly, so whatever the order, some of the people researched are slow.
    us = [p["pid"] for p in fake.people if p["country"] == "United States"]
    fake.slow_pids.update(us[::3])
    pid = start(client, US, target=40, max_credits=500)
    p = run(client, pid, clock=clock, ticks=20000)
    late = p["stats"]["late"]
    assert late, "the test needs slow answers"
    charged = fake.charged
    fake.slow_pids.clear()
    clock.t += 60
    engine.tick()
    filled = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND research_message LIKE 'Arrived late%'",
                   (pid,))["n"]
    assert filled > 0 and fake.charged == charged
    check_books(fake)
    check_people(fake)


def test_finish_now_and_stop(env):
    client, fake, clock = env
    fake.slow_pids.update(p["pid"] for p in fake.people)
    pid = start(client, US, target=200, max_credits=900)
    for _ in range(6):
        engine.tick()
    spent = fake.charged
    client.post(f"/api/pulls/{pid}/finish-now")
    for _ in range(30):
        engine.tick()
    p = client.get(f"/api/pulls/{pid}").json()
    assert p["status"] == "done" and fake.charged == spent
    fake.slow_pids.clear()
    clock.t += 3600
    engine.tick()
    check_people(fake)
    check_books(fake)


def test_stop_all_and_spending_switch(env, monkeypatch):
    client, fake, clock = env
    a = start(client, US, target=300, max_credits=900)
    b = start(client, ISRAEL, target=300, max_credits=900)
    for _ in range(3):
        engine.tick()
    client.post("/api/stop-all")
    for _ in range(50):
        engine.tick()
    spent = fake.charged
    for pid in (a, b):
        assert client.get(f"/api/pulls/{pid}").json()["status"] == "done"
    monkeypatch.setenv("SPENDING_ENABLED", "0")
    c = start(client, US, target=50)
    for _ in range(30):
        engine.tick()
    assert fake.charged == spent
    assert client.get(f"/api/pulls/{c}").json()["pause_kind"] == "spending"
    check_books(fake)


# ---------------------------------------------------------------- skips before paying
def test_do_not_pull_list_is_never_researched_or_downloaded(env):
    client, fake, clock = env
    us = [p for p in fake.people if p["country"] == "United States" and p["domain"]]
    client.post("/api/exclusions", json={"text": f"{email_for(us[0])}\n{us[1]['domain']}\n{us[2]['company']}\n"
                                                 f"{us[3]['liUrl'] or 'linkedin.com/in/none'}"})
    blocked_dom = {us[1]["domain"]}
    pid = start(client, US, target=150, max_credits=900)
    run(client, pid)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (pid,)):
        assert c["domain"] not in blocked_dom
        assert db.norm_company(c["company"]) != db.norm_company(us[2]["company"])
    # Added after research: left out of the download.
    got = db.q("SELECT email, company FROM contacts WHERE pull_id=? AND email IS NOT NULL LIMIT 1", (pid,))[0]
    client.post("/api/exclusions", json={"text": got["email"]})
    csv_text = client.get(f"/api/pulls/{pid}/download").content.decode("utf-8-sig")
    assert got["email"] not in csv_text
    check_books(fake)


def test_excludes_go_to_seamless_where_possible(env):
    client, fake, clock = env
    f = {"contactCountry": {"in": ["United States", "Canada"], "out": ["Germany"]},
         "companySize": {"out": ["10,001+", "5,001 - 10,000"]},
         "jobTitle": {"in": ["CTO"], "out": ["intern", "recruiter"]},
         "industry": {"out": ["Staffing & Recruiting"]}}
    pid = start(client, f, target=40, max_credits=400)
    run(client, pid)
    body = search_bodies(fake)[0]
    assert "-Germany" in body["contactCountry"]
    assert "10,001+" not in json.dumps(body.get("companySize"))
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (pid,)):
        assert "intern" not in c["title"].lower() and "recruiter" not in c["title"].lower()
        assert "Staffing" not in (c["industry"] or "")
    check_books(fake)


def test_no_website_no_mail_server_and_max_per_company(env):
    client, fake, clock = env
    pid = start(client, US, target=150, max_credits=900, max_per_company=2)
    run(client, pid)
    per = {}
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (pid,)):
        assert c["domain"], "researched someone with no company website"
        assert c["domain"] not in ("acme13.com", "acme26.com"), "researched a company that can't receive email"
        per[c["domain"]] = per.get(c["domain"], 0) + 1
    assert max(per.values()) <= 2
    check_books(fake)


def test_shared_email_goes_out_once(env):
    client, fake, clock = env
    us = [p for p in fake.people if p["country"] == "United States" and p["domain"]][:40]
    for p in us:
        fake.email_override[p["pid"]] = "info@shared-inbox.com"
    pid = start(client, US, target=60, max_credits=600)
    run(client, pid)
    text = client.get(f"/api/pulls/{pid}/download").content.decode("utf-8-sig")
    assert text.count("info@shared-inbox.com") <= 1
    # Counted once too: the other people with the same address are marked, not counted as emails.
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE email='info@shared-inbox.com' AND dup_email=0")["n"] <= 1
    check_books(fake)


def test_check_count_costs_one_credit_and_is_logged(env):
    client, fake, clock = env
    r = client.post("/api/check-count", json={"filters": US}).json()
    assert r["total"] > 0 and r["credits"] == 1
    assert fake.charged == 1
    check_books(fake)


# ---------------------------------------------------------------- each safety layer on its own
def test_people_are_checked_again_right_before_paying(env):
    client, fake, clock = env
    b = add_account(client, fake)
    a = db.q1("SELECT id FROM accounts WHERE name='Account A'")["id"]
    pa = start(client, US, target=40, max_credits=400, account=a)
    engine.tick()                                          # A searches and keeps people, hasn't paid yet
    held = db.q("SELECT name FROM contacts WHERE pull_id=? AND state='kept'", (pa,))
    assert held and not db.q1("SELECT COUNT(*) n FROM contacts WHERE submitted_at IS NOT NULL")["n"]
    client.post(f"/api/pulls/{pa}/pause")
    run(client, start(client, US, target=60, max_credits=400, account=b["id"]))   # B pays for the same people
    client.post(f"/api/pulls/{pa}/resume")
    run(client, pa)
    rechecked = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_kind='recheck'", (pa,))["n"]
    assert rechecked > 0
    check_people(fake)                                     # nobody paid for on both accounts
    check_books(fake)


def test_blocked_website_covers_its_subdomains(env):
    client, fake, clock = env
    us = [p for p in fake.people if p["country"] == "United States"][:25]
    for p in us:
        p["domain"] = "eu.blockedco.com"
        p["company"] = "Blocked Co Europe"
    client.post("/api/exclusions", json={"text": "blockedco.com"})
    pid = start(client, US, target=100, max_credits=600)
    run(client, pid)
    assert not db.q1("SELECT COUNT(*) n FROM contacts WHERE submitted_at IS NOT NULL AND domain LIKE '%blockedco.com'")["n"]
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE skip_kind='dnp' AND domain LIKE '%blockedco.com'")["n"] > 0
    check_books(fake)


def test_companies_without_an_email_server_are_never_paid_for(env):
    client, fake, clock = env
    us = [p for p in fake.people if p["country"] == "United States"][:20]
    for p in us:
        p["domain"] = "acme13.com"                         # the fake DNS says: no email server
    pid = start(client, US, target=100, max_credits=600)
    run(client, pid)
    assert not db.q1("SELECT COUNT(*) n FROM contacts WHERE submitted_at IS NOT NULL AND domain='acme13.com'")["n"]
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE skip_kind='no_mx'")["n"] > 0


def test_exclude_from_a_fixed_list_is_sent_to_seamless(env):
    client, fake, clock = env
    f = {"contactCountry": {"in": ["United States"]}, "companyRevenue": {"out": ["$1B+", "$500M - $1B"]}}
    run(client, start(client, f, target=10, max_credits=100))
    body = search_bodies(fake)[0]
    assert "$1B+" not in body["companyRevenue"] and len(body["companyRevenue"]) == 7




def test_old_lead_puller_database_is_imported_so_nobody_is_paid_for_again(env, tmp_path):
    import shutil
    from app import importer
    client, fake, clock = env
    old = tmp_path / "leadpuller.db"
    shutil.copy(os.path.join(os.path.dirname(__file__), "data", "leadpuller-v1.2.db"), old)
    out = importer.import_v1(str(old))
    assert out["people"] > 50
    import sqlite3
    con = sqlite3.connect(str(old))
    paid = con.execute("SELECT name, company, li_url, email FROM contacts WHERE submitted_at IS NOT NULL").fetchall()
    for name, company, li, email in paid:
        assert memory.find(db.person_keys(name, company, li, email)), name
    # Old people come back in new searches (same LinkedIn profiles): none is paid for again, all are skipped.
    old_li = {db.norm_li(li) for _, _, li, _ in paid if db.norm_li(li)}
    pid = start(client, US, target=80, max_credits=600)
    run(client, pid)
    paid_now = db.q("SELECT li_url FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (pid,))
    assert not [c for c in paid_now if db.norm_li(c["li_url"]) in old_li]
    skipped_old = [c for c in db.q("SELECT li_url FROM contacts WHERE pull_id=? AND state='skipped'", (pid,))
                   if db.norm_li(c["li_url"]) in old_li]
    assert skipped_old, "the test needs old people to show up again"


def test_old_database_is_imported_once_on_first_start(env, tmp_path):
    import shutil
    from app import importer
    shutil.copy(os.path.join(os.path.dirname(__file__), "data", "leadpuller-v1.2.db"), tmp_path / "leadpuller.db")
    db.x("DELETE FROM meta WHERE k='old_import'")
    first = importer.auto_import()
    assert first["people"] > 50 and db.get_meta("old_import")["found"]
    assert importer.auto_import() is None  # only once
    client, _, _ = env
    assert client.get("/api/settings").json()["old_import"]["people"] == first["people"]


def test_old_database_accounts_and_do_not_pull_are_imported(env, tmp_path):
    """The 1.3 shape: accounts table, do-not-pull entries of every kind, downloaded emails, seen without outcome."""
    import sqlite3
    from app import importer
    client, fake, _ = env
    old = str(tmp_path / "leadpuller.db")
    con = sqlite3.connect(old)
    con.executescript("""
    CREATE TABLE seen (key TEXT PRIMARY KEY, pull_id INTEGER, created_at REAL);
    CREATE TABLE exclusions (key TEXT PRIMARY KEY, kind TEXT, value TEXT, source TEXT, created_at REAL);
    CREATE TABLE downloaded (email TEXT PRIMARY KEY, batch_id INTEGER, at REAL);
    CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, api_key TEXT, priority INTEGER DEFAULT 0,
      enabled INTEGER DEFAULT 1, monthly_budget INTEGER DEFAULT 0, source TEXT DEFAULT 'ui', balance INTEGER,
      balance_at REAL, last_error TEXT, created_at REAL);
    INSERT INTO seen VALUES ('li:linkedin.com/in/old-person-9', 1, 0), ('nc:jane doe|old co inc', 1, 0);
    INSERT INTO downloaded VALUES ('Sent.Before@Example.com', 1, 0);
    INSERT INTO exclusions VALUES ('em:boss@client.com','email','boss@client.com','ui',0),
      ('domain:client.com','domain','client.com','ui',0),
      ('li:linkedin.com/in/ceo-x','linkedin','https://www.linkedin.com/in/ceo-x/','ui',0),
      ('cn:rival corp','company_name','Rival Corp','ui',0),
      ('nc:sam lee|partner llc','name_company','Sam Lee @ Partner LLC','ui',0);
    INSERT INTO accounts(name, api_key, priority, monthly_budget) VALUES ('Account A', 'key-alpha-1111', 1, 0),
      ('Gaurav old', 'key-old-3333', 2, 4000);
    """)
    con.commit()
    con.close()
    out = importer.import_v1(old)
    assert out == {"people": 0, "emails": 1, "do_not_pull": 5, "accounts": 1}  # Account A was already there
    assert memory.find(db.person_keys("X", "Y", "https://linkedin.com/in/old-person-9")) 
    assert memory.find(db.person_keys("Jane Doe", "Old Co"))
    assert memory.find(db.person_keys("A B", "C", None, "sent.before@example.com"))
    block = memory.Blocklist()
    assert block.reason("Any One", "Whatever", "mail.client.com", None, None)
    assert block.reason("Any One", "Rival Corp", None, None, None)
    assert block.reason("Sam Lee", "Partner", None, None, None)
    assert block.reason("Z Z", "Q", None, "http://linkedin.com/in/ceo-x?trk=1", None)
    acc = db.q1("SELECT * FROM accounts WHERE api_key='key-old-3333'")
    assert acc["name"] == "Gaurav old" and acc["monthly_budget"] == 4000
    assert importer.import_v1(old)["do_not_pull"] == 0  # running it again adds nothing twice


def test_old_database_without_its_seen_table_is_still_imported(env, tmp_path):
    import shutil
    import sqlite3
    from app import importer
    old = tmp_path / "leadpuller.db"
    shutil.copy(os.path.join(os.path.dirname(__file__), "data", "leadpuller-v1.2.db"), old)
    con = sqlite3.connect(str(old))
    con.execute("DROP TABLE seen")
    con.commit()
    paid = con.execute("SELECT name, company, li_url, email FROM contacts WHERE submitted_at IS NOT NULL").fetchall()
    con.close()
    importer.import_v1(str(old))
    for name, company, li, email in paid:
        assert memory.find(db.person_keys(name, company, li, email)), name
