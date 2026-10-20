"""Run the app against the fake Seamless + Claude so the screens can be tried without spending credits.

    python -m tests.demo_server      # then open http://localhost:8765 , password: demo-pass-1
    A second fake Seamless key to try on the "Seamless accounts" page: demo-second-key-2222
"""
import os
import tempfile

os.environ.update({"APP_PASSWORD": "demo-pass-1", "SEAMLESS_API_KEY": "good-key", "ANTHROPIC_API_KEY": "claude-key",
                   "POLL_SECONDS": "1", "TEST_BATCH_SIZE": "30", "TEST_BATCH_MIN_EMAIL_RATE": "60",
                   "LOW_BALANCE_WARNING": "500", "UPDATED_WITHIN_MONTHS": "12", "EMAIL_CHANCE_MIN_SAMPLES": "20"})
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="leadpuller-demo-"))
os.environ["ENV_FILE"] = os.path.join(os.environ["DATA_DIR"], "no.env")

import httpx
import uvicorn

from app import claude, db, dnscheck, seamless
from tests.fakes import FakeClaude, FakeSeamless

fake_s = FakeSeamless()
fake_s.chaos_poll = True
fake_s.add_key("demo-second-key-2222", 9_400)    # a second Seamless account to add on the accounts page
seamless.TRANSPORT = httpx.MockTransport(fake_s.handler)
claude.TRANSPORT = httpx.MockTransport(FakeClaude().handler)
dnscheck.LOOKUP = lambda d: "no" if d in ("acme13.com", "acme26.com") else "yes"
db.reset_for_tests(os.path.join(os.environ["DATA_DIR"], "leadpuller.db"))

if __name__ == "__main__":
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8765)), log_level="warning")
