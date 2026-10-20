import os

os.environ["DISABLE_WORKER"] = "1"
from fastapi.testclient import TestClient

from app import db, main


def test_login_from_env(tmp_path, monkeypatch):
    db.reset_for_tests(str(tmp_path / "a.db"))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    c = TestClient(main.app)
    assert c.get("/api/auth/status").json()["no_password"] is True
    assert c.post("/api/auth/login", json={"password": "anything"}).status_code == 400
    monkeypatch.setenv("APP_PASSWORD", "longenough1")
    assert c.get("/api/meta").status_code == 401
    assert c.post("/api/auth/login", json={"password": "wrong-one"}).status_code == 401
    assert c.post("/api/auth/login", json={"password": "longenough1"}).status_code == 200
    assert c.get("/api/meta").status_code == 200
    s = c.get("/api/settings").json()
    flat = {r["key"]: r["value"] for g in s["groups"] for r in g["rows"]}
    assert flat["APP_PASSWORD"] == "•••• ugh1"
    assert "COMPANY_COOLDOWN_DAYS" in flat


def test_env_file_is_read(tmp_path, monkeypatch):
    from app import config
    f = tmp_path / ".env"
    f.write_text('MAX_PER_COMPANY=7\nCOMPANY_COOLDOWN_DAYS="45"\n# comment\nFIT_CHECK=0  # off\n')
    for k in ("MAX_PER_COMPANY", "COMPANY_COOLDOWN_DAYS", "FIT_CHECK"):
        monkeypatch.delenv(k, raising=False)
    config._load_dotenv(str(f))
    assert config.cfg.MAX_PER_COMPANY == 7 and config.cfg.COMPANY_COOLDOWN_DAYS == 45 and config.cfg.FIT_CHECK is False


def test_every_setting_is_in_env_example():
    """Every .env setting the code reads is documented in .env.example (so changes never need code edits)."""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = open(os.path.join(root, "app", "config.py")).read()
    example = open(os.path.join(root, ".env.example")).read()
    keys = set(re.findall(r'_(?:s|i|b|f)\("([A-Z_]+)"', code))
    assert len(keys) > 40
    missing = [k for k in sorted(keys) if not re.search(r"^#? ?" + k + "=", example, re.M)]
    assert not missing, missing
