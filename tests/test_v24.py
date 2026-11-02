"""Lead Puller 2.4: when Seamless has no more people at your companies, Prospeo's own database is searched for the
companies that are still short, and those people get Prospeo-verified emails.

Every test ends with check_books (Seamless and Prospeo, search pages included, to the credit) and check_people."""
import collections
import os

from app import db, engine, prospeo
from tests.conftest import add_prospeo, add_verifier, check_books, check_people, run
from tests.test_guessing import company_domains, start_list

TITLES = {"jobTitle": {"in": ["CTO", "VP Engineering", "Head of IT", "Director of Engineering"]}}


def on(client):
    if os.environ.get("LP_TEST_PROSPEO") != "1":
        add_prospeo(client)


def short_list(fake, n=60):
    """Companies where Prospeo knows extra people (every 3rd website), as a Hiring Signal Finder list."""
    extra = {p["domain"] for p in fake.pr.extra}
    doms = [d for d in company_domains(fake, 200, min_people=1) if d in extra]
    return doms[:n]


def per_company(pid):
    by = collections.Counter()
    for c in db.q(f"SELECT domain FROM contacts WHERE pull_id=? AND {engine.DELIVERABLE}", (pid,)):
        by[c["domain"]] += 1
    return by


def test_prospeo_search_fills_companies_seamless_ran_out_of(env):
    client, fake, clock = env
    on(client)
    doms = short_list(fake)
    pid = start_list(client, doms, per_company=3, filters=dict(TITLES))
    p = run(client, pid)
    found = db.q("SELECT * FROM contacts WHERE pull_id=? AND prospeo_pid IS NOT NULL AND email_source='prospeo'", (pid,))
    assert found, "Prospeo's search added nobody"
    assert p["prospeo"]["search_found"] == len(found) and p["prospeo"]["search_pages"] > 0
    extra = {x["pid"] for x in fake.pr.extra}
    for c in found:
        owner = fake.mailboxes[c["email"]]
        assert fake.by_pid[owner]["name"] == c["name"] and c["submitted_at"] is None
        assert any(t.lower() in (c["title"] or "").lower() for t in TITLES["jobTitle"]["in"])
    assert any(fake.mailboxes[c["email"]] in extra for c in found)    # people Seamless never had
    assert max(per_company(pid).values()) <= 3                        # never more than you asked for per company
    assert "Prospeo" in p["stop_reason"] or p["stats"]["emails"] >= p["target"]
    check_books(fake)
    check_people(fake)


def test_only_companies_that_are_still_short_are_searched(env):
    client, fake, clock = env
    on(client)
    doms = short_list(fake, 40)
    pid = start_list(client, doms, per_company=1, filters=dict(TITLES))
    run(client, pid)
    full_by_seamless = {c["domain"] for c in db.q(f"""SELECT domain FROM contacts WHERE pull_id=? AND {engine.DELIVERABLE}
                                                      AND prospeo_pid IS NULL""", (pid,))}
    searched = set()
    for body in fake.pr.search_calls:
        searched |= set(body["filters"]["company"]["websites"]["include"])
    assert not (searched & full_by_seamless), searched & full_by_seamless
    check_books(fake)


def test_search_respects_exclusions_countries_and_memory(env):
    client, fake, clock = env
    on(client)
    doms = short_list(fake, 40)
    f = {"jobTitle": {"in": ["CTO", "VP Engineering", "Engineering Intern"], "out": ["Intern"]},
         "contactCountry": {"in": ["United States"]}}
    pid = start_list(client, doms, per_company=3, filters=f)
    run(client, pid)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND prospeo_pid IS NOT NULL", (pid,)):
        assert "intern" not in (c["title"] or "").lower()
        assert c["country"] == "United States"
    # Run it again: everyone already found is skipped (new people only), nobody is looked up twice.
    pid2 = start_list(client, doms, per_company=3, filters=f)
    run(client, pid2)
    one = {c["prospeo_pid"] for c in db.q("SELECT prospeo_pid FROM contacts WHERE pull_id=? AND prospeo_pid IS NOT NULL",
                                          (pid,))}
    two = {c["prospeo_pid"] for c in db.q("SELECT prospeo_pid FROM contacts WHERE pull_id=? AND prospeo_pid IS NOT NULL",
                                          (pid2,))}
    assert not one & two
    emails = [r["email"] for r in db.q("SELECT email FROM contacts WHERE email_source='prospeo'")]
    assert len(emails) == len(set(emails))
    check_books(fake)
    check_people(fake)


def test_search_off_and_filter_pulls_never_search(env, monkeypatch):
    client, fake, clock = env
    on(client)
    monkeypatch.setenv("PROSPEO_SEARCH", "0")
    pid = start_list(client, short_list(fake, 20), per_company=2, filters=dict(TITLES))
    run(client, pid)
    assert not fake.pr.search_calls
    monkeypatch.setenv("PROSPEO_SEARCH", "1")
    from tests.conftest import US, start
    pid2 = start(client, US, target=30, max_credits=200)
    run(client, pid2)
    assert not fake.pr.search_calls                                   # only company lists are searched
    check_books(fake)


def test_plan_without_search_stops_cleanly(env):
    client, fake, clock = env
    on(client)
    fake.pr.search_plan_required = True
    pid = start_list(client, short_list(fake, 20), per_company=5, filters=dict(TITLES))
    p = run(client, pid)
    assert p["status"] == "done" and len(fake.pr.search_calls) == 1
    assert p["prospeo"]["search_stopped"] and "PLAN_REQUIRED" in p["prospeo"]["search_stopped"]
    check_books(fake)


def test_search_counts_against_the_prospeo_cap(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("PROSPEO_MAX_CREDITS_PER_PULL", "4")
    on(client)
    pid = start_list(client, short_list(fake, 60), per_company=3, filters=dict(TITLES))
    run(client, pid)
    assert prospeo.spent(pid) <= 4 + 3
    assert fake.pr.charged == db.q1("SELECT COALESCE(SUM(cost),0) s FROM prospeo_log")["s"]
    assert not db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND state='pkept'", (pid,))
    check_books(fake)


def test_search_without_titles_uses_seniority(env):
    client, fake, clock = env
    on(client)
    pid = start_list(client, short_list(fake, 20), per_company=2, filters={"seniority": {"in": ["C-Level"]}})
    run(client, pid)
    sen = [b["filters"].get("person_seniority") for b in fake.pr.search_calls]
    assert sen and all(s and set(s["include"]) == {"C-Suite", "Founder/Owner"} for s in sen)
    check_books(fake)
    check_people(fake)


def test_stop_during_search(env):
    client, fake, clock = env
    on(client)
    pid = start_list(client, short_list(fake, 60), per_company=3, filters=dict(TITLES))
    for _ in range(400):
        engine.tick()
        clock.t += 1
        if fake.pr.search_calls:
            break
    client.post(f"/api/pulls/{pid}/stop")
    n = len(fake.pr.search_calls)
    p = run(client, pid)
    assert len(fake.pr.search_calls) == n and p["status"] == "done"
    assert not db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND state='pkept'", (pid,))
    check_books(fake)


def test_search_with_guessing_and_chaos(env):
    client, fake, clock = env
    add_verifier(client)
    on(client)
    fake.chaos_poll = True
    pid = start_list(client, short_list(fake, 60), per_company=2, filters=dict(TITLES))
    run(client, pid, ticks=20000, clock=clock)
    check_books(fake)
    check_people(fake)


def test_people_found_by_search_are_in_downloads_and_lists(env):
    client, fake, clock = env
    on(client)
    pid = start_list(client, short_list(fake, 40), per_company=2, filters=dict(TITLES))
    run(client, pid)
    em = {c["email"] for c in db.q("SELECT email FROM contacts WHERE pull_id=? AND prospeo_pid IS NOT NULL AND "
                                     "email_source='prospeo'", (pid,))}
    body = client.get(f"/api/pulls/{pid}/download?kind=anavmail").content.decode("utf-8-sig")
    assert em and all(e in body for e in em)
    rows = client.get(f"/api/pulls/{pid}/people?view=emails&limit=250").json()["rows"]
    assert any(r["prospeo_pid"] for r in rows)
