"""A deliberately awkward fake of the Seamless.AI v1 API (and of Claude), used to test money and data safety.

It charges exactly as Seamless's docs describe and keeps its own books, so tests can compare what the
tool *thinks* it spent with what the "real" side charged. It can also misbehave on purpose:
shuffled / partial / duplicated poll results, rows without searchResultId, 429s, 500s, timeouts after
charging, expired paging tokens, short pages when credits run low.
"""
import base64
import json
import random
import re
import time

import httpx

COUNTRIES = ["United States"] * 6 + ["Canada"] * 2 + ["Israel", "Germany"]
TITLES = ["Chief Technology Officer", "VP Engineering", "Head of IT", "CTO", "Engineering Intern",
          "Director of Engineering", "IT Recruiter"]
FIRST = ["John", "Priya", "José", "Anna", "Mark", "Wei", "Sara", "Omar", "Liam", "Noa", "Chloé", "Dan"]
LAST = ["Smith", "Müller", "O'Brien", "Cohen", "Garcia", "Chen", "Patel", "Levi", "Brown", "Dubois"]


def make_people(n=400, seed=7):
    """People with realistic traps: shared names across companies, same company name on different
    domains, missing domains/LinkedIn, accents and apostrophes, LinkedIn URL variants."""
    rnd = random.Random(seed)
    people = []
    for i in range(n):
        co = rnd.randint(0, 120)
        company = f"Staffing Partners {co}" if co % 17 == 0 else f"Acme Software {co}"
        domain = f"acme{co}.com"
        if co == 5:                       # same company *name* as co 6 but a different firm/domain
            company, domain = "Acme Software 6", "acme6-labs.io"
        if co == 9:
            domain = ""                   # no domain on record
        first, last = FIRST[i % len(FIRST)], LAST[(i * 7) % len(LAST)]
        li = f"https://www.linkedin.com/in/{first.lower()}-{i}/"
        if i % 9 == 0:
            li = ""                       # no LinkedIn
        elif i % 4 == 0:
            li = f"http://linkedin.com/in/{first.lower()}-{i}?trk=x"   # variant spelling of same kind of URL
        people.append({
            "pid": i, "first": first, "last": last, "name": f"{first} {last}",
            "company": company, "domain": domain, "title": TITLES[i % len(TITLES)],
            "seniority": "C-Level" if i % 3 == 0 else "VP", "department": "Engineering",
            "country": COUNTRIES[i % len(COUNTRIES)], "liUrl": li,
            "industries": ["Computer Software"], "employeeSizeRange": "201 - 500",
        })
    return people


def ascii_slug(s):
    return re.sub(r"[^a-z]", "", s.lower().replace("é", "e").replace("ü", "u"))


def email_for(p):
    """Encodes the person id, so a test can prove each email went to the right person."""
    dom = p["domain"] or "nodomain.example"
    return f"{ascii_slug(p['first'])}.{ascii_slug(p['last'])}.p{p['pid']}@{dom}"


class FakeSeamless:
    def __init__(self, people=None, balance=100_000, seed=1):
        self.people = people if people is not None else make_people()
        self.by_pid = {p["pid"]: p for p in self.people}
        self.balance = balance
        self.rnd = random.Random(seed)
        self.charged = 0                  # total credits actually charged
        self.charges = []                 # (kind, amount)
        self.calls = []                   # (time, method, path)
        self.sid_to_pid = {}              # searchResultIds are new per search, like the real API
        self.search_count = 0
        self.requests = {}                # requestId -> {sid, pid, polls_needed, polls}
        self.researched_pids = {}         # pid -> times charged for research
        self.already_in_org = set()       # pids researched in the Seamless app before this tool existed
        self.org_decoys = []              # extra saved contacts that look similar to real people
        # misbehaviour switches
        self.chaos_poll = False
        self.flaky_rate = 0.0             # chance of 500 / 429 on search, poll, locations
        self.research_fail_next = None    # "500_after_charge" | "timeout_after_charge" | "500_before_charge"
        self.token_ttl = None             # seconds; older paging tokens are rejected with 400
        self.shuffle_request_ids = False  # answer research with requestIds in a different order than sent
        self.email_override = {}          # pid -> email (e.g. a shared info@ address)
        self.clock = time.time

    # ------------------------------------------------------------------ helpers
    def _resp(self, status, body, extra=None):
        headers = {"X-PublicAPI-Credits": str(self.balance), "X-RateLimit-Limit": "60",
                   "X-RateLimit-Remaining": "50", "X-RateLimit-Reset": str(self.clock() + 1)}
        headers.update(extra or {})
        return httpx.Response(status, json=body, headers=headers)

    def charge(self, kind, n):
        self.balance -= n
        self.charged += n
        self.charges.append((kind, n))

    def _search_row(self, p, sid):
        return {"searchResultId": sid, "name": p["name"], "company": p["company"], "domain": p["domain"],
                "title": p["title"], "seniority": p["seniority"], "department": p["department"],
                "country": p["country"], "state": "", "city": "", "companyCountry": p["country"],
                "liUrl": p["liUrl"], "industries": p["industries"], "employeeSizeRange": p["employeeSizeRange"]}

    def contact_record(self, p):
        i = p["pid"]
        has_email = not (p["country"] in ("Germany", "Israel") and i % 2 == 0)
        email = self.email_override.get(i) or (email_for(p) if has_email else None)
        status = "invalid" if i % 23 == 0 else ("risky" if i % 5 == 0 else "valid")
        return {"firstName": p["first"], "lastName": p["last"], "fullName": p["name"],
                "email": email, "email1": email, "email1EmailAI": status, "email1TotalAI": "95%",
                "contactPhone1": f"415.555.{i:04d}", "contactPhone1DataType": "mobile",
                "companyName": p["company"], "jobTitle": p["title"], "lIProfileUrl": p["liUrl"],
                "companyDomain": p["domain"], "country": p["country"]}

    # ------------------------------------------------------------------ router
    def handler(self, request: httpx.Request):
        path = request.url.path.split("/api/client/v1")[-1]
        self.calls.append((self.clock(), request.method, path))
        if request.headers.get("Token") != "good-key":
            return self._resp(401, {"message": "Unauthorized"})
        if path in ("/search/contacts", "/contacts/research/poll", "/search/locations") and self.flaky_rate:
            r = self.rnd.random()
            if r < self.flaky_rate / 2:
                return self._resp(500, {"message": "boom"})       # rejected before doing anything
            if r < self.flaky_rate:
                return self._resp(429, {"message": "slow down"}, {"X-RateLimit-Reset": str(self.clock() + 2)})
        if path == "/search/locations":
            return self.locations(request)
        if path == "/search/contacts":
            return self.search(request)
        if path == "/contacts/research":
            return self.research(request)
        if path == "/contacts/research/poll":
            return self.poll(request)
        if path == "/contacts" and request.method == "GET":
            return self.org(request)
        return self._resp(404, {"message": "not found"})

    def locations(self, request):
        q = request.url.params.get("q", "").lower()
        known = ["United States", "Canada", "Israel", "Germany", "United Kingdom", "Netherlands"]
        hits = [{"location": k, "type": "country", "count": 1000} for k in known if k.lower().startswith(q)]
        return self._resp(200, {"data": hits})

    def search(self, request):
        body = json.loads(request.content)
        countries = set(body.get("contactCountry") or [])
        titles = set(body.get("jobTitle") or [])
        pool = [p for p in self.people if (not countries or p["country"] in countries)]
        if titles == {"Head of IT"}:
            pool = [p for p in pool if p["title"] == "Head of IT"]
        key = json.dumps({k: v for k, v in body.items() if k not in ("limit", "nextToken")}, sort_keys=True)
        start = 0
        if body.get("nextToken"):
            tok = json.loads(base64.b64decode(body["nextToken"]))
            if tok["k"] != key:
                return self._resp(400, {"message": "token does not match search"})
            if self.token_ttl is not None and self.clock() - tok["t"] > self.token_ttl:
                return self._resp(400, {"message": "token expired"})
            start = tok["o"]
        if self.balance < 1:
            return self._resp(422, {"code": "insufficientCredits", "msg": "no credits"})
        limit = int(body.get("limit") or 50)
        limit = min(limit, self.balance * 10)          # docs: page shortened to what the balance can pay for
        page = pool[start:start + limit]
        self.search_count += 1
        rows = []
        for p in page:
            sid = f"sr{self.search_count}-{p['pid']}-{self.rnd.randint(1000, 9999)}"
            self.sid_to_pid[sid] = p["pid"]
            rows.append(self._search_row(p, sid))
        if rows:
            self.charge("search", -(-len(rows) // 10))
        more = start + len(page) < len(pool)
        tok = base64.b64encode(json.dumps({"k": key, "o": start + len(page), "t": self.clock()}).encode()).decode() \
            if more else None
        return self._resp(200, {"data": rows, "supplementalData": {
            "isMore": more, "total": len(pool), "perPage": limit, "nextToken": tok}})

    def research(self, request):
        body = json.loads(request.content)
        sids = body.get("searchResultIds") or []
        if len(sids) > 500:
            return self._resp(400, {"message": "max 500"})
        fail = self.research_fail_next
        if fail == "500_before_charge":
            self.research_fail_next = None
            return self._resp(500, {"message": "boom"})
        new = [s for s in sids if self.sid_to_pid.get(s) is not None
               and self.sid_to_pid[s] not in self.researched_pids and self.sid_to_pid[s] not in self.already_in_org]
        if self.balance < len(new):
            return self._resp(422, {"code": "insufficientCredits", "msg": "no credits",
                                    "data": {"additionalCreditsNeeded": len(new) - self.balance}})
        ids = []
        for sid in sids:
            pid = self.sid_to_pid.get(sid)
            rid = f"rq{len(self.requests)}x{self.rnd.randint(100, 999)}"
            dup = pid in self.researched_pids or pid in self.already_in_org
            if pid is not None and not dup:
                self.charge("research", 1)
                self.researched_pids[pid] = self.researched_pids.get(pid, 0) + 1
            elif pid is not None:
                self.researched_pids[pid] = self.researched_pids.get(pid, 0)
            self.requests[rid] = {"sid": sid, "pid": pid, "dup": dup, "polls": 0,
                                  "need": self.rnd.randint(1, 4) if self.chaos_poll else 1}
            ids.append(rid)
        if fail == "500_after_charge":
            self.research_fail_next = None
            return self._resp(500, {"message": "boom after charging"})
        if fail == "timeout_after_charge":
            self.research_fail_next = None
            raise httpx.ReadTimeout("read timed out", request=request)
        if self.shuffle_request_ids:
            self.rnd.shuffle(ids)
        return self._resp(202, {"success": True, "requestIds": ids})

    def poll(self, request):
        rids = request.url.params.get("requestIds", "").split(",")
        assert len(rids) <= 500
        out = []
        for rid in rids:
            req = self.requests.get(rid)
            if not req:
                continue
            if self.chaos_poll and self.rnd.random() < 0.3:
                continue                                   # left out of this answer
            req["polls"] += 1
            sid, pid = req["sid"], req["pid"]
            row = {"requestId": rid, "searchResultId": sid}
            if self.chaos_poll and self.rnd.random() < 0.25:
                row.pop("searchResultId")                  # some rows come back without it
            if pid is None:
                row.update(status="error", message="unknown search result")
            elif req["polls"] < req["need"]:
                row.update(status=self.rnd.choice(["queued", "researching"]))
            elif req["dup"]:
                row.update(status="duplicate")
            elif pid % 11 == 0:
                row.update(status="not found", message="")
            else:
                row.update(status="done", contact=self.contact_record(self.by_pid[pid]))
            out.append(row)
            if self.chaos_poll and self.rnd.random() < 0.1:
                out.append(dict(row))                      # duplicated row
        if self.chaos_poll:
            self.rnd.shuffle(out)
        return self._resp(200, {"success": True, "data": out})

    def org(self, request):
        page = int(request.url.params.get("page", 1))
        limit = int(request.url.params.get("limit", 500))
        assert request.url.params.get("startDate") and request.url.params.get("endDate")
        pids = sorted(set(self.researched_pids) | self.already_in_org)
        records = self.org_decoys + [self.contact_record(self.by_pid[p]) for p in pids]
        chunk = records[(page - 1) * limit: page * limit]
        return self._resp(200, {"data": chunk})


class FakeClaude:
    def __init__(self, filters=None):
        self.filters = filters or {"jobTitle": ["Chief Technology Officer", "CTO", "VP Engineering"],
                                   "seniority": ["C-Level", "VP"], "contactCountry": ["United States", "Canada"],
                                   "excludeCompanyWords": ["staffing"], "industry": ["Not A Real Industry"]}
        self.calls = 0
        self.fail = False
        self.no_forced_tool = False     # behave like a model that rejects tool_choice "tool"/"any"
        self.text_answer = False        # answer with JSON in a text block instead of calling the tool

    def handler(self, request: httpx.Request):
        self.calls += 1
        if request.headers.get("x-api-key") != "claude-key":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        if self.fail:
            return httpx.Response(529, json={"error": {"message": "overloaded"}})
        body = json.loads(request.content)
        tool = body["tools"][0]["name"]
        if self.no_forced_tool and (body.get("tool_choice") or {}).get("type") in ("tool", "any"):
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                  "message": 'tool_choice: type "tool" and "any" are not supported for this model.'}})
        if tool == "set_search":
            data = {"name": "US & Canada CTOs", "target": 120, "filters": self.filters,
                    "notes": ["Read 'tech leaders' as CTO and VP Engineering."]}
        else:
            text = body["messages"][0]["content"]
            ratings = []
            for line in text.splitlines():
                if line.startswith("{"):
                    p = json.loads(line)
                    if "id" not in p:
                        continue
                    t = (p.get("title") or "").lower()
                    fit = "low" if ("intern" in t or "recruiter" in t) else "high"
                    ratings.append({"id": p["id"], "fit": fit, "reason": "wrong role" if fit == "low" else ""})
            ratings.append({"id": "999999999", "fit": "high"})   # an id that was never asked about
            ratings.reverse()
            data = {"ratings": ratings}
        if self.text_answer:
            return httpx.Response(200, json={"content": [
                {"type": "thinking", "thinking": "..."},
                {"type": "text", "text": "Here you go:\n```json\n" + json.dumps(data) + "\n```"}]})
        return httpx.Response(200, json={"content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "tool_use", "id": "t1", "name": tool, "input": data}]})
