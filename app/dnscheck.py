"""Free check: can this company's domain receive email at all? (Has an MX record.)

Answers "yes", "no" or "unknown". Only a clear "no" ever skips anyone; a timeout or DNS error is
"unknown" and the person is kept, so a flaky network never costs you leads.
Results are cached in the database for 30 days.
"""
import time

from . import db
from .config import cfg

CACHE_DAYS = 30
LOOKUP = None          # tests replace this with a fake: domain -> "yes" | "no" | "unknown"


def _real_lookup(domain):
    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        return "unknown", "dnspython not installed"
    r = dns.resolver.Resolver()
    r.lifetime = cfg.DNS_TIMEOUT_SECONDS
    try:
        answers = r.resolve(domain, "MX")
        hosts = [str(a.exchange).rstrip(".") for a in answers]
        if hosts == [""] or hosts == []:          # "null MX" (RFC 7505): domain says it takes no email
            return "no", "domain says it accepts no email"
        return "yes", ", ".join(hosts[:3])
    except dns.resolver.NXDOMAIN:
        return "no", "domain does not exist"
    except dns.resolver.NoAnswer:
        return "no", "no email server (MX) record"
    except (dns.resolver.NoNameservers, dns.exception.Timeout, Exception) as e:  # noqa: BLE001
        return "unknown", type(e).__name__


def has_mail_server(domain):
    domain = db.norm_domain(domain)
    if not domain or "." not in domain:
        return "unknown"
    row = db.q1("SELECT has_mx, checked_at FROM domains WHERE domain=?", (domain,))
    if row and time.time() - row["checked_at"] < CACHE_DAYS * 86400:
        return {1: "yes", 0: "no"}.get(row["has_mx"], "unknown")
    if LOOKUP:
        out = LOOKUP(domain)
        result, detail = (out if isinstance(out, tuple) else (out, ""))
    else:
        result, detail = _real_lookup(domain)
    if result in ("yes", "no"):          # don't cache "unknown": try again next time
        db.x("INSERT INTO domains(domain,has_mx,detail,checked_at) VALUES(?,?,?,?) ON CONFLICT(domain) DO UPDATE "
             "SET has_mx=excluded.has_mx, detail=excluded.detail, checked_at=excluded.checked_at",
             (domain, 1 if result == "yes" else 0, detail, time.time()))
    return result
