"""Email guessing and company lists: every rule that protects your Seamless and MillionVerifier credits.

Each money test ends with check_books (the tool's ledger equals what each Seamless key was charged, to the credit) and
check_people (nobody paid for twice, every email is that person's own real mailbox, every guess was checked 'ok')."""
import collections
import io
import os

import pytest

from app import db, engine, guess, verifier
from app.config import cfg
from tests.conftest import US, add_account, add_verifier, check_books, check_people, run, start
from tests.fakes import FakeSeamless, is_catch_all, make_people


def first_last_company(d):
    """A fake company whose one email format is first.last (not catch-all, not mixed)."""
    import re
    m = re.fullmatch(r"acme(\d+)\.com", d or "")
    return bool(m) and int(m.group(1)) % 6 == 0 and int(m.group(1)) % 13 != 4 and not is_catch_all(d)


def company_domains(fake, n=40, min_people=2, fmt=None):
    """Websites of fake companies with at least `min_people` US C-Level/VP people (as a Hiring Signal Finder list)."""
    by = collections.Counter(p["domain"] for p in fake.people if p["domain"] and p["country"] == "United States"
                             and p["seniority"] in ("C-Level", "VP"))
    out = [d for d, k in by.most_common() if k >= min_people]
    if fmt is not None:
        out = [d for d in out if fmt(d)]
    return out[:n]


def start_list(client, domains, target=None, per_company=3, max_credits=2000, filters=None, signals=None, name=None):
    items = [{"name": "", "domain": d, "signal": (signals or {}).get(d, "")} for d in domains]
    r = client.post("/api/pulls", json={"filters": filters if filters is not None else
                                        {"seniority": {"in": ["C-Level", "VP"]}},
                                        "target": target or len(domains) * per_company, "max_credits": max_credits,
                                        "max_per_company": per_company, "companies": items, "name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def research_credits(pid):
    return db.pull_spend(pid, "research")


def per_domain(pid):
    out = collections.defaultdict(lambda: {"paid": 0, "guessed": 0, "learners": 0, "rows": []})
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND state NOT IN ('skipped','banked')", (pid,)):
        d = out[c["domain"]]
        d["rows"].append(c)
        d["paid"] += 1 if c["submitted_at"] else 0
        d["guessed"] += 1 if c["email_source"] == "guessed" else 0
        d["learners"] += 1 if (c["guess_note"] or "").startswith("Paid for in Seamless to learn") else 0
    return out


# ---------------------------------------------------------------- the saving itself
def test_guessing_saves_seamless_credits_on_a_company_list(env, tmp_path, monkeypatch):
    client, fake, clock = env
    client.delete("/api/verifier")
    doms = company_domains(fake, 40)
    # Without guessing.
    pid0 = start_list(client, doms)
    run(client, pid0)
    without = research_credits(pid0)
    emails0 = engine.counts(pid0)["emails"]
    check_books(fake)
    check_people(fake)
    # Same list, fresh database and fresh Seamless, with guessing on.
    db.reset_for_tests(str(tmp_path / "t2.db"))
    fake2 = FakeSeamless(seed=int(os.environ.get("FAKE_SEED", "1")))
    fake2.clock = clock
    import httpx
    from app import seamless
    from tests.fakes import FakeMillionVerifier
    mv = FakeMillionVerifier(fake2)
    fake2.mv = mv
    monkeypatch.setattr(seamless, "TRANSPORT", httpx.MockTransport(fake2.handler))
    monkeypatch.setattr(verifier, "TRANSPORT", httpx.MockTransport(mv.handler))
    monkeypatch.setattr(seamless, "limiter", seamless._Limiter())
    assert client.post("/api/auth/login", json={"password": "test-pass-123"}).status_code == 200
    assert client.post("/api/accounts", json={"name": "Account A", "api_key": "key-alpha-1111"}).status_code == 200
    add_verifier(client)
    pid1 = start_list(client, doms)
    p = run(client, pid1)
    with_g = research_credits(pid1)
    assert p["guessing"]["guessed"] > 0
    assert with_g < without, (with_g, without)
    # Same people found, at least as many emails (guessing can also find people Seamless had no email for).
    assert engine.counts(pid1)["emails"] >= emails0 - 1
    assert p["guessing"]["saved"] == p["guessing"]["guessed"]
    check_books(fake2)
    check_people(fake2)


def test_only_a_valid_answer_is_used_everything_else_is_paid_in_seamless(env):
    client, fake, clock = env
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 50))
    run(client, pid)
    log = db.q("SELECT * FROM verify_log WHERE pull_id=?", (pid,))
    assert log
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND email_source='guessed'", (pid,)):
        assert any(r["email"] == c["email"] and r["result"] == "ok" for r in log)
    # People none of whose checks came back valid: researched in Seamless (never dropped, never given a guess).
    # (2.2: a person can have a few invalid formats before the one that exists; a made-up address is never used.)
    ok_people = {r["contact_id"] for r in log if r["result"] == "ok" and not r["email"].startswith("zq")}
    failed = {r["contact_id"] for r in log if r["result"] != "ok"} - ok_people
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND email_source='guessed'", (pid,)):
        assert not c["email"].startswith("zq")
    for cid in failed:
        c = db.q1("SELECT * FROM contacts WHERE id=?", (cid,))
        assert c["email_source"] != "guessed"
        assert c["submitted_at"] is not None or c["state"] in ("kept", "banked", "skipped")
    check_books(fake)
    check_people(fake)


def test_only_full_name_formats_are_guessed(env):
    """john@ or jsmith@ could be another John / J. Smith at the company: never guessed (paid in Seamless)."""
    client, fake, clock = env
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 60))
    run(client, pid)
    assert fake.mv.checks
    for r in db.q("SELECT v.email, c.first_name, c.last_name FROM verify_log v JOIN contacts c ON c.id=v.contact_id "
                  "WHERE v.cached=0 AND v.email NOT LIKE 'zq%'"):          # 2.2: the made-up address check
        parts = guess.name_parts(r["first_name"], r["last_name"])
        pats = guess.patterns_of(r["email"].split("@")[0], *parts)
        assert pats and set(pats) <= guess.FULL_NAME, (r["email"], pats)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND email_source='guessed'", (pid,)):
        parts = guess.name_parts(c["first_name"], c["last_name"])
        assert parts[0] in c["email"] and parts[1] in c["email"]
    check_people(fake)


def test_catch_all_companies_cost_at_most_one_check_and_are_then_never_guessed(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 60)
    assert any(is_catch_all(d) for d in doms)
    pid = start_list(client, doms)
    run(client, pid)
    by = collections.Counter(e.split("@")[1] for e in fake.mv.checks)
    for d, n in by.items():
        if is_catch_all(d):
            assert n == 1, (d, n)
            assert guess.domain_state(d)["catch_all"]
    ca = [d for d in doms if is_catch_all(d)]
    assert not db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND email_source='guessed' AND domain IN (%s)" %
                     ",".join("?" * len(ca)), (pid, *ca))
    check_books(fake)
    check_people(fake)


def test_at_a_new_company_one_person_is_paid_first_to_learn_the_format(env):
    client, fake, clock = env
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 40, min_people=3))
    run(client, pid)
    for d, x in per_domain(pid).items():
        # A second learner is only paid for when the first one's answer couldn't teach the format.
        taught = sum(1 for c in x["rows"] if (c["guess_note"] or "").startswith("Paid for in Seamless to learn")
                     and c["email"] and db.q1("SELECT 1 x FROM email_examples WHERE email=?", (c["email"].lower(),)))
        assert taught <= 1, (d, taught)
    # At companies where guessing worked, only the learner(s) and failed guesses were paid for.
    for d, x in per_domain(pid).items():
        if x["guessed"]:
            failed = sum(1 for c in x["rows"] if c["submitted_at"] and c["guess_done"])
            assert x["paid"] <= x["learners"] + failed + sum(1 for c in x["rows"] if c["submitted_at"] and
                                                             not c["guess_note"])
    check_books(fake)
    check_people(fake)


def test_formats_from_your_seamless_saved_contacts_mean_no_learner_is_needed(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 30, min_people=3)
    # Everyone except the most junior was researched on the Seamless website earlier (saved contacts, free to read).
    first = {}
    for p in fake.people:
        if p["domain"] in doms and p["domain"] not in first and p["country"] != "United States":
            first[p["domain"]] = p
    for p in fake.people:
        if p["domain"] in doms and p["domain"] not in first:
            first[p["domain"]] = p
    for d, p in first.items():
        fake.researched_in("key-alpha-1111")[p["pid"]] = 1
        fake.charged_by_key["key-alpha-1111"] = fake.charged_by_key.get("key-alpha-1111", 0)
        fake.saved.setdefault("key-alpha-1111", {})[p["pid"]] = fake.record(p)
    db.x("DELETE FROM saved_sync")
    pre = sum(fake.charged_by_key.values())
    pid = start_list(client, doms)
    run(client, pid)
    learners = sum(x["learners"] for x in per_domain(pid).values())
    known = sum(1 for d in doms if guess.formats(d))
    assert learners < len(doms) - known // 2
    fake.charged_by_key["key-alpha-1111"] -= 0
    assert sum(fake.charged_by_key.values()) >= pre


# ---------------------------------------------------------------- when MillionVerifier can't help: pay as usual
def test_no_key_means_no_checks_and_everything_works_as_before(env):
    client, fake, clock = env
    client.delete("/api/verifier")
    pid = start_list(client, company_domains(fake, 20))
    p = run(client, pid)
    assert fake.mv.checks == [] and p["guessing"]["guessed"] == 0
    check_books(fake)
    check_people(fake)


def test_low_millionverifier_balance_stops_guessing(env, monkeypatch):
    client, fake, clock = env
    fake.mv.credits = 150                              # below GUESS_MIN_CHECKS_LEFT=200
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 20))
    run(client, pid)
    assert fake.mv.checks == []
    check_books(fake)
    check_people(fake)


def test_millionverifier_running_out_mid_pull(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("GUESS_MIN_CHECKS_LEFT", "0")
    fake.mv.credits = 5
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 40))
    p = run(client, pid)
    assert len(fake.mv.checks) == 5                   # it ran out; nothing was checked after that
    assert p["status"] == "done"
    check_books(fake)
    check_people(fake)


@pytest.mark.parametrize("mode", ["error", "timeout", "500", "garbage", "no_credits"])
def test_millionverifier_errors_never_count_as_valid_and_pause_guessing(env, mode):
    client, fake, clock = env
    add_verifier(client)
    fake.mv.fail_next = [mode] * 50
    pid = start_list(client, company_domains(fake, 30))
    p = run(client, pid)
    assert p["guessing"]["guessed"] == 0
    errs = db.q1("SELECT COUNT(*) n FROM verify_log WHERE result='error'")["n"]
    assert 1 <= errs <= 3 if mode != "no_credits" else errs >= 1   # 3 errors in a row: guessing pauses 30 min
    assert verifier.status()["last_error"]
    check_books(fake)
    check_people(fake)


def test_same_address_is_never_checked_twice(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 40)
    run(client, start_list(client, doms))
    run(client, start_list(client, doms, name="again"))
    assert len(fake.mv.checks) == len(set(fake.mv.checks))
    check_books(fake)
    check_people(fake)


def test_a_company_whose_guesses_keep_failing_is_given_up(env):
    """Seamless keeps returning first.last addresses, but the company's real mailboxes are something else, so
    every guess comes back invalid. After 3 failed checks there, no more checks are spent at that company."""
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 6, min_people=4, fmt=lambda d: not is_catch_all(d))
    for p in fake.people:
        if p["domain"] in doms:
            fake.mailboxes.pop(p["email"], None)
            p["email"] = f"x{p['pid']}@{p['domain']}"                 # real mailbox: not guessable
            fake.mailboxes[p["email"]] = p["pid"]
            # Seamless's (stale) answer: first.last, which doesn't exist any more.
            fake.email_override[p["pid"]] = f"{guess._ascii(p['first'])}.{guess._ascii(p['last'])}@{p['domain']}"
    pid = start_list(client, doms, per_company=10, filters={"contactCountry": {"in": ["United States"]}})
    run(client, pid)
    by = collections.Counter(e.split("@")[1] for e in fake.mv.checks)
    assert by, "the test needs some guesses"
    for d, n in by.items():
        # 2.2: at most 1 made-up address + the common formats once (first person) + 3 failed guesses, then given up
        assert n <= 1 + cfg.GUESS_PROBE_FORMATS + 3, (d, n)
    assert not db.q1("SELECT 1 x FROM contacts WHERE email_source='guessed'")
    check_books(fake)


def test_unknown_answers_are_never_used(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 40)
    fake.mv.unknown_domains = set(doms)
    pid = start_list(client, doms)
    run(client, pid)
    assert fake.mv.checks and not db.q1("SELECT 1 x FROM contacts WHERE email_source='guessed'")
    check_books(fake)
    check_people(fake)


def test_an_address_someone_else_already_has_is_never_guessed(env):
    """Zora Quill at a first.last company; zora.quill@ is another Zora Quill's (in your memory from Seamless, never in
    these results). Guessing zora.quill@ for her would reach the wrong person."""
    from app import memory
    client, fake, clock = env
    add_verifier(client)
    d = company_domains(fake, 50, min_people=2, fmt=first_last_company)[0]
    base = next(p for p in fake.people if p["domain"] == d)
    other = dict(base, pid=7000, first="Zora", last="Quill", name="Zora Quill", country="Germany",
                 liUrl="https://www.linkedin.com/in/zora-quill-old/", email=f"zora.quill@{d}")
    me = dict(base, pid=7001, first="Zora", last="Quill", name="Zora Quill", country="United States",
              seniority="VP", title="VP Engineering", liUrl="https://www.linkedin.com/in/zora-quill-new/",
              email=f"zora.quill2@{d}")
    for p in (other, me):
        fake.people.append(p)
        fake.by_pid[p["pid"]] = p
        fake.mailboxes[p["email"]] = p["pid"]
    memory.remember([f"em:zora.quill@{d}"], "seamless", None, 1, "email")
    guess.learn("Zed", "Example", f"zed.example@{d}", d, "seamless")     # the company's format is already known
    pid = start_list(client, [d], per_company=10, filters={"contactCountry": {"in": ["United States"]}})
    run(client, pid)
    assert f"zora.quill@{d}" not in fake.mv.checks
    zora = db.q1("SELECT * FROM contacts WHERE pull_id=? AND name='Zora Quill'", (pid,))
    assert zora and zora["email_source"] != "guessed"
    check_people(fake)


def test_a_millionverifier_answer_is_reused_not_paid_again(env):
    client, fake, clock = env
    add_verifier(client)
    assert verifier.check("someone@acme1.com") in ("ok", "invalid")
    assert verifier.check("Someone@ACME1.com") in ("ok", "invalid")
    assert fake.mv.checks == ["someone@acme1.com"]
    clock.t += 31 * 86400                                          # GUESS_CACHE_DAYS=30
    verifier.check("someone@acme1.com")
    assert len(fake.mv.checks) == 2


def test_people_outside_your_list_are_never_paid_for_even_if_seamless_returns_them(env):
    client, fake, clock = env
    add_verifier(client)
    fake.leak_domains = True
    doms = company_domains(fake, 20)
    pid = start_list(client, doms)
    run(client, pid)
    assert db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND skip_kind='not_listed'", (pid,))
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND (submitted_at IS NOT NULL OR email_source='guessed')",
                  (pid,)):
        assert c["domain"] in doms
    check_books(fake)
    check_people(fake)


def test_people_waiting_for_a_format_that_never_comes_are_not_stuck(env):
    """Seamless answers never say who they're for: the person paid to learn a company's format is never matched.
    Their colleagues must not wait for ever."""
    client, fake, clock = env
    add_verifier(client)
    fake.drop_sid = True
    fake.shuffle_request_ids = True
    pid = start_list(client, company_domains(fake, 30, min_people=3))
    p = run(client, pid)
    assert p["status"] == "done"
    check_books(fake)
    check_people(fake)


def test_namesakes_at_one_company_are_never_guessed(env):
    """Two Anna Kims at one company (different LinkedIn): a guess could reach the wrong one, so both are paid."""
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 10, min_people=3, fmt=first_last_company)
    assert doms
    d = doms[0]
    base = next(p for p in fake.people if p["domain"] == d)
    for k, li in enumerate(("anna-kim-a", "anna-kim-b")):
        pid = 5000 + k
        p = dict(base, pid=pid, first="Anna", last="Kim", name="Anna Kim", liUrl=f"https://www.linkedin.com/in/{li}/",
                 seniority="C-Level", country="United States", title="CTO")
        p["email"] = "anna.kim@" + d if k == 0 else "anna.kim2@" + d
        fake.people.append(p)
        fake.by_pid[pid] = p
        fake.mailboxes[p["email"]] = pid
    pid = start_list(client, [d], per_company=10)
    run(client, pid)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND first_name='Anna' AND last_name='Kim'", (pid,)):
        assert c["email_source"] != "guessed"
    assert "anna.kim@" + d not in fake.mv.checks or db.q1(
        "SELECT 1 x FROM contacts WHERE email=? AND email_source='seamless'", ("anna.kim@" + d,))
    check_books(fake)
    check_people(fake)


# ---------------------------------------------------------------- never paid twice, on any account
def test_guessed_people_are_never_paid_for_later_on_either_account(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 40)
    p1 = start_list(client, doms)
    run(client, p1)
    guessed = {(c["name"], c["company"]) for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND "
                                                        "email_source='guessed'", (p1,))}
    assert guessed
    b = add_account(client, fake)
    client.patch(f"/api/accounts/{b['id']}", json={"priority": 0})
    p2 = start_list(client, doms, name="other account")
    run(client, p2)
    run(client, start(client, US, target=200, max_credits=1000, account=str(b["id"])))
    for c in db.q("SELECT * FROM contacts WHERE pull_id>? AND submitted_at IS NOT NULL", (p1,)):
        assert (c["name"], c["company"]) not in guessed
    check_books(fake)
    check_people(fake)


def test_stop_means_no_more_checks(env):
    client, fake, clock = env
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 60))
    for _ in range(12):
        engine.tick()
    client.post(f"/api/pulls/{pid}/stop", json={})
    n = len(fake.mv.checks)
    run(client, pid)
    assert len(fake.mv.checks) == n
    check_books(fake)
    check_people(fake)


def test_guesses_do_not_count_towards_the_first_batch_email_rate(env):
    client, fake, clock = env
    add_verifier(client)
    pid = start(client, US, target=120, max_credits=600)
    run(client, pid)
    r = engine.hit_rate(pid)
    paid = db.q("SELECT * FROM contacts WHERE pull_id=? AND submitted_at IS NOT NULL AND state IN "
                "('done','no_email','failed') AND filled_free=0", (pid,))
    if len(paid) >= 20:
        expect = sum(1 for c in paid if c["state"] == "done" and c["email"] and c["dup_email"] == 0 and
                     (c["email_status"] or "").lower() != "invalid") / len(paid)
        assert abs(r - max(0.05, expect)) < 1e-9
    check_books(fake)
    check_people(fake)


def test_normal_pulls_guess_where_the_format_is_known(env):
    client, fake, clock = env
    add_verifier(client)
    run(client, start(client, US, target=150, max_credits=700))
    p2 = run(client, start(client, US, target=150, max_credits=700))
    assert p2["guessing"]["guessed"] >= 1
    check_books(fake)
    check_people(fake)


# ---------------------------------------------------------------- company lists
def test_company_list_file_is_read_and_checked_for_free(env):
    client, fake, clock = env
    client.post("/api/exclusions", json={"text": "blocked-client.com"})
    csv = ("Company Name,Website,Hiring Signal,Posted\n"
           "Northwind Health,https://www.northwind-health.com/careers,Hiring: Salesforce Developer,2026-10-01\n"
           "BrightPath,brightpath.io,Raised $12M,2026-10-02\n"
           "Harbor Freight Partners,,Hiring: Salesforce Admin,2026-10-03\n"
           "Northwind again,northwind-health.com,dup,2026-10-04\n"
           "Blocked Co,blocked-client.com,Hiring: React,2026-10-05\n"
           "Bad,not a website,Hiring,2026-10-05\n")
    n0 = len(fake.calls)
    r = client.post("/api/companies/upload", files={"file": ("hiring-signals.csv", io.BytesIO(csv.encode()), "text/csv")})
    assert r.status_code == 200, r.text
    d = r.json()
    st = [i["status"] for i in d["items"]]
    assert st == ["ready", "ready", "no_website", "duplicate", "dnp", "no_website"]
    assert d["items"][0]["domain"] == "northwind-health.com" and d["items"][0]["signal"] == "Hiring: Salesforce Developer"
    assert d["summary"]["ready"] == 2
    assert len(fake.calls) == n0                       # checking a list costs nothing
    r = client.post("/api/companies/check", json={"text": "acme1.com\nAcme 2, acme2.com\nhttps://www.acme3.com/jobs\n"})
    assert [i["domain"] for i in r.json()["items"]] == ["acme1.com", "acme2.com", "acme3.com"]


def test_company_list_pull_only_pays_for_listed_companies_and_respects_per_company(env):
    client, fake, clock = env
    doms = company_domains(fake, 25)
    pid = start_list(client, doms, per_company=2)
    run(client, pid)
    cnt = collections.Counter()
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND (submitted_at IS NOT NULL OR email_source='guessed')",
                  (pid,)):
        assert c["domain"] in doms
        cnt[c["domain"]] += 1
    # 2.2: someone without an email doesn't count, so a company can get one more try (EXTRA_TRIES_PER_COMPANY)...
    assert cnt and max(cnt.values()) <= 2 + cfg.EXTRA_TRIES_PER_COMPANY
    emails = collections.Counter(c["domain"] for c in db.q(f"SELECT * FROM contacts WHERE pull_id=? AND {engine.DELIVERABLE}",
                                                            (pid,)))
    assert max(emails.values()) <= 2                    # ...but never ends with more emails than you asked for
    for d, n in cnt.items():                            # and an extra try only after someone came back without one
        if n > 2:
            assert db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND domain=? AND (state IN ('no_email','failed') "
                         "OR LOWER(COALESCE(email_status,''))='invalid')", (pid, d))
    for who, body in fake.searches:
        assert len(body.get("companyDomain") or []) <= 100
        assert set(body["companyDomain"]) <= set(doms)
        for k in ("industry", "companySize", "technologies"):
            assert k not in body
    check_books(fake)
    check_people(fake)


def test_company_list_keeps_the_most_senior_first_on_a_page(env):
    client, fake, clock = env
    d = next(dom for dom in company_domains(fake, 200, min_people=2)
             if {p["seniority"] for p in fake.people if p["domain"] == dom and p["country"] == "United States"}
             >= {"VP", "C-Level"})
    # Seamless lists the VP before the CTO on the page.
    fake.people.sort(key=lambda p: (p["domain"] != d, p["seniority"] != "VP"))
    pid = start_list(client, [d], per_company=1, target=1, filters={"contactCountry": {"in": ["United States"]}})
    run(client, pid)
    kept = db.q("SELECT * FROM contacts WHERE pull_id=? AND state NOT IN ('skipped','banked')", (pid,))
    assert len(kept) == 1 and kept[0]["seniority"] == "C-Level"
    check_books(fake)


def test_full_companies_are_left_out_of_later_searches(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("PAGE_SIZE", "20")
    doms = company_domains(fake, 80, min_people=1)
    pid = start_list(client, doms, per_company=1, filters={"contactCountry": {"in": ["United States"]}})
    run(client, pid)
    sl = db.q("SELECT * FROM pull_slices WHERE pull_id=? ORDER BY idx", (pid,))
    assert any("still to fill" in s["label"] for s in sl), [s["label"] for s in sl]
    check_books(fake)
    check_people(fake)


def test_signal_goes_into_the_download_and_rerun_keeps_the_list(env):
    client, fake, clock = env
    doms = company_domains(fake, 15)
    sig = {d: f"Hiring: Salesforce Developer ({d})" for d in doms}
    pid = start_list(client, doms, signals=sig)
    run(client, pid)
    r = client.get(f"/api/pulls/{pid}/download?kind=anavmail&columns=email,company,domain,signal,email_source")
    lines = r.text.lstrip("\ufeff").strip().splitlines()
    assert lines[0] == "email,company,domain,signal,email_source"
    for line in lines[1:]:
        dom = line.split(",")[2]
        assert sig[dom] in line
    r = client.post(f"/api/pulls/{pid}/rerun", json={})
    new = r.json()["id"]
    assert db.q1("SELECT COUNT(*) n FROM pull_companies WHERE pull_id=?", (new,))["n"] == len(doms)
    run(client, new)
    first = {(c["name"], c["company"]) for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND "
                                                      "(submitted_at IS NOT NULL OR email_source='guessed')", (pid,))}
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND (submitted_at IS NOT NULL OR email_source='guessed')",
                  (new,)):
        assert (c["name"], c["company"]) not in first
    check_books(fake)
    check_people(fake)


def test_company_list_with_guessing_and_two_accounts_and_chaos(env):
    client, fake, clock = env
    add_verifier(client)
    add_account(client, fake, balance=60)
    fake.chaos_poll = True
    fake.flaky_rate = 0.1
    fake.mv.fail_next = ["timeout", "500"]
    pid = start_list(client, company_domains(fake, 70), max_credits=3000)
    run(client, pid, ticks=20000, clock=clock)
    check_books(fake)
    check_people(fake)


def test_same_name_at_two_companies_never_swap_emails(env):
    """Seamless answers without saying who they're for, in shuffled order: two Anna Duboises at two companies must
    each get their own email (or none), never the other's."""
    client, fake, clock = env
    doms = company_domains(fake, 2, min_people=1, fmt=lambda d: not is_catch_all(d))
    for k, d in enumerate(doms):
        base = next(p for p in fake.people if p["domain"] == d)
        p = dict(base, pid=6000 + k, first="Anna", last="Dubois", name="Anna Dubois", liUrl="", seniority="C-Level",
                 country="United States", title="CTO", email=f"anna.dubois@{d}")
        fake.people.insert(0, p)
        fake.by_pid[p["pid"]] = p
        fake.mailboxes[p["email"]] = p["pid"]
    fake.drop_sid = True
    fake.shuffle_request_ids = True
    pid = start_list(client, doms, per_company=3, filters={"contactCountry": {"in": ["United States"]}})
    run(client, pid)
    annas = db.q("SELECT * FROM contacts WHERE pull_id=? AND name='Anna Dubois' AND (submitted_at IS NOT NULL OR "
                 "email_source='guessed')", (pid,))
    assert len(annas) == 2
    for a in annas:                                   # each Anna has her own company's address (or none), never the other's
        assert not a["email"] or a["email"].endswith("@" + a["domain"])
    check_books(fake)
    check_people(fake)


def test_long_company_lists_are_searched_in_batches_of_100(env):
    client, fake, clock = env
    doms = company_domains(fake, 250, min_people=1)
    assert len(doms) > 100
    pid = start_list(client, doms, per_company=1, filters={"contactCountry": {"in": ["United States"]}},
                     max_credits=5000)
    run(client, pid)
    assert all(len(b.get("companyDomain") or []) <= 100 for _, b in fake.searches)
    assert len({tuple(b["companyDomain"]) for _, b in fake.searches}) >= 2
    check_books(fake)
    check_people(fake)


def test_company_filters_are_ignored_on_a_company_list(env):
    client, fake, clock = env
    doms = company_domains(fake, 20)
    pid = start_list(client, doms, filters={"seniority": {"in": ["C-Level", "VP"]},
                                            "industry": {"in": ["Computer Software"]},
                                            "companySize": {"in": ["2 - 10"]}})
    run(client, pid)
    assert fake.searches
    for _, body in fake.searches:
        assert "industry" not in body and "companySize" not in body and body.get("seniority")
    check_books(fake)


def test_guessed_people_are_in_every_download(env):
    client, fake, clock = env
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 40))
    run(client, pid)
    guessed = [c["email"] for c in db.q("SELECT email FROM contacts WHERE pull_id=? AND email_source='guessed'", (pid,))]
    assert guessed
    from app import exports
    everything = {c["email"] for c in exports.all_rows(pid)}
    anavmail = {c["email"] for c in exports.anavmail_rows(pid)}
    assert set(guessed) <= everything and set(guessed) <= anavmail
    r = client.get(f"/api/pulls/{pid}/download?kind=all&format=csv&columns=email,email_source")
    assert "guessed (MillionVerifier: valid)" in r.text
