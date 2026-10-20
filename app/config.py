"""Every setting lives in .env. Each value is read fresh from the environment when it is used, so changing
.env and running `docker compose up -d` is all it takes. Nothing here needs a code change."""
import os

APP_VERSION = "2.0.0"

# (name, default, type, group, what it does). Order = order on the Settings page and in .env.example.
SETTINGS = [
    ("APP_PASSWORD", "", "secret", "Login", "Password for the tool"),
    ("SESSION_DAYS", "30", "int", "Login", "Days before you have to log in again"),
    ("COOKIE_SECURE", "1", "bool", "Login", "1 once the site is on HTTPS (it should be); 0 only for local testing"),

    ("SEAMLESS_API_KEY", "", "secret", "Keys", "Optional. Seamless keys are normally added on the Accounts page; "
                                               "a key here shows up there as one more account"),
    ("ANTHROPIC_API_KEY", "", "secret", "Keys", "Optional. Only for the \"Describe it\" box (console.anthropic.com → API Keys)"),
    ("CLAUDE_MODEL", "claude-sonnet-5-5", "str", "Keys", "Claude model used by the \"Describe it\" box"),

    ("SPENDING_ENABLED", "1", "bool", "Spending", "0 = master switch: nothing is searched or researched"),
    ("MONTHLY_CREDIT_BUDGET", "9500", "int", "Spending", "Max credits per calendar month, all accounts together (0 = no limit)"),
    ("ACCOUNT_RESERVE", "50", "int", "Spending", "Never take an account's balance below this; the next account is used instead"),
    ("LOW_BALANCE_WARNING", "500", "int", "Spending", "Warning banner when an account's balance is below this (0 = off)"),
    ("DEFAULT_TARGET", "1000", "int", "Spending", "Emails pre-filled in \"Contacts with email\" on a new pull"),
    ("DEFAULT_MAX_CREDITS_PER_EMAIL", "1.3", "float", "Spending",
     "Max credits pre-filled on a new pull = target × this (you can change it per pull)"),

    ("REQUIRE_COMPANY_WEBSITE", "1", "bool", "Saving credits",
     "Skip people whose company has no website before paying (no website = almost never a work email)"),
    ("SKIP_NO_EMAIL_SERVER", "1", "bool", "Saving credits",
     "Skip companies whose domain can't receive email (free DNS check) before paying"),
    ("MAX_PER_COMPANY", "3", "int", "Saving credits", "Default max people per company in one pull (0 = no limit)"),
    ("UPDATED_WITHIN_MONTHS", "12", "int", "Saving credits",
     "Default for new pulls: only contacts Seamless updated in the last N months (fresher = more emails; 0 = any)"),
    ("SLICE_BY_COMPANY_SIZE", "1", "bool", "Saving credits",
     "Split each search by company size and track every slice, so expensive slices are left for later"),
    ("MOVE_ON_SEARCH_COST", "1.0", "float", "Saving credits",
     "Move to the next slice when finding each new email costs more than this many search credits"),
    ("MOVE_ON_PAGES", "3", "int", "Saving credits", "…measured over this many search pages"),
    ("FRESH_RESCAN", "1", "bool", "Saving credits",
     "When a search has been fully scanned, later runs ask Seamless only for people updated since then"),
    ("BANK_DAYS", "14", "int", "Saving credits",
     "People found but not needed (target reached) are kept and used first by the next pull with the same "
     "filters, for this many days"),
    ("SYNC_SEAMLESS_SAVED", "1", "bool", "Saving credits",
     "Add everyone already researched in your Seamless accounts (website, Chrome extension) to the "
     "already-pulled memory, so they're never paid for again (free)"),
    ("SAVED_SYNC_HOURS", "24", "int", "Saving credits", "How often to re-read your Seamless saved contacts"),
    ("SAVED_LOOKBACK_DAYS", "730", "int", "Saving credits", "How far back to read saved contacts the first time"),
    ("SAVED_MAX_PAGES", "60", "int", "Saving credits", "Max pages of 500 saved contacts read per sync"),
    ("FIRST_BATCH", "25", "int", "Saving credits",
     "The first research batch of every pull is this small; the email rate is checked before sending more"),
    ("PAUSE_BELOW_EMAIL_RATE", "30", "int", "Saving credits",
     "If fewer than this % of that first batch have an email, the pull pauses and asks before spending more (0 = off)"),
    ("EXPECTED_EMAIL_RATE", "80", "int", "Saving credits",
     "% of researched people expected to have an email, until your own history says otherwise"),

    ("LOCATION_APPLIES_TO", "contact", "str", "Search",
     "Default \"Location applies to\": contact = person's location (recommended), company, bothOR, bothAND"),
    ("PAGE_SIZE", "50", "int", "Search", "Max people per search page (pages are sized to what's still needed)"),
    ("RESEARCH_BATCH", "100", "int", "Search", "Max people sent for research per call (Seamless max 500)"),
    ("POLL_SECONDS", "4", "int", "Search", "Seconds between checks for research results (free)"),
    ("RATE_LIMIT_PER_MIN", "55", "int", "Search", "Calls per minute per endpoint (Seamless allows 60 per organization)"),
    ("RESEARCH_WAIT_MINUTES", "15", "int", "Search", "Wait this long for slow research answers, then finish without them"),
    ("LATE_ANSWER_HOURS", "24", "int", "Search", "Keep checking (free) for answers that arrive late, for this many hours"),
    ("LATE_POLL_MINUTES", "5", "int", "Search", "How often to check for late answers"),
    ("ERROR_PAUSE_AFTER", "5", "int", "Search", "Pause a pull after this many Seamless errors in a row"),
    ("DNS_TIMEOUT_SECONDS", "3", "float", "Search", "Wait this long for the email-server check (a timeout never skips anyone)"),
    ("DNS_CACHE_DAYS", "30", "int", "Search", "Days to remember the email-server check per domain"),

    ("EMAIL_PICK", "best", "str", "Downloads",
     "best = per person the email with the best status, then highest confidence; seamless = Seamless's primary email"),
    ("DEFAULT_COLUMNS", "email,first_name,last_name,company", "str", "Downloads",
     "Columns of the \"Download for AnavMail\" file, in order"),
    ("TABLE_PAGE_SIZE", "50", "int", "Downloads", "Rows per page in tables"),

    ("IMPORT_OLD_DATABASE", "1", "bool", "Advanced",
     "On the first start, copy everyone already paid for, the do-not-pull list and the Seamless accounts from the old "
     "Lead Puller database (leadpuller.db in DATA_DIR), so nobody is paid for again"),
    ("SEAMLESS_BASE_URL", "https://api.seamless.ai/api/client/v1", "str", "Advanced", "Seamless API address"),
    ("DATA_DIR", "./data", "str", "Advanced", "Folder for the database (docker-compose sets /data)"),
]

_TYPES = {k: t for k, _, t, _, _ in SETTINGS}
_DEFAULTS = {k: d for k, d, _, _, _ in SETTINGS}


def _load_dotenv(path):
    """Plain KEY=value lines; # comments; quotes optional. Never overrides a variable already set."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if v and v[0] in "\"'":
                    q = v[0]
                    v = v[1:v.index(q, 1)] if q in v[1:] else v[1:]
                elif " #" in v:
                    v = v.split(" #", 1)[0].strip()
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


_load_dotenv(os.environ.get("ENV_FILE", ".env"))


def _raw(name):
    v = os.environ.get(name)
    return _DEFAULTS.get(name, "") if v is None or v.strip() == "" and _DEFAULTS.get(name, "") else (v or "").strip()


class _Cfg:
    def __getattr__(self, name):
        if name not in _TYPES:
            raise AttributeError(name)
        raw, t = _raw(name), _TYPES[name]
        if t == "int":
            try:
                return int(float(raw))
            except ValueError:
                return int(float(_DEFAULTS[name]))
        if t == "float":
            try:
                return float(raw)
            except ValueError:
                return float(_DEFAULTS[name])
        if t == "bool":
            return raw.lower() in ("1", "true", "yes", "on")
        return raw


cfg = _Cfg()


def _mask(v):
    return ("•••• " + v[-4:]) if v else ""


def shown_settings():
    """For the read-only Settings page: every setting, its current value (secrets masked) and what it does."""
    groups = {}
    for k, _, t, g, help_ in SETTINGS:
        v = _raw(k)
        groups.setdefault(g, []).append({"key": k, "value": _mask(v) if t == "secret" else v, "help": help_})
    return [{"group": g, "rows": rows} for g, rows in groups.items()]
