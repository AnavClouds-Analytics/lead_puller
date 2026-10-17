"""Fake Seamless.AI and Claude APIs (httpx MockTransport handlers) used by the tests and demo mode."""
import base64
import json
import random
import time

import httpx

COUNTRIES = ["United States"] * 6 + ["Canada"] * 2 + ["Israel", "Germany"]
TITLES = ["Chief Technology Officer", "VP Engineering", "Head of IT", "CTO", "Engineering Intern",
          "Director of Engineering", "IT Recruiter"]


def make_people(n=400, seed=7):
    rnd = random.Random(seed)
    people = []
    for i in range(n):
        co = rnd.randint(0, 120)
        company = f"Staffing Partners {co}" if co % 17 == 0 else f"Acme Software {co}"
        country = COUNTRIES[i % len(COUNTRIES)]
        people.append({
            "searchResultId": f"sr-{i:05d}",
            "name": f"Person{i} Lastname{i}",
            "company": company, "domain": f"acme{co}.com",
            "title": TITLES[i % len(TITLES)], "seniority": "C-Level" if i % 3 == 0 else "VP",
            "department": "Engineering", "country": country, "state": "", "city": "",
            "companyCountry": country, "liUrl": f"https://www.linkedin.com/in/person-{i}/",
            "industries": ["Computer Software"], "employeeSizeRange": "201 - 500",
        })
    return people


class FakeSeamless:
    def __init__(self, people=None, balance=10_000, rate_limit_every=0, shuffle_poll=True):
        self.people = people if people is not None else make_people()
        self.balance = balance
        self.calls = []
        self.rate_limit_every = rate_limit_every
        self.shuffle_poll = shuffle_poll
        self.requests = {}  # requestId -> {sid, polls}
        self.researched = set()
        self.n = 0

    def _resp(self, status, body, extra=None):
        headers = {"X-PublicAPI-Credits": str(self.balance), "X-RateLimit-Limit": "60",
                   "X-RateLimit-Remaining": "50", "X-RateLimit-Reset": str(int(time.time()) + 1)}
        headers.update(extra or {})
        return httpx.Response(status, json=body, headers=headers)

    def handler(self, request: httpx.Request):
        self.n += 1
        path = request.url.path.split("/api/client/v1")[-1]
        self.calls.append((request.method, path))
        if request.headers.get("Token") != "good-key":
            return self._resp(401, {"message": "Unauthorized"})
        if self.rate_limit_every and self.n % self.rate_limit_every == 0:
            return self._resp(429, {"message": "Too many"}, {"X-RateLimit-Reset": str(time.time() + 0.01)})
        if path == "/search/locations":
            q = request.url.params.get("q", "").lower()
            known = ["United States", "Canada", "Israel", "Germany", "United Kingdom", "Ontario, Canada"]
            hits = [{"location": k, "type": "country", "count": 1000} for k in known if k.lower().startswith(q)]
            return self._resp(200, {"data": hits})
        if path == "/search/contacts":
            body = json.loads(request.content)
            countries = set(body.get("contactCountry") or [])
            pool = [p for p in self.people if not countries or p["country"] in countries]
            start = int(json.loads(base64.b64decode(body["nextToken"]))["o"]) if body.get("nextToken") else 0
            limit = int(body.get("limit") or 50)
            page = pool[start:start + limit]
            cost = -(-len(page) // 10)
            if self.balance < 1:
                return self._resp(422, {"code": "insufficientCredits", "msg": "no credits"})
            self.balance -= cost
            more = start + limit < len(pool)
            tok = base64.b64encode(json.dumps({"o": start + limit}).encode()).decode() if more else None
            return self._resp(200, {"data": page, "supplementalData": {
                "isMore": more, "total": len(pool), "perPage": limit, "nextToken": tok}})
        if path == "/contacts/research":
            body = json.loads(request.content)
            sids = body.get("searchResultIds") or []
            if len(sids) > 500:
                return self._resp(400, {"message": "max 500"})
            if self.balance < len(sids):
                return self._resp(422, {"code": "insufficientCredits", "msg": "no credits",
                                        "data": {"additionalCreditsNeeded": len(sids) - self.balance}})
            self.balance -= len(sids)
            ids = []
            for sid in sids:
                rid = "rq-" + sid
                self.requests[rid] = {"sid": sid, "polls": 0}
                ids.append(rid)
            return self._resp(202, {"success": True, "requestIds": ids})
        if path == "/contacts/research/poll":
            rids = request.url.params.get("requestIds", "").split(",")
            assert len(rids) <= 500
            out = []
            for rid in rids:
                req = self.requests.get(rid)
                if not req:
                    continue
                req["polls"] += 1
                sid = req["sid"]
                i = int(sid.split("-")[1])
                p = next(x for x in self.people if x["searchResultId"] == sid)
                if req["polls"] < 2:
                    out.append({"requestId": rid, "searchResultId": sid, "status": "researching"})
                elif i % 11 == 0:
                    out.append({"requestId": rid, "searchResultId": sid, "status": "not found", "message": ""})
                elif i % 13 == 0:
                    out.append({"requestId": rid, "searchResultId": sid, "status": "duplicate"})
                else:
                    first, last = p["name"].split(" ")
                    has_email = not (p["country"] in ("Germany", "Israel") and i % 2 == 0)
                    email = f"{first.lower()}.{last.lower()}@{p['domain']}" if has_email else None
                    out.append({"requestId": rid, "searchResultId": sid, "status": "done", "contact": {
                        "firstName": first, "lastName": last, "fullName": p["name"],
                        "email": email, "email1": email, "email1EmailAI": "valid" if i % 5 else "risky",
                        "email1TotalAI": "95%", "contactPhone1": "415.555.0101", "contactPhone1DataType": "mobile",
                        "companyName": p["company"], "jobTitle": p["title"], "lIProfileUrl": p["liUrl"],
                        "companyDomain": p["domain"], "country": p["country"]}})
            if self.shuffle_poll:
                random.Random(len(out)).shuffle(out)
            return self._resp(200, {"success": True, "data": out})
        return self._resp(404, {"message": "not found"})


class FakeClaude:
    def __init__(self, filters=None):
        self.filters = filters or {"jobTitle": ["Chief Technology Officer", "CTO", "VP Engineering"],
                                   "seniority": ["C-Level", "VP"], "contactCountry": ["United States", "Canada"],
                                   "excludeCompanyWords": ["staffing"], "industry": ["Not A Real Industry"]}
        self.calls = 0

    def handler(self, request: httpx.Request):
        self.calls += 1
        if request.headers.get("x-api-key") != "claude-key":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        body = json.loads(request.content)
        tool = body["tools"][0]["name"]
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
            ratings.reverse()  # order must not matter
            data = {"ratings": ratings}
        return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "t1", "name": tool, "input": data}]})
