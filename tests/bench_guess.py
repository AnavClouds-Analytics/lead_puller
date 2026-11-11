"""2.6: the guessing benchmark. Runs the same pulls on the fake Seamless + fake MillionVerifier and reports what they
cost: Seamless research credits, emails found, guessed emails and MillionVerifier checks.

test_v26.py compares a run of the current code with the numbers 2.5.1 produced on the very same data
(tests/data/bench_2_5_1.json): Seamless credits and emails may never get worse, MillionVerifier checks must drop.

Recording the 2.5.1 numbers (done once, on the 2.5.1 code):
    LP_BENCH_RECORD=1 python -m pytest -q tests/test_v26_bench.py
"""
import collections
import json
import os
import sys

BASELINE = os.path.join(os.path.dirname(__file__), "data", "bench_2_5_1.json")
SEEDS = (7, 11, 23, 42, 99)
SCENARIOS = ("company_list", "filter_pull", "two_pulls")


def _company_domains(fake, n, min_people=2):
    by = collections.Counter(p["domain"] for p in fake.people if p["domain"] and p["country"] == "United States"
                             and p["seniority"] in ("C-Level", "VP"))
    return [d for d, k in by.most_common() if k >= min_people][:n]


def measure(env, scenario, seed=7):
    """One scenario on an env fixture (client, fake, clock) with the verifier key added. Returns the numbers.
    `seed` picks a different set of fake people (and so a different mix of companies and formats)."""
    from app import db, engine
    from tests.conftest import add_verifier, check_books, check_people, run, start
    from tests.fakes import make_people
    client, fake, clock = env
    fake.people = make_people(seed=seed)
    fake.by_pid = {p["pid"]: p for p in fake.people}
    fake.mailboxes = {p["email"]: p["pid"] for p in fake.people}
    add_verifier(client)
    pids, target = [], 0
    if scenario == "company_list":
        doms = _company_domains(fake, 70)
        items = [{"name": "", "domain": d, "signal": ""} for d in doms]
        r = client.post("/api/pulls", json={"filters": {"seniority": {"in": ["C-Level", "VP"]}}, "target": len(doms) * 3,
                                            "max_credits": 3000, "max_per_company": 3, "companies": items})
        assert r.status_code == 200, r.text
        target = len(doms) * 3
        pids.append(r.json()["id"])
        run(client, pids[-1])
    elif scenario == "filter_pull":
        target = 150
        pids.append(start(client, {"seniority": {"in": ["C-Level", "VP"]}}, target=150, max_credits=3000))
        run(client, pids[-1])
    else:                                     # the second pull meets companies the first one taught
        for t in (90, 90):
            target += t
            pids.append(start(client, {"seniority": {"in": ["C-Level", "VP"]}}, target=t, max_credits=3000))
            run(client, pids[-1])
    check_books(fake)
    check_people(fake)
    research = sum(db.pull_spend(p, "research") for p in pids)
    search = sum(db.pull_spend(p, "search") for p in pids)
    emails = sum(engine.counts(p)["emails"] for p in pids)
    guessed = db.q1(f"SELECT COUNT(*) n FROM contacts WHERE pull_id IN ({','.join('?' * len(pids))}) "
                    "AND email_source='guessed'", pids)["n"]
    return {"research": research, "search": search, "emails": emails, "guessed": guessed,
            "mv_checks": len(fake.mv.checks), "target": target}
