"""v1.2 features: downloads (batches, columns, country split, no-email file), email picking, fewer no-email
people (website / LinkedIn / email server / freshness / test batch / email chance / charge check), new filters
and native excludes, saved searches, retries, blocked companies, stop buttons, spending switch, upgrades."""
import csv
import io
import json
import sqlite3
import time

import pytest

from app import db, dnscheck, worker
from tests.conftest import (batch_rows, check_batches, check_books, check_people, full_pull, new_pull, pid_from_email, run_until)
from tests.fakes import email_for, make_people

PEOPLE = make_people()
US = {"contactCountry": ["United States"], "seniority": ["C-Level", "VP"]}


def collect(client, filters, target=100, cap=0, cooldown=None, from_top=True):
    p = new_pull(client)
    patch = {"filters": filters}
    if cooldown is not None:
        patch["company_cooldown"] = cooldown
    r = client.patch(f"/api/pulls/{p['id']}", json=patch)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/pulls/{p['id']}/preview", json={"from_top": from_top})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/pulls/{p['id']}/collect", json={"target": target, "per_company_cap": cap})
    assert r.status_code == 200, r.text
    return run_until(client, p["id"], {"collected"})


def research(client, pid, choice="all", **kw):
    r = client.post(f"/api/pulls/{pid}/research", json={"choice": choice, **kw})
    assert r.status_code == 200, r.text
    return run_until(client, pid, {"done", "paused"})


def person(c, fake):
    return fake.by_pid[fake.sid_to_pid[c["search_result_id"]]]


# ================================================================ downloads
def test_batches_never_overlap_and_remember_columns(env):
    client, fake, _, _ = env
    p = full_pull(client, target=150, cap=0)
    pid = p["id"]
    left0 = p["stats"]["left_to_download"]
    assert left0 > 60
    r = client.post(f"/api/pulls/{pid}/batches", json={"count": 25, "columns": ["first_name", "company", "title"]})
    assert r.status_code == 200, r.text
    b1 = r.json()["batch_id"]
    rows1 = batch_rows(client, b1)
    assert len(rows1) == 25
    assert list(rows1[0].keys()) == ["email", "first_name", "company", "title"]   # email always added, first
    r = client.post(f"/api/pulls/{pid}/batches", json={"count": 30})
    b2 = r.json()["batch_id"]
    rows2 = batch_rows(client, b2)
    assert list(rows2[0].keys()) == ["email", "first_name", "last_name", "company"]  # .env default
    assert not ({x["email"] for x in rows1} & {x["email"] for x in rows2})
    assert batch_rows(client, b1) == rows1                                            # re-download identical
    stats = client.get(f"/api/pulls/{pid}").json()["stats"]
    assert stats["left_to_download"] == left0 - 55 and stats["downloaded"] == 55
    # asking for more than is left gives what is left, then a clear error
    r = client.post(f"/api/pulls/{pid}/batches", json={"count": 100000})
    assert r.status_code == 200
    r = client.post(f"/api/pulls/{pid}/batches", json={"count": 10})
    assert r.status_code == 400 and "Nobody left" in r.json()["error"]
    lst = client.get(f"/api/pulls/{pid}/batches").json()["rows"]
    assert [b["count"] for b in lst][-2:] == [30, 25]
    check_people(client, fake)


def test_batches_risky_toggle_invalid_never_and_exclusions(env):
    client, fake, _, _ = env
    p = full_pull(client, target=150, cap=0)
    pid = p["id"]
    dom = db.q1("SELECT domain FROM contacts WHERE pull_id=? AND email IS NOT NULL AND email!='' LIMIT 1", (pid,))["domain"]
    client.post("/api/exclusions", json={"text": dom})
    bid = client.post(f"/api/pulls/{pid}/batches", json={"count": 1000, "include_risky": False}).json()["batch_id"]
    for r in batch_rows(client, bid):
        c = db.q1("SELECT email_status, domain FROM contacts WHERE LOWER(email)=?", (r["email"].lower(),))
        assert c["email_status"] == "valid"
        assert r["email"].split("@")[1] != dom
    bid2 = client.post(f"/api/pulls/{pid}/batches", json={"count": 1000, "include_risky": True}).json()["batch_id"]
    statuses = {db.q1("SELECT email_status FROM contacts WHERE LOWER(email)=?", (r["email"].lower(),))["email_status"]
                for r in batch_rows(client, bid2)}
    assert statuses == {"risky"}
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE email_status='invalid' AND batch_id IS NOT NULL")["n"] == 0
    check_people(client, fake)


def test_same_email_never_goes_out_twice_across_pulls(env):
    client, fake, _, _ = env
    shared = f"info.p{PEOPLE[1]['pid']}@{PEOPLE[1]['domain']}"
    us = [p for p in PEOPLE if p["country"] == "United States"]
    for x in us[:80]:
        fake.email_override[x["pid"]] = shared if x["pid"] % 2 else fake.email_override.get(x["pid"])
    a = full_pull(client, target=40, cap=0, filters=US)
    client.post(f"/api/pulls/{a['id']}/batches", json={"count": 1000})
    b = full_pull(client, target=40, cap=0, cooldown=0, filters={"contactCountry": ["United States"],
                                                                   "seniority": ["C-Level", "VP"]})
    client.post(f"/api/pulls/{b['id']}/batches", json={"count": 1000})
    emails = [r["email"] for r in db.q("SELECT email FROM downloaded")]
    assert emails.count(shared.lower()) == 1
    holders = db.q("SELECT pull_id FROM contacts WHERE LOWER(email)=?", (shared.lower(),))
    assert len({h["pull_id"] for h in holders}) == 2      # the shared email really was in both pulls
    check_batches(client, fake)


def test_country_split_zip(env):
    client, fake, _, _ = env
    p = full_pull(client, target=120, cap=0)
    bid = client.post(f"/api/pulls/{p['id']}/batches", json={"count": 80, "split": "country",
                                                             "columns": ["email", "country"]}).json()["batch_id"]
    files = batch_rows(client, bid)
    assert isinstance(files, dict) and len(files) >= 2
    total = 0
    for name, rows in files.items():
        countries = {r["country"] for r in rows}
        assert len(countries) == 1 and next(iter(countries)).replace(" ", "-") in name
        total += len(rows)
    assert total == 80
    check_people(client, fake)


def test_column_sets_and_full_export(env):
    client, fake, _, _ = env
    r = client.post("/api/column-sets", json={"name": "AnavAgent", "columns": ["email", "first_name", "company", "bogus"]})
    assert r.json()["rows"][0]["columns"] == ["email", "first_name", "company"]
    r = client.post("/api/column-sets", json={"name": "AnavAgent", "columns": ["email", "company"]})
    assert len(r.json()["rows"]) == 1 and r.json()["rows"][0]["columns"] == ["email", "company"]
    assert client.post("/api/column-sets", json={"name": "x", "columns": ["nope"]}).status_code == 400
    meta = client.get("/api/meta").json()
    assert len(meta["columns"]) >= 70 and meta["defaults"]["columns"] == ["email", "first_name", "last_name", "company"]
    p = full_pull(client, target=60, cap=0)
    text = client.get(f"/api/pulls/{p['id']}/export?format=full").content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows[0]) >= 70
    for r in rows:                                   # every row is the right person, formulas neutralised
        if r["email"]:
            pid = pid_from_email(r["email"])
            assert r["first_name"] == fake.by_pid[pid]["first"] and r["company"] == fake.by_pid[pid]["company"]
            assert r["phone"] == f"415.555.{pid:04d}"
        assert not r["news"].startswith("=")
    assert any("=cmd" in r["news"] for r in rows)                 # planted formula present, but never at cell start
    text = client.get(f"/api/pulls/{p['id']}/export?format=full&columns=email,funding,time_zone").content.decode("utf-8-sig")
    assert text.splitlines()[0] == "email,funding,time_zone"
    assert client.get(f"/api/pulls/{p['id']}/export?format=xlsx").status_code == 200


def test_no_email_download(env):
    client, fake, _, _ = env
    p = full_pull(client, target=200, cap=0, filters={"contactCountry": ["Germany", "Israel", "United States"],
                                                      "seniority": ["C-Level", "VP"]})
    text = client.get(f"/api/pulls/{p['id']}/export?format=no_email").content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows and len(rows) == p["stats"]["no_email"]
    assert "email" not in rows[0]
    for r in rows:
        c = db.q1("SELECT * FROM contacts WHERE pull_id=? AND li_url=? AND first_name=?", (p["id"], r["linkedin"], r["first_name"])) \
            if r["linkedin"] else None
        if c:
            assert not c["email"]
            assert r["company"] == person(c, fake)["company"]


# ================================================================ email picking
def test_best_email_is_picked_and_belongs_to_the_person(env, monkeypatch):
    client, fake, _, _ = env
    us = [p for p in PEOPLE if p["country"] == "United States" and p["pid"] % 11 and p["pid"] % 23]
    better = {}
    for x in us[:60]:
        alt = f"alt.{x['first'].lower()[:1]}.p{x['pid']}@{x['domain'] or 'nodomain.example'}"
        better[x["pid"]] = alt
        fake.alt_emails[x["pid"]] = {"email1EmailAI": "risky", "email1TotalAI": "90%",
                                     "email2": alt, "email2EmailAI": "valid", "email2TotalAI": "80%"}
    p = full_pull(client, target=120, cap=0, filters=US)
    picked = 0
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND email IS NOT NULL", (p["id"],)):
        pid = fake.sid_to_pid[c["search_result_id"]]
        assert pid_from_email(c["email"]) == pid
        if pid in better:
            assert c["email"] == better[pid] and c["email_status"] == "valid" and c["email_pick"] == "email 2"
            picked += 1
    assert picked > 10
    monkeypatch.setenv("EMAIL_PICK", "seamless")
    fake2 = worker.pick_email({"email": "a@x.com", "email1": "a@x.com", "email1EmailAI": "risky",
                               "email2": "b@x.com", "email2EmailAI": "valid"})
    assert fake2[0] == "a@x.com"
    check_people(client, fake)


# ================================================================ fewer people without email
def test_website_linkedin_and_email_server_skips(env):
    client, fake, _, _ = env
    no_mx = {"acme13.com", "acme26.com", "acme39.com", "acme52.com"}
    calls = []

    def lookup(d):
        calls.append(d)
        return "no" if d in no_mx else ("unknown" if d == "acme40.com" else "yes")
    dnscheck.LOOKUP = lookup
    f = dict(US, requireCompanyWebsite=True, requireLinkedIn=True)
    p = collect(client, f, target=200)
    rows = db.q("SELECT * FROM contacts WHERE pull_id=?", (p["id"],))
    for c in rows:
        if c["kept"]:
            assert c["domain"] and c["li_url"] and c["domain"] not in no_mx
        elif not c["domain"]:
            assert c["skip_reason"] == "No company website"
    assert any(c["skip_reason"] == "Company domain can't receive email" for c in rows)
    assert any(c["domain"] == "acme40.com" and c["kept"] for c in rows)       # unknown = kept, never skipped
    n_calls = len(calls)
    collect(client, f, target=50, cooldown=0)
    assert len(calls) - n_calls <= 2           # yes/no answers are cached; only "unknown" is asked again
    research(client, p["id"])
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE submitted_at IS NOT NULL AND domain IN ('acme13.com','acme26.com')")["n"] == 0
    check_people(client, fake)


def test_whole_word_matching():
    assert worker.has_words("Co-founder & CTO", "CTO")
    assert not worker.has_words("Director of Engineering", "CTO")
    assert not worker.has_words("International Sales Lead", "intern")
    assert worker.has_words("Engineering Intern", "intern")
    assert worker.has_words("Chief Technology Officer", "chief technology")
    assert worker.has_words("Staffing Partners 5", "staffing")


def test_title_must_contain_role_time_and_skip_seniority(env):
    client, fake, _, _ = env
    f = dict(US, titleMustContain=["CTO", "Chief Technology"], roleMinMonths=3, excludeSeniority=["VP"])
    p = collect(client, f, target=200)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND kept=1", (p["id"],)):
        assert "CTO" in c["title"] or "Chief Technology" in c["title"]
        assert c["seniority"] != "VP"
        assert c["title_started_at"] != "2026-09-01"
    reasons = {r["reason"] for r in p["stats"]["skip_reasons"]}
    assert any(r.startswith("Title doesn't contain") for r in reasons)
    assert "In current role under 3 months" in reasons


def test_native_excludes_and_freshness_go_to_seamless(env):
    client, fake, _, _ = env
    f = {"contactCountry": ["United States", "Germany", "Canada"], "excludeCountries": ["Germany"],
         "seniority": ["C-Level", "VP"], "updatedWithinMonths": 12, "titlesExactMatch": True,
         "jobTitle": ["CTO", "VP Engineering"], "pastCompany": ["Wix"], "pastCompanyRecentOnly": True}
    p = collect(client, f, target=100)
    body = fake.searches[-1]
    assert body["contactCountry"] == ["United States", "Germany", "Canada", "-Germany"]
    assert body["lastModifiedAfter"].endswith("-01T00:00:00Z") and body["titlesExactMatch"] is True
    assert body["pastCompany"] == {"names": ["Wix"], "onlyMostRecentDeparture": True}
    for c in db.q("SELECT * FROM contacts WHERE pull_id=?", (p["id"],)):
        pp = person(c, fake)
        assert pp["country"] != "Germany" and pp["lastModifiedAt"] >= "2025"
        assert pp["title"] in ("CTO", "VP Engineering")


def test_test_batch_pauses_when_too_few_emails_then_continue_or_stop(env, monkeypatch):
    client, fake, _, _ = env
    monkeypatch.setenv("TEST_BATCH_SIZE", "40")
    monkeypatch.setenv("TEST_BATCH_MIN_EMAIL_RATE", "90")
    f = {"contactCountry": ["Germany", "Israel"], "seniority": ["C-Level", "VP"]}
    p = collect(client, f, target=120)
    p = research(client, p["id"])
    assert p["status"] == "paused" and p["test_state"] == "failed" and "Test batch" in p["error"]
    assert p["stats"]["submitted"] == 40
    charged = fake.charged
    for _ in range(10):
        worker.tick()
    assert fake.charged == charged                     # nothing more spent while paused
    # Continue anyway -> finishes everyone
    client.post(f"/api/pulls/{p['id']}/resume")
    p = run_until(client, p["id"], {"done"})
    assert p["test_state"] == "accepted" and p["stats"]["submitted"] == p["stats"]["selected"]
    check_books(fake)
    check_people(client, fake)


def test_test_batch_stop_spends_nothing_more(env, monkeypatch):
    client, fake, _, _ = env
    monkeypatch.setenv("TEST_BATCH_SIZE", "30")
    monkeypatch.setenv("TEST_BATCH_MIN_EMAIL_RATE", "95")
    p = collect(client, {"contactCountry": ["Germany", "Israel"], "seniority": ["C-Level", "VP"]}, target=100)
    p = research(client, p["id"])
    assert p["status"] == "paused"
    research_calls = len([c for c in fake.calls if c[2] == "/contacts/research"])
    client.post(f"/api/pulls/{p['id']}/cancel")
    p = run_until(client, p["id"], {"done"})
    assert len([c for c in fake.calls if c[2] == "/contacts/research"]) == research_calls
    assert p["stats"]["submitted"] == 30 and p["test_state"] == "stopped"
    check_books(fake)


def test_test_batch_passes_and_continues_by_itself(env, monkeypatch):
    client, fake, _, _ = env
    monkeypatch.setenv("TEST_BATCH_SIZE", "30")
    monkeypatch.setenv("TEST_BATCH_MIN_EMAIL_RATE", "50")
    p = collect(client, US, target=120)
    p = research(client, p["id"])
    assert p["status"] == "done" and p["test_state"] == "passed" and p["stats"]["submitted"] == 120
    check_books(fake)


def test_email_chance_from_history(env):
    client, fake, _, _ = env
    p0 = collect(client, US, target=50)
    r = client.post(f"/api/pulls/{p0['id']}/research", json={"choice": "chance", "chance_min": 50})
    assert r.status_code == 400 and "Not enough history" in r.json()["error"]
    research(client, p0["id"])
    f = {"contactCountry": ["United States", "Germany", "Israel"], "seniority": ["C-Level", "VP"]}
    full_pull(client, target=40, cap=0, cooldown=0, filters={"contactCountry": ["Germany", "Israel"],
                                                               "seniority": ["C-Level", "VP"]})
    p = collect(client, f, target=150, cooldown=0)
    rows = db.q("SELECT country, email_chance FROM contacts WHERE pull_id=? AND kept=1", (p["id"],))
    assert all(r["email_chance"] is not None for r in rows)
    us = [r["email_chance"] for r in rows if r["country"] == "United States"]
    il = [r["email_chance"] for r in rows if r["country"] == "Israel"]
    assert us and il and min(us) > max(il)             # learned: Israel finds far fewer emails in this data
    p = research(client, p["id"], choice="chance", chance_min=70)
    sent = db.q("SELECT country FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL", (p["id"],))
    assert sent and not any(r["country"] == "Israel" for r in sent)
    check_people(client, fake)


@pytest.mark.parametrize("charges", [True, False])
def test_charge_check(env, charges):
    client, fake, _, _ = env
    fake.charge_not_found = charges
    p = full_pull(client, target=150, cap=0, filters={"contactCountry": ["Germany", "Israel", "United States"],
                                                      "seniority": ["C-Level", "VP"]})
    cc = p["stats"]["charge_check"]
    assert cc and cc["code"] == ("charged" if charges else "free"), cc
    assert cc["no_email"] > 0


# ================================================================ retries of people with no email
def test_no_email_people_never_paid_twice_by_default(env):
    client, fake, _, _ = env
    a = full_pull(client, target=150, cap=0, filters=US)
    no_email = db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL AND (email IS NULL OR email='')",
                     (a["id"],))["n"]
    assert no_email > 0
    b = collect(client, US, target=300, cooldown=0)
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_reason LIKE 'Pulled before, no email%'",
                 (b["id"],))["n"] == no_email
    research(client, b["id"])
    check_people(client, fake)


def test_paid_retry_after_n_days_with_free_fill_first(env, monkeypatch):
    client, fake, _, _ = env
    a = full_pull(client, target=150, cap=0, filters=US)
    missed = [person(c, fake) for c in db.q("""SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL
                                               AND research_status='not found'""", (a["id"],))]
    assert len(missed) >= 3
    fake.found_later = {m["pid"] for m in missed}
    free = next(m for m in missed if m["liUrl"])           # one is now in saved contacts -> filled for free
    rec = fake.contact_record(free, fresh=True)
    fake.org_decoys.append(rec)
    monkeypatch.setenv("NO_EMAIL_RETRY_DAYS", "30")
    b = collect(client, US, target=300, cooldown=0)
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND skip_reason LIKE 'Pulled before, no email%'",
                 (b["id"],))["n"] == len(missed)           # too soon: still skipped
    db.x("UPDATE seen SET outcome_at=outcome_at-?, created_at=created_at-? WHERE outcome='no_email'",
         (31 * 86400, 31 * 86400))
    c = collect(client, US, target=300, cooldown=0)
    retry = db.q("SELECT * FROM contacts WHERE pull_id=? AND retry=1 AND kept=1", (c["id"],))
    assert {person(r, fake)["pid"] for r in retry} == {m["pid"] for m in missed}
    before = fake.charged
    c = research(client, c["id"])
    fresh_calls = [b for b in fake.searches if False]
    got = db.q("SELECT * FROM contacts WHERE pull_id=? AND retry=1 AND submitted_at IS NOT NULL", (c["id"],))
    for r in got:
        assert r["email"] and pid_from_email(r["email"]) == person(r, fake)["pid"]
    free_row = next(r for r in got if person(r, fake)["pid"] == free["pid"])
    assert free_row["filled_free"] == 1
    assert fake.researched_pids[free["pid"]] == 1                       # not charged again
    assert all(fake.researched_pids[m["pid"]] == 2 for m in missed if m["pid"] != free["pid"])
    check_people(client, fake, retries_allowed=True)


# ================================================================ saved search
def test_saved_search_runs_alone_and_tool_skips_still_apply(env):
    client, fake, _, _ = env
    fake.saved_searches[77] = [p["pid"] for p in PEOPLE[:120]]
    p = collect(client, {"savedSearchId": 77, "requireLinkedIn": True, "excludeCompanyWords": ["staffing"],
                         "jobTitle": ["ignored"]}, target=60)
    assert all(set(b) <= {"savedSearchId", "limit", "nextToken"} for b in fake.searches)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND kept=1", (p["id"],)):
        assert c["li_url"] and "Staffing" not in c["company"]
    research(client, p["id"])
    check_people(client, fake)


# ================================================================ blocked companies
def test_blocked_companies_by_name_and_domain(env):
    client, fake, _, _ = env
    client.post("/api/exclusions", json={"companies": "Acme Software 7\nmail.acme8.com\nacme10.com"})
    data = "company\nAcme Software 11\n"
    r = client.post("/api/exclusions/upload", files={"file": ("cos.csv", data, "text/csv")})
    assert r.status_code == 200 and r.json()["added"] == 1
    p = collect(client, US, target=300)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND kept=1", (p["id"],)):
        assert c["company"] not in ("Acme Software 7", "Acme Software 11")
        assert c["domain"] != "acme10.com"
    assert p["stats"]["skipped_blocked"] > 0
    # 'Acme Software 7' must not block 'Acme Software 70'
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND company='Acme Software 70' AND kept=1",
                 (p["id"],))["n"] >= 0
    assert worker.blocked_company("Acme Software 70", "acme70.com") is None
    assert worker.blocked_company("Acme Software 7 Ltd", "") is not None
    assert worker.blocked_company("x", "sub.acme10.com") is not None
    # blocked AFTER collecting -> still never researched (re-check before paying)
    kept = db.q1("SELECT company, domain FROM contacts WHERE pull_id=? AND kept=1 LIMIT 1", (p["id"],))
    client.post("/api/exclusions", json={"companies": kept["domain"]})
    research(client, p["id"])
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE domain=? AND submitted_at IS NOT NULL", (kept["domain"],))["n"] == 0
    check_people(client, fake)


# ================================================================ stop buttons & spending switch
def test_stop_all(env):
    client, fake, _, _ = env
    a = collect(client, US, target=200)
    client.post(f"/api/pulls/{a['id']}/research", json={"choice": "all"})
    worker.tick()
    b = new_pull(client)
    client.patch(f"/api/pulls/{b['id']}", json={"filters": {"contactCountry": ["Canada"], "seniority": ["C-Level", "VP"]}})
    client.post(f"/api/pulls/{b['id']}/preview", json={})
    client.post(f"/api/pulls/{b['id']}/collect", json={"target": 300, "per_company_cap": 0})
    assert client.post("/api/stop-all").json()["stopped"] == 2
    research_calls = len([c for c in fake.calls if c[2] == "/contacts/research"])
    searches = len(fake.searches)
    a = run_until(client, a["id"], {"done"})
    assert client.get(f"/api/pulls/{b['id']}").json()["status"] == "collected"
    assert len([c for c in fake.calls if c[2] == "/contacts/research"]) == research_calls
    assert len(fake.searches) == searches
    check_books(fake)


def test_spending_switch_off(env, monkeypatch):
    client, fake, _, _ = env
    p = collect(client, US, target=200)
    client.post(f"/api/pulls/{p['id']}/research", json={"choice": "all"})
    worker.tick()                               # one batch sent
    monkeypatch.setenv("SPENDING_ENABLED", "0")
    calls = len([c for c in fake.calls if c[2] == "/contacts/research"])
    p = run_until(client, p["id"], {"paused"})
    assert "SPENDING_ENABLED" in p["error"]
    assert len([c for c in fake.calls if c[2] == "/contacts/research"]) == calls
    assert p["stats"]["pending"] == 0           # people already sent were still collected (free)
    q = new_pull(client)
    r = client.post(f"/api/pulls/{q['id']}/preview", json={})
    assert r.status_code == 400 and "SPENDING_ENABLED" in r.json()["error"]
    assert client.get("/api/meta").json()["credits"]["spending_enabled"] is False
    monkeypatch.setenv("SPENDING_ENABLED", "1")
    client.post(f"/api/pulls/{p['id']}/resume")
    run_until(client, p["id"], {"done"})
    check_books(fake)


def test_fill_in_can_be_stopped(env, monkeypatch):
    from app import seamless
    client, fake, _, _ = env
    fake.already_in_org = {p["pid"] for p in PEOPLE[:200]}
    p = full_pull(client, target=100, cap=0)
    assert p["stats"]["dup_missing"] > 0
    real = seamless.org_contacts
    pages = []

    def org(*a):
        pages.append(1)
        client.post("/api/fill-stop")          # pressed Stop while the first page was loading
        return real(*a)
    monkeypatch.setattr(seamless, "org_contacts", org)
    r1 = worker.fill_from_saved(p["id"])
    assert r1["stopped"] is True and len(pages) == 1        # stopped: no further pages read
    monkeypatch.setattr(seamless, "org_contacts", real)
    r2 = worker.fill_from_saved(p["id"])
    assert r2["stopped"] is False
    assert r1["filled"] + r2["filled"] > 0                   # what was read before Stop is still used (free)
    check_people(client, fake)


def test_low_balance_banner(env, monkeypatch):
    client, fake, _, _ = env
    monkeypatch.setenv("LOW_BALANCE_WARNING", "500")
    fake.balance = 400
    client.post("/api/settings/test-seamless")
    c = client.get("/api/meta").json()["credits"]
    assert c["low_balance"] is True and c["balance"] == 400
    fake.balance = 900
    client.post("/api/settings/test-seamless")
    assert client.get("/api/meta").json()["credits"]["low_balance"] is False


def test_cost_per_email_by_country(env):
    client, fake, _, _ = env
    full_pull(client, target=200, cap=0, filters={"contactCountry": ["Germany", "Israel", "United States"],
                                                  "seniority": ["C-Level", "VP"]})
    rows = {r["country"]: r for r in client.get("/api/credits/countries").json()["rows"]}
    assert rows["Israel"]["emails"] == 0 and rows["Israel"]["per_email"] is None     # shown as "no emails"
    assert rows["United States"]["per_email"] < 2
    total = sum(r["credits"] for r in rows.values())
    spent = db.q1("SELECT SUM(credits) s FROM ledger")["s"]
    assert abs(total - spent) < 1.0


def test_companies_page_statuses(env):
    client, fake, _, _ = env
    full_pull(client, target=150, cap=0, filters={"contactCountry": ["Germany", "Israel"], "seniority": ["C-Level", "VP"]})
    d = client.get("/api/companies?status=noemail&limit=200").json()
    assert d["counts"]["noemail"] > 0 and all(r["no_email"] and not r["on_cooldown"] for r in d["rows"])
    d = client.get("/api/companies?status=cooldown&limit=200").json()
    assert d["rows"] and all(r["on_cooldown"] for r in d["rows"])


# ================================================================ settings & upgrade
def test_settings_page_shows_every_env_setting(env):
    client, _, _, _ = env
    s = client.get("/api/settings").json()
    keys = {r["key"] for g in s["groups"] for r in g["rows"]}
    for k in ("SPENDING_ENABLED", "LOW_BALANCE_WARNING", "UPDATED_WITHIN_MONTHS", "REQUIRE_COMPANY_WEBSITE",
              "REQUIRE_LINKEDIN", "SKIP_NO_EMAIL_SERVER", "TEST_BATCH_SIZE", "TEST_BATCH_MIN_EMAIL_RATE",
              "EMAIL_CHANCE_MIN", "NO_EMAIL_RETRY_DAYS", "DEFAULT_BATCH_SIZE", "DEFAULT_EXPORT_COLUMNS", "EMAIL_PICK"):
        assert k in keys, k
    assert s["version"] == "1.2.0"


def test_new_pull_gets_env_defaults(env, monkeypatch):
    client, _, _, _ = env
    monkeypatch.setenv("UPDATED_WITHIN_MONTHS", "12")
    monkeypatch.setenv("REQUIRE_COMPANY_WEBSITE", "1")
    p = new_pull(client)
    assert p["filters"]["updatedWithinMonths"] == 12 and p["filters"]["requireCompanyWebsite"] is True
    assert "requireLinkedIn" not in p["filters"]


def test_upgrade_from_v11_database(tmp_path, monkeypatch):
    """A database made by v1.1 opens in v1.2 with all data kept and old 'seen' people still skipped."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE pulls (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, prompt TEXT, filters_json TEXT,
        notes_json TEXT, status TEXT, paused_from TEXT, target INTEGER DEFAULT 0, per_company_cap INTEGER DEFAULT 0,
        total_matches INTEGER, next_token TEXT, pages_fetched INTEGER DEFAULT 0, rows_seen INTEGER DEFAULT 0,
        search_credits INTEGER DEFAULT 0, research_credits INTEGER DEFAULT 0, stop_reason TEXT, error TEXT,
        cloned_from INTEGER, created_at REAL, updated_at REAL, finished_at REAL, company_cooldown INTEGER,
        search_hash TEXT, started_from TEXT, from_top INTEGER DEFAULT 0);
      CREATE TABLE seen (key TEXT PRIMARY KEY, pull_id INTEGER, created_at REAL);
      CREATE TABLE companies (key TEXT PRIMARY KEY, name TEXT, domain TEXT, first_at REAL, last_at REAL,
        first_pull_id INTEGER, last_pull_id INTEGER, people INTEGER DEFAULT 0, released_at REAL);
      INSERT INTO pulls(name, status, filters_json, notes_json) VALUES ('Old pull', 'done', '{}', '[]');
      INSERT INTO seen VALUES ('li:linkedin.com/in/old-person', 1, 1700000000);
      INSERT INTO companies(key, name, domain, last_at, last_pull_id, people) VALUES ('d:old.com','Old','old.com',?,1,2);
    """.replace("?", str(time.time())))
    con.commit()
    con.close()
    db.reset_for_tests(str(path))
    db.connect()
    cols = {r["name"] for r in db.q("PRAGMA table_info(contacts)")}
    assert {"batch_id", "email_chance", "email_pick", "retry"} <= cols
    assert db.q1("SELECT name FROM pulls WHERE id=1")["name"] == "Old pull"
    assert worker.people_reason("X", "Y", "https://www.linkedin.com/in/old-person/")[0].startswith("Already pulled")
    pull = {"id": 99, "company_cooldown": 90}
    assert worker._cooldown_reason({"company": "Old", "domain": "old.com"}, pull)
