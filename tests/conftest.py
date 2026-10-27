import os

os.environ["DISABLE_WORKER"] = "1"
os.environ["ENV_FILE"] = "/nonexistent.env"

import httpx
import pytest
from fastapi.testclient import TestClient

from app import claude, db, dnscheck, engine, guess, seamless, verifier
from tests.fakes import FakeClaude, FakeMillionVerifier, FakeSeamless, email_for

ENV = {"APP_PASSWORD": "test-pass-123", "COOKIE_SECURE": "0", "POLL_SECONDS": "0", "RATE_LIMIT_PER_MIN": "100000",
       "MONTHLY_CREDIT_BUDGET": "0", "ACCOUNT_RESERVE": "0", "LOW_BALANCE_WARNING": "0", "UPDATED_WITHIN_MONTHS": "0",
       "ANTHROPIC_API_KEY": "claude-key", "SEAMLESS_API_KEY": "", "LATE_POLL_MINUTES": "0",
       "EXPECTED_EMAIL_RATE": "80", "MAX_PER_COMPANY": "0", "SYNC_SEAMLESS_SAVED": "1"}


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.01)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    db.reset_for_tests(str(tmp_path / "t.db"))
    clock = Clock()
    fake = FakeSeamless(seed=int(os.environ.get("FAKE_SEED", "1")))
    fake.clock = clock
    monkeypatch.setattr(seamless, "TRANSPORT", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(seamless, "NOW", clock)
    monkeypatch.setattr(seamless, "SLEEP", clock.sleep)
    monkeypatch.setattr(engine, "NOW", clock)
    monkeypatch.setattr(seamless, "limiter", seamless._Limiter())
    monkeypatch.setattr(claude, "TRANSPORT", httpx.MockTransport(FakeClaude().handler))
    mv = FakeMillionVerifier(fake)
    fake.mv = mv
    monkeypatch.setattr(verifier, "TRANSPORT", httpx.MockTransport(mv.handler))
    monkeypatch.setattr(verifier, "NOW", clock)
    monkeypatch.setattr(guess, "NOW", clock)
    monkeypatch.setattr(dnscheck, "LOOKUP", lambda d: "no" if d in ("acme13.com", "acme26.com") else "yes")
    from app import main
    client = TestClient(main.app)
    client.__enter__()
    assert client.post("/api/auth/login", json={"password": "test-pass-123"}).status_code == 200
    r = client.post("/api/accounts", json={"name": "Account A", "api_key": "key-alpha-1111"})
    assert r.status_code == 200, r.text
    if os.environ.get("LP_TEST_GUESSING") == "1":
        add_verifier(client)          # run the whole suite again with email guessing on
    yield client, fake, clock
    client.__exit__(None, None, None)


def add_verifier(client, key="mv-key-7d3e"):
    r = client.post("/api/verifier", json={"api_key": key})
    assert r.status_code == 200, r.text
    return r.json()


def add_account(client, fake, key="key-bravo-2222", balance=100_000, name="Account B", budget=0):
    fake.add_key(key, balance)
    r = client.post("/api/accounts", json={"name": name, "api_key": key, "monthly_budget": budget})
    assert r.status_code == 200, r.text
    return next(a for a in r.json()["accounts"] if a["name"] == name)


def start(client, filters, target=50, max_credits=None, account="auto", max_per_company=None, name=None):
    r = client.post("/api/pulls", json={"name": name, "filters": filters, "target": target, "max_credits": max_credits,
                                        "account": account, "max_per_company": max_per_company})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def run(client, pid, until=("done",), ticks=5000, clock=None):
    for _ in range(ticks):
        p = client.get(f"/api/pulls/{pid}").json()
        if p["status"] in until:
            return p
        engine.tick()
        if clock:
            clock.t += 1
    raise AssertionError(f"pull {pid} stuck in {p['status']}: {p.get('error')} / {p.get('stop_reason')}")


def check_books(fake):
    """Per account: the tool's credit ledger equals what that Seamless key was charged, to the credit."""
    open_ = db.q1("SELECT COUNT(*) n FROM research_requests WHERE resolved_at IS NULL")["n"]
    assert open_ == 0, "check_books needs every research answer back"
    for a in db.q("SELECT * FROM accounts"):
        logged = db.q1("SELECT COALESCE(SUM(credits),0) s FROM ledger WHERE account_id=?", (a["id"],))["s"]
        assert logged == fake.charged_by_key.get(a["api_key"], 0), (a["name"], logged, fake.charged_by_key)
    assert db.q1("SELECT COUNT(*) n FROM ledger WHERE account_id IS NULL AND credits!=0")["n"] == 0
    assert fake.foreign_sid_rejected == 0, "sent one account's search results to another account"


def check_people(fake):
    """Nobody is ever paid for twice (across pulls AND accounts), and every email belongs to its person."""
    total = {}
    for org, done in fake.researched.items():
        for pid, n in done.items():
            total[pid] = total.get(pid, 0) + n
    twice = {pid: n for pid, n in total.items() if n > 1}
    assert not twice, f"paid for these people more than once: {twice}"
    owners = dict(fake.mailboxes)
    for pid, e in fake.email_override.items():
        if e:
            owners.setdefault(e, pid)
    for c in db.q("SELECT * FROM contacts WHERE email IS NOT NULL AND email!=''"):
        pid = owners.get(c["email"])
        assert pid is not None, f"{c['email']} isn't anybody's real address"
        p = fake.by_pid[pid]
        if c["email"] in fake.email_override.values() and c["email"] not in fake.mailboxes:
            continue                      # a test-made shared address (several people)
        assert c["email"] == email_for(p) or c["email"] == fake.email_override.get(pid)
        assert c["name"] == p["name"] and c["company"] == p["company"], (c["email"], c["name"], p["name"])
    # Guessed people were never researched in Seamless, and every guess was checked "ok" first.
    for c in db.q("SELECT * FROM contacts WHERE email_source='guessed'"):
        assert c["submitted_at"] is None and c["request_id"] is None
        assert c["email"] in fake.mv.checks or db.q1("SELECT 1 x FROM verify_log WHERE email=? AND result='ok'",
                                                     (c["email"],))


US = {"contactCountry": {"in": ["United States"]}, "seniority": {"in": ["C-Level", "VP"]}}
