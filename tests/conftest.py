import csv
import io
import os
import re

os.environ["DISABLE_WORKER"] = "1"

import httpx
import pytest
from fastapi.testclient import TestClient

from app import claude, db, dnscheck, main, seamless, worker
from tests.fakes import FakeClaude, FakeSeamless


class Clock:
    """Fake time for the rate limiter, so tests can check the per-minute limit without waiting."""
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.001)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"APP_PASSWORD": "secret-pass", "SEAMLESS_API_KEY": "good-key", "ANTHROPIC_API_KEY": "claude-key",
                 "DATA_DIR": str(tmp_path), "MONTHLY_CREDIT_BUDGET": "0", "MAX_PER_COMPANY": "3",
                 "COMPANY_COOLDOWN_DAYS": "90", "FIT_CHECK": "1", "CONTINUE_SAME_SEARCH": "1",
                 # v1.2 features are off here unless a test turns them on, so the v1.1 money tests stay exact
                 "UPDATED_WITHIN_MONTHS": "0", "REQUIRE_COMPANY_WEBSITE": "0", "REQUIRE_LINKEDIN": "0",
                 "TEST_BATCH_SIZE": "0", "SKIP_NO_EMAIL_SERVER": "1", "NO_EMAIL_RETRY_DAYS": "0",
                 "LOW_BALANCE_WARNING": "0", "SPENDING_ENABLED": "1", "EMAIL_PICK": "best",
                 "DEFAULT_EXPORT_COLUMNS": "email,first_name,last_name,company",
                 # v1.3 collecting guards are off here unless a test turns them on (tests in test_v13.py do)
                 "COLLECT_CLOSE_ENOUGH_PCT": "0", "COLLECT_LOW_KEEP_PCT": "0", "COLLECT_EMPTY_PAGES_STOP": "100000",
                 "COLLECT_SPEND_CAP_PCT": "100000", "OPEN_PULL_HOLD_DAYS": "0", "ACCOUNT_RESERVE": "0",
                 "LATE_ANSWER_HOURS": "24", "RESEARCH_WAIT_MINUTES": "15"}.items():
        monkeypatch.setenv(k, v)
    dnscheck.LOOKUP = lambda d: "yes"
    db.reset_for_tests(str(tmp_path / "t.db"))
    clock = Clock()
    fake_s, fake_c = FakeSeamless(seed=int(os.environ.get("FAKE_SEED", "1"))), FakeClaude()
    fake_s.clock = clock
    seamless.TRANSPORT = httpx.MockTransport(fake_s.handler)
    claude.TRANSPORT = httpx.MockTransport(fake_c.handler)
    seamless.NOW = clock
    seamless.SLEEP = clock.sleep
    seamless.limiter = seamless._Limiter()
    worker.POLL_EVERY = 0
    worker._last_poll.clear()
    worker._failures.clear()
    worker._saved_cache.clear()
    worker._fill_stop.clear()
    client = TestClient(main.app)
    client.__enter__()
    assert client.post("/api/auth/login", json={"password": "secret-pass"}).status_code == 200
    yield client, fake_s, fake_c, clock
    client.__exit__(None, None, None)


# ---------------------------------------------------------------- helpers used by tests
def new_pull(client, prompt="tech leaders"):
    r = client.post("/api/pulls/interpret", json={"prompt": prompt})
    assert r.status_code == 200, r.text
    return r.json()


def run_until(client, pid, statuses, max_ticks=3000):
    p = None
    for _ in range(max_ticks):
        p = client.get(f"/api/pulls/{pid}").json()
        if p["status"] in statuses:
            return p
        worker.tick()
    raise AssertionError(f"pull {pid} stuck in {p['status']}: {p.get('error')}")


def full_pull(client, target=120, cap=3, choice="all", prompt="tech leaders", filters=None, cooldown=None):
    p = new_pull(client, prompt)
    pid = p["id"]
    patch = {}
    if filters is not None:
        patch["filters"] = filters
    if cooldown is not None:
        patch["company_cooldown"] = cooldown
    if patch:
        assert client.patch(f"/api/pulls/{pid}", json=patch).status_code == 200
    r = client.post(f"/api/pulls/{pid}/preview", json={})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/pulls/{pid}/collect", json={"target": target, "per_company_cap": cap})
    assert r.status_code == 200, r.text
    run_until(client, pid, {"collected"})
    r = client.post(f"/api/pulls/{pid}/research", json={"choice": choice})
    assert r.status_code == 200, r.text
    return run_until(client, pid, {"done", "paused"})


def anavmail_rows(client, pid, risky=1):
    text = client.get(f"/api/pulls/{pid}/export?format=anavmail&include_risky={risky}").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["email", "first_name", "last_name", "company", "title", "linkedin"]
    return rows[1:]


def pid_from_email(email):
    m = re.search(r"\.p(\d+)@", email or "")
    return int(m.group(1)) if m else None


def check_books(fake):
    """The tool's own credit ledger must equal what the (fake) Seamless actually charged, to the credit."""
    pending = db.q1("SELECT COUNT(*) n FROM research_requests WHERE resolved_at IS NULL")["n"]
    assert pending == 0, "check_books needs every research request resolved"
    logged = db.q1("SELECT COALESCE(SUM(credits),0) s FROM ledger")["s"]
    assert logged == fake.charged, f"ledger says {logged}, Seamless charged {fake.charged}"
    for p in db.q("SELECT id, search_credits, research_credits FROM pulls"):
        s = db.q1("SELECT COALESCE(SUM(credits),0) s FROM ledger WHERE pull_id=?", (p["id"],))["s"]
        assert s == p["search_credits"] + p["research_credits"]


def check_people(client, fake, retries_allowed=False):
    """No one is paid for twice, nobody gets someone else's details, and exports are clean."""
    # 1. each person charged for research at most once, ever (unless a test turned on paid retries)
    if not retries_allowed:
        assert all(n <= 1 for n in fake.researched_pids.values()), "someone was charged twice"
    # 2. each searchResultId sent for research at most once
    sent = [r["sid"] for r in fake.requests.values()]
    resent = {s for s in sent if sent.count(s) > 1}
    # (re-sends are only allowed after an unconfirmed batch, and then Seamless doesn't charge)
    for s in resent:
        assert retries_allowed or fake.researched_pids.get(fake.sid_to_pid[s], 0) <= 1
    # 3. every stored detail belongs to the right person
    for c in db.q("SELECT * FROM contacts WHERE submitted_at IS NOT NULL"):
        pid = fake.sid_to_pid[c["search_result_id"]]
        person = fake.by_pid[pid]
        assert c["kept"] == 1, f"skipped person {c['name']} was researched"
        if c["email"]:
            assert pid_from_email(c["email"]) == pid, f"{c['name']} got {c['email']}"
            assert c["first_name"] == person["first"] and c["last_name"] == person["last"]
            assert c["company"] == person["company"]
            assert c["phone"] in (None, f"415.555.{pid:04d}")
    # 4. exports
    excluded = {r["value"] for r in db.q("SELECT value FROM exclusions WHERE kind='domain'")}
    for p in db.q("SELECT id FROM pulls WHERE status='done'"):
        rows = anavmail_rows(client, p["id"])
        emails = [r[0].lower() for r in rows]
        assert len(emails) == len(set(emails)), "duplicate email in AnavMail export"
        for email, first, last, company, title, li in rows:
            pid = pid_from_email(email)
            person = fake.by_pid[pid]
            assert (first, last, company) == (person["first"], person["last"], person["company"]), email
            assert email.split("@")[1] not in excluded
            c = db.q1("SELECT email_status FROM contacts WHERE pull_id=? AND LOWER(email)=?", (p["id"], email))
            assert (c["email_status"] or "").lower() != "invalid"

    check_batches(client, fake)


def batch_rows(client, bid):
    r = client.get(f"/api/batches/{bid}/download")
    assert r.status_code == 200, r.text
    if r.headers["content-type"].startswith("application/zip"):
        import zipfile
        out = {}
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for name in z.namelist():
                out[name] = list(csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig"))))
        return out
    return list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))


def check_batches(client, fake):
    """Across ALL batches ever made: no email twice, every row is the right person, no invalid emails."""
    all_emails = []
    for b in db.q("SELECT * FROM batches"):
        got = batch_rows(client, b["id"])
        rows = [r for rs in got.values() for r in rs] if isinstance(got, dict) else got
        assert len(rows) == b["count"], f"batch {b['id']} file has {len(rows)} rows, says {b['count']}"
        for r in rows:
            e = r["email"].lower()
            all_emails.append(e)
            pid = pid_from_email(e) if ".p" in e else None
            c = db.q1("SELECT * FROM contacts WHERE LOWER(email)=? AND batch_id=?", (e, b["id"]))
            assert c, f"{e} in batch file but not in batch"
            assert (c["email_status"] or "").lower() != "invalid"
            if pid is not None and "first_name" in r:
                assert r["first_name"] == fake.by_pid[pid]["first"]
            if pid is not None and "company" in r:
                assert r["company"] == fake.by_pid[pid]["company"]
    assert len(all_emails) == len(set(all_emails)), "the same email went out in two batches"
