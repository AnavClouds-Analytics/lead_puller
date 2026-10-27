"""Run Lead Puller against the fake Seamless (no credits spent), to try the screens.

    python -m tests.demo_server        # http://localhost:8765  password: demo-pass-1
    A second fake Seamless key to add on the Accounts page: demo-second-key-2222
    A fake MillionVerifier key for the Accounts page:      mv-key-7d3e
    A company list to upload on New pull:                  tests/data/demo-hiring-signals.csv
"""
import os
import tempfile

os.environ.update({"APP_PASSWORD": "demo-pass-1", "COOKIE_SECURE": "0", "POLL_SECONDS": "1",
                   "ANTHROPIC_API_KEY": "claude-key", "MONTHLY_CREDIT_BUDGET": "19000", "UPDATED_WITHIN_MONTHS": "0",
                   "SEAMLESS_API_KEY": "key-alpha-1111", "ENV_FILE": "/nonexistent.env"})
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="leadpuller2-demo-"))

import httpx
import uvicorn

from app import claude, dnscheck, seamless, verifier
from tests.fakes import FakeClaude, FakeMillionVerifier, FakeSeamless

fake = FakeSeamless(balance=9_400)
fake.chaos_poll = True
fake.add_key("demo-second-key-2222", 10_000)
seamless.TRANSPORT = httpx.MockTransport(fake.handler)
claude.TRANSPORT = httpx.MockTransport(FakeClaude().handler)
verifier.TRANSPORT = httpx.MockTransport(FakeMillionVerifier(fake, credits=18_420).handler)
dnscheck.LOOKUP = lambda d: "no" if d in ("acme13.com", "acme26.com") else "yes"

if __name__ == "__main__":
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8765)), log_level="warning")
