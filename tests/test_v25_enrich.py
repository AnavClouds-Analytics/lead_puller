"""Lead Puller 2.5: Icypeas as the second source (ported from the 2.3 Prospeo tests), and the Enrich contacts page.

Every money test ends with check_books (the ledger equals what each Seamless key was charged, and Icypeas's log equals
what Icypeas charged, to the credit) and check_people (nobody paid for twice, every email is that person's own real
mailbox, a Icypeas email only when Icypeas verified it)."""
import collections
import csv
import io
import os

import pytest

from app import db, engine, icypeas
from tests.conftest import US, add_account, add_icypeas, add_verifier, check_books, check_people, run, start
from tests.fakes import is_catch_all
from tests.test_guessing import company_domains, start_list


def icy_on(client):
    if os.environ.get("LP_TEST_ICYPEAS") != "1":
        add_icypeas(client)


def emails_by_source(pid):
    return dict(collections.Counter(r["email_source"] for r in db.q(
        f"SELECT email_source FROM contacts WHERE pull_id=? AND {engine.DELIVERABLE}", (pid,))))


def own_mailbox(fake, c):
    owner = fake.mailboxes.get(c["email"])
    return owner is not None and fake.by_pid[owner]["name"] == c["name"]


# ================================================================ Icypeas fills what Seamless misses
def test_icypeas_fills_seamless_misses_and_books_match(env):
    client, fake, clock = env
    icy_on(client)
    pid = start(client, US, target=150, max_credits=600)
    p = run(client, pid)
    src = emails_by_source(pid)
    assert src.get("icypeas", 0) > 0, src
    assert p["icypeas"]["found"] == src["icypeas"]
    assert p["icypeas"]["credits"] == fake.ic.charged
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND email_source='icypeas'", (pid,)):
        assert c["submitted_at"] is not None                 # Seamless was asked first (default order)
        assert own_mailbox(fake, c), c["email"]
    check_books(fake)
    check_people(fake)


def test_icypeas_is_only_asked_about_people_seamless_had_no_usable_email_for(env):
    client, fake, clock = env
    icy_on(client)
    pid = start(client, US, target=120, max_credits=500)
    run(client, pid)
    asked = {int(x) for x in fake.ic.asked}
    for cid in asked:
        c = db.q1("SELECT * FROM contacts WHERE id=?", (cid,))
        assert c["icy_state"] in ("found", "none", "mismatch", "dup", "error", "retry", "asked", "skipped"), c
        assert c["email_source"] in (None, "icypeas") or (c["email_status"] or "").lower() == "invalid", c
    good_seamless = db.q(f"SELECT id FROM contacts WHERE pull_id=? AND email_source='seamless' AND {engine.DELIVERABLE}",
                         (pid,))
    assert not asked & {r["id"] for r in good_seamless}
    check_books(fake)


def test_icypeas_cap_per_pull(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("ICYPEAS_MAX_CREDITS_PER_PULL", "3")
    monkeypatch.setenv("ICYPEAS_PER_STEP", "5")
    icy_on(client)
    for p in fake.people:
        fake.email_override[p["pid"]] = ""
    pid = start(client, US, target=60, max_credits=300)
    p = run(client, pid, until=("done", "paused"))
    if p["status"] == "paused":
        client.post(f"/api/pulls/{pid}/resume")
        run(client, pid)
    assert icypeas.spent(pid) <= 3 + 4                         # one call may find a few more than the cap left
    assert fake.ic.charged == icypeas.spent(pid)
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE pull_id=? AND icy_state='skipped'", (pid,))["n"] > 0
    check_books(fake)


def test_icypeas_off_means_2_2_behaviour(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("ICYPEAS_ENABLED", "0")
    add_icypeas(client)
    pid = start(client, US, target=60, max_credits=200)
    run(client, pid)
    assert not fake.ic.asked and fake.ic.charged == 0
    assert not db.q1("SELECT 1 x FROM contacts WHERE email_source='icypeas'")
    check_books(fake)
    check_people(fake)


def test_icypeas_switched_off_on_accounts_page(env):
    client, fake, clock = env
    add_icypeas(client)
    assert client.post("/api/icypeas", json={"enabled": False}).json()["enabled"] is False
    pid = start(client, US, target=40, max_credits=200)
    run(client, pid)
    assert not fake.ic.asked
    check_books(fake)


def test_stop_means_no_more_icypeas(env):
    client, fake, clock = env
    icy_on(client)
    for p in fake.people:
        fake.email_override[p["pid"]] = ""
    pid = start(client, US, target=200, max_credits=400)
    for _ in range(6):
        engine.tick()
        clock.t += 1
    client.post(f"/api/pulls/{pid}/stop")
    before = len(fake.ic.asked)
    run(client, pid)
    assert len(fake.ic.asked) == before
    check_books(fake)


def test_target_is_not_overshot_by_icypeas(env):
    client, fake, clock = env
    icy_on(client)
    pid = start(client, US, target=40, max_credits=200)
    run(client, pid)
    st = engine.counts(pid)
    assert st["emails"] <= 40 + 25                           # research is sent in batches; Icypeas never adds a batch more
    check_books(fake)


def test_icypeas_and_two_accounts_with_chaos(env):
    client, fake, clock = env
    icy_on(client)
    add_account(client, fake)
    fake.chaos_poll = True
    fake.flaky_rate = 0.05
    pid = start(client, US, target=150, max_credits=600)
    run(client, pid, ticks=20000, clock=clock)
    check_books(fake)
    check_people(fake)


def test_icypeas_email_that_belongs_to_someone_else_is_not_used(env):
    client, fake, clock = env
    icy_on(client)
    pid = start(client, US, target=30, max_credits=200)
    run(client, pid)
    a, b = db.q(f"SELECT * FROM contacts WHERE pull_id=? AND {engine.DELIVERABLE} LIMIT 2", (pid,))
    engine._apply_icy(dict(b, email=None), a["email"], "test")
    b2 = db.q1("SELECT * FROM contacts WHERE id=?", (b["id"],))
    assert b2["email"] == b["email"] and b2["icy_state"] == "dup"


def test_icypeas_with_guessing_company_lists_and_catch_all(env):
    client, fake, clock = env
    add_verifier(client)
    icy_on(client)
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
    icy_on(client)
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
    assert {r[si] for r in got} <= {"seamless", "icypeas (verified)", "guessed (MillionVerifier: valid)", "already had"}
    check_books(fake)
    check_people(fake)


def test_enrich_twice_never_pays_twice(env):
    client, fake, clock = env
    icy_on(client)
    rows, _ = _people_rows(fake, 30)
    _, pid1 = enrich(client, _csv(rows))
    run(client, pid1)
    s1, p1 = fake.charged, fake.ic.charged
    _, pid2 = enrich(client, _csv(rows))
    p = run(client, pid2)
    assert fake.charged == s1 and fake.ic.charged == p1                       # nothing spent the second time
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


def test_enrich_without_seamless_uses_only_free_checks_and_icypeas(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("ENRICH_USE_SEAMLESS", "0")
    icy_on(client)
    rows, _ = _people_rows(fake, 30)
    _, pid = enrich(client, _csv(rows))
    p = run(client, pid)
    assert fake.charged == 0 and db.pull_spend(pid) == 0
    assert p["icypeas"]["found"] > 0
    check_books(fake)
    check_people(fake)


def test_enrich_max_credits_then_icypeas_for_the_rest(env):
    client, fake, clock = env
    icy_on(client)
    rows, _ = _people_rows(fake, 60)
    _, pid = enrich(client, _csv(rows), max_credits=10)
    p = run(client, pid)
    assert db.pull_spend(pid) <= 10
    assert p["icypeas"]["asked"] > 0
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
    if c["email_source"] in ("guessed", "icypeas"):          # found by its format or by Icypeas instead
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


def test_enrich_with_guessing_and_icypeas_and_chaos(env):
    client, fake, clock = env
    add_verifier(client)
    icy_on(client)
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
    icy_on(client)
    rows, _ = _people_rows(fake, 10)
    _, pid = enrich(client, _csv(rows))
    run(client, pid)
    p = client.get(f"/api/pulls/{pid}").json()
    assert p["enrich"]["rows"] == 10 and "icypeas" in p
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


def test_people_icypeas_had_nothing_for_are_not_asked_again_within_90_days(env):
    client, fake, clock = env
    icy_on(client)
    for p in fake.people:
        fake.email_override[p["pid"]] = ""
    pid = start(client, US, target=40, max_credits=200)
    p = run(client, pid, until=("done", "paused"))
    if p["status"] == "paused":
        client.post(f"/api/pulls/{pid}/resume")
        run(client, pid)
    nothing = db.q("SELECT * FROM contacts WHERE pull_id=? AND icy_state='none'", (pid,))
    assert nothing
    asked_before = len(fake.ic.asked)
    rows = [(c["first_name"], c["last_name"], c["company"], c["domain"], c["li_url"] or "", "", "") for c in nothing]
    _, pid2 = enrich(client, _csv(rows))
    run(client, pid2)
    assert len(fake.ic.asked) == asked_before               # not asked again (and Seamless not paid again)
    check_books(fake)


def test_enrich_pause_resume_stop(env):
    client, fake, clock = env
    icy_on(client)
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