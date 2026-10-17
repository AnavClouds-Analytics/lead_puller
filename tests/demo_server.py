"""Run the app against fake Seamless + Claude so the screens can be tried without spending credits.

    python -m tests.demo_server      # then open http://localhost:8765 , demo@anavclouds.test / demo-pass-1
Keys to paste in Settings: Seamless "good-key", Claude "claude-key".
"""
import os
import tempfile

os.environ.setdefault("ADMIN_USERS", "demo@anavclouds.test:demo-pass-1")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="leadpuller-demo-"))

import httpx
import uvicorn

from app import claude, db, seamless, worker
from tests.fakes import FakeClaude, FakeSeamless

fake_s = FakeSeamless()
seamless.TRANSPORT = httpx.MockTransport(fake_s.handler)
claude.TRANSPORT = httpx.MockTransport(FakeClaude().handler)
db.DATA_DIR = os.environ["DATA_DIR"]
db.DB_PATH = os.path.join(db.DATA_DIR, "leadpuller.db")
db.set_setting("seamless_api_key", "good-key")
db.set_setting("anthropic_api_key", "claude-key")
worker.POLL_EVERY = 1

if __name__ == "__main__":
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8765)), log_level="warning")
