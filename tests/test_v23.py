"""Lead Puller 2.3: Prospeo as the second source, and the Enrich contacts page.

Every money test ends with check_books (the ledger equals what each Seamless key was charged, and Prospeo's log equals
what Prospeo charged, to the credit) and check_people (nobody paid for twice, every email is that person's own real
mailbox, a Prospeo email only when Prospeo verified it)."""
import collections
import csv
import io
import os

import pytest

from app import db, engine, prospeo
from tests.conftest import US, add_account, add_prospeo, add_verifier, check_books, check_people, run, start
from tests.fakes import is_catch_all
from tests.test_guessing import company_domains, start_list


def prospeo_on(client):
    if os.environ.get("LP_TEST_PROSPEO") != "1":
        add_prospeo(client)


def emails_by_source(pid):
    return dict(collections.Counter(r["email_source"] for r in db.q(
        f"SELECT email_source FROM contacts WHERE pull_id=? AND {engine.DELIVERABLE}", (pid,))))


def own_mailbox(fake, c):
    owner = fake.mailboxes.get(c["email"])
    return owner is not None and fake.by_pid[owner]["name"] == c["name"]


# ================================================================ Prospeo fills what Seamless misses
def test_prospeo_fills_seamless_misses_and_books_match(env):
    client, fake, clock = env
    prospeo_on(client)
    pid = start(client, US, target=150, max_credits=600)
    p = run(client, pid)
    src = emails_by_source(pid)
    assert src.get("prospeo", 0) > 0, src
    assert p["prospeo"]["found"] == src["prospeo"]
    assert p["prospeo"]["credits"] == fake.pr.charged
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND email_source='prospeo'", (pid,)):
        assert c["submitted_at"] is not None                 # Seamless was asked first (default order)
        assert own_mailbox(fake, c), c["email"]
    check_books(fake)
    check_people(fake)


def test_prospeo_is_only_asked_about_people_seamless_had_no_usable_email_for(env):
    client, fake, clock = env
    prospeo_on(client)
    pid = start(client, US, target=120, max_credits=500)
    run(client, pid)
    asked = {int(x) for x in fake.pr.asked}
    for cid in asked:
        c = db.q1("SELECT * FROM contacts WHERE id=?", (cid,))
        assert c["prospeo_state"] in ("found", "none", "mismatch", "dup", "error", "retry"), c
        assert c["email_source"] in (None, "prospeo") or (c["email_status"] or "").lower() == "invalid", c
    good_seamless = db.q(f"SELECT id FROM contacts WHERE pull_id=? AND email_source='seamless' AND {engine.DELIVERABLE}",
                         (pid,))
    assert not asked & {r["id"] for r in good_seamless}
    check_books(fake)


def test_wrong_person_and_unverified_answers_are_never_used(env):
    client, fake, clock = env
    prospeo_on(client)
    for p in fake.people:                                     # Seamless knows nobody's email: everything to Prospeo
        fake.email_override[p["pid"]] = ""
    pid = start(client, US, target=300, max_credits=400)
    p = run(client, pid, until=("done", "paused"))
    if p["status"] == "paused":                               # the email-rate check may pause; carry on
        client.post(f"/api/pulls/{pid}/resume")
        run(client, pid)
    mism = db.q("SELECT * FROM contacts WHERE pull_id=? AND prospeo_state='mismatch'", (pid,))
    assert mism, "the wrong-person trap never fired"
    assert all(not c["email"] for c in mism)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND email_source='prospeo'", (pid,)):
        pp = next(x for x in fake.people if x["email"] == c["email"])
        assert fake.pr.verified(pp["pid"]) and not fake.pr.wrong_person(pp["pid"])
        assert own_mailbox(fake, c)
    check_books(fake)
    check_people(fake)


def test_prospeo_cap_per_pull(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("PROSPEO_MAX_CREDITS_PER_PULL", "3")
    monkeypatch.setenv("PROSPEO_PER_STEP", "5")
    prospeo_on(client)
    for p in fake.people:
        fake.email_override[p["pid"]] = ""
    pid = start(client, US, target=60, max_credits=300)
    p = run(client, pid, until=("done", "paused"))
    if p["status"] == "paused":
        client.post(f"/api/pulls/{pid}/resume")
        run(client, pid)
    assert prospeo.spent(pid) <= 3 + 4                         # one call may find a few more than the cap left
    assert fake.pr.charged == prospeo.spent(pid)
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND prospeo_state='skipped'", (pid,))["n"] > 0
    check_books(fake)


def test_prospeo_with_few_credits_left_is_never_asked(env, monkeypatch):
    client, fake, clock = env
    fake.pr.credits = 10
    monkeypatch.setenv("PROSPEO_MIN_CREDITS_LEFT", "20")
    add_prospeo(client)
    pid = start(client, US, target=60, max_credits=200)
    p = run(client, pid)
    assert fake.pr.charged == 0 and not fake.pr.asked
    assert "fewer than 20" in p["prospeo"]["why_not"]
    check_books(fake)


def test_prospeo_off_means_2_2_behaviour(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("PROSPEO_ENABLED", "0")
    add_prospeo(client)
    pid = start(client, US, target=60, max_credits=200)
    run(client, pid)
    assert not fake.pr.asked and fake.pr.charged == 0
    assert not db.q1("SELECT 1 x FROM contacts WHERE email_source='prospeo'")
    check_books(fake)
    check_people(fake)


def test_prospeo_switched_off_on_accounts_page(env):
    client, fake, clock = env
    add_prospeo(client)
    assert client.post("/api/prospeo", json={"enabled": False}).json()["enabled"] is False
    pid = start(client, US, target=40, max_credits=200)
    run(client, pid)
    assert not fake.pr.asked
    check_books(fake)


@pytest.mark.parametrize("modes", [["500", "500", "500"], ["timeout", "garbage"], ["no_credits"]])
def test_prospeo_errors_never_become_emails_and_pause_it(env, modes):
    client, fake, clock = env
    prospeo_on(client)
    for p in fake.people:
        fake.email_override[p["pid"]] = ""
    fake.pr.fail_next = list(modes)
    pid = start(client, US, target=80, max_credits=300)
    p = run(client, pid, until=("done", "paused"))
    if p["status"] == "paused":
        client.post(f"/api/pulls/{pid}/resume")
        run(client, pid)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND email_source='prospeo'", (pid,)):
        assert own_mailbox(fake, c)
    assert db.q1("SELECT COUNT(*) n FROM prospeo_log WHERE result='error'")["n"] >= 1
    check_books(fake)
    check_people(fake)


def test_bad_prospeo_key_is_never_saved(env):
    client, fake, clock = env
    if os.environ.get("LP_TEST_PROSPEO") == "1":
        client.delete("/api/prospeo")
    r = client.post("/api/prospeo", json={"api_key": "wrong-key-1234567"})
    assert r.status_code == 400 and "didn't accept" in r.json()["error"]
    assert client.get("/api/prospeo").json()["has_key"] is False
    ok = add_prospeo(client)
    assert ok["has_key"] and ok["key"].endswith("9f1c") and ok["credits"] == 5000
    assert "prospeo-key-9f1c" not in client.get("/api/overview").text       # never sent to the browser in full


def test_stop_means_no_more_prospeo(env):
    client, fake, clock = env
    prospeo_on(client)
    for p in fake.people:
        fake.email_override[p["pid"]] = ""
    pid = start(client, US, target=200, max_credits=400)
    for _ in range(6):
        engine.tick()
        clock.t += 1
    client.post(f"/api/pulls/{pid}/stop")
    before = len(fake.pr.asked)
    run(client, pid)
    assert len(fake.pr.asked) == before
    check_books(fake)


def test_target_is_not_overshot_by_prospeo(env):
    client, fake, clock = env
    prospeo_on(client)
    pid = start(client, US, target=40, max_credits=200)
    run(client, pid)
    st = engine.counts(pid)
    assert st["emails"] <= 40 + 25                           # research is sent in batches; Prospeo never adds a batch more
    check_books(fake)


def test_prospeo_first_order_saves_seamless_research(env, monkeypatch, tmp_path):
    client, fake, clock = env
    prospeo_on(client)
    pid0 = start(client, US, target=100, max_credits=500)
    run(client, pid0)
    r0 = db.pull_spend(pid0, "research")
    check_books(fake)
    monkeypatch.setenv("SOURCE_ORDER", "prospeo,seamless")
    us_ca = {"contactCountry": {"in": ["Canada"]}, "seniority": {"in": ["C-Level", "VP"]}}
    pid1 = start(client, us_ca, target=40, max_credits=300)
    p1 = run(client, pid1)
    found_first = db.q("SELECT * FROM contacts WHERE pull_id=? AND email_source='prospeo'", (pid1,))
    assert found_first, "Prospeo-first found nobody"
    assert all(c["submitted_at"] is None for c in found_first)          # never paid in Seamless
    assert db.pull_spend(pid1, "research") < p1["stats"]["emails"]      # fewer Seamless credits than emails
    assert r0 > 0
    check_books(fake)
    check_people(fake)


def test_prospeo_and_two_accounts_with_chaos(env):
    client, fake, clock = env
    prospeo_on(client)
    add_account(client, fake)
    fake.chaos_poll = True
    fake.flaky_rate = 0.05
    pid = start(client, US, target=150, max_credits=600)
    run(client, pid, ticks=20000, clock=clock)
    check_books(fake)
    check_people(fake)


def test_prospeo_email_that_belongs_to_someone_else_is_not_used(env):
    client, fake, clock = env
    prospeo_on(client)
    pid = start(client, US, target=30, max_credits=200)
    run(client, pid)
    a, b = db.q(f"SELECT * FROM contacts WHERE pull_id=? AND {engine.DELIVERABLE} LIMIT 2", (pid,))
    engine._apply_prospeo(dict(b, email=None), a["email"], "test")
    b2 = db.q1("SELECT * FROM contacts WHERE id=?", (b["id"],))
    assert b2["email"] == b["email"] and b2["prospeo_state"] == "dup"


def test_prospeo_with_guessing_company_lists_and_catch_all(env):
    client, fake, clock = env
    add_verifier(client)
    prospeo_on(client)
    doms = company_domains(fake, 60, min_people=2)
    assert any(is_catch_all(d) for d in doms)
    pid = start_list(client, doms, per_company=2)
    run(client, pid)
    src = emails_by_source(pid)
    assert src.get("guessed", 0) > 0
    check_books(fake)
    check_people(fake)


# ================================================================ Enrich contacts
def _csv(rows, header=("First Name", "Last Name", "Company", "Website", "LinkedIn URL", "Job Title", "Notes"), sep=","):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _people_rows(fake, n=40, li_every=3, start_at=0):
    out = []
    ps = [p for p in fake.people if p["domain"] and p["country"] == "United States"][start_at:start_at + n]
    for i, p in enumerate(ps):
        li = p["liUrl"] if (i % li_every == 0 and p["liUrl"]) else ""
        out.append((p["first"], p["last"], p["company"], p["domain"], li, p["title"], f"row {i}"))
    return out, ps


def enrich(client, text, name="t.csv", mime="text/csv", **kw):
    r = client.post("/api/enrich/check", files={"file": (name, text if isinstance(text, bytes) else text.encode(),
                                                         mime)})
    assert r.status_code == 200, r.text
    chk = r.json()
    body = {"token": chk["token"], **kw}
    r = client.post("/api/enrich", json=body)
    assert r.status_code == 200, r.text
    return chk, r.json()["id"]


def download_enriched(client, pid, fmt="csv"):
    r = client.get(f"/api/pulls/{pid}/download?kind=enriched&format={fmt}")
    assert r.status_code == 200, r.text
    if fmt == "xlsx":
        return r.content
    return list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))


def test_enrich_basic_file_comes_back_in_order_with_emails(env):
    client, fake, clock = env
    prospeo_on(client)
    rows, ps = _people_rows(fake, 40)
    rows.insert(5, ("", "", "Nobody Inc", "", "", "", "thin row"))            # not enough to look up
    rows.insert(9, rows[2])                                                   # same person twice
    chk, pid = enrich(client, _csv(rows))
    assert chk["rows"] == 42 and chk["counts"]["thin"] == 1 and chk["counts"]["dup"] == 1
    assert chk["columns"]["first_name"] == "First Name" and chk["columns"]["linkedin"] == "LinkedIn URL"
    p = run(client, pid)
    assert p["status"] == "done" and p["mode"] == "enrich"
    out = download_enriched(client, pid)
    header, body = out[0], out[1:]
    assert header[:7] == ["First Name", "Last Name", "Company", "Website", "LinkedIn URL", "Job Title", "Notes"]
    assert "Email" in header and "How we got the email" in header and "Lead Puller note" in header
    assert len(body) == 42 and [r[6] for r in body] == [r[6] for r in rows]      # same rows, same order
    ei, si = header.index("Email"), header.index("How we got the email")
    got = [r for r in body if r[ei]]
    assert len(got) >= 25, len(got)
    for r in got:
        owner = fake.mailboxes.get(r[ei])
        assert owner is not None and fake.by_pid[owner]["first"] == r[0] and fake.by_pid[owner]["last"] == r[1], r
    assert body[9][ei] == body[2][ei]                                          # the duplicate row shares the answer
    assert not body[5][ei] and "Not enough" in body[5][header.index("Lead Puller note")]
    assert {r[si] for r in got} <= {"seamless", "prospeo (verified)", "guessed (MillionVerifier: valid)", "already had"}
    check_books(fake)
    check_people(fake)


def test_enrich_twice_never_pays_twice(env):
    client, fake, clock = env
    prospeo_on(client)
    rows, _ = _people_rows(fake, 30)
    _, pid1 = enrich(client, _csv(rows))
    run(client, pid1)
    s1, p1 = fake.charged, fake.pr.charged
    _, pid2 = enrich(client, _csv(rows))
    p = run(client, pid2)
    assert fake.charged == s1 and fake.pr.charged == p1                       # nothing spent the second time
    out1, out2 = download_enriched(client, pid1), download_enriched(client, pid2)
    ei = out1[0].index("Email")
    assert [r[ei] for r in out1[1:]] == [r[ei] for r in out2[1:]]             # same emails, now "already had"
    assert p["enrich"]["already"] == sum(1 for r in out1[1:] if r[ei])
    check_books(fake)
    check_people(fake)


def test_people_from_an_earlier_pull_are_filled_free(env):
    client, fake, clock = env
    pid0 = start(client, US, target=40, max_credits=200)
    run(client, pid0)
    have = db.q(f"SELECT * FROM contacts WHERE pull_id=? AND {engine.DELIVERABLE} LIMIT 10", (pid0,))
    spent = fake.charged
    rows = [(c["first_name"], c["last_name"], c["company"], c["domain"], "", "", "x") for c in have]
    chk, pid = enrich(client, _csv(rows))
    assert chk["counts"].get("have") == len(have)
    run(client, pid)
    assert fake.charged == spent
    out = download_enriched(client, pid)
    ei = out[0].index("Email")
    assert [r[ei] for r in out[1:]] == [c["email"] for c in have]
    check_books(fake)


def test_enrich_without_seamless_uses_only_free_checks_and_prospeo(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("ENRICH_USE_SEAMLESS", "0")
    prospeo_on(client)
    rows, _ = _people_rows(fake, 30)
    _, pid = enrich(client, _csv(rows))
    p = run(client, pid)
    assert fake.charged == 0 and db.pull_spend(pid) == 0
    assert p["prospeo"]["found"] > 0
    check_books(fake)
    check_people(fake)


def test_enrich_max_credits_then_prospeo_for_the_rest(env):
    client, fake, clock = env
    prospeo_on(client)
    rows, _ = _people_rows(fake, 60)
    _, pid = enrich(client, _csv(rows), max_credits=10)
    p = run(client, pid)
    assert db.pull_spend(pid) <= 10
    assert p["prospeo"]["asked"] > 0
    check_books(fake)
    check_people(fake)


def test_enrich_seamless_answer_for_a_different_person_is_not_used(env):
    client, fake, clock = env
    rows, ps = _people_rows(fake, 6, li_every=99)
    target = ps[2]
    real_record = fake.record

    def record(p):
        rec = real_record(p)
        if p["pid"] == target["pid"]:
            rec = dict(rec, fullName="Somebody Else", firstName="Somebody", lastName="Else")
        return rec
    fake.record = record
    _, pid = enrich(client, _csv(rows))
    run(client, pid)
    c = db.q1("SELECT * FROM contacts WHERE pull_id=? AND first_name=? AND last_name=? AND domain=?",
              (pid, target["first"], target["last"], target["domain"]))
    if c["email_source"] in ("guessed", "prospeo"):          # found by its format or by Prospeo instead
        assert c["email"] == target["email"]
        assert "different person" in (c["research_message"] or "") or c["email_source"] == "guessed"
    else:
        assert not c["email"] and "different person" in (c["research_message"] or ""), c
    check_books(fake)


def test_enrich_reads_excel_semicolons_bom_and_odd_headers(env):
    client, fake, clock = env
    rows, _ = _people_rows(fake, 8)
    text = "﻿" + _csv(rows, header=("first", "SURNAME", "Organisation", "Company Domain", "Person Linkedin Url",
                                          "Position", "Notes"), sep=";")
    chk, pid = enrich(client, text)
    assert chk["columns"]["last_name"] == "SURNAME" and chk["columns"]["website"] == "Company Domain"
    assert chk["counts"].get("lookup", 0) + chk["counts"].get("have", 0) == 8
    run(client, pid)
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Full Name", "Employer", "Email"])
    for r in rows[:5]:
        ws.append([f"{r[0]} {r[1]}", r[2], f"{r[0].lower()}@{r[3]}"])
    buf = io.BytesIO()
    wb.save(buf)
    chk2, pid2 = enrich(client, buf.getvalue(), name="list.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert chk2["columns"]["full_name"] == "Full Name" and chk2["columns"]["company"] == "Employer"
    run(client, pid2)
    x = download_enriched(client, pid2, "xlsx")
    assert x[:2] == b"PK"
    check_books(fake)
    check_people(fake)


def test_enrich_column_choice_can_be_changed(env):
    client, fake, clock = env
    rows, _ = _people_rows(fake, 5)
    text = _csv(rows, header=("A", "B", "C", "D", "E", "F", "G"))
    r = client.post("/api/enrich/check", files={"file": ("x.csv", text.encode(), "text/csv")}).json()
    assert r["counts"].get("thin", 0) == 5 or not r["mapping"]
    m = {"first_name": 0, "last_name": 1, "company": 2, "website": 3}
    r2 = client.post("/api/enrich/recheck", json={"token": r["token"], "mapping": m}).json()
    assert r2["counts"].get("lookup", 0) + r2["counts"].get("have", 0) == 5
    r3 = client.post("/api/enrich", json={"token": r["token"], "mapping": m})
    assert r3.status_code == 200
    run(client, r3.json()["id"])
    check_books(fake)


def test_enrich_respects_do_not_pull_and_spreadsheet_formulas(env):
    client, fake, clock = env
    rows, ps = _people_rows(fake, 6)
    client.post("/api/exclusions", json={"text": ps[0]["domain"]})
    rows.append(("=HYPERLINK(\"x\")", "Evil", "Acme", "acme1.com", "", "", "+1"))
    chk, pid = enrich(client, _csv(rows))
    assert chk["counts"].get("dnp", 0) >= 1
    run(client, pid)
    assert not db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND domain=? AND submitted_at IS NOT NULL",
                     (pid, ps[0]["domain"]))
    out = download_enriched(client, pid)
    assert out[-1][0].startswith("'=") and out[-1][6] == "'+1"
    check_books(fake)


def test_enrich_file_limits(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("ENRICH_MAX_ROWS", "3")
    rows, _ = _people_rows(fake, 5)
    r = client.post("/api/enrich/check", files={"file": ("x.csv", _csv(rows).encode(), "text/csv")})
    assert r.status_code == 400 and "most is 3" in r.json()["error"]
    r = client.post("/api/enrich/check", files={"file": ("x.csv", b"name\n", "text/csv")})
    assert r.status_code == 400
    r = client.post("/api/enrich", json={"token": "nope"})
    assert r.status_code == 400 and "again" in r.json()["error"]


def test_enrich_with_guessing_and_prospeo_and_chaos(env):
    client, fake, clock = env
    add_verifier(client)
    prospeo_on(client)
    fake.chaos_poll = True
    pid0 = start(client, US, target=60, max_credits=300)       # teaches some company formats
    run(client, pid0, ticks=20000, clock=clock)
    rows, _ = _people_rows(fake, 60, start_at=200)
    _, pid = enrich(client, _csv(rows))
    run(client, pid, ticks=20000, clock=clock)
    check_books(fake)
    check_people(fake)


def test_enrich_pull_page_and_lists_work(env):
    client, fake, clock = env
    prospeo_on(client)
    rows, _ = _people_rows(fake, 10)
    _, pid = enrich(client, _csv(rows))
    run(client, pid)
    p = client.get(f"/api/pulls/{pid}").json()
    assert p["enrich"]["rows"] == 10 and "prospeo" in p
    for view in ("emails", "no_email", "researched", "skipped", "waiting"):
        assert client.get(f"/api/pulls/{pid}/people?view={view}").status_code == 200
    for kind in ("anavmail", "no_email", "all"):
        assert client.get(f"/api/pulls/{pid}/download?kind={kind}").status_code == 200
    lst = client.get("/api/pulls").json()["rows"]
    assert any(r["id"] == pid for r in lst)
    assert client.get(f"/api/pulls/{pid}/download?kind=enriched").status_code == 200
    pid2 = start(client, US, target=10, max_credits=50)
    assert client.get(f"/api/pulls/{pid2}/download?kind=enriched").status_code == 400


def test_enrich_fills_free_from_seamless_saved_contacts(env):
    client, fake, clock = env
    rows, ps = _people_rows(fake, 8, li_every=1)
    org = fake.org()
    for p in ps[:4]:                                         # researched on the Seamless website before
        fake.researched.setdefault(org, {})[p["pid"]] = 1
        rec = fake.record(p)
        if rec.get("email"):
            fake.saved.setdefault(org, {})[p["pid"]] = rec
    client.post(f"/api/accounts/{db.q1('SELECT id FROM accounts')['id']}/check")
    from app import memory, accounts
    memory.sync_saved(accounts.get(db.q1("SELECT id FROM accounts")["id"]), force=True)
    spent = fake.charged
    chk, pid = enrich(client, _csv(rows))
    assert chk["counts"].get("have", 0) >= 3
    run(client, pid)
    assert fake.charged - spent <= 4                         # only the 4 others can cost anything
    check_books(fake)
    check_people(fake)


def test_people_prospeo_had_nothing_for_are_not_asked_again_within_90_days(env):
    client, fake, clock = env
    prospeo_on(client)
    for p in fake.people:
        fake.email_override[p["pid"]] = ""
    pid = start(client, US, target=40, max_credits=200)
    p = run(client, pid, until=("done", "paused"))
    if p["status"] == "paused":
        client.post(f"/api/pulls/{pid}/resume")
        run(client, pid)
    nothing = db.q("SELECT * FROM contacts WHERE pull_id=? AND prospeo_state='none'", (pid,))
    assert nothing
    asked_before = len(fake.pr.asked)
    rows = [(c["first_name"], c["last_name"], c["company"], c["domain"], c["li_url"] or "", "", "") for c in nothing]
    _, pid2 = enrich(client, _csv(rows))
    run(client, pid2)
    assert len(fake.pr.asked) == asked_before               # not asked again (and Seamless not paid again)
    check_books(fake)


def test_prospeo_key_from_env(env, monkeypatch):
    client, fake, clock = env
    client.delete("/api/prospeo")
    monkeypatch.setenv("PROSPEO_API_KEY", "prospeo-key-9f1c")
    st = client.get("/api/prospeo").json()
    assert st["has_key"] and st["from_env"]
    client.post("/api/prospeo", json={"check": 1})
    for p in fake.people:
        fake.email_override[p["pid"]] = ""
    pid = start(client, US, target=30, max_credits=200)
    p = run(client, pid, until=("done", "paused"))
    if p["status"] == "paused":
        client.post(f"/api/pulls/{pid}/resume")
        run(client, pid)
    assert fake.pr.charged > 0
    check_books(fake)


def test_enrich_pause_resume_stop(env):
    client, fake, clock = env
    prospeo_on(client)
    rows, _ = _people_rows(fake, 50)
    _, pid = enrich(client, _csv(rows))
    for _ in range(3):
        engine.tick()
    client.post(f"/api/pulls/{pid}/pause")
    assert client.get(f"/api/pulls/{pid}").json()["status"] == "paused"
    client.post(f"/api/pulls/{pid}/resume")
    engine.tick()
    client.post(f"/api/pulls/{pid}/stop")
    p = run(client, pid)
    assert p["status"] == "done"
    assert not db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND state IN ('kept','banked')", (pid,))
    out = download_enriched(client, pid)
    assert len(out) == 51
    check_books(fake)
    check_people(fake)


def test_enrich_with_prospeo_first(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("SOURCE_ORDER", "prospeo,seamless")
    prospeo_on(client)
    rows, _ = _people_rows(fake, 40)
    _, pid = enrich(client, _csv(rows))
    p = run(client, pid)
    found = db.q("SELECT * FROM contacts WHERE pull_id=? AND email_source='prospeo'", (pid,))
    assert found and all(c["submitted_at"] is None for c in found)
    assert db.pull_spend(pid, "research") <= 40 - len(found)
    check_books(fake)
    check_people(fake)


def test_2_3_gets_more_emails_than_2_2_for_the_same_seamless_credits(env, monkeypatch, tmp_path):
    """The bottom line on the same fake data: Seamless only (2.2) vs Seamless + Prospeo (2.3)."""
    client, fake, clock = env
    if os.environ.get("LP_TEST_PROSPEO") == "1":
        pytest.skip("compares with Prospeo off")
    pid0 = start(client, US, target=120, max_credits=400)
    run(client, pid0)
    e0, s0 = engine.counts(pid0)["emails"], db.pull_spend(pid0)
    check_books(fake)
    from tests.test_v22 import fresh
    fake2 = fresh(client, monkeypatch, tmp_path, "v23", verifier_on=os.environ.get("LP_TEST_GUESSING") == "1")
    from tests.fakes import FakeProspeo
    import httpx
    fake2.pr = FakeProspeo(fake2)
    fake2.pr.clock = clock
    monkeypatch.setattr(prospeo, "TRANSPORT", httpx.MockTransport(fake2.pr.handler))
    add_prospeo(client)
    pid1 = start(client, US, target=120, max_credits=400)
    run(client, pid1)
    e1, s1 = engine.counts(pid1)["emails"], db.pull_spend(pid1)
    print(f"\n2.2: {e0} emails for {s0} Seamless credits · 2.3: {e1} emails for {s1} Seamless credits + "
          f"{fake2.pr.charged} Prospeo credits")
    assert s1 / e1 < s0 / e0                                  # fewer Seamless credits per email
    check_books(fake2)
    check_people(fake2)


def test_no_match_for_a_whole_batch_is_not_an_error(env, monkeypatch):
    """2.3.1: Prospeo answers 400 NO_MATCH when nobody in the batch has a verified email. That's 'nothing found'
    (free), not an error: Prospeo must not pause, and the people are recorded as having no Prospeo email."""
    client, fake, clock = env
    monkeypatch.setenv("PROSPEO_PER_STEP", "3")
    prospeo_on(client)
    for p in fake.people:
        fake.email_override[p["pid"]] = ""
    fake.pr.covers = lambda pid: False                      # Prospeo knows nobody
    pid = start(client, US, target=30, max_credits=200)
    p = run(client, pid, until=("done", "paused"))
    if p["status"] == "paused":
        client.post(f"/api/pulls/{pid}/resume")
        p = run(client, pid)
    st = client.get("/api/prospeo").json()
    assert not st["last_error"] and not st["paused_until"]
    assert db.q1("SELECT COUNT(*) n FROM prospeo_log WHERE result='error'")["n"] == 0
    assert p["prospeo"]["none"] > 3 and p["prospeo"]["waiting"] == 0
    assert fake.pr.charged == 0
    check_books(fake)


def test_2_3_0_no_match_errors_are_repaired_on_start(env):
    client, fake, clock = env
    prospeo_on(client)
    pid = start(client, US, target=10, max_credits=60)
    run(client, pid)
    c = db.q1("SELECT * FROM contacts WHERE pull_id=? LIMIT 1", (pid,))
    db.x("UPDATE contacts SET prospeo_state='error', prospeo_note='Prospeo error: NO_MATCH' WHERE id=?", (c["id"],))
    db.x("INSERT INTO prospeo_log(at,pull_id,contact_id,result,cost,note) VALUES(1,?,?,'error',0,'Prospeo error: NO_MATCH')",
         (pid, c["id"]))
    db.x("UPDATE prospeo SET last_error='Prospeo error: NO_MATCH', errors_in_row=3, paused_until=9999999999 WHERE id=1")
    assert prospeo.repair_no_match() == 1
    st = client.get("/api/prospeo").json()
    assert not st["last_error"] and not st["paused_until"]
    assert db.q1("SELECT prospeo_state s FROM contacts WHERE id=?", (c["id"],))["s"] is None
    assert prospeo.ready()[0]



def test_too_many_requests_is_a_wait_not_an_error(env):
    """2.4.1: a 429 from Prospeo means 'wait', never an error, and nobody is given up on."""
    client, fake, clock = env
    prospeo_on(client)
    for p in fake.people:
        fake.email_override[p["pid"]] = ""
    fake.pr.fail_next = ["429", "429"]
    pid = start(client, US, target=60, max_credits=300)
    p = run(client, pid, until=("done", "paused"), ticks=20000, clock=clock)
    if p["status"] == "paused":
        client.post(f"/api/pulls/{pid}/resume")
        run(client, pid, ticks=20000, clock=clock)
    for _ in range(400):                       # waits a minute, then (even after the pull ended) asks by itself
        engine.tick()
        clock.t += 1
    assert db.q1("SELECT COUNT(*) n FROM prospeo_log WHERE result='error'")["n"] == 0
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE prospeo_state='error'")["n"] == 0
    p = client.get(f"/api/pulls/{pid}").json()
    assert fake.pr.charged > 0 or p["stats"]["emails"] >= p["target"]     # (guessing may already reach the target)
    check_books(fake)
    check_people(fake)
