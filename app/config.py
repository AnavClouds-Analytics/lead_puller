"""All settings come from the .env file (or real environment variables, which win).

To change anything: edit .env, then restart the app (docker compose up -d).
Nothing here needs a code change.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            elif " #" in val:
                val = val.split(" #", 1)[0].strip()
            os.environ.setdefault(key, val)


_load_dotenv(os.environ.get("ENV_FILE", os.path.join(ROOT, ".env")))


def _s(name, default=""):
    return os.environ.get(name, default).strip()


def _i(name, default):
    try:
        return int(float(_s(name, str(default)) or default))
    except ValueError:
        return default


def _b(name, default):
    v = _s(name, "1" if default else "0").lower()
    return v in ("1", "true", "yes", "on")


class Config:
    """Read fresh each time so tests (and a restart) pick up changes."""

    # --- login
    @property
    def APP_PASSWORD(self): return _s("APP_PASSWORD")
    @property
    def SESSION_DAYS(self): return _i("SESSION_DAYS", 30)
    @property
    def COOKIE_SECURE(self): return _b("COOKIE_SECURE", False)

    # --- keys
    @property
    def SEAMLESS_API_KEY(self): return _s("SEAMLESS_API_KEY")
    @property
    def ANTHROPIC_API_KEY(self): return _s("ANTHROPIC_API_KEY")
    @property
    def CLAUDE_MODEL(self): return _s("CLAUDE_MODEL", "claude-sonnet-5-5")
    @property
    def CLAUDE_FAST_MODEL(self): return _s("CLAUDE_FAST_MODEL", "claude-haiku-5-5")

    # --- spending and rules (defaults; some can be changed per pull on screen)
    @property
    def MONTHLY_CREDIT_BUDGET(self): return _i("MONTHLY_CREDIT_BUDGET", 0)
    @property
    def MAX_PER_COMPANY(self): return _i("MAX_PER_COMPANY", 3)
    @property
    def COMPANY_COOLDOWN_DAYS(self): return _i("COMPANY_COOLDOWN_DAYS", 90)
    @property
    def FIT_CHECK(self): return _b("FIT_CHECK", True)
    @property
    def CONTINUE_SAME_SEARCH(self): return _b("CONTINUE_SAME_SEARCH", True)

    # --- Seamless API behaviour
    @property
    def PREVIEW_SIZE(self): return max(10, min(_i("PREVIEW_SIZE", 25), 100))
    @property
    def PAGE_SIZE(self): return max(10, min(_i("PAGE_SIZE", 50), 100))
    @property
    def RESEARCH_BATCH(self): return max(1, min(_i("RESEARCH_BATCH", 100), 500))
    @property
    def POLL_SECONDS(self): return max(0, _i("POLL_SECONDS", 4))
    @property
    def RATE_LIMIT_PER_MIN(self): return max(1, _i("RATE_LIMIT_PER_MIN", 55))
    @property
    def SEARCH_SPEND_GUARD(self): return max(1, _i("SEARCH_SPEND_GUARD", 4))
    @property
    def ORG_LOOKBACK_DAYS(self): return max(1, _i("ORG_LOOKBACK_DAYS", 730))
    @property
    def ORG_LOOKUP_MAX_PAGES(self): return max(1, _i("ORG_LOOKUP_MAX_PAGES", 60))
    @property
    def SEAMLESS_BASE_URL(self): return _s("SEAMLESS_BASE_URL", "https://api.seamless.ai/api/client/v1")

    # --- server
    @property
    def DATA_DIR(self): return _s("DATA_DIR", os.path.join(ROOT, "data"))


cfg = Config()

# What the Settings page shows (in this order). Secrets are masked.
SHOWN = [
    ("Login", [("APP_PASSWORD", "secret", "Password for the tool"),
               ("SESSION_DAYS", "", "Days before you have to log in again"),
               ("COOKIE_SECURE", "", "1 once the site is on HTTPS")]),
    ("Keys", [("SEAMLESS_API_KEY", "secret", "Seamless → Settings → Public API → API Key"),
              ("ANTHROPIC_API_KEY", "secret", "console.anthropic.com → API Keys"),
              ("CLAUDE_MODEL", "", "Model that turns your request into filters"),
              ("CLAUDE_FAST_MODEL", "", "Model that rates fit")]),
    ("Spending and rules", [
        ("MONTHLY_CREDIT_BUDGET", "", "Max Seamless credits this tool spends per calendar month (0 = no limit)"),
        ("MAX_PER_COMPANY", "", "Default max people per company in one pull (0 = no limit)"),
        ("COMPANY_COOLDOWN_DAYS", "", "Skip companies pulled in the last N days, across all pulls (0 = off)"),
        ("FIT_CHECK", "", "1 = Claude rates each person against your request"),
        ("CONTINUE_SAME_SEARCH", "", "1 = a repeated search starts where the last one stopped")]),
    ("Seamless API", [
        ("PREVIEW_SIZE", "", "People fetched in Preview (1 credit per 10)"),
        ("PAGE_SIZE", "", "People per search page while collecting"),
        ("RESEARCH_BATCH", "", "People sent for research per call (max 500)"),
        ("POLL_SECONDS", "", "Seconds between checks for research results"),
        ("RATE_LIMIT_PER_MIN", "", "Calls per minute per endpoint (Seamless allows 60)"),
        ("SEARCH_SPEND_GUARD", "", "Stop collecting if search credits pass target ÷ 10 × this"),
        ("ORG_LOOKBACK_DAYS", "", "How far back to look in your Seamless saved contacts for “already researched” people"),
        ("ORG_LOOKUP_MAX_PAGES", "", "Max pages (500 each) read from saved contacts per lookup")]),
]


def shown_settings():
    out = []
    for group, items in SHOWN:
        rows = []
        for key, kind, help_ in items:
            val = getattr(cfg, key)
            if kind == "secret":
                display = ("•••• " + str(val)[-4:]) if val else ""
            else:
                display = str(int(val)) if isinstance(val, bool) else str(val)
            rows.append({"key": key, "value": display, "set": bool(val) or val == 0, "help": help_})
        out.append({"group": group, "rows": rows})
    return out
