"""A deliberately awkward fake Seamless.AI v1 API, for money and data-safety tests.

It charges exactly as Seamless's docs describe (search: 1 credit per 10 rows returned; research: 1 per person
sent, people already researched in that Seamless organization are 'duplicate' and free) and keeps its own books
per API key, so tests can compare what the tool thinks it spent with what was really charged.
It misbehaves on purpose: shuffled / partial / duplicated poll answers, rows without searchResultId, 429s,
500s, timeouts after charging, expired paging tokens, short pages when credits run low, another account's
search IDs rejected.
"""
import base64
import json
import random
import re

import httpx

SIZES = ["0 - 1 (Self-employed)", "2 - 10", "11 - 50", "51 - 200", "201 - 500", "501 - 1,000",
         "1,001 - 5,000", "5,001 - 10,000", "10,001+"]
COUNTRIES = ["United States"] * 6 + ["Canada"] * 2 + ["Israel", "Germany"]
TITLES = ["Chief Technology Officer", "VP Engineering", "Head of IT", "CTO", "Engineering Intern",
          "Director of Engineering", "IT Recruiter"]
FIRST = ["John", "Priya", "José", "Anna", "Mark", "Wei", "Sara", "Omar", "Liam", "Noa", "Chloé", "Dan", "Ava", "Raj"]
LAST = ["Smith", "Müller", "O'Brien", "Cohen", "Garcia", "Chen", "Patel", "Levi", "Brown", "Dubois", "Rossi", "Kim"]
TECHS = [["Salesforce", "Slack"], ["HubSpot"], ["Salesforce", "Tableau"], ["Zoho CRM"], [], ["AWS", "Salesforce"]]


# Every fake company uses one real-world email format; some accept every address (catch-all), some mix two formats.
FORMATS = ["first.last", "first", "flast", "firstlast", "first_last", "f.last"]


def company_format(co, i):
    if co % 13 == 4:
        return "first.last" if i % 2 else "flast"          # two formats in use at one company
    return FORMATS[co % len(FORMATS)]


def is_catch_all(domain):
    m = re.match(r"acme(\d+)\.com$", domain or "")
    return bool(m) and int(m.group(1)) % 7 == 3


def local_for(fmt, first, last):
    f, l = slug(first), slug(last)
    return {"first.last": f"{f}.{l}", "first": f, "flast": f"{f[0]}{l}", "firstlast": f"{f}{l}",
            "first_last": f"{f}_{l}", "f.last": f"{f[0]}.{l}"}[fmt]


def make_people(n=900, seed=7):
    rnd = random.Random(seed)
    people = []
    names_at = set()
    taken = set()
    for i in range(n):
        co = rnd.randint(0, 260)
        company = f"Staffing Partners {co}" if co % 17 == 0 else f"Acme Software {co}"
        domain = f"acme{co}.com"
        if co == 5:
            company, domain = "Acme Software 6", "acme6-labs.io"     # same name as co 6, another firm
        if co % 29 == 9:
            domain = ""                                               # no website on record
        size = "" if co % 41 == 3 else SIZES[co % len(SIZES)]          # some companies have no size on record
        first, last = FIRST[i % len(FIRST)], LAST[(i * 7) % len(LAST)]
        j = 1
        while (first, last, co) in names_at:                      # no namesakes at one company (a test adds some)
            last = LAST[(i * 7 + j) % len(LAST)]
            j += 1
            if j > len(LAST):
                first = FIRST[(i + j) % len(FIRST)]
        names_at.add((first, last, co))
        li = f"https://www.linkedin.com/in/{first.lower()}-{i}/"
        if i % 9 == 0:
            li = ""
        elif i % 4 == 0:
            li = f"http://linkedin.com/in/{first.lower()}-{i}?trk=x"
        people.append({"pid": i, "first": first, "last": last, "name": f"{first} {last}", "company": company,
                       "domain": domain, "title": TITLES[i % len(TITLES)], "seniority": "C-Level" if i % 3 == 0 else "VP",
                       "department": "Engineering", "country": COUNTRIES[i % len(COUNTRIES)], "liUrl": li,
                       "industries": ["Computer Software"] if co % 5 else ["Staffing & Recruiting"], "size": size,
                       "techs": TECHS[co % len(TECHS)],
                       "lastModifiedAt": "2023-01-15 10:00:00" if i % 6 == 0 else "2026-08-01 10:00:00"})
        people[-1]["email"] = mailbox_for(people[-1], co, i, taken)
    return people


def mailbox_for(p, co, i, taken):
    """The person's real work mailbox, in their company's format (made unique if two people collide)."""
    if not p["domain"]:
        return f"{slug(p['first'])}.{slug(p['last'])}.p{p['pid']}@nodomain.example"
    local = local_for(company_format(co, i), p["first"], p["last"])
    e, k = f"{local}@{p['domain']}", 2
    while e in taken:
        e, k = f"{local}{k}@{p['domain']}", k + 1
    taken.add(e)
    return e


def slug(s):
    return re.sub(r"[^a-z]", "", s.lower().replace("é", "e").replace("ü", "u"))


def email_for(p):
    """The person's real mailbox: the fake knows whose it is, so a test can prove every email went to the right person."""
    return p["email"]


class FakeSeamless:
    def __init__(self, people=None, balance=100_000, seed=1):
        self.people = people if people is not None else make_people()
        self.by_pid = {p["pid"]: p for p in self.people}
        self.mailboxes = {p["email"]: p["pid"] for p in self.people}
        self.rnd = random.Random(seed)
        self.pools = {"key-alpha-1111": balance}
        self.shared_pool = False          # True: every key draws on (and de-duplicates in) one organization
        self.cur = None
        self.charged_by_key = {}
        self.charges = []
        self.calls = []                   # (key, method, path)
        self.searches = []                # (key, body)
        self.sid_to_pid = {}
        self.sid_key = {}
        self.search_n = 0
        self.requests = {}
        self.rid_key = {}
        self.researched = {}              # org -> {pid: times charged}
        self.saved = {}                   # org -> {pid: record}
        self.foreign_sid_rejected = 0
        self.clock = lambda: 0.0
        # misbehaviour switches
        self.chaos_poll = False
        self.flaky_rate = 0.0
        self.research_fail_next = None    # 500_after_charge | timeout_after_charge | 500_before_charge
        self.token_ttl = None
        self.shuffle_request_ids = False
        self.slow_pids = set()
        self.row_email_for_researched = True   # search rows show the email of people your org already researched
        self.email_override = {}
        self.drop_sid = False                  # every poll answer without searchResultId
        self.leak_domains = False

    # ------------------------------------------------------------------ accounts / books
    def add_key(self, key, balance):
        self.pools[key] = balance

    def org(self, key=None):
        return "shared" if self.shared_pool else (key or self.cur)

    def pool_key(self, key=None):
        return next(iter(self.pools)) if self.shared_pool else (key or self.cur)

    def balance_of(self, key):
        return self.pools[self.pool_key(key)]

    @property
    def charged(self):
        return sum(self.charged_by_key.values())

    def charge(self, kind, n):
        self.pools[self.pool_key()] -= n
        self.charged_by_key[self.cur] = self.charged_by_key.get(self.cur, 0) + n
        self.charges.append((self.cur, kind, n))

    def researched_in(self, org):
        return self.researched.setdefault(org, {})

    def _resp(self, status, body, extra=None):
        headers = {"X-PublicAPI-Credits": str(self.balance_of(self.cur)), "X-RateLimit-Reset": str(self.clock() + 1)}
        headers.update(extra or {})
        return httpx.Response(status, json=body, headers=headers)

    # ------------------------------------------------------------------ records
    def record(self, p):
        i = p["pid"]
        has = not (p["country"] in ("Germany", "Israel") and i % 4 == 0)
        email = (self.email_override[i] or None) if i in self.email_override else (email_for(p) if has else None)
        status = "invalid" if i % 23 == 0 else ("risky" if i % 5 == 0 else "valid")
        return {"firstName": p["first"], "lastName": p["last"], "fullName": p["name"], "email": email,
                "email1": email, "email1EmailAI": status, "email1TotalAI": "95%",
                "contactPhone1": f"415.555.{i:04d}", "contactPhone1DataType": "mobile", "companyName": p["company"],
                "jobTitle": p["title"], "lIProfileUrl": p["liUrl"], "companyDomain": p["domain"],
                "country": p["country"], "companyTechnologies": ", ".join(p["techs"])}

    def row(self, p, sid):
        r = {"searchResultId": sid, "name": p["name"], "company": p["company"], "domain": p["domain"],
             "title": p["title"], "seniority": p["seniority"], "department": p["department"], "country": p["country"],
             "state": "", "city": "Springfield", "liUrl": p["liUrl"], "industries": p["industries"],
             "employeeSizeRange": p["size"], "lastModifiedAt": p["lastModifiedAt"]}
        if self.row_email_for_researched and p["pid"] in self.saved.get(self.org(), {}):
            r["email"] = self.saved[self.org()][p["pid"]]["email"]
        return r

    # ------------------------------------------------------------------ router
    def handler(self, request: httpx.Request):
        path = request.url.path.split("/api/client/v1")[-1]
        key = request.headers.get("Token")
        self.calls.append((key, request.method, path))
        if key not in self.pools:
            self.cur = next(iter(self.pools))
            return httpx.Response(401, json={"message": "Unauthorized"})
        self.cur = key
        if path in ("/search/contacts", "/contacts/research/poll", "/search/locations") and self.flaky_rate:
            r = self.rnd.random()
            if r < self.flaky_rate / 2:
                return self._resp(500, {"message": "boom"})
            if r < self.flaky_rate:
                return self._resp(429, {"message": "slow down"}, {"X-RateLimit-Reset": str(self.clock() + 2)})
        if path == "/search/locations":
            q = request.url.params.get("q", "").lower()
            known = ["United States", "Canada", "Israel", "Germany", "Toronto, Ontario, Canada"]
            return self._resp(200, {"data": [{"location": k, "type": "country", "count": 1000} for k in known
                                             if k.lower().startswith(q)]})
        if path == "/search/contacts":
            return self.search(request)
        if path == "/contacts/research":
            return self.research(request)
        if path == "/contacts/research/poll":
            return self.poll(request)
        if path == "/contacts" and request.method == "GET":
            page, limit = int(request.url.params.get("page", 1)), int(request.url.params.get("limit", 500))
            recs = [self.saved[self.org()][k] for k in sorted(self.saved.get(self.org(), {}))]
            return self._resp(200, {"data": recs[(page - 1) * limit: page * limit]})
        return self._resp(404, {"message": "not found"})

    def _match(self, p, body):
        def incl(field, value):
            vals = body.get(field) or []
            inc = [v for v in vals if not str(v).startswith("-")]
            exc = [v[1:] for v in vals if str(v).startswith("-")]
            return (not inc or value in inc) and value not in exc
        if not incl("contactCountry", p["country"]):
            return False
        if body.get("companyDomain") and p["domain"] not in body["companyDomain"] and not (
                self.leak_domains and p["pid"] % 9 == 1):
            return False                      # leak_domains: Seamless also returns some other companies
        if body.get("companySize") is not None and p["size"] not in body["companySize"]:
            return False
        if body.get("industry") and not set(body["industry"]) & set(p["industries"]):
            return False
        if body.get("seniority") and p["seniority"] not in body["seniority"]:
            return False
        if body.get("titlesExactMatch") and p["title"] not in (body.get("jobTitle") or []):
            return False
        if body.get("technologies"):
            want = set(body["technologies"])
            have = set(p["techs"])
            if (body.get("technologiesIsOr") and not want & have) or (not body.get("technologiesIsOr") and not want <= have):
                return False
        if body.get("lastModifiedAfter") and p["lastModifiedAt"][:10] < body["lastModifiedAfter"][:10]:
            return False
        return True

    def search(self, request):
        body = json.loads(request.content)
        self.searches.append((self.cur, body))
        pool = [p for p in self.people if self._match(p, body)]
        skey = json.dumps({k: v for k, v in body.items() if k not in ("limit", "nextToken")}, sort_keys=True)
        start = 0
        if body.get("nextToken"):
            try:
                tok = json.loads(base64.b64decode(body["nextToken"]))
            except Exception:
                return self._resp(400, {"message": "bad token"})
            if tok["k"] != skey or tok["key"] != self.cur:
                return self._resp(400, {"message": "token does not match search"})
            if self.token_ttl is not None and self.clock() - tok["t"] > self.token_ttl:
                return self._resp(400, {"message": "token expired"})
            start = tok["o"]
        if self.balance_of(self.cur) < 1:
            return self._resp(422, {"code": "insufficientCredits", "msg": "no credits"})
        limit = int(body.get("limit") or 50)
        limit = min(limit, 100, self.balance_of(self.cur) * 10)     # shortened to what the balance can pay for
        page = pool[start:start + limit]
        self.search_n += 1
        rows = []
        for p in page:
            sid = f"s{self.search_n}-{p['pid']}-{self.rnd.randint(1000, 9999)}"
            self.sid_to_pid[sid] = p["pid"]
            self.sid_key[sid] = self.cur
            rows.append(self.row(p, sid))
        if rows:
            self.charge("search", -(-len(rows) // 10))
        more = start + len(page) < len(pool)
        tok = base64.b64encode(json.dumps({"k": skey, "o": start + len(page), "t": self.clock(),
                                           "key": self.cur}).encode()).decode() if more else None
        return self._resp(200, {"data": rows, "supplementalData": {"isMore": more, "total": len(pool),
                                                                   "nextToken": tok}})

    def research(self, request):
        body = json.loads(request.content)
        sids = body.get("searchResultIds") or []
        if len(sids) > 500:
            return self._resp(400, {"message": "max 500"})
        fail = self.research_fail_next
        if fail == "500_before_charge":
            self.research_fail_next = None
            return self._resp(500, {"message": "boom"})
        foreign = [s for s in sids if s in self.sid_key and self.sid_key[s] != self.cur]
        if foreign:
            self.foreign_sid_rejected += len(foreign)
            return self._resp(400, {"message": f"{len(foreign)} searchResultIds not found for this account"})
        done = self.researched_in(self.org())
        new = [s for s in sids if self.sid_to_pid.get(s) is not None and self.sid_to_pid[s] not in done]
        if self.balance_of(self.cur) < len(new):
            return self._resp(422, {"code": "insufficientCredits", "msg": "no credits"})
        ids = []
        for sid in sids:
            pid = self.sid_to_pid.get(sid)
            rid = f"r{len(self.requests)}x{self.rnd.randint(100, 999)}"
            dup = pid in done
            if pid is not None and not dup:
                self.charge("research", 1)
                done[pid] = done.get(pid, 0) + 1
            self.requests[rid] = {"sid": sid, "pid": pid, "dup": dup, "polls": 0,
                                  "need": self.rnd.randint(1, 4) if self.chaos_poll else 1}
            self.rid_key[rid] = self.cur
            ids.append(rid)
        if fail in ("500_after_charge", "timeout_after_charge"):
            # Seamless did the research anyway: the people land in your saved contacts.
            for rid in ids:
                self._complete(self.requests[rid])
        if fail == "500_after_charge":
            self.research_fail_next = None
            return self._resp(500, {"message": "boom after charging"})
        if fail == "timeout_after_charge":
            self.research_fail_next = None
            raise httpx.ReadTimeout("read timed out", request=request)
        if self.shuffle_request_ids:
            self.rnd.shuffle(ids)
        return self._resp(202, {"success": True, "requestIds": ids})

    def _complete(self, req):
        """Research finished: people with an email are now in that organization's saved contacts."""
        pid = req["pid"]
        if pid is None or req["dup"] or pid % 11 == 0:
            return
        rec = self.record(self.by_pid[pid])
        if rec.get("email"):
            self.saved.setdefault(self.org(), {})[pid] = rec

    def poll(self, request):
        rids = request.url.params.get("requestIds", "").split(",")
        assert len(rids) <= 500
        out = []
        for rid in rids:
            req = self.requests.get(rid)
            if not req or self.rid_key.get(rid) != self.cur:
                continue
            if self.chaos_poll and self.rnd.random() < 0.3:
                continue
            req["polls"] += 1
            pid = req["pid"]
            row = {"requestId": rid, "searchResultId": req["sid"]}
            if (self.chaos_poll and self.rnd.random() < 0.25) or self.drop_sid:
                row.pop("searchResultId")
            if pid is None:
                row.update(status="error", message="unknown search result")
            elif req["polls"] < req["need"] or pid in self.slow_pids:
                row.update(status=self.rnd.choice(["queued", "researching"]))
            elif req["dup"]:
                row.update(status="duplicate")
            elif pid % 11 == 0:
                row.update(status="not found", message="")
            else:
                row.update(status="done", contact=self.record(self.by_pid[pid]))
                self._complete(req)
            out.append(row)
            if self.chaos_poll and self.rnd.random() < 0.1:
                out.append(dict(row))
        if self.chaos_poll:
            self.rnd.shuffle(out)
        return self._resp(200, {"success": True, "data": out})


class FakeMillionVerifier:
    """MillionVerifier: 'ok' only for mailboxes that really exist; catch-all companies say 'catch_all' for anything."""

    def __init__(self, seamless_fake, credits=50_000, key="mv-key-7d3e"):
        self.s = seamless_fake
        self.credits = credits
        self.key = key
        self.checks = []                  # every paid check: email
        self.credit_calls = 0
        self.fail_next = []               # list of: "error" | "timeout" | "500" | "no_credits" | "garbage"
        self.unknown_domains = set()

    def handler(self, request: httpx.Request):
        path = request.url.path.rstrip("/")
        params = request.url.params
        if params.get("api") != self.key:
            return httpx.Response(200, json={"error": "invalid_api_key", "credits": 0})
        if path.endswith("/credits"):
            self.credit_calls += 1
            return httpx.Response(200, json={"credits": self.credits, "bulk_credits": 0, "renewing_credits": 0, "plan": 4})
        email = (params.get("email") or "").lower()
        if self.fail_next:
            mode = self.fail_next.pop(0)
            if mode == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if mode == "500":
                return httpx.Response(500, text="boom")
            if mode == "garbage":
                return httpx.Response(200, text="<html>")
            if mode == "no_credits":
                return httpx.Response(200, json={"email": email, "result": "error", "error": "Insufficient credits",
                                                 "credits": 0})
            return httpx.Response(200, json={"email": email, "result": "error", "resultcode": 4,
                                             "error": "Internal error"})
        if self.credits < 1:
            return httpx.Response(200, json={"email": email, "result": "error", "error": "Insufficient credits",
                                             "credits": 0})
        self.credits -= 1
        self.checks.append(email)
        domain = email.split("@")[-1]
        if domain in self.unknown_domains:
            res, code = "unknown", 3
        elif is_catch_all(domain):
            res, code = "catch_all", 2
        elif email in self.s.mailboxes:
            res, code = "ok", 1
        else:
            res, code = "invalid", 6
        return httpx.Response(200, json={"email": email, "quality": "good", "result": res, "resultcode": code,
                                         "subresult": "", "free": False, "role": False, "didyoumean": "",
                                         "credits": self.credits, "executiontime": 1, "error": "", "livemode": True})


class FakeClaude:
    def __init__(self):
        self.calls = 0

    def handler(self, request: httpx.Request):
        self.calls += 1
        if request.headers.get("x-api-key") != "claude-key":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        data = {"name": "Israel CTOs", "target": 300, "notes": ["Read 'tech leaders' as CTO and VP Engineering."],
                "filters": {"jobTitle": {"in": ["CTO", "Chief Technology Officer"], "out": ["intern"]},
                            "contactCountry": {"in": ["Israel"]},
                            "companySize": {"in": ["2 - 10", "11 - 50", "51 - 200", "201 - 500", "Bogus"]},
                            "industry": {"in": ["Not A Real Industry", "Computer Software"]}}}
        return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "t1", "name": "set_search",
                                                      "input": data}]})
