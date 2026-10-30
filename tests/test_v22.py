"""Lead Puller 2.2: fewer Seamless credits per email on company lists.

- At a new company the common full-name formats are tried with MillionVerifier before paying Seamless.
- A made-up address is checked first, once per company: a server that says "valid" to anything is never guessed.
- Companies bigger than GUESS_MAX_COMPANY_SIZE are never guessed.
- Full companies leave the search as soon as that saves more than it costs, and come back once if someone had no email.
- Every test ends with check_books / check_people (books match Seamless to the credit, nobody paid twice,
  every email is that person's own mailbox).
"""
import collections
import os

import httpx
import pytest

from app import db, engine, guess, seamless, verifier
from tests.conftest import add_verifier, check_books, check_people, run
from tests.fakes import FakeMillionVerifier, FakeSeamless, is_catch_all
from tests.test_guessing import company_domains, start_list

OLD_RULES = {"GUESS_PROBE": "0", "RESLICE_SMART": "0", "EXTRA_TRIES_PER_COMPANY": "0", "GUESS_MAX_COMPANY_SIZE": "0"}


def fresh(client, monkeypatch, tmp_path, name, people=None, verifier_on=True):
    """A brand-new database and a brand-new Seamless with the same people, so two runs can be compared fairly."""
    db.reset_for_tests(str(tmp_path / f"{name}.db"))
    fake = FakeSeamless(people=people, seed=int(os.environ.get("FAKE_SEED", "1")))
    fake.clock = engine.NOW
    mv = FakeMillionVerifier(fake)
    fake.mv = mv
    monkeypatch.setattr(seamless, "TRANSPORT", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(verifier, "TRANSPORT", httpx.MockTransport(mv.handler))
    monkeypatch.setattr(seamless, "limiter", seamless._Limiter())
    assert client.post("/api/auth/login", json={"password": "test-pass-123"}).status_code == 200
    assert client.post("/api/accounts", json={"name": "Account A", "api_key": "key-alpha-1111"}).status_code == 200
    if verifier_on:
        add_verifier(client)
    return fake


def books(pid):
    c = engine.counts(pid)
    return {"search": db.pull_spend(pid, "search"), "research": db.pull_spend(pid, "research"), "emails": c["emails"]}


def big_and_small_companies():
    """5 big companies with 40 matching people each (listed first by Seamless, as big firms usually are) and 25 small
    ones with 3 each. Everyone's address is first.last@ and every server is honest."""
    people, pid = [], 0
    firsts = ["John", "Priya", "Anna", "Mark", "Wei", "Sara", "Omar", "Liam", "Noa", "Dan", "Ava", "Raj", "Eli",
              "Maya", "Tom", "Lea", "Ben", "Ida", "Kai", "Zoe"]
    lasts = ["Smith", "Cohen", "Garcia", "Chen", "Patel", "Levi", "Brown", "Rossi", "Kim", "Stone", "Hart", "Wolfe"]

    def add(co, i):
        nonlocal pid
        f, l = firsts[i % len(firsts)], lasts[(i // len(firsts) + co) % len(lasts)]
        d = f"firm{co}.com"
        people.append({"pid": pid, "first": f, "last": l, "name": f"{f} {l}", "company": f"Firm {co}", "domain": d,
                       "title": "VP Engineering", "seniority": "VP", "department": "Engineering",
                       "country": "United States", "liUrl": "", "industries": ["Computer Software"],
                       "size": "201 - 500", "techs": [], "lastModifiedAt": "2026-08-01 10:00:00",
                       "email": f"{f.lower()}.{l.lower()}@{d}"})
        pid += 1
    for i in range(40):
        for co in range(5):
            add(co, i)
    for co in range(5, 30):
        for i in range(3):
            add(co, i)
    return people


# ---------------------------------------------------------------- 1. trying the common formats
def test_probe_finds_first_person_free_and_teaches_the_format(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 40, min_people=2)
    pid = start_list(client, doms)
    p = run(client, pid)
    probed = db.q("SELECT * FROM contacts WHERE pull_id=? AND guess_how='probe'", (pid,))
    assert probed, "nobody was found by trying the common formats"
    for c in probed:
        assert c["submitted_at"] is None and c["request_id"] is None      # never paid for in Seamless
        a, b = guess.name_parts(c["first_name"], c["last_name"])[:2]
        assert c["email"].split("@")[0] in (f"{a}.{b}", f"{a}{b}", f"{a}_{b}"), c["email"]
    assert p["guessing"]["probed"] == len([c for c in probed if c["email_source"] == "guessed"])
    check_books(fake)
    check_people(fake)


def test_probe_never_guesses_initials_or_first_name_only(env):
    """Companies using john@ or jsmith@ are never probed into a guess; the person is paid for in Seamless."""
    client, fake, clock = env
    add_verifier(client)
    import re
    def initial_fmt(d):
        m = re.fullmatch(r"acme(\d+)\.com", d or "")
        return bool(m) and int(m.group(1)) % 6 in (1, 2, 5) and int(m.group(1)) % 13 != 4 and not is_catch_all(d)
    doms = company_domains(fake, 30, min_people=1, fmt=initial_fmt)
    assert doms
    pid = start_list(client, doms)
    run(client, pid)
    assert not db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND email_source='guessed'", (pid,))
    check_books(fake)
    check_people(fake)


def test_made_up_address_is_checked_once_and_accept_all_servers_are_never_guessed(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 40, min_people=2)
    big = {p["domain"] for p in fake.people if guess.too_big(p)}
    trap = [d for d in doms if not is_catch_all(d) and d not in big][:6]
    fake.mv.accept_all_domains |= set(trap)               # MillionVerifier says "ok" to anything there
    pid = start_list(client, doms)
    run(client, pid)
    made_up = collections.Counter(e.split("@")[1] for e in fake.mv.checks if e.startswith("zq"))
    assert made_up and max(made_up.values()) == 1, made_up    # once per company, never again
    for d in trap:
        assert not db.q1("SELECT 1 x FROM contacts WHERE domain=? AND email_source='guessed'", (d,)), d
        assert guess.domain_state(d)["catch_all"]
        assert sum(1 for e in fake.mv.checks if e.endswith("@" + d)) <= 1   # only the made-up address
    check_books(fake)
    check_people(fake)


def test_made_up_address_is_never_checked_twice_across_pulls(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 20, min_people=2)
    run(client, start_list(client, doms, per_company=1))
    first = [e for e in fake.mv.checks if e.startswith("zq")]
    run(client, start_list(client, doms, per_company=3))
    second = [e for e in fake.mv.checks if e.startswith("zq")]
    assert collections.Counter(e.split("@")[1] for e in second) == collections.Counter(e.split("@")[1] for e in first)
    check_books(fake)
    check_people(fake)


def test_probe_off_works_exactly_like_2_1(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("GUESS_PROBE", "0")
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 40, min_people=2))
    run(client, pid)
    assert not [e for e in fake.mv.checks if e.startswith("zq")]
    assert not db.q1("SELECT 1 x FROM contacts WHERE guess_how='probe'")
    check_books(fake)
    check_people(fake)


def test_probe_formats_setting_limits_checks(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("GUESS_PROBE_FORMATS", "1")
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 40, min_people=2))
    run(client, pid)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND guess_how='probe'", (pid,)):
        assert c["email"].split("@")[0].count(".") == 1       # only first.last@ was tried
    for d, n in collections.Counter(e.split("@")[1] for e in fake.mv.checks).items():
        probes = db.q1("SELECT probe_tries t FROM domain_guess WHERE domain=?", (d,))
        if probes:
            assert probes["t"] <= 2
    check_books(fake)
    check_people(fake)


def test_a_company_is_probed_at_most_twice(env):
    client, fake, clock = env
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 80, min_people=1), per_company=3)
    run(client, pid)
    assert all(r["probe_tries"] <= 2 for r in db.q("SELECT probe_tries FROM domain_guess"))
    check_books(fake)
    check_people(fake)


def test_namesakes_are_never_probed(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 10, min_people=2)
    d = doms[0]
    at = [p for p in fake.people if p["domain"] == d and p["country"] == "United States"
          and p["seniority"] in ("C-Level", "VP")]
    twin = dict(at[0], pid=max(fake.by_pid) + 1, email=at[0]["email"].replace("@", "2@"),
                liUrl="", title="CTO")
    fake.people.insert(0, twin)
    fake.by_pid[twin["pid"]] = twin
    fake.mailboxes[twin["email"]] = twin["pid"]
    pid = start_list(client, doms)
    run(client, pid)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND domain=? AND first_name=? AND last_name=?",
                  (pid, d, at[0]["first"], at[0]["last"])):
        assert c["email_source"] != "guessed", c["email"]
    check_books(fake)
    check_people(fake)


def test_big_companies_are_never_guessed(env, monkeypatch):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 120, min_people=1)
    big = {p["domain"] for p in fake.people if p["size"] in ("5,001 - 10,000", "10,001+")}
    assert big & set(doms)
    pid = start_list(client, doms)
    run(client, pid)
    for d in big & set(doms):
        assert not any(e.endswith("@" + d) for e in fake.mv.checks), d
        assert not db.q1("SELECT 1 x FROM contacts WHERE domain=? AND email_source='guessed'", (d,))
    check_books(fake)
    check_people(fake)


def test_size_limit_zero_means_any_size(env, monkeypatch):
    monkeypatch.setenv("GUESS_MAX_COMPANY_SIZE", "0")
    assert not guess.too_big({"size": "10,001+"})
    monkeypatch.setenv("GUESS_MAX_COMPANY_SIZE", "5000")
    assert guess.too_big({"size": "10,001+"}) and guess.too_big({"size": "5,001 - 10,000"})
    assert not guess.too_big({"size": "1,001 - 5,000"}) and not guess.too_big({"size": ""})


def test_unknown_answer_to_the_made_up_address_means_no_guess(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 20, min_people=2)
    fake.mv.unknown_domains |= set(doms)
    pid = start_list(client, doms)
    run(client, pid)
    assert not db.q1("SELECT 1 x FROM contacts WHERE pull_id=? AND email_source='guessed'", (pid,))
    check_books(fake)
    check_people(fake)


def test_millionverifier_errors_during_probe_never_count_as_valid(env):
    client, fake, clock = env
    add_verifier(client)
    fake.mv.fail_next = ["error", "timeout", "500", "garbage"] * 3
    pid = start_list(client, company_domains(fake, 30, min_people=2))
    run(client, pid)
    check_books(fake)
    check_people(fake)


# ---------------------------------------------------------------- 2. full companies & extra tries
def test_extra_tries_only_after_no_email_and_never_beyond_the_limit(env, monkeypatch):
    client, fake, clock = env
    pid = start_list(client, company_domains(fake, 80, min_people=1), per_company=2)
    run(client, pid)
    by = collections.defaultdict(list)
    for c in db.q("SELECT * FROM contacts WHERE pull_id=? AND state IN ('kept','queued','done','unconfirmed',"
                  "'timed_out','no_email','failed') ORDER BY id", (pid,)):
        by[c["domain"]].append(c)
    for d, rows in by.items():
        assert len(rows) <= 3, (d, len(rows))                     # 2 + EXTRA_TRIES_PER_COMPANY
        usable = lambda c: c["state"] == "done" and c["email"] and (c["email_status"] or "").lower() != "invalid" \
            and not c["dup_email"]
        assert sum(1 for c in rows if usable(c)) <= 2, d
        if len(rows) == 3:                                          # a 3rd only because one came back unusable
            assert any(not usable(c) for c in rows[:2]), d
    check_books(fake)
    check_people(fake)


def test_extra_tries_zero_means_at_most_cap(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("EXTRA_TRIES_PER_COMPANY", "0")
    pid = start_list(client, company_domains(fake, 80, min_people=1), per_company=2)
    run(client, pid)
    for r in db.q("""SELECT domain, COUNT(*) n FROM contacts WHERE pull_id=? AND state IN ('kept','queued','done',
                     'unconfirmed','timed_out','no_email','failed') GROUP BY domain""", (pid,)):
        assert r["n"] <= 2, dict(r)
    check_books(fake)
    check_people(fake)


def test_full_companies_leave_the_search_and_save_search_credits(env, monkeypatch, tmp_path):
    client, _, clock = env
    monkeypatch.setenv("PAGE_SIZE", "20")
    people = big_and_small_companies()
    doms = sorted({p["domain"] for p in people})
    res = {}
    for name, rules in (("old", OLD_RULES), ("new", {})):
        for k in OLD_RULES:
            monkeypatch.delenv(k, raising=False)
        for k, v in rules.items():
            monkeypatch.setenv(k, v)
        fake = fresh(client, monkeypatch, tmp_path, name, people=[dict(p) for p in people], verifier_on=False)
        pid = start_list(client, doms, per_company=2, filters={"contactCountry": {"in": ["United States"]}})
        run(client, pid)
        res[name] = books(pid)
        check_books(fake)
        check_people(fake)
    print("\nbig + small companies, 2.1 rules vs 2.2 rules:", res)
    # 2.1 gives up once the big companies fill the first pages (every later page is "already full" people);
    # 2.2 searches the other companies on their own, so it reaches them and pays far less search per email.
    assert res["new"]["emails"] > res["old"]["emails"] * 3, res
    assert res["new"]["search"] / res["new"]["emails"] < res["old"]["search"] / res["old"]["emails"] / 2, res


def test_reslice_never_loops(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("PAGE_SIZE", "10")
    monkeypatch.setenv("RESLICE_MAX_TIMES", "3")
    pid = start_list(client, company_domains(fake, 100, min_people=1), per_company=1,
                     filters={"contactCountry": {"in": ["United States"]}})
    run(client, pid)
    gens = [engine._slice_gen(s) for s in db.q("SELECT * FROM pull_slices WHERE pull_id=?", (pid,))]
    assert max(gens) <= 3
    check_books(fake)
    check_people(fake)


def test_a_full_company_reopens_once_when_someone_has_no_email(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("PAGE_SIZE", "20")
    pid = start_list(client, company_domains(fake, 80, min_people=1), per_company=1,
                     filters={"contactCountry": {"in": ["United States"]}})
    run(client, pid)
    m = db.get_meta(f"excluded_full:{pid}") or {"ex": [], "reopened": []}
    assert set(m["reopened"]) <= set(m["ex"])
    reopened = [s for s in db.q("SELECT * FROM pull_slices WHERE pull_id=?", (pid,)) if "searched again" in s["label"]
                and "still to fill" not in s["label"]]
    seen = collections.Counter(d for s in reopened for d in engine._slice_domains(s) or [])
    assert all(n == 1 for n in seen.values()), seen
    check_books(fake)
    check_people(fake)


def test_first_batch_rate_check_waits_for_ten_answers(env, monkeypatch):
    """With probing, the first paid batch can be tiny; the email-rate pause must not fire on 2 or 3 people."""
    client, fake, clock = env
    add_verifier(client)
    monkeypatch.setenv("PAUSE_BELOW_EMAIL_RATE", "30")
    pid = start_list(client, company_domains(fake, 5, min_people=2))
    p = run(client, pid, until=("done", "paused"))
    assert p["status"] == "done", p.get("stop_reason")
    check_books(fake)
    check_people(fake)


# ---------------------------------------------------------------- the bottom line
def test_2_2_uses_fewer_credits_per_email_than_2_1(env, monkeypatch, tmp_path):
    client, fake0, clock = env
    doms = company_domains(fake0, 80, min_people=2)
    res = {}
    for name, rules in (("2.1", OLD_RULES), ("2.2", {})):
        for k in OLD_RULES:
            monkeypatch.delenv(k, raising=False)
        for k, v in rules.items():
            monkeypatch.setenv(k, v)
        fake = fresh(client, monkeypatch, tmp_path, name.replace(".", "_"))
        pid = start_list(client, doms, per_company=2)
        p = run(client, pid)
        b = books(pid)
        b["mv_checks"] = len(fake.mv.checks)
        b["per_email"] = (b["search"] + b["research"]) / max(1, b["emails"])
        res[name] = b
        check_books(fake)
        check_people(fake)
    print("\n2.1 vs 2.2 on the same company list:", res)
    assert res["2.2"]["research"] < res["2.1"]["research"], res
    assert res["2.2"]["per_email"] < res["2.1"]["per_email"], res
    assert res["2.2"]["emails"] >= res["2.1"]["emails"] - 2, res


def test_report_shows_free_vs_paid(env):
    client, fake, clock = env
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 40, min_people=2))
    p = run(client, pid)
    g = p["guessing"]
    assert {"guessed", "probed", "paid", "saved"} <= set(g)
    assert g["probed"] <= g["guessed"]
    page = client.get("/").text
    assert "by trying the common formats" in page
    check_books(fake)
