"""Email guessing: learn each company's email format from real emails, guess the next person's address, and use it
only if MillionVerifier says the mailbox exists. Every rule here leans towards paying Seamless instead of guessing:
no known format, a company that accepts every address (catch-all), an unclear name, a possible namesake, a slow or
failed check, or a company whose guesses keep failing all mean "pay Seamless as usual".
"""
import json
import re
import time
import unicodedata

from . import db, verifier
from .config import cfg

NOW = time.time

PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "first_last": lambda f, l: f"{f}_{l}",
    "first-last": lambda f, l: f"{f}-{l}",
    "first": lambda f, l: f,
    "last": lambda f, l: l,
    "flast": lambda f, l: f"{f[0]}{l}",
    "f.last": lambda f, l: f"{f[0]}.{l}",
    "f_last": lambda f, l: f"{f[0]}_{l}",
    "firstl": lambda f, l: f"{f}{l[0]}",
    "first.l": lambda f, l: f"{f}.{l[0]}",
    "lastfirst": lambda f, l: f"{l}{f}",
    "last.first": lambda f, l: f"{l}.{f}",
    "last_first": lambda f, l: f"{l}_{f}",
    "lastf": lambda f, l: f"{l}{f[0]}",
}
LABEL = {k: k + "@" for k in PATTERNS}
# Only formats with the whole first AND last name are guessed: an address like john@ or jsmith@ can belong to a
# different John or J. Smith at the same company, and MillionVerifier would still say "valid".
FULL_NAME = {"first.last", "firstlast", "first_last", "first-last", "lastfirst", "last.first", "last_first"}
INITIALS = {"flast", "f.last", "f_last", "firstl", "first.l", "lastf"}
SENIORITY_RANK = [("founder", 0), ("owner", 0), ("c-level", 0), ("c level", 0), ("chief", 0), ("partner", 1),
                  ("vp", 1), ("vice president", 1), ("head", 2), ("director", 2), ("manager", 3)]


def seniority_rank(seniority, title=""):
    s = f"{seniority or ''} {title or ''}".lower()
    return min([r for k, r in SENIORITY_RANK if k in s] or [9])


def _ascii(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower().strip()
    return s.replace("'", "").replace("’", "").replace("ß", "ss").replace("ø", "o").replace("æ", "ae").replace("ł", "l")


def name_parts(first, last):
    """('jose', 'garcia') for clear names; None when the name is unclear (initials, several words, odd letters)."""
    f, l = _ascii(first), _ascii(last)
    if not re.fullmatch(r"[a-z]{2,30}", f or "") or not re.fullmatch(r"[a-z]{2,40}", l or ""):
        return None
    return f, l


def patterns_of(local, f, l):
    local = (local or "").lower()
    return sorted(k for k, fn in PATTERNS.items() if fn(f, l) == local)


# ---------------------------------------------------------------- learning from real emails
def learn(first, last, email, domain, source):
    """Remember a real (email, name) pair at a company: which format(s) its local part follows."""
    em = db.norm_email(email)
    d = db.norm_domain(domain)
    parts = name_parts(first, last)
    if not em or not d or not parts:
        return False
    local, edom = em.split("@", 1)
    pats = patterns_of(local, *parts)
    if not pats:
        return False                               # info@, sales@ or a format we don't guess
    db.x("""INSERT INTO email_examples(email,domain,email_domain,first,last,patterns,source,at) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(email) DO NOTHING""", (em, d, edom, parts[0], parts[1], json.dumps(pats), source, NOW()))
    return True


def learn_from_record(rec, domain, source):
    emails = [rec.get(k) for k in ("email", "email1") if rec.get(k)]
    if not emails:
        return False
    return learn(rec.get("firstName"), rec.get("lastName"), emails[0],
                 domain or rec.get("companyDomain") or "", source)


def backfill():
    """Once: learn formats from everything already in the database (earlier pulls, Seamless saved contacts)."""
    if db.get_meta("guess_backfill"):
        return
    for r in db.q("""SELECT first_name, last_name, email, domain, email_status FROM contacts WHERE email IS NOT NULL
                     AND email!='' AND domain IS NOT NULL AND domain!='' AND COALESCE(email_source,'')!='guessed'"""):
        learn(r["first_name"], r["last_name"], r["email"], r["domain"], "pull")
    for r in db.q("SELECT record_json FROM saved_contacts WHERE email IS NOT NULL"):
        try:
            learn_from_record(json.loads(r["record_json"]), None, "seamless")
        except (ValueError, TypeError):
            pass
    db.set_meta("guess_backfill", {"at": NOW()})


# ---------------------------------------------------------------- per-company state
def domain_state(domain):
    return db.q1("SELECT * FROM domain_guess WHERE domain=?", (db.norm_domain(domain),)) or \
        {"domain": domain, "catch_all": 0, "tried": 0, "valid": 0}


def _bump(domain, valid=None, catch_all=None):
    d = db.norm_domain(domain)
    db.x("INSERT OR IGNORE INTO domain_guess(domain, updated_at) VALUES(?,?)", (d, NOW()))
    if catch_all:
        db.x("UPDATE domain_guess SET catch_all=1, updated_at=? WHERE domain=?", (NOW(), d))
    if valid is not None:
        db.x("UPDATE domain_guess SET tried=tried+1, valid=valid+?, updated_at=? WHERE domain=?",
             (1 if valid else 0, NOW(), d))


def formats(domain):
    """[(email_domain, pattern, examples)] for a company, most examples first."""
    counts = {}
    for r in db.q("SELECT email_domain, patterns FROM email_examples WHERE domain=?", (db.norm_domain(domain),)):
        pats = json.loads(r["patterns"] or "[]")
        for p in pats:
            k = (r["email_domain"], p)
            counts[k] = counts.get(k, 0) + 1.0 / len(pats)
    return sorted(((e, p, n) for (e, p), n in counts.items()), key=lambda x: (-x[2], x[1]))


def format_label(domain):
    """For the Companies table: 'first.last@', 'Catch-all', or None (not learned yet)."""
    st = domain_state(domain)
    if st["catch_all"]:
        return "catch_all"
    f = formats(domain)
    return LABEL.get(f[0][1]) if f else None


def blocked(domain):
    """Why this company is never guessed, or None."""
    st = domain_state(domain)
    if st["catch_all"]:
        return "Company accepts every address (catch-all), so a guess can't be checked."
    if st["tried"] >= 3 and 100.0 * st["valid"] / st["tried"] < cfg.GUESS_STOP_BELOW_RATE:
        return f"Guesses at this company keep failing ({st['valid']} of {st['tried']} valid)."
    return None


def plan(c, others_same_name):
    """The addresses to check for this person, best first ([] = don't guess), and why not if empty."""
    d = db.norm_domain(c.get("domain"))
    if not d:
        return [], "No company website."
    parts = name_parts(c.get("first_name"), c.get("last_name"))
    if not parts:
        return [], "Name not clear enough to guess."
    why = blocked(d)
    if why:
        return [], why
    if others_same_name:
        return [], "Someone else with the same name at this company: a guess could reach the wrong person."
    fm = formats(d)
    if not fm:
        return [], "Company's email format not known yet."
    allowed = FULL_NAME | (INITIALS if cfg.GUESS_INITIAL_FORMATS else set())
    usable = [x for x in fm if x[1] in allowed]
    if not usable:
        return [], (f"Company's format ({LABEL[fm[0][1]]}) could belong to someone else with the same "
                    f"{'first name' if fm[0][1] in ('first', 'last') else 'initial and surname'}, so it isn't guessed.")
    out = []
    for edom, pat, _ in usable:
        if pat in INITIALS and _initials_clash(d, edom, pat, parts):
            continue
        e = f"{PATTERNS[pat](*parts)}@{edom}"
        if e not in out:
            out.append(e)
        if len(out) >= max(1, cfg.GUESS_MAX_CHECKS_PER_PERSON):
            break
    if not out:
        return [], "Someone you know of at this company would have the same address."
    # Never guess an address that already belongs to someone (a namesake, or an earlier pull).
    for e in list(out):
        if db.q1("SELECT 1 x FROM memory WHERE key=?", ("em:" + e,)) or \
                db.q1("SELECT 1 x FROM email_examples WHERE email=?", (e,)):
            return [], "That address already belongs to someone you have."
    return out, None


def _initials_clash(domain, edom, pat, parts):
    """Initial formats only: would anyone else seen at this company (in any search result) get the same address?"""
    mine = PATTERNS[pat](*parts)
    for r in db.q("SELECT DISTINCT first_name, last_name FROM contacts WHERE domain=?", (domain,)):
        other = name_parts(r["first_name"], r["last_name"])
        if other and other != parts and PATTERNS[pat](*other) == mine:
            return True
    for r in db.q("SELECT first, last FROM email_examples WHERE domain=?", (domain,)):
        if (r["first"], r["last"]) != parts and PATTERNS[pat](r["first"], r["last"]) == mine:
            return True
    return False


def try_person(c, addresses, pull_id):
    """Check the planned addresses in order. Returns (email or None, note)."""
    tried = []
    for e in addresses:
        res = verifier.check(e, pull_id, c["id"])
        tried.append(f"{e}: {res}")
        if res == "ok":
            _bump(c["domain"], valid=True)
            return e, "; ".join(tried)
        if res == "catch_all":
            _bump(c["domain"], catch_all=True)
            return None, "; ".join(tried)
        if res in ("invalid", "disposable"):
            _bump(c["domain"], valid=False)
            continue
        return None, "; ".join(tried)          # unknown / error: stop, pay Seamless as usual
    return None, "; ".join(tried)
