"""Lead Puller 2.6: fewer MillionVerifier checks, never more Seamless credits.

- The made-up address is checked only when a guess at a new company comes back valid (it guards against servers that
  say "valid" to everything). A company whose server rejects a guess can't be such a server, so it's never checked
  there. Before, it was checked first at every new company.
- The common formats are tried in the order that's most often right at your own companies (same formats, new order).
- GUESS_PROBE_PEOPLE: how many people at one company get the common formats tried.
- The pull shows MillionVerifier checks per Seamless credit saved.
Every test ends with check_books / check_people (books match Seamless to the credit, nobody paid twice, every email is
that person's own mailbox, every guess checked "ok").
"""
import collections

import pytest

from app import db, guess
from app.config import cfg
from tests.conftest import add_verifier, check_books, check_people, run
from tests.fakes import is_catch_all
from tests.test_guessing import company_domains, start_list


def made_up(fake):
    return collections.Counter(e.split("@")[1] for e in fake.mv.checks if e.startswith("zq"))


def checks_at(fake, d):
    return [e for e in fake.mv.checks if e.endswith("@" + d)]


def test_made_up_address_only_after_a_valid_guess(env):
    client, fake, clock = env
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 60, min_people=2))
    run(client, pid)
    mu = made_up(fake)
    assert mu and max(mu.values()) <= 1                       # never twice at one company
    for d in mu:
        # every made-up check came right after a guess at that company that MillionVerifier called valid
        log = db.q("SELECT email, result FROM verify_log WHERE email LIKE ? AND cached=0 ORDER BY id", ("%@" + d,))
        i = next(k for k, r in enumerate(log) if r["email"].startswith("zq"))
        assert i > 0 and log[i - 1]["result"] == "ok", (d, log)
    # companies where every guess came back "doesn't exist" never had the made-up address checked
    failed = db.q("SELECT domain FROM domain_guess WHERE probe_tries>0 AND valid=0 AND catch_all=0")
    assert failed
    for r in failed:
        assert r["domain"] not in mu, r["domain"]
    check_books(fake)
    check_people(fake)


def test_server_that_accepts_everything_is_still_never_guessed(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 40, min_people=2)
    big = {p["domain"] for p in fake.people if guess.too_big(p)}
    trap = [d for d in doms if not is_catch_all(d) and d not in big][:8]
    fake.mv.accept_all_domains |= set(trap)
    pid = start_list(client, doms)
    run(client, pid)
    for d in trap:
        assert not db.q1("SELECT 1 x FROM contacts WHERE domain=? AND email_source='guessed'", (d,)), d
        st = guess.domain_state(d)
        assert st["catch_all"] and st["accept_all_checked"]
        assert len(checks_at(fake, d)) <= 2, checks_at(fake, d)     # one guess + the made-up address, then never again
    before = len(fake.mv.checks)
    pid2 = start_list(client, trap, per_company=3)
    run(client, pid2)
    assert not [e for e in fake.mv.checks[before:] if e.split("@")[1] in trap]
    check_books(fake)
    check_people(fake)


def test_catch_all_company_costs_one_check(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 80, min_people=2)
    pid = start_list(client, doms)
    run(client, pid)
    assert any(is_catch_all(d) for d in doms)
    for d in doms:
        if is_catch_all(d):
            assert len(checks_at(fake, d)) <= 1, (d, checks_at(fake, d))
            assert not db.q1("SELECT 1 x FROM contacts WHERE domain=? AND email_source='guessed'", (d,))
    check_books(fake)
    check_people(fake)


def test_unclear_answers_still_mean_pay_seamless(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 30, min_people=2)
    fake.mv.unknown_domains |= set(doms[:10])
    pid = start_list(client, doms)
    run(client, pid)
    for d in doms[:10]:
        assert not db.q1("SELECT 1 x FROM contacts WHERE domain=? AND email_source='guessed'", (d,))
        assert not guess.domain_state(d).get("accept_all_checked")    # "unknown" proves nothing
    check_books(fake)
    check_people(fake)


def test_errors_mid_probe_never_turn_into_a_guess(env):
    client, fake, clock = env
    add_verifier(client)
    fake.mv.fail_next = ["error", "timeout", "500", "garbage"] * 3
    pid = start_list(client, company_domains(fake, 30, min_people=2))
    run(client, pid)
    check_books(fake)
    check_people(fake)


def test_probe_order_follows_your_companies(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("GUESS_PROBE_LEARN_MIN", "5")
    assert guess.probe_order() == guess.PROBE_ORDER                  # nothing known yet: the usual order
    for i in range(8):                                               # your companies mostly use firstlast@
        guess.learn("John", "Smith", f"johnsmith@co{i}.test", f"co{i}.test", "pull")
    for i in range(3):
        guess.learn("Anna", "Cohen", f"anna_cohen@x{i}.test", f"x{i}.test", "pull")
    assert guess.probe_order() == ("firstlast", "first_last", "first.last")
    monkeypatch.setenv("GUESS_PROBE_LEARN_MIN", "100")
    assert guess.probe_order() == guess.PROBE_ORDER                  # too few known: the usual order
    # the same formats are always tried: only the order changes
    monkeypatch.setenv("GUESS_PROBE_LEARN_MIN", "5")
    c = {"id": 1, "domain": "newco.test", "first_name": "Wei", "last_name": "Chen", "size": ""}
    addrs, why = guess.probe_plan(c, False)
    assert why is None and [a.split("@")[0] for a in addrs] == ["weichen", "wei_chen", "wei.chen"]


def test_probe_people_setting(env, monkeypatch):
    client, fake, clock = env
    monkeypatch.setenv("GUESS_PROBE_PEOPLE", "1")
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 60, min_people=2), per_company=3)
    run(client, pid)
    tries = [r["probe_tries"] for r in db.q("SELECT probe_tries FROM domain_guess")]
    assert 1 in tries and all(t <= 1 for t in tries)
    check_books(fake)
    check_people(fake)


@pytest.mark.parametrize("value,most", [("0", 1), ("9", 3), ("2", 2)])
def test_probe_people_is_kept_in_range(env, monkeypatch, value, most):
    monkeypatch.setenv("GUESS_PROBE_PEOPLE", value)
    d = "range.test"
    db.x("INSERT INTO domain_guess(domain, probe_tries, updated_at) VALUES(?,?,0)", (d, most))
    c = {"id": 1, "domain": d, "first_name": "Wei", "last_name": "Chen", "size": ""}
    assert guess.probe_plan(c, False)[0] == []
    db.x("UPDATE domain_guess SET probe_tries=? WHERE domain=?", (most - 1, d))
    assert guess.probe_plan(c, False)[0]


def test_made_up_address_is_never_checked_twice_across_pulls(env):
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 30, min_people=2)
    run(client, start_list(client, doms, per_company=1))
    first = made_up(fake)
    run(client, start_list(client, doms, per_company=3))
    assert all(n <= 1 for n in made_up(fake).values())
    assert set(first) <= set(made_up(fake))
    check_books(fake)
    check_people(fake)


def test_pull_shows_checks_per_saved_credit(env):
    client, fake, clock = env
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 40, min_people=2))
    p = run(client, pid)
    g = p["guessing"]
    assert g["saved"] > 0
    assert g["checks_per_saved"] == pytest.approx(g["checks"] / g["saved"], abs=0.05)
    html = client.get("/").text
    assert "per Seamless credit saved" in html


@pytest.mark.skipif(__import__("os").environ.get("LP_TEST_GUESSING") == "1", reason="that run adds the key to every test")
def test_no_guessing_key_no_checks(env):
    client, fake, clock = env
    pid = start_list(client, company_domains(fake, 20, min_people=2))
    p = run(client, pid)
    assert fake.mv.checks == [] and (p["guessing"].get("checks_per_saved") in (None, 0))
    check_books(fake)
    check_people(fake)


def test_company_records_are_well_formed(env):
    """Every company row is keyed by its website and counts its probes (a swapped insert once stored the time as the
    website, so the probe count never went up)."""
    client, fake, clock = env
    add_verifier(client)
    pid = start_list(client, company_domains(fake, 60, min_people=2), per_company=3)
    run(client, pid)
    rows = db.q("SELECT * FROM domain_guess")
    assert rows
    for r in rows:
        assert "." in r["domain"] and not r["domain"].replace(".", "").isdigit(), r
        assert isinstance(r["updated_at"], (int, float)), r
    assert any(r["probe_tries"] >= 1 for r in rows)
    assert all(r["probe_tries"] <= 2 for r in rows)
    check_books(fake)
    check_people(fake)


def test_branding_and_version(env):
    """2.6: AnavClouds logo in the side panel and on the login page, the tab icon, and the version under the name.
    The images load without logging in (the login page shows one)."""
    client, fake, clock = env
    from fastapi.testclient import TestClient
    from app.config import APP_VERSION
    from app.main import app
    anon = TestClient(app)
    for path in ("/logo.png", "/logo-full.png", "/favicon.png", "/favicon.ico"):
        r = anon.get(path)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG", path
    assert APP_VERSION == "2.6.0"
    assert anon.get("/healthz").json()["version"] == "2.6.0"
    assert client.get("/api/meta").json()["version"] == "2.6.0"
    assert anon.get("/api/meta").status_code == 401               # the data still needs the login
    html = client.get("/").text
    assert 'href="/favicon.png"' in html and 'src="/logo.png"' in html and 'src="/logo-full.png"' in html
    assert "Version ${esc(META.version" in html and "#0277BD" in html
