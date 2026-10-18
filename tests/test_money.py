"""Money and data-safety tests. Every scenario ends by checking the books (credits logged == credits
Seamless charged) and the people (nobody paid for twice, nobody gets someone else's details)."""
import time

import pytest

from app import db, worker
from tests.conftest import (anavmail_rows, check_books, check_people, full_pull, new_pull, pid_from_email,
                            run_until)
from tests.fakes import email_for, make_people

US_CA = [p for p in make_people() if p["country"] in ("United States", "Canada")]


# ---------------------------------------------------------------- the normal path
def test_full_pull_books_and_people(env):
    client, fake, _, _ = env
    p = full_pull(client, target=120, cap=2, choice="high_medium")
    assert p["status"] == "done"
    check_books(fake)
    check_people(client, fake)
    # cap held, staffing skipped, low-fit not researched
    kept = db.q("SELECT company, domain, fit, submitted_at FROM contacts WHERE pull_id=? AND kept=1", (p["id"],))
    per = {}
    for r in kept:
        k = db.company_key(r["company"], r["domain"])
        per[k] = per.get(k, 0) + 1
        assert "Staffing" not in r["company"]
        if r["fit"] == "low":
            assert r["submitted_at"] is None
    assert max(per.values()) <= 2
    assert len(kept) == 120


def test_chaotic_seamless_answers(env):
    """Shuffled, partial and duplicated poll rows, rows missing searchResultId, 500s and 429s."""
    client, fake, _, _ = env
    fake.chaos_poll = True
    fake.flaky_rate = 0.15
    p = full_pull(client, target=150, cap=0)
    assert p["status"] == "done", p.get("error")
    check_books(fake)
    check_people(client, fake)
    res = db.q("SELECT research_status FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (p["id"],))
    assert all(r["research_status"] in ("done", "not found", "duplicate") for r in res)


def test_requestids_in_a_different_order_never_mix_people(env):
    """Seamless returns requestIds in another order AND some answers lack searchResultId."""
    client, fake, _, _ = env
    fake.chaos_poll = True
    fake.shuffle_request_ids = True
    p = full_pull(client, target=200, cap=0)
    assert p["status"] == "done"
    check_books(fake)
    check_people(client, fake)


def test_shared_email_appears_once_in_export(env):
    client, fake, _, _ = env
    for person in US_CA[:40]:
        fake.email_override[person["pid"]] = f"info.p{US_CA[0]['pid']}@{US_CA[0]['domain']}"
    p = full_pull(client, target=60, cap=0)
    emails = [r[0] for r in anavmail_rows(client, p["id"])]
    assert len(emails) == len(set(e.lower() for e in emails))
    assert emails.count(f"info.p{US_CA[0]['pid']}@{US_CA[0]['domain']}") <= 1


def test_same_name_different_companies_not_merged(env):
    client, fake, _, _ = env
    p = full_pull(client, target=200, cap=0)
    names = db.q("""SELECT name, COUNT(DISTINCT company) n FROM contacts WHERE pull_id=? AND kept=1
                    GROUP BY name HAVING n > 1""", (p["id"],))
    assert names, "test data should contain the same name at different companies"
    check_people(client, fake)


def test_same_company_name_different_domains_are_different_companies(env):
    client, fake, _, _ = env
    full_pull(client, target=250, cap=1)
    keys = {r["key"] for r in db.q("SELECT key FROM companies")}
    assert "d:acme6.com" in keys or "d:acme6-labs.io" in keys
    rows = db.q("SELECT domain FROM contacts WHERE kept=1 AND company='Acme Software 6'")
    assert len({r["domain"] for r in rows}) <= 2 and len(rows) <= 2  # one per real company


# ---------------------------------------------------------------- never pay twice
def test_never_pay_twice_across_pulls(env):
    client, fake, _, _ = env
    a = full_pull(client, target=100, cap=0, filters={"contactCountry": ["United States"], "seniority": ["C-Level", "VP"]})
    b = full_pull(client, target=100, cap=0, cooldown=0,
                  filters={"contactCountry": ["United States", "Canada"], "seniority": ["C-Level", "VP"]})
    check_books(fake)
    check_people(client, fake)
    a_people = {fake.sid_to_pid[r["search_result_id"]] for r in
                db.q("SELECT search_result_id FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (a["id"],))}
    b_people = {fake.sid_to_pid[r["search_result_id"]] for r in
                db.q("SELECT search_result_id FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (b["id"],))}
    assert not (a_people & b_people)
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_reason LIKE 'Already pulled before%'",
                 (b["id"],))["n"] > 0


def test_two_pulls_collected_at_once_dont_both_pay(env):
    """Both pulls collect the same people before either researches. Only the first may pay."""
    client, fake, _, _ = env
    f = {"contactCountry": ["United States"], "seniority": ["C-Level", "VP"]}
    ids = []
    for _ in range(2):
        p = new_pull(client)
        client.patch(f"/api/pulls/{p['id']}", json={"filters": f, "company_cooldown": 0})
        client.post(f"/api/pulls/{p['id']}/preview", json={"from_top": True})
        client.post(f"/api/pulls/{p['id']}/collect", json={"target": 60, "per_company_cap": 0})
        run_until(client, p["id"], {"collected"})
        ids.append(p["id"])
    for pid in ids:
        assert client.post(f"/api/pulls/{pid}/research", json={"choice": "all"}).status_code == 200
    for pid in ids:
        run_until(client, pid, {"done"})
    check_books(fake)
    check_people(client, fake)
    second = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_reason LIKE 'Already pulled before%'",
                   (ids[1],))["n"]
    assert second > 0


def test_linkedin_url_variants_are_the_same_person(env):
    a, b = db.person_keys("X Y", "Co", "https://www.linkedin.com/in/anna-4/"), \
        db.person_keys("X Y", "Co", "http://linkedin.com/in/anna-4?trk=abc")
    assert a[0] == b[0]


# ---------------------------------------------------------------- company memory and cooldown
def test_company_cooldown_skips_and_release(env):
    client, fake, _, _ = env
    f = {"contactCountry": ["United States"], "seniority": ["C-Level", "VP"]}
    a = full_pull(client, target=80, cap=0, filters=f)
    tried = {db.company_key(r["company"], r["domain"]) for r in
             db.q("SELECT company, domain FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (a["id"],))}
    a_companies = {db.company_key(r["company"], r["domain"]) for r in
                   db.q("""SELECT company, domain FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                           AND email IS NOT NULL AND email!=''""", (a["id"],))}
    no_email_only = tried - a_companies
    assert db.q1("SELECT COUNT(*) n FROM companies")["n"] == len(tried)
    assert db.q1("SELECT COUNT(*) n FROM companies WHERE last_at IS NOT NULL")["n"] == len(a_companies)
    assert no_email_only, "test data should have companies where nobody got an email"

    b = full_pull(client, target=60, cap=0, filters={"contactCountry": ["United States", "Canada"], "seniority": ["C-Level", "VP"]})
    b_kept = db.q("SELECT company, domain FROM contacts WHERE pull_id=? AND kept=1", (b["id"],))
    assert not ({db.company_key(r["company"], r["domain"]) for r in b_kept} & a_companies)
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_reason LIKE 'Company emailed%'",
                 (b["id"],))["n"] > 0
    # companies where nobody got an email are NOT on cooldown: their other people can come through
    b_all = {db.company_key(r["company"], r["domain"]) for r in
             db.q("SELECT company, domain FROM contacts WHERE pull_id=?", (b["id"],))}
    for r in db.q("SELECT company, domain, skip_reason FROM contacts WHERE pull_id=?", (b["id"],)):
        if db.company_key(r["company"], r["domain"]) in no_email_only:
            assert not (r["skip_reason"] or "").startswith("Company emailed")

    # release one company -> it can come back; block one -> never
    rel = sorted(a_companies)[0]
    blk = [k for k in sorted(a_companies) if k.startswith("d:")][1]
    assert client.post("/api/companies/release", json={"key": rel}).status_code == 200
    assert client.post("/api/companies/block", json={"key": blk}).status_code == 200
    lst = client.get("/api/companies?status=all&limit=200").json()
    row = {r["key"]: r for r in lst["rows"]}
    assert row[rel]["on_cooldown"] is False and row[blk]["blocked"] is True
    c = new_pull(client)
    client.patch(f"/api/pulls/{c['id']}", json={"filters": f})
    client.post(f"/api/pulls/{c['id']}/preview", json={"from_top": True})
    client.post(f"/api/pulls/{c['id']}/collect", json={"target": 400, "per_company_cap": 0})
    run_until(client, c["id"], {"collected"})
    kept_keys = {db.company_key(r["company"], r["domain"]) for r in
                 db.q("SELECT company, domain FROM contacts WHERE pull_id=? AND kept=1", (c["id"],))}
    assert blk not in kept_keys
    assert (kept_keys & a_companies) <= {rel}
    check_people(client, fake)


def test_cooldown_expires(env):
    client, fake, _, _ = env
    f = {"contactCountry": ["United States"], "seniority": ["C-Level", "VP"]}
    full_pull(client, target=40, cap=0, filters=f)
    db.x("UPDATE companies SET last_at=last_at-?", (91 * 86400,))
    c = new_pull(client)
    client.patch(f"/api/pulls/{c['id']}", json={"filters": f})
    client.post(f"/api/pulls/{c['id']}/preview", json={"from_top": True})
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_reason LIKE 'Company emailed%'",
                 (c["id"],))["n"] == 0


def test_cooldown_zero_means_off(env, monkeypatch):
    client, fake, _, _ = env
    f = {"contactCountry": ["United States"], "seniority": ["C-Level", "VP"]}
    full_pull(client, target=40, cap=0, filters=f)
    monkeypatch.setenv("COMPANY_COOLDOWN_DAYS", "0")
    c = new_pull(client)
    assert c["company_cooldown"] == 0
    client.patch(f"/api/pulls/{c['id']}", json={"filters": f})
    client.post(f"/api/pulls/{c['id']}/preview", json={"from_top": True})
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_reason LIKE 'Company emailed%'",
                 (c["id"],))["n"] == 0


# ---------------------------------------------------------------- search memory
def test_same_search_continues_where_it_stopped(env):
    client, fake, _, _ = env
    a = full_pull(client, target=60, cap=0)
    spent_a = fake.charges.copy()
    b = new_pull(client)
    b = client.post(f"/api/pulls/{b['id']}/preview", json={}).json()
    assert b["started_from"] and b["started_from"].startswith("Same search as pull #%d" % a["id"])
    # nobody in the preview is a person already pulled -> no credits wasted re-buying known people
    # (people at companies already pulled can still appear further down the list; those are skipped by cooldown)
    skipped_known = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_reason LIKE 'Already pulled%'",
                          (b["id"],))["n"]
    assert skipped_known == 0
    # "start from the top" goes back to the beginning (and then skips known people)
    b = client.post(f"/api/pulls/{b['id']}/preview", json={"from_top": True}).json()
    assert not b["started_from"]
    assert db.q1("""SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND
                    (skip_reason LIKE 'Already pulled%' OR skip_reason LIKE 'Company emailed%')""", (b["id"],))["n"] > 0


def test_expired_saved_position_falls_back_to_top(env):
    client, fake, _, clock = env
    full_pull(client, target=30, cap=0)
    fake.token_ttl = 60
    clock.t += 3600
    b = new_pull(client)
    b = client.post(f"/api/pulls/{b['id']}/preview", json={}).json()
    assert b["status"] == "previewed"
    assert "no longer accepted" in b["started_from"]
    check_people(client, fake)


def test_end_of_search_starts_from_top(env):
    client, fake, _, _ = env
    f = {"contactCountry": ["United States"], "jobTitle": ["Head of IT"]}
    full_pull(client, target=500, cap=0, filters=f, cooldown=0)
    b = new_pull(client)
    client.patch(f"/api/pulls/{b['id']}", json={"filters": f, "company_cooldown": 0})
    b = client.post(f"/api/pulls/{b['id']}/preview", json={}).json()
    assert "went through every result" in b["started_from"]
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND kept=1", (b["id"],))["n"] == 0


# ---------------------------------------------------------------- failures that could cost money
@pytest.mark.parametrize("mode", ["500_after_charge", "timeout_after_charge"])
def test_unconfirmed_research_is_never_resent_automatically(env, mode):
    client, fake, _, _ = env
    p = new_pull(client)
    pid = p["id"]
    client.post(f"/api/pulls/{pid}/preview", json={})
    client.post(f"/api/pulls/{pid}/collect", json={"target": 150, "per_company_cap": 0})
    run_until(client, pid, {"collected"})
    client.post(f"/api/pulls/{pid}/research", json={"choice": "all"})
    fake.research_fail_next = mode
    p = run_until(client, pid, {"paused", "done"})
    assert p["status"] == "paused" and "did not resend" in p["error"]
    research_calls = [c for c in fake.calls if c[2] == "/contacts/research"]
    assert len(research_calls) == 1
    for _ in range(20):
        worker.tick()          # paused: must not resend on its own
    assert len([c for c in fake.calls if c[2] == "/contacts/research"]) == 1
    charged_once = fake.charged
    client.post(f"/api/pulls/{pid}/resume")
    p = run_until(client, pid, {"done"})
    # the resend is answered "duplicate" and not charged; the second batch is charged normally
    dups = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND research_status='duplicate'", (pid,))["n"]
    assert dups > 0
    first_batch = 100
    assert dups >= first_batch                              # the resent first batch came back as duplicates
    assert fake.charged - charged_once == p["stats"]["submitted"] - first_batch   # only the new people were charged
    # The tool didn't see the first charge, so its ledger is short by exactly that batch -- the
    # live Seamless balance (shown everywhere) is the truth. Fill-in recovers the details free.
    r = client.post(f"/api/pulls/{pid}/fill-saved").json()
    assert r["fill_result"]["filled"] > 0
    check_people(client, fake)


def test_research_500_before_charge_pauses_and_resumes_cleanly(env):
    client, fake, _, _ = env
    p = new_pull(client)
    pid = p["id"]
    client.post(f"/api/pulls/{pid}/preview", json={})
    client.post(f"/api/pulls/{pid}/collect", json={"target": 50, "per_company_cap": 0})
    run_until(client, pid, {"collected"})
    client.post(f"/api/pulls/{pid}/research", json={"choice": "all"})
    fake.research_fail_next = "500_before_charge"
    p = run_until(client, pid, {"paused"})
    client.post(f"/api/pulls/{pid}/resume")
    run_until(client, pid, {"done"})
    check_books(fake)
    check_people(client, fake)


def test_out_of_credits_mid_research_pauses_then_resumes(env):
    client, fake, _, _ = env
    p = new_pull(client)
    pid = p["id"]
    client.post(f"/api/pulls/{pid}/preview", json={})
    client.post(f"/api/pulls/{pid}/collect", json={"target": 150, "per_company_cap": 0})
    run_until(client, pid, {"collected"})
    fake.balance = 120
    client.post(f"/api/pulls/{pid}/research", json={"choice": "all"})
    p = run_until(client, pid, {"paused", "done"})
    assert p["status"] == "paused" and "credits" in p["error"].lower()
    fake.balance = 1000
    client.post(f"/api/pulls/{pid}/resume")
    p = run_until(client, pid, {"done"})
    assert p["stats"]["submitted"] == p["stats"]["selected"]
    check_books(fake)
    check_people(client, fake)


def test_monthly_budget_is_never_exceeded(env, monkeypatch):
    client, fake, _, _ = env
    monkeypatch.setenv("MONTHLY_CREDIT_BUDGET", "100")
    p = new_pull(client)
    pid = p["id"]
    client.post(f"/api/pulls/{pid}/preview", json={})
    client.post(f"/api/pulls/{pid}/collect", json={"target": 300, "per_company_cap": 0})
    p = run_until(client, pid, {"collected", "paused"})
    assert fake.charged <= 100
    if p["status"] == "collected":
        r = client.post(f"/api/pulls/{pid}/research", json={"choice": "all"})
        assert r.status_code == 400 and "budget" in r.json()["error"]
        r = client.post(f"/api/pulls/{pid}/research", json={"choice": "first_n", "first_n": 100 - fake.charged})
        assert r.status_code == 200
        run_until(client, pid, {"done", "paused"})
    assert fake.charged <= 100
    check_people(client, fake)


def test_low_balance_short_search_page(env):
    client, fake, _, _ = env
    fake.balance = 2           # can pay for 20 rows only
    p = new_pull(client)
    p = client.post(f"/api/pulls/{p['id']}/preview", json={}).json()
    assert p["stats"]["seen"] == 20 and fake.charged == 2
    check_books(fake)


def test_pause_and_stop_never_charge_extra(env):
    client, fake, _, _ = env
    p = new_pull(client)
    pid = p["id"]
    client.post(f"/api/pulls/{pid}/preview", json={})
    client.post(f"/api/pulls/{pid}/collect", json={"target": 250, "per_company_cap": 0})
    run_until(client, pid, {"collected"})
    client.post(f"/api/pulls/{pid}/research", json={"choice": "all"})
    worker.tick()                                  # first batch of 100 sent
    client.post(f"/api/pulls/{pid}/pause")
    before = fake.charged
    for _ in range(10):
        worker.tick()
    assert fake.charged == before
    client.post(f"/api/pulls/{pid}/resume")
    client.post(f"/api/pulls/{pid}/cancel")        # stop: finish the 100 already sent, send no more
    p = run_until(client, pid, {"done"})
    assert p["stats"]["submitted"] == 100
    check_books(fake)
    check_people(client, fake)


def test_restart_in_the_middle(env):
    """Server restarts while research is running: nothing lost, nothing paid twice."""
    client, fake, _, _ = env
    p = new_pull(client)
    pid = p["id"]
    client.post(f"/api/pulls/{pid}/preview", json={})
    client.post(f"/api/pulls/{pid}/collect", json={"target": 220, "per_company_cap": 0})
    run_until(client, pid, {"collected"})
    client.post(f"/api/pulls/{pid}/research", json={"choice": "all"})
    worker.tick()
    worker.tick()
    path = db.DB_PATH
    db.reset_for_tests(path)        # drop the connection, like a process restart
    worker._last_poll.clear()
    worker._failures.clear()
    p = run_until(client, pid, {"done"})
    assert p["stats"]["submitted"] == p["stats"]["selected"]
    check_books(fake)
    check_people(client, fake)


# ---------------------------------------------------------------- already-researched people
def test_fill_in_from_saved_contacts_is_strict(env):
    client, fake, _, _ = env
    pool = [p for p in US_CA if p["title"] in ("Chief Technology Officer", "CTO", "VP Engineering")]
    fake.already_in_org = {p["pid"] for p in pool[:40]}
    # decoys: same name + company as a real saved contact but a different person/email and no LinkedIn
    for p in pool[:40]:
        if not p["liUrl"]:
            d = fake.contact_record(p)
            d.update(email=f"decoy.p{p['pid'] + 100000}@other.example", email1=None, lIProfileUrl="")
            fake.org_decoys.append(d)
    # a decoy with the same LinkedIn but a different email -> ambiguous -> must not be used
    amb = next(p for p in pool[:40] if p["liUrl"])
    d = fake.contact_record(amb)
    d.update(email=f"wrong.p{amb['pid'] + 200000}@other.example")
    fake.org_decoys.append(d)

    p = full_pull(client, target=150, cap=0)
    dups = db.q("SELECT * FROM contacts WHERE pull_id=? AND research_status='duplicate'", (p["id"],))
    assert dups and all(not d["email"] for d in dups)
    check_books(fake)     # duplicates were logged then credited back, matching Seamless (no charge)
    r = client.post(f"/api/pulls/{p['id']}/fill-saved").json()["fill_result"]
    assert 0 < r["filled"] < r["checked"]
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND research_status='duplicate'", (p["id"],)):
        pid = fake.sid_to_pid[c["search_result_id"]]
        if c["email"]:
            assert pid_from_email(c["email"]) == pid
        if pid == amb["pid"]:
            assert not c["email"] or pid_from_email(c["email"]) == pid
    check_people(client, fake)


# ---------------------------------------------------------------- exports
def test_export_rules(env):
    client, fake, _, _ = env
    p = full_pull(client, target=150, cap=0)
    rows = anavmail_rows(client, p["id"])
    strict = anavmail_rows(client, p["id"], risky=0)
    assert len(strict) < len(rows)
    statuses = {r["email"].lower(): r["email_status"] for r in
                db.q("SELECT email, email_status FROM contacts WHERE pull_id=? AND email IS NOT NULL", (p["id"],))}
    assert all(statuses[r[0].lower()] != "invalid" for r in rows)
    assert all(statuses[r[0].lower()] == "valid" for r in strict)
    # add a domain to the do-not-pull list after the fact -> dropped from the export
    dom = rows[0][0].split("@")[1]
    client.post("/api/exclusions", json={"text": dom})
    assert all(r[0].split("@")[1] != dom for r in anavmail_rows(client, p["id"]))
    check_people(client, fake)


def test_spreadsheet_formula_injection_neutralised(env):
    client, fake, _, _ = env
    for person in fake.people[:30]:
        person["title"] = '=HYPERLINK("http://evil.example","x")'
    p = full_pull(client, target=20, cap=0)
    for r in anavmail_rows(client, p["id"]):
        assert not r[4].startswith("=")
    full = client.get(f"/api/pulls/{p['id']}/export?format=full").content.decode("utf-8-sig")
    assert ',=HYPERLINK' not in full


# ---------------------------------------------------------------- rate limit
def test_never_exceeds_rate_limit(env, monkeypatch):
    client, fake, _, _ = env
    monkeypatch.setenv("PAGE_SIZE", "10")
    monkeypatch.setenv("RESEARCH_BATCH", "5")
    fake.people = make_people(1500)
    fake.by_pid = {p["pid"]: p for p in fake.people}
    fake.chaos_poll = True
    full_pull(client, target=700, cap=0)
    assert max(sum(1 for c in fake.calls if c[2] == ep) for ep in ("/search/contacts", "/contacts/research")) > 60
    from collections import defaultdict
    by_ep = defaultdict(list)
    for t, m, path in fake.calls:
        by_ep[path].append(t)
    for path, ts in by_ep.items():
        ts.sort()
        j = 0
        for i in range(len(ts)):
            while ts[i] - ts[j] >= 60:
                j += 1
            assert i - j + 1 <= 55, f"{path}: {i - j + 1} calls in one minute"


# ---------------------------------------------------------------- inputs from Claude / locations
def test_bad_filters_from_claude_are_dropped(env):
    client, fake, claude_fake, _ = env
    claude_fake.filters = {"industry": ["Software Developmentt"], "seniority": ["CEO"], "companySize": ["50-500"],
                           "contactCountry": ["Isreal", "Germany"], "jobTitle": ["CTO"] * 3 + [""]}
    p = new_pull(client)
    assert p["filters"] == {"contactCountry": ["Isreal", "Germany"], "jobTitle": ["CTO"]}
    p = client.post(f"/api/pulls/{p['id']}/preview", json={}).json()
    assert p["filters"]["contactCountry"] == ["Germany"]
    assert any("Isreal" in n for n in p["notes"])


def test_fit_ratings_matched_by_id_not_order(env):
    client, fake, _, _ = env
    p = new_pull(client)
    client.post(f"/api/pulls/{p['id']}/preview", json={})
    for r in db.q("SELECT title, fit FROM contacts WHERE pull_id=? AND kept=1", (p["id"],)):
        bad = "Intern" in r["title"] or "Recruiter" in r["title"]
        assert r["fit"] == ("low" if bad else "high")


def test_claude_down_still_works(env):
    client, fake, claude_fake, _ = env
    p = new_pull(client)
    claude_fake.fail = True
    p = client.post(f"/api/pulls/{p['id']}/preview", json={}).json()
    assert p["status"] == "previewed" and "Fit check skipped" in (p["error"] or "")


def test_model_that_rejects_forced_tool_use(env):
    """Seen live: 'tool_choice: type "tool" and "any" are not supported for this model.'"""
    from app import claude
    client, fake, claude_fake, _ = env
    claude._NO_FORCED_TOOL.clear()
    claude_fake.no_forced_tool = True
    assert client.post("/api/settings/test-claude", json={}).status_code == 200
    p = new_pull(client)
    assert p["filters"]["contactCountry"] == ["United States", "Canada"]
    p = client.post(f"/api/pulls/{p['id']}/preview", json={}).json()
    assert p["status"] == "previewed" and not p["error"]          # fit check worked too
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND fit IS NOT NULL", (p["id"],))["n"] > 0
    claude_fake.text_answer = True                                 # model answers in text, not the tool
    p = new_pull(client)
    assert p["filters"]["contactCountry"] == ["United States", "Canada"]
    claude._NO_FORCED_TOOL.clear()
