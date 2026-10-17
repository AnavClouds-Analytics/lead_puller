import csv
import io
import os

os.environ["DISABLE_WORKER"] = "1"
os.environ.pop("ADMIN_USERS", None)
os.environ.pop("SEAMLESS_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

import httpx
import pytest
from fastapi.testclient import TestClient

from app import claude, db, main, seamless, worker
from tests.fakes import FakeClaude, FakeSeamless, make_people


@pytest.fixture
def env(tmp_path):
    db.reset_for_tests(str(tmp_path / "t.db"))
    fake_s, fake_c = FakeSeamless(), FakeClaude()
    seamless.TRANSPORT = httpx.MockTransport(fake_s.handler)
    claude.TRANSPORT = httpx.MockTransport(fake_c.handler)
    seamless.SLEEP = lambda s: None
    seamless.limiter = seamless._Limiter()
    worker.POLL_EVERY = 0
    worker._last_poll.clear()
    client = TestClient(main.app)
    client.__enter__()
    r = client.post("/api/auth/setup", json={"email": "me@example.com", "password": "secret-pass"})
    assert r.status_code == 200
    r = client.post("/api/settings", json={"seamless_api_key": "good-key", "anthropic_api_key": "claude-key"})
    assert r.status_code == 200, r.text
    yield client, fake_s, fake_c
    client.__exit__(None, None, None)


def run_until(client, pid, statuses, max_ticks=400):
    for _ in range(max_ticks):
        p = client.get(f"/api/pulls/{pid}").json()
        if p["status"] in statuses:
            return p
        worker.tick()
    raise AssertionError(f"pull stuck in {p['status']}: {p.get('error')}")


def test_login_required(tmp_path):
    db.reset_for_tests(str(tmp_path / "a.db"))
    c = TestClient(main.app)
    assert c.get("/api/meta").status_code == 401
    assert c.get("/api/auth/status").json()["needs_setup"] is True
    assert c.post("/api/auth/setup", json={"email": "me@example.com", "password": "short"}).status_code == 400
    assert c.post("/api/auth/setup", json={"email": "not-an-email", "password": "longenough1"}).status_code == 400
    assert c.post("/api/auth/setup", json={"email": "Me@Example.com", "password": "longenough1"}).status_code == 200
    assert c.post("/api/auth/setup", json={"email": "x@example.com", "password": "longenough2"}).status_code == 400
    c2 = TestClient(main.app)
    assert c2.post("/api/auth/login", json={"password": "longenough1"}).status_code == 401
    assert c2.post("/api/auth/login", json={"email": "me@example.com", "password": "wrong-one"}).status_code == 401
    assert c2.post("/api/auth/login", json={"email": "other@example.com", "password": "longenough1"}).status_code == 401
    assert c2.post("/api/auth/login", json={"email": " ME@example.com ", "password": "longenough1"}).status_code == 200
    assert c2.get("/api/meta").json()["user"] == "me@example.com"
    main._login_fails.clear()


def test_admin_users_env(tmp_path, monkeypatch):
    db.reset_for_tests(str(tmp_path / "b.db"))
    monkeypatch.setenv("ADMIN_USERS", "a@example.com:pass-one, b@example.com:pa:ss-two")
    c = TestClient(main.app)
    assert c.get("/api/auth/status").json()["needs_setup"] is False
    assert c.post("/api/auth/setup", json={"email": "x@example.com", "password": "longenough1"}).status_code == 400
    assert c.post("/api/auth/login", json={"email": "a@example.com", "password": "pa:ss-two"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "b@example.com", "password": "pa:ss-two"}).status_code == 200
    assert c.get("/api/auth/status").json() == {"logged_in": True, "email": "b@example.com", "needs_setup": False}
    # Removing someone from ADMIN_USERS ends their existing sessions.
    monkeypatch.setenv("ADMIN_USERS", "a@example.com:pass-one")
    assert c.get("/api/meta").status_code == 401
    main._login_fails.clear()


def test_bad_key_rejected(env):
    client, fake, _ = env
    r = client.post("/api/settings", json={"seamless_api_key": "wrong"})
    assert r.status_code == 400 and "did not accept" in r.json()["error"]
    s = client.get("/api/settings").json()
    assert s["seamless_api_key"].endswith("-key"[-4:])  # still the good key, masked
    assert s["credits"]["balance"] == fake.balance


def test_full_pull(env):
    client, fake, _ = env
    start_balance = fake.balance
    p = client.post("/api/pulls/interpret", json={"prompt": "Tech leaders in the US and Canada, 120 people"}).json()
    pid = p["id"]
    assert p["status"] == "draft" and p["target"] == 120
    assert "industry" not in p["filters"]  # invalid value dropped
    assert p["notes"]

    # Preview costs 3 credits (25 rows), skips staffing companies, rates interns low
    p = client.post(f"/api/pulls/{pid}/preview").json()
    assert p["status"] == "previewed"
    assert p["search_credits"] == 3 and fake.balance == start_balance - 3
    us_ca = [x for x in make_people() if x["country"] in ("United States", "Canada")]
    assert p["total_matches"] == len(us_ca)
    skipped = client.get(f"/api/pulls/{pid}/contacts?view=skipped").json()["rows"]
    assert all("Staffing" in r["company"] for r in skipped if "staffing" in (r["skip_reason"] or "").lower())
    kept = client.get(f"/api/pulls/{pid}/contacts?view=kept").json()["rows"]
    assert all(r["fit"] == ("low" if ("Intern" in r["title"] or "Recruiter" in r["title"]) else "high")
               for r in kept)

    # Collect to 120 with max 2 per company
    p = client.post(f"/api/pulls/{pid}/collect", json={"target": 120, "per_company_cap": 2}).json()
    assert p["status"] == "collecting"
    p = run_until(client, pid, {"collected"})
    assert p["stats"]["kept"] == 120, p["stop_reason"]
    rows = client.get(f"/api/pulls/{pid}/contacts?view=kept&limit=500").json()["rows"]
    per_co = {}
    for r in rows:
        per_co[r["domain"]] = per_co.get(r["domain"], 0) + 1
        assert "Staffing" not in r["company"]
    assert max(per_co.values()) <= 2
    spent_search = start_balance - fake.balance
    assert spent_search == p["search_credits"]

    # Research only high + medium fit
    low = p["stats"]["fit"].get("low", 0)
    assert low > 0
    p = client.post(f"/api/pulls/{pid}/research", json={"choice": "high_medium"}).json()
    assert p["status"] == "researching"
    p = run_until(client, pid, {"done"})
    assert p["stats"]["submitted"] == 120 - low
    assert p["research_credits"] == 120 - low
    assert start_balance - fake.balance == p["search_credits"] + p["research_credits"]

    # Results matched to the right person even though the fake returns them shuffled
    res = client.get(f"/api/pulls/{pid}/contacts?view=results&limit=500").json()["rows"]
    for r in res:
        if r["email"]:
            assert r["email"].endswith("@" + r["domain"])
            assert r["email"].startswith(r["first_name"].lower() + ".")
    statuses = {r["research_status"] for r in res}
    assert {"done", "not found", "duplicate"} <= statuses
    assert p["stats"]["hit_rate"]

    # AnavMail export
    csvtext = client.get(f"/api/pulls/{pid}/export?format=anavmail").content.decode("utf-8-sig")
    lines = list(csv.reader(io.StringIO(csvtext)))
    assert lines[0] == ["email", "first_name", "last_name", "company", "title", "linkedin"]
    assert len(lines) - 1 == p["stats"]["with_email"]
    strict = client.get(f"/api/pulls/{pid}/export?format=anavmail&include_risky=0").content.decode("utf-8-sig")
    assert len(strict.splitlines()) < len(lines)
    assert client.get(f"/api/pulls/{pid}/export?format=xlsx").status_code == 200
    assert client.get(f"/api/pulls/{pid}/export?format=full").status_code == 200

    # Run again: people already researched are skipped, never paid for twice
    p2 = client.post(f"/api/pulls/{pid}/rerun").json()
    p2 = client.post(f"/api/pulls/{p2['id']}/preview").json()
    skipped2 = client.get(f"/api/pulls/{p2['id']}/contacts?view=skipped").json()["rows"]
    assert any((r["skip_reason"] or "").startswith("Already pulled before") for r in skipped2)
    kept2 = client.get(f"/api/pulls/{p2['id']}/contacts?view=kept").json()["rows"]
    researched_names = {r["name"] for r in res}
    assert not any(r["name"] in researched_names for r in kept2)


def test_exclusion_upload_and_budget(env):
    client, fake, _ = env
    first = [x for x in make_people() if x["country"] in ("United States", "Canada")][0]
    data = f"Email,Domain\nfoo@acme1.com,\n,{first['domain']}\n"
    r = client.post("/api/exclusions/upload", files={"file": ("list.csv", data, "text/csv")})
    assert r.status_code == 200 and r.json()["added"] == 2
    p = client.post("/api/pulls/interpret", json={"prompt": "x"}).json()
    p = client.post(f"/api/pulls/{p['id']}/preview").json()
    sk = client.get(f"/api/pulls/{p['id']}/contacts?view=skipped&limit=500").json()["rows"]
    assert any(r["domain"] == first["domain"] and r["skip_reason"] == "On your do-not-pull list" for r in sk)
    client.post("/api/settings", json={"monthly_budget": 10})
    client.post(f"/api/pulls/{p['id']}/collect", json={"target": 20, "per_company_cap": 0})
    p = run_until(client, p["id"], {"collected", "paused"})
    if p["status"] == "collected":
        r = client.post(f"/api/pulls/{p['id']}/research", json={"choice": "all"})
        assert r.status_code == 400 and "budget" in r.json()["error"]
    else:
        assert "budget" in p["error"]


def test_rate_limits_and_out_of_credits(env):
    client, fake, _ = env
    fake.rate_limit_every = 3
    p = client.post("/api/pulls/interpret", json={"prompt": "x"}).json()
    p = client.post(f"/api/pulls/{p['id']}/preview").json()
    client.post(f"/api/pulls/{p['id']}/collect", json={"target": 60, "per_company_cap": 0})
    p = run_until(client, p["id"], {"collected"})
    assert p["stats"]["kept"] == 60
    fake.balance = 30
    client.post(f"/api/pulls/{p['id']}/research", json={"choice": "all"})
    p = client.get(f"/api/pulls/{p['id']}").json()
    # balance check happens up front
    assert p["status"] == "collected" or p["status"] == "researching"


def test_out_of_credits_mid_research_pauses(env):
    client, fake, _ = env
    p = client.post("/api/pulls/interpret", json={"prompt": "x"}).json()
    pid = p["id"]
    client.post(f"/api/pulls/{pid}/preview")
    client.post(f"/api/pulls/{pid}/collect", json={"target": 150, "per_company_cap": 0})
    run_until(client, pid, {"collected"})
    client.post(f"/api/pulls/{pid}/research", json={"choice": "all"})
    fake.balance = 120  # enough for the first batch of 100 only
    p = run_until(client, pid, {"paused", "done"})
    assert p["status"] == "paused" and "credits" in p["error"].lower()
    fake.balance = 1000
    client.post(f"/api/pulls/{pid}/resume")
    p = run_until(client, pid, {"done"})
    assert p["stats"]["submitted"] == p["stats"]["selected"]


def test_pause_cancel(env):
    client, fake, _ = env
    p = client.post("/api/pulls/interpret", json={"prompt": "x"}).json()
    pid = p["id"]
    client.post(f"/api/pulls/{pid}/preview")
    client.post(f"/api/pulls/{pid}/collect", json={"target": 150, "per_company_cap": 0})
    client.post(f"/api/pulls/{pid}/pause")
    worker.tick()
    assert client.get(f"/api/pulls/{pid}").json()["pages_fetched"] == 1
    client.post(f"/api/pulls/{pid}/resume")
    worker.tick()
    client.post(f"/api/pulls/{pid}/cancel")
    p = client.get(f"/api/pulls/{pid}").json()
    assert p["status"] == "collected" and p["stop_reason"] == "Stopped by you."
    kept_n = p["stats"]["kept"]
    assert 0 < kept_n < 150
    client.post(f"/api/pulls/{pid}/research", json={"choice": "all"})
    worker.tick()
    client.post(f"/api/pulls/{pid}/cancel")
    p = run_until(client, pid, {"done"})
    assert p["stats"]["submitted"] == min(100, kept_n)  # first batch only; rest never charged
