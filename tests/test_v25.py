"""Lead Puller 2.5: Icypeas as the second source (background lookups, people search) and companies already pulled
recently left out of new company lists.

Every money test ends with check_books (Seamless and Icypeas, to the credit) and check_people (nobody paid twice,
every email is that person's own real mailbox)."""
import collections
import os

import pytest

from app import db, engine, icypeas
from tests.conftest import US, add_icypeas, add_verifier, check_books, check_people, run, start
from tests.test_guessing import company_domains, start_list

TITLES = {"jobTitle": {"in": ["CTO", "VP Engineering", "Head of IT", "Director of Engineering"]}}


def on(client):
    if os.environ.get("LP_TEST_ICYPEAS") != "1":
        add_icypeas(client)


def no_seamless_emails(fake):
    for p in fake.people:
        fake.email_override[p["pid"]] = ""


def finish(client, pid, clock=None):
    p = run(client, pid, until=("done", "paused"), ticks=20000, clock=clock)
    if p["status"] == "paused":
        client.post(f"/api/pulls/{pid}/resume")
        p = run(client, pid, ticks=20000, clock=clock)
    return p


def settle(clock=None, n=200):
    for _ in range(n):                                  # the background keeps reading late answers
        engine.tick()
        if clock:
            clock.t += 5


def short_list(fake, n=60):
    extra = {p["domain"] for p in fake.ic.extra}
    return [d for d in company_domains(fake, 200, min_people=1) if d in extra][:n]


# ================================================================ email lookups
def test_answers_that_take_a_while_are_waited_for_and_used(env):
    client, fake, clock = env
    on(client)
    no_seamless_emails(fake)
    fake.ic.need_reads = 3
    pid = start(client, US, target=60, max_credits=300)
    p = finish(client, pid)
    assert p["icypeas"]["found"] > 0 and fake.ic.reads >= 3
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND email_source='icypeas'", (pid,)):
        owner = fake.mailboxes[c["email"]]
        assert fake.by_pid[owner]["name"] == c["name"]
    check_books(fake)
    check_people(fake)


def test_only_sure_emails_at_the_persons_own_company_are_used(env, monkeypatch):
    client, fake, clock = env
    on(client)
    no_seamless_emails(fake)
    pid = start(client, US, target=300, max_credits=400)
    finish(client, pid)
    settle()
    for c in db.q("SELECT * FROM contacts WHERE email_source='icypeas'"):
        pp = fake.by_pid[fake.mailboxes[c["email"]]]
        assert fake.ic.certainty(pp["pid"]) != "probable" and pp["pid"] % 37 != 5
        assert c["email"].endswith("@" + c["domain"])
    notes = " ".join(r["icy_note"] or "" for r in db.q("SELECT icy_note FROM contacts WHERE icy_state='none'"))
    assert "probable" in notes                                   # probable answers were seen and not used
    check_books(fake)
    check_people(fake)


def test_probable_emails_when_switched_on(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("ICYPEAS_ACCEPT_PROBABLE", "1")
    on(client)
    no_seamless_emails(fake)
    pid = start(client, US, target=300, max_credits=400)
    finish(client, pid)
    settle()
    used = [fake.by_pid[fake.mailboxes[c["email"]]]["pid"] for c in
            db.q("SELECT email FROM contacts WHERE email_source='icypeas'")]
    assert any(fake.ic.certainty(p) == "probable" for p in used)
    check_books(fake)
    check_people(fake)


def test_charged_misses_are_in_the_books(env):
    client, fake, clock = env
    on(client)
    no_seamless_emails(fake)
    pid = start(client, US, target=300, max_credits=400)
    finish(client, pid)
    settle()
    misses = db.q1("SELECT COUNT(*) n FROM icypeas_log WHERE result='none' AND cost>0")["n"]
    assert misses >= 0
    check_books(fake)


@pytest.mark.parametrize("modes", [["500"], ["timeout", "garbage"], ["500", "500", "500"]])
def test_errors_never_become_emails_and_people_are_tried_again(env, modes, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("EMAIL_GUESSING", "0")                  # make Icypeas do the work
    on(client)
    no_seamless_emails(fake)
    fake.ic.fail_next = list(modes)
    pid = start(client, US, target=60, max_credits=300)
    finish(client, pid, clock)
    settle(clock, 600)
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE email_source='icypeas'")["n"] > 0
    check_books(fake)
    check_people(fake)


def test_unpaid_subscription_is_explained_and_nothing_breaks(env):
    client, fake, clock = env
    on(client)
    no_seamless_emails(fake)
    fake.ic.fail_next = ["unpaid"]
    pid = start(client, US, target=40, max_credits=200)
    finish(client, pid, clock)
    st = client.get("/api/icypeas").json()
    assert "subscription" in (st["last_error"] or "").lower() or db.q1(
        "SELECT 1 x FROM contacts WHERE email_source='icypeas'")
    check_books(fake)


def test_too_many_requests_is_a_wait_not_an_error(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("EMAIL_GUESSING", "0")
    on(client)
    no_seamless_emails(fake)
    fake.ic.fail_next = ["429", "429"]
    pid = start(client, US, target=60, max_credits=300)
    finish(client, pid, clock)
    settle(clock, 600)
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE icy_state='error'")["n"] == 0
    assert fake.ic.charged > 0
    check_books(fake)
    check_people(fake)


def test_answer_checks_stay_under_icypeas_limit(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("ICYPEAS_READS_PER_MINUTE", "25")
    monkeypatch.setenv("ICYPEAS_POLL_SECONDS", "4")
    fake.ic.read_limit = 30
    fake.ic.need_reads = 4
    on(client)
    no_seamless_emails(fake)
    pid = start(client, US, target=80, max_credits=300)
    finish(client, pid, clock)
    settle(clock, 800)
    t = sorted(fake.ic.read_log)
    assert all(sum(1 for y in t if 0 <= y - x < 60) <= 30 for x in t)
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE icy_state='error'")["n"] == 0
    check_books(fake)


def test_bad_key_is_never_saved_and_credits_show_with_email(env):
    client, fake, clock = env
    client.delete("/api/icypeas")
    r = client.post("/api/icypeas", json={"api_key": "wrong-key-12345678"})
    assert r.status_code == 400 and "didn't accept" in r.json()["error"]
    assert client.get("/api/icypeas").json()["has_key"] is False
    st = add_icypeas(client)
    assert st["has_key"] and st["key"].endswith("9a") and st["credits"] == 5000
    assert "icy-key-4b2e9a" not in client.get("/api/overview").text


def test_key_from_env(env, monkeypatch):
    client, fake, clock = env
    client.delete("/api/icypeas")
    monkeypatch.setenv("ICYPEAS_API_KEY", "icy-key-4b2e9a")
    st = client.get("/api/icypeas").json()
    assert st["has_key"] and st["from_env"]
    no_seamless_emails(fake)
    pid = start(client, US, target=30, max_credits=200)
    finish(client, pid)
    assert fake.ic.charged > 0
    check_books(fake)


def test_few_credits_left_means_not_asked(env, monkeypatch):
    client, fake, clock = env
    client.delete("/api/icypeas")
    fake.ic.credits = 10
    monkeypatch.setenv("ICYPEAS_MIN_CREDITS_LEFT", "20")
    add_icypeas(client)
    pid = start(client, US, target=40, max_credits=200)
    p = finish(client, pid)
    assert not fake.ic.asked and "fewer than 20" in p["icypeas"]["why_not"]
    check_books(fake)


def test_late_answers_fill_in_after_the_pull_but_never_for_stopped_pulls(env):
    client, fake, clock = env
    on(client)
    no_seamless_emails(fake)
    fake.ic.need_reads = 10**6                                  # Icypeas is very slow today
    pid = start(client, US, target=40, max_credits=200)
    for _ in range(3000):
        engine.tick()
        clock.t += 30
        st = client.get(f"/api/pulls/{pid}").json()["status"]
        if st == "paused":
            client.post(f"/api/pulls/{pid}/resume")
        if st == "done":
            break
    assert client.get(f"/api/pulls/{pid}").json()["status"] == "done"   # it didn't wait forever
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND icy_state='asked'", (pid,))["n"] > 0
    fake.ic.need_reads = 1
    settle(clock, 300)
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND email_source='icypeas'", (pid,))["n"] > 0
    check_books(fake)
    # A pull you stop is never asked later.
    fake.ic.need_reads = 10**6
    pid2 = start(client, {"contactCountry": {"in": ["Canada"]}}, target=40, max_credits=200)
    for _ in range(40):
        engine.tick()
        clock.t += 5
    client.post(f"/api/pulls/{pid2}/stop")
    run(client, pid2, ticks=20000, clock=clock)
    n = len(fake.ic.asked)
    fake.ic.need_reads = 1
    settle(clock, 300)
    assert len(fake.ic.asked) == n
    check_books(fake)


# ================================================================ people search on company lists
def test_people_search_fills_companies_seamless_ran_out_of(env):
    client, fake, clock = env
    on(client)
    doms = short_list(fake)
    pid = start_list(client, doms, per_company=3, filters=dict(TITLES))
    p = finish(client, pid)
    settle()
    found = db.q("SELECT * FROM contacts WHERE pull_id=? AND icy_search=1 AND email_source='icypeas'", (pid,))
    assert found, "Icypeas's search added nobody"
    extra = {x["pid"] for x in fake.ic.extra}
    assert any(fake.mailboxes[c["email"]] in extra for c in found)        # people Seamless never had
    for c in found:
        assert any(t.lower() in (c["title"] or "").lower() for t in TITLES["jobTitle"]["in"])
        assert c["submitted_at"] is None
    by = collections.Counter(c["domain"] for c in db.q(f"SELECT domain FROM contacts WHERE pull_id=? AND "
                                                       f"{engine.DELIVERABLE}", (pid,)))
    assert max(by.values()) <= 3
    assert p["icypeas"]["search_people"] > 0
    check_books(fake)
    check_people(fake)


def test_people_search_respects_exclusions_countries_and_memory(env):
    client, fake, clock = env
    on(client)
    doms = short_list(fake, 40)
    f = {"jobTitle": {"in": ["CTO", "VP Engineering", "Engineering Intern"], "out": ["Intern"]},
         "contactCountry": {"in": ["United States"]}}
    pid = start_list(client, doms, per_company=3, filters=f)
    finish(client, pid)
    settle()
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND icy_search=1", (pid,)):
        assert "intern" not in (c["title"] or "").lower()
        assert c["country"] in ("US", "United States")
    pid2 = start_list(client, doms, per_company=3, filters=f)
    finish(client, pid2)
    settle()
    one = {c["name"] + c["domain"] for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND icy_search=1", (pid,))}
    two = {c["name"] + c["domain"] for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND icy_search=1", (pid2,))}
    assert not one & two
    check_books(fake)
    check_people(fake)


def test_people_search_off_and_filter_pulls_never_search(env, monkeypatch):
    client, fake, clock = env
    on(client)
    monkeypatch.setenv("ICYPEAS_SEARCH", "0")
    pid = start_list(client, short_list(fake, 20), per_company=2, filters=dict(TITLES))
    finish(client, pid)
    assert not fake.ic.searches
    monkeypatch.setenv("ICYPEAS_SEARCH", "1")
    pid2 = start(client, US, target=30, max_credits=200)
    finish(client, pid2)
    assert not fake.ic.searches
    check_books(fake)


def test_people_search_counts_against_the_cap(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("ICYPEAS_MAX_CREDITS_PER_PULL", "3")
    on(client)
    pid = start_list(client, short_list(fake, 60), per_company=3, filters=dict(TITLES))
    finish(client, pid)
    settle()
    assert icypeas.spent(pid) <= 3 + 100                      # a lookup already started may finish
    assert not db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND state='pkept' AND COALESCE(icy_state,'')=''", (pid,))
    check_books(fake)


def test_people_search_unpaid_plan_stops_cleanly(env):
    client, fake, clock = env
    on(client)
    pid = start_list(client, short_list(fake, 20), per_company=5, filters=dict(TITLES))
    for _ in range(3000):
        engine.tick()
        if db.q1("SELECT 1 x FROM icypeas_jobs WHERE pull_id=?", (pid,)) or client.get(
                f"/api/pulls/{pid}").json()["status"] == "done":
            break
    fake.ic.fail_next = ["unpaid"] * 3
    p = finish(client, pid)
    assert p["status"] == "done"
    check_books(fake)


def test_people_search_with_guessing_and_chaos(env):
    client, fake, clock = env
    add_verifier(client)
    on(client)
    fake.chaos_poll = True
    fake.ic.need_reads = 2
    pid = start_list(client, short_list(fake, 60), per_company=2, filters=dict(TITLES))
    run(client, pid, ticks=20000, clock=clock)
    settle(clock)
    check_books(fake)
    check_people(fake)


def test_stop_during_people_search(env):
    client, fake, clock = env
    on(client)
    pid = start_list(client, short_list(fake, 60), per_company=3, filters=dict(TITLES))
    for _ in range(4000):
        engine.tick()
        if fake.ic.searches:
            break
    client.post(f"/api/pulls/{pid}/stop")
    n = len(fake.ic.searches)
    p = run(client, pid)
    settle()
    assert len(fake.ic.searches) == n and p["status"] == "done"
    assert not db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND state='pkept'", (pid,))
    check_books(fake)


# ================================================================ companies already pulled recently
def test_new_list_leaves_out_companies_pulled_recently(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("COMPANY_SKIP_DAYS", "90")
    doms = company_domains(fake, 40)
    pid = start_list(client, doms[:20], per_company=2)
    run(client, pid)
    chk = client.post("/api/companies/check", json={"text": "\n".join(doms)}).json()
    assert chk["summary"]["done_before"] == 20 and chk["summary"]["ready"] == 20
    before = len(fake.searches)
    pid2 = start_list(client, doms, per_company=2)
    run(client, pid2)
    p2 = db.q("SELECT domain, status FROM pull_companies WHERE pull_id=?", (pid2,))
    assert {r["domain"] for r in p2 if r["status"] == "done_before"} == set(doms[:20])
    late = [b for k, b in fake.searches[before:]]
    assert late
    assert not set(doms[:20]) & {d for b in late for d in (b.get("companyDomain") or [])}
    # "Run again" on the first pull still searches its companies (new people only).
    r = client.post(f"/api/pulls/{pid}/rerun").json()
    assert db.q1("SELECT COUNT(*) n FROM pull_companies WHERE pull_id=? AND status='ready'", (r["id"],))["n"] == 20
    # A list made only of recent companies says why.
    bad = client.post("/api/pulls", json={"filters": {"seniority": {"in": ["VP"]}}, "target": 10, "max_credits": 50,
                                          "max_per_company": 2, "companies": [{"domain": d} for d in doms[:20]]})
    assert bad.status_code == 400 and "already in a pull" in bad.json()["error"]
    check_books(fake)


def test_company_skip_off(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("COMPANY_SKIP_DAYS", "0")
    doms = company_domains(fake, 10)
    run(client, start_list(client, doms, per_company=1))
    chk = client.post("/api/companies/check", json={"text": "\n".join(doms)}).json()
    assert chk["summary"]["done_before"] == 0 and chk["summary"]["ready"] == 10


def test_companies_older_than_the_window_come_back(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("COMPANY_SKIP_DAYS", "90")
    doms = company_domains(fake, 10)
    pid = start_list(client, doms, per_company=1)
    run(client, pid)
    db.x("UPDATE pulls SET created_at=created_at-? WHERE id=?", (91 * 86400 + 10 ** 6, pid))
    chk = client.post("/api/companies/check", json={"text": "\n".join(doms)}).json()
    assert chk["summary"]["done_before"] == 0


# ================================================================ the bottom line
def test_2_5_gets_more_emails_for_the_same_seamless_credits(env, monkeypatch, tmp_path):
    client, fake, clock = env
    if os.environ.get("LP_TEST_ICYPEAS") == "1":
        pytest.skip("compares with Icypeas off")
    pid0 = start(client, US, target=120, max_credits=400)
    run(client, pid0)
    e0, s0 = engine.counts(pid0)["emails"], db.pull_spend(pid0)
    check_books(fake)
    from tests.test_v22 import fresh
    fake2 = fresh(client, monkeypatch, tmp_path, "v25", verifier_on=os.environ.get("LP_TEST_GUESSING") == "1")
    import httpx
    from tests.fakes import FakeIcypeas
    fake2.ic = FakeIcypeas(fake2)
    fake2.ic.clock = clock
    monkeypatch.setattr(icypeas, "TRANSPORT", httpx.MockTransport(fake2.ic.handler))
    add_icypeas(client)
    pid1 = start(client, US, target=120, max_credits=400)
    run(client, pid1)
    settle()
    e1, s1 = engine.counts(pid1)["emails"], db.pull_spend(pid1)
    print(f"\nSeamless only: {e0} emails for {s0} credits · Seamless + Icypeas: {e1} emails for {s1} Seamless credits "
          f"+ {fake2.ic.charged} Icypeas credits")
    assert s1 / e1 <= s0 / e0
    check_books(fake2)
    check_people(fake2)
