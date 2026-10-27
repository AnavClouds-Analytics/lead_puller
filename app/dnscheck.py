"""Free check: can this company's domain receive email at all? (Has an MX record.)

Answers "yes", "no" or "unknown". Only a clear "no" ever skips anyone; a timeout or DNS error is
"unknown" and the person is kept, so a flaky network never costs you leads.
Results are cached in the database for DNS_CACHE_DAYS (default 30).
"""
import time

from . import db
from .config import cfg

LOOKUP = None          # tests replace this with a fake: domain -> "yes" | "no" | "unknown"
KNOWN_GOOD = ("gmail.com", "outlook.com")
_health = {"ok": None, "at": 0.0}


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


def dns_healthy():
    """A "no" is only believed if this server's DNS can see big mail providers. If DNS on the server is
    broken (it answers "doesn't exist" for everything), nobody is skipped."""
    if _health["ok"] is not None and time.time() - _health["at"] < 600:
        return _health["ok"]
    ok = any(_real_lookup(d)[0] == "yes" for d in KNOWN_GOOD)
    _health.update(ok=ok, at=time.time())
    return ok


def has_mail_server(domain):
    domain = db.norm_domain(domain)
    if not domain or "." not in domain:
        return "unknown"
    row = db.q1("SELECT result, at FROM dns WHERE domain=?", (domain,))
    if row and time.time() - row["at"] < cfg.DNS_CACHE_DAYS * 86400:
        return row["result"]
    if LOOKUP:
        out = LOOKUP(domain)
        result, detail = (out if isinstance(out, tuple) else (out, ""))
    else:
        result, detail = _real_lookup(domain)
        if result == "no" and not dns_healthy():
            result = "unknown"
    if result in ("yes", "no"):          # don't cache "unknown": try again next time
        db.x("INSERT INTO dns(domain,result,at) VALUES(?,?,?) ON CONFLICT(domain) DO UPDATE "
             "SET result=excluded.result, at=excluded.at", (domain, result, time.time()))
    return result
