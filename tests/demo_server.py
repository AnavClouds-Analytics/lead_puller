"""Run the app against the fake Seamless + Claude so the screens can be tried without spending credits.

    python -m tests.demo_server      # then open http://localhost:8765 , password: demo-pass-1
"""
import os
import tempfile

os.environ.update({"APP_PASSWORD": "demo-pass-1", "SEAMLESS_API_KEY": "good-key", "ANTHROPIC_API_KEY": "claude-key",
                   "POLL_SECONDS": "1"})
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="leadpuller-demo-"))
os.environ["ENV_FILE"] = os.path.join(os.environ["DATA_DIR"], "no.env")

import httpx
import uvicorn

from app import claude, db, seamless
from tests.fakes import FakeClaude, FakeSeamless

fake_s = FakeSeamless()
fake_s.chaos_poll = True
seamless.TRANSPORT = httpx.MockTransport(fake_s.handler)
claude.TRANSPORT = httpx.MockTransport(FakeClaude().handler)
db.reset_for_tests(os.path.join(os.environ["DATA_DIR"], "leadpuller.db"))

if __name__ == "__main__":
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8765)), log_level="warning")
