"""Lead Puller 2.8: faster pulls.

- Up to MAX_PULLS_AT_ONCE pulls run side by side on the same Seamless account (same or different filters). Whoever
  keeps a person first has them: the others skip them, so nobody is ever paid for twice. Two pulls with the same filters
  share one place in Seamless's list instead of each reading it from the top.
- MillionVerifier checks run GUESS_PARALLEL at a time, one company per lane, in the same order within a company, so the
  checks are the same as one at a time, only faster.
- "Where the time went" on the pull page.
Every test ends with check_books / check_people (books match Seamless to the credit, nobody paid twice, every email is
that person's own mailbox)."""
import collections
import threading
import time

import pytest

from app import db, engine
from tests.conftest import add_verifier, check_books, check_people, run, start
from tests.test_guessing import company_domains, start_list

US = {"contactCountry": {"in": ["United States"]}}
SENIOR_US = {**US, "seniority": {"in": ["C-Level", "VP"]}}


def run_all(client, pids, ticks=8000):
    for _ in range(ticks):
        st = [client.get(f"/api/pulls/{p}").json()["status"] for p in pids]
        if all(s == "done" for s in st):
            return [client.get(f"/api/pulls/{p}").json() for p in pids]
        engine.tick()
    raise AssertionError(f"stuck: {st}")


def paid_people():
    """Every person paid for in Seamless or found by a guess, with the pulls they're in."""
    seen = collections.defaultdict(set)
    for r in db.q("""SELECT pull_id, name, company, li_url, row_email FROM contacts
                     WHERE submitted_at IS NOT NULL OR email_source='guessed'"""):
        for k in db.person_keys(r["name"], r["company"], r["li_url"], r["row_email"]):
            seen[k].add(r["pull_id"])
    return seen


def assert_nobody_twice(pids):
    dup = {k: v for k, v in paid_people().items() if len(v) > 1}
    assert not dup, list(dup.items())[:5]
    emails = collections.Counter(r["email"].lower() for r in db.q(
        f"SELECT email FROM contacts WHERE pull_id IN ({','.join('?' * len(pids))}) AND email IS NOT NULL AND email!=''"
        f" AND {engine.DELIVERABLE}", pids))
    assert not [e for e, n in emails.items() if n > 1]


def test_three_pulls_run_at_the_same_time(env, monkeypatch):
    monkeypatch.setenv("MAX_PULLS_AT_ONCE", "3")
    client, fake, clock = env
    a = start(client, US, target=40, max_credits=500)
    b = start(client, {"contactCountry": {"in": ["Canada"]}}, target=30, max_credits=500)
    c = start(client, SENIOR_US, target=20, max_credits=500)
    d = start(client, {"contactCountry": {"in": ["Israel"]}}, target=10, max_credits=500)
    engine.tick()
    engine.tick()
    st = {x: client.get(f"/api/pulls/{x}").json()["status"] for x in (a, b, c, d)}
    assert [st[a], st[b], st[c]] == ["running"] * 3 and st[d] == "queued"         # MAX_PULLS_AT_ONCE=3
    run_all(client, [a, b, c, d])
    assert_nobody_twice([a, b, c, d])
    check_books(fake)
    check_people(fake)


@pytest.mark.parametrize("guessing", [False, True])
def test_same_filters_at_once_never_pay_twice(env, guessing):
    client, fake, clock = env
    if guessing:
        add_verifier(client)
    pids = [start(client, SENIOR_US, target=60, max_credits=800) for _ in range(3)]
    ps = run_all(client, pids)
    assert_nobody_twice(pids)
    assert sum(engine.counts(p)["emails"] for p in pids) >= 120
    assert db.q1("SELECT COUNT(*) n FROM contacts WHERE skip_kind='other_pull'")["n"] >= 0
    check_books(fake)
    check_people(fake)


def test_overlapping_filters_at_once(env):
    client, fake, clock = env
    add_verifier(client)
    pids = [start(client, US, target=50, max_credits=800),
            start(client, SENIOR_US, target=40, max_credits=800),
            start(client, {**US, "companySize": {"in": ["51 - 200", "201 - 500"]}}, target=40, max_credits=800)]
    run_all(client, pids)
    assert_nobody_twice(pids)
    check_books(fake)
    check_people(fake)


def test_same_filters_share_one_place_in_the_list(env):
    """Two pulls with identical filters read each part of Seamless's list once between them, not each from the top."""
    client, fake, clock = env
    a = start(client, SENIOR_US, target=500, max_credits=3000)
    b = start(client, SENIOR_US, target=500, max_credits=3000)
    run_all(client, [a, b])
    pop = collections.Counter(p["size"] for p in fake.people if p["country"] == "United States"
                              and p["seniority"] in ("C-Level", "VP"))
    rows = collections.Counter()
    for r in db.q("SELECT label, rows FROM pull_slices WHERE pull_id IN (?,?) AND size_value IS NOT NULL", (a, b)):
        rows[r["label"]] += r["rows"]
    for label, n in rows.items():
        assert n <= pop[label] + 50, (label, n, pop[label])               # read once (plus at most one page)
    notes = [r["note"] for r in db.q("SELECT note FROM pull_slices WHERE pull_id IN (?,?)", (a, b))]
    assert any("same filters running at the same time" in (n or "") for n in notes)
    assert_nobody_twice([a, b])
    check_books(fake)
    check_people(fake)


def test_one_at_a_time_when_set_to_1(env, monkeypatch):
    monkeypatch.setenv("MAX_PULLS_AT_ONCE", "1")
    client, fake, clock = env
    a = start(client, US, target=30, max_credits=400)
    b = start(client, {"contactCountry": {"in": ["Canada"]}}, target=20, max_credits=400)
    engine.tick()
    assert client.get(f"/api/pulls/{b}").json()["status"] == "queued"
    run_all(client, [a, b])
    check_books(fake)


def test_resume_waits_for_a_free_slot(env, monkeypatch):
    monkeypatch.setenv("MAX_PULLS_AT_ONCE", "2")
    client, fake, clock = env
    a = start(client, US, target=30, max_credits=400)
    engine.tick()
    client.post(f"/api/pulls/{a}/pause")
    b = start(client, {"contactCountry": {"in": ["Canada"]}}, target=20, max_credits=400)
    c = start(client, {"contactCountry": {"in": ["Germany"]}}, target=5, max_credits=400)
    engine.tick()
    client.post(f"/api/pulls/{a}/resume")
    assert client.get(f"/api/pulls/{a}").json()["status"] == "queued"        # 2 already running
    run_all(client, [a, b, c])
    check_books(fake)
    check_people(fake)


def test_pause_stop_and_finish_now_while_others_run(env):
    client, fake, clock = env
    a = start(client, US, target=100, max_credits=800)
    b = start(client, SENIOR_US, target=40, max_credits=800)
    c = start(client, {"contactCountry": {"in": ["Canada"]}}, target=30, max_credits=800)
    engine.tick()
    client.post(f"/api/pulls/{a}/pause")
    client.post(f"/api/pulls/{b}/stop")
    for _ in range(3):
        engine.tick()
    assert client.get(f"/api/pulls/{a}").json()["status"] == "paused"
    client.post(f"/api/pulls/{a}/resume")
    run_all(client, [a, b, c])
    assert "Stopped by you" in client.get(f"/api/pulls/{b}").json()["stop_reason"]
    assert_nobody_twice([a, b, c])
    check_books(fake)
    check_people(fake)


def test_restart_in_the_middle(env):
    client, fake, clock = env
    add_verifier(client)
    pids = [start(client, SENIOR_US, target=50, max_credits=800) for _ in range(2)] + \
           [start(client, {"contactCountry": {"in": ["Canada"]}}, target=30, max_credits=800)]
    for _ in range(10):
        engine.tick()
    engine._TIMES.clear()                               # what a server restart loses (only the timing in memory)
    db._local.con = None
    run_all(client, pids)
    assert_nobody_twice(pids)
    check_books(fake)
    check_people(fake)


def test_one_pull_failing_doesnt_stop_the_others(env, monkeypatch):
    monkeypatch.setenv("MAX_PULLS_AT_ONCE", "3")
    client, fake, clock = env
    a = start(client, US, target=30, max_credits=400)
    b = start(client, {"contactCountry": {"in": ["Canada"]}}, target=20, max_credits=400)
    real = engine.step

    def flaky(p):
        if p["id"] == a:
            raise RuntimeError("boom")
        return real(p)
    monkeypatch.setattr(engine, "step", flaky)
    for _ in range(400):
        engine.tick()
        if client.get(f"/api/pulls/{b}").json()["status"] == "done":
            break
    assert client.get(f"/api/pulls/{b}").json()["status"] == "done"
    monkeypatch.setattr(engine, "step", real)
    run(client, a)
    check_books(fake)


def test_even_split_still_works_alongside_other_pulls(env):
    client, fake, clock = env
    sp = client.post("/api/pulls", json={"filters": {**SENIOR_US, "companySize": {"in": ["51 - 200", "201 - 500"]}},
                                         "target": 40, "max_credits": 2000, "split_sizes": "even"}).json()["id"]
    other = start(client, {"contactCountry": {"in": ["Canada"]}}, target=30, max_credits=800)
    ps = run_all(client, [sp, other])
    sizes = collections.Counter(r["size"] for r in db.q(f"SELECT size FROM contacts WHERE pull_id=? AND {engine.DELIVERABLE}",
                                                         (sp,)))
    assert abs(sizes["51 - 200"] - sizes["201 - 500"]) <= 4, sizes
    assert ps[0]["groups"]
    check_books(fake)
    check_people(fake)


# ---------------------------------------------------------------- parallel MillionVerifier checks
def test_parallel_checks_are_faster_with_the_same_checks(env, monkeypatch):
    """With a slow MillionVerifier (0.05 s a check), side by side is several times faster, and checks exactly the same
    addresses as one at a time."""
    monkeypatch.setenv("GUESS_PARALLEL", "10")
    client, fake, clock = env
    add_verifier(client)
    from app import verifier
    real = verifier.check
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow_check(*a, **k):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        try:
            return real(*a, **k)
        finally:
            with lock:
                active[0] -= 1
    monkeypatch.setattr(verifier, "check", slow_check)
    doms = company_domains(fake, 40, min_people=2)
    t0 = time.time()
    run(client, start_list(client, doms))
    took = time.time() - t0
    assert peak[0] >= 3, peak                           # really side by side
    checks = db.q1("SELECT COUNT(*) n FROM verify_log")["n"]
    assert checks and took < checks * 0.05 * 0.6, (took, checks)
    check_books(fake)
    check_people(fake)


def test_parallel_never_checks_a_company_twice_at_once(env, monkeypatch):
    monkeypatch.setenv("GUESS_PARALLEL", "10")
    client, fake, clock = env
    add_verifier(client)
    doms = company_domains(fake, 60, min_people=3)
    run(client, start_list(client, doms, per_company=3))
    # at most one made-up address per company, never the same address twice
    zq = collections.Counter(e.split("@")[1] for e in fake.mv.checks if e.startswith("zq"))
    assert all(n <= 1 for n in zq.values())
    assert len(fake.mv.checks) == len(set(fake.mv.checks))
    check_books(fake)
    check_people(fake)


def test_millionverifier_failing_mid_batch_stops_quickly(env):
    client, fake, clock = env
    add_verifier(client)
    fake.mv.fail_next = ["error"] * 200
    pid = start_list(client, company_domains(fake, 40))
    p = run(client, pid)
    assert p["guessing"]["guessed"] == 0
    assert 1 <= db.q1("SELECT COUNT(*) n FROM verify_log WHERE result='error'")["n"] <= 3
    check_books(fake)
    check_people(fake)


# ---------------------------------------------------------------- where the time went
def test_where_the_time_went(env):
    client, fake, clock = env
    add_verifier(client)
    pid = start(client, SENIOR_US, target=60, max_credits=800)
    p = run(client, pid)
    t = p["time"]
    assert t and abs(sum(x["pct"] for x in t) - 100) <= len(t)
    labels = {x["key"] for x in t}
    assert labels <= set(engine.PHASES) and "search" in labels
    assert db.q1("SELECT time_json FROM pulls WHERE id=?", (pid,))["time_json"]
    html = client.get("/").text
    assert "Where the time went" in html and "function timeCard" in html


def test_page_says_several_pulls_can_run(env):
    client, fake, clock = env
    html = client.get("/").text
    assert "Pulls run one at a time" not in html
    assert "Already in another pull running at the same time" in html
