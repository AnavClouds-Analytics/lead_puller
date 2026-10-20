"""All settings come from the .env file (or real environment variables, which win).

To change anything: edit .env, then restart the app (docker compose up -d).
Nothing here needs a code change.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_VERSION = "1.3.0"


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
    @property
    def SPENDING_ENABLED(self): return _b("SPENDING_ENABLED", True)
    @property
    def LOW_BALANCE_WARNING(self): return max(0, _i("LOW_BALANCE_WARNING", 500))
    @property
    def ACCOUNT_RESERVE(self): return max(0, _i("ACCOUNT_RESERVE", 50))
    @property
    def BALANCE_MISMATCH_WARN(self): return max(1, _i("BALANCE_MISMATCH_WARN", 20))
    @property
    def LOCATION_APPLIES_TO(self):
        v = _s("LOCATION_APPLIES_TO", "contact")
        return v if v in ("contact", "company", "bothOR", "bothAND") else "contact"
    @property
    def OPEN_PULL_HOLD_DAYS(self): return max(0, _i("OPEN_PULL_HOLD_DAYS", 14))

    # --- collecting: stop wasting search credits
    @property
    def COLLECT_CLOSE_ENOUGH_PCT(self): return max(0, min(_i("COLLECT_CLOSE_ENOUGH_PCT", 1), 20))
    @property
    def COLLECT_LOW_KEEP_PCT(self): return max(0, min(_i("COLLECT_LOW_KEEP_PCT", 20), 100))
    @property
    def COLLECT_LOW_KEEP_PAGES(self): return max(1, _i("COLLECT_LOW_KEEP_PAGES", 3))
    @property
    def COLLECT_EMPTY_PAGES_STOP(self): return max(1, _i("COLLECT_EMPTY_PAGES_STOP", 3))
    @property
    def COLLECT_SPEND_CAP_PCT(self): return max(100, _i("COLLECT_SPEND_CAP_PCT", 150))

    # --- fewer people without email
    @property
    def UPDATED_WITHIN_MONTHS(self): return max(0, _i("UPDATED_WITHIN_MONTHS", 12))
    @property
    def REQUIRE_COMPANY_WEBSITE(self): return _b("REQUIRE_COMPANY_WEBSITE", True)
    @property
    def REQUIRE_LINKEDIN(self): return _b("REQUIRE_LINKEDIN", False)
    @property
    def SKIP_NO_EMAIL_SERVER(self): return _b("SKIP_NO_EMAIL_SERVER", True)
    @property
    def DNS_TIMEOUT_SECONDS(self): return max(1, _i("DNS_TIMEOUT_SECONDS", 3))
    @property
    def TEST_BATCH_SIZE(self): return max(0, _i("TEST_BATCH_SIZE", 100))
    @property
    def TEST_BATCH_MIN_EMAIL_RATE(self): return max(0, min(_i("TEST_BATCH_MIN_EMAIL_RATE", 60), 100))
    @property
    def EMAIL_CHANCE_MIN(self): return max(0, min(_i("EMAIL_CHANCE_MIN", 60), 100))
    @property
    def EMAIL_CHANCE_MIN_SAMPLES(self): return max(5, _i("EMAIL_CHANCE_MIN_SAMPLES", 30))
    @property
    def NO_EMAIL_RETRY_DAYS(self): return max(0, _i("NO_EMAIL_RETRY_DAYS", 0))

    # --- downloads
    @property
    def EMAIL_PICK(self):
        v = _s("EMAIL_PICK", "best").lower()
        return v if v in ("best", "seamless") else "best"
    @property
    def DEFAULT_BATCH_SIZE(self): return max(1, _i("DEFAULT_BATCH_SIZE", 500))
    @property
    def TABLE_PAGE_SIZE(self):
        n = _i("TABLE_PAGE_SIZE", 50)
        return n if n in (25, 50, 100, 250) else 50
    @property
    def DEFAULT_EXPORT_COLUMNS(self): return _s("DEFAULT_EXPORT_COLUMNS", "email,first_name,last_name,company")

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
    def RESEARCH_WAIT_MINUTES(self): return max(1, _i("RESEARCH_WAIT_MINUTES", 15))
    @property
    def LATE_ANSWER_HOURS(self): return max(0, _i("LATE_ANSWER_HOURS", 24))
    @property
    def LATE_POLL_MINUTES(self): return max(1, _i("LATE_POLL_MINUTES", 5))
    @property
    def ERROR_PAUSE_AFTER(self): return max(1, _i("ERROR_PAUSE_AFTER", 5))
    @property
    def SAVED_CONTACTS_CACHE_MINUTES(self): return max(0, _i("SAVED_CONTACTS_CACHE_MINUTES", 10))
    @property
    def DNS_CACHE_DAYS(self): return max(1, _i("DNS_CACHE_DAYS", 30))
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
    ("Keys", [("SEAMLESS_API_KEY", "secret", "Optional fallback. Seamless keys are normally added on the Seamless accounts page"),
              ("ANTHROPIC_API_KEY", "secret", "console.anthropic.com → API Keys"),
              ("CLAUDE_MODEL", "", "Model that turns your request into filters"),
              ("CLAUDE_FAST_MODEL", "", "Model that rates fit")]),
    ("Spending and rules", [
        ("MONTHLY_CREDIT_BUDGET", "", "Max Seamless credits this tool spends per calendar month (0 = no limit)"),
        ("MAX_PER_COMPANY", "", "Default max people per company in one pull (0 = no limit)"),
        ("COMPANY_COOLDOWN_DAYS", "", "Skip companies pulled in the last N days, across all pulls (0 = off)"),
        ("FIT_CHECK", "", "1 = Claude rates each person against your request"),
        ("CONTINUE_SAME_SEARCH", "", "1 = a repeated search starts where the last one stopped"),
        ("SPENDING_ENABLED", "", "0 = master switch: stop all Seamless spending (search and research)"),
        ("LOW_BALANCE_WARNING", "", "Red banner when the Seamless balance is below this (0 = off)"),
        ("ACCOUNT_RESERVE", "", "Stop using a Seamless account when its balance would go below this; new pulls use the next account"),
        ("BALANCE_MISMATCH_WARN", "", "Warn when Seamless's balance and this tool's count differ by more than this many credits"),
        ("LOCATION_APPLIES_TO", "", "Default for “Location applies to”: contact = person's location only, company, bothOR, bothAND"),
        ("OPEN_PULL_HOLD_DAYS", "", "People collected in another open pull (not yet researched) are skipped for this many days")]),
    ("Collecting (search credits)", [
        ("COLLECT_CLOSE_ENOUGH_PCT", "", "Finish collecting when this close to the target (% of target)"),
        ("COLLECT_LOW_KEEP_PCT", "", "Pause and ask when the last pages kept fewer than this % of people"),
        ("COLLECT_LOW_KEEP_PAGES", "", "…over this many pages in a row"),
        ("COLLECT_EMPTY_PAGES_STOP", "", "Stop collecting after this many pages in a row that keep nobody"),
        ("COLLECT_SPEND_CAP_PCT", "", "Pause and ask when search credits reach this % of the estimate shown before collecting")]),
    ("Fewer people without email", [
        ("UPDATED_WITHIN_MONTHS", "", "Default: only contacts Seamless updated in the last N months (0 = any age)"),
        ("REQUIRE_COMPANY_WEBSITE", "", "Default: skip people whose company has no website (1 = on)"),
        ("REQUIRE_LINKEDIN", "", "Default: skip people with no LinkedIn profile (1 = on)"),
        ("SKIP_NO_EMAIL_SERVER", "", "1 = skip companies whose domain has no email server (free check)"),
        ("DNS_TIMEOUT_SECONDS", "", "Seconds to wait for that email-server check"),
        ("TEST_BATCH_SIZE", "", "Research this many first, then check the email rate (0 = off)"),
        ("TEST_BATCH_MIN_EMAIL_RATE", "", "Pause if the test batch's email rate is below this %"),
        ("EMAIL_CHANCE_MIN", "", "Default % for “only people with email chance ≥ X%”"),
        ("EMAIL_CHANCE_MIN_SAMPLES", "", "People of a kind researched before the tool trusts its email chance"),
        ("NO_EMAIL_RETRY_DAYS", "", "Retry people who had no email after N days, paying again (0 = never)")]),
    ("Downloads", [
        ("EMAIL_PICK", "", "best = per person, the email with the best status and highest confidence; seamless = Seamless's primary"),
        ("DEFAULT_BATCH_SIZE", "", "Number pre-filled in “Download next …”"),
        ("DEFAULT_EXPORT_COLUMNS", "", "Columns ticked by default in the AnavMail download, in order (see README for all names)"),
        ("TABLE_PAGE_SIZE", "", "Rows per page in every table (25, 50, 100 or 250)")]),
    ("Seamless API", [
        ("PREVIEW_SIZE", "", "People fetched in Preview (1 credit per 10)"),
        ("PAGE_SIZE", "", "People per search page while collecting"),
        ("RESEARCH_BATCH", "", "People sent for research per call (max 500)"),
        ("POLL_SECONDS", "", "Seconds between checks for research results"),
        ("RATE_LIMIT_PER_MIN", "", "Calls per minute per endpoint (Seamless allows 60)"),
        ("RESEARCH_WAIT_MINUTES", "", "Wait this long for a slow Seamless answer before finishing without it"),
        ("LATE_ANSWER_HOURS", "", "Keep checking (free) for answers that arrive late, for this many hours"),
        ("LATE_POLL_MINUTES", "", "How often to check for those late answers"),
        ("ERROR_PAUSE_AFTER", "", "Pause a pull after this many errors in a row"),
        ("SAVED_CONTACTS_CACHE_MINUTES", "", "Minutes to reuse the saved-contacts list during fill-in"),
        ("DNS_CACHE_DAYS", "", "Days to remember the email-server check for a domain"),
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
