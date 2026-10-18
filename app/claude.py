"""Claude helpers: turn a plain-English request into Seamless filters, and rate how well people fit it."""
import json
import os

import httpx

from .config import cfg
from .filters import FILTERS, clean_filters

API_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages"
TRANSPORT = None  # tests swap in a MockTransport


class ClaudeError(Exception):
    pass


def api_key():
    return cfg.ANTHROPIC_API_KEY


# Models that refused a forced tool_choice; for these we ask for the tool in words instead.
_NO_FORCED_TOOL = set()


def _post(body, key):
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        with httpx.Client(timeout=180, transport=TRANSPORT) as c:
            return c.post(API_URL, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise ClaudeError(f"Could not reach Claude: {e}")


def _error_message(r):
    try:
        return r.json().get("error", {}).get("message") or ""
    except Exception:
        return ""


def _extract(data, tool_name):
    """Take the tool's input; if the model answered in text instead, take the JSON object from the text."""
    text = []
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool_name:
            return block.get("input") or {}
        if block.get("type") == "text":
            text.append(block.get("text") or "")
    joined = "\n".join(text)
    start, end = joined.find("{"), joined.rfind("}")
    if start != -1 and end > start:
        try:
            out = json.loads(joined[start:end + 1])
            if isinstance(out, dict):
                return out
        except ValueError:
            pass
    return None


def _call(model, system, messages, tool, max_tokens=2000):
    key = api_key()
    if not key:
        raise ClaudeError("No Claude API key. Ask your developer to set ANTHROPIC_API_KEY in .env.")
    forced = model not in _NO_FORCED_TOOL
    for attempt in range(3):
        body = {"model": model, "max_tokens": max_tokens, "messages": messages, "tools": [tool]}
        if forced:
            body["system"] = system
            body["tool_choice"] = {"type": "tool", "name": tool["name"]}
        else:
            # Some models (e.g. with extended thinking on by default) reject forced tool use.
            body["system"] = (system + f"\n\nAlways answer by calling the {tool['name']} tool exactly once. "
                              "Do not answer in plain text.")
            body["tool_choice"] = {"type": "auto"}
        r = _post(body, key)
        if r.status_code == 400 and forced and "tool_choice" in _error_message(r):
            _NO_FORCED_TOOL.add(model)
            forced = False
            continue
        if r.status_code == 401:
            raise ClaudeError("Claude did not accept the API key (ANTHROPIC_API_KEY in .env).")
        if r.status_code == 404 or (r.status_code == 400 and "model" in _error_message(r).lower()):
            raise ClaudeError(f"Claude model “{model}” isn't available to this key: {_error_message(r)} "
                              "(CLAUDE_MODEL / CLAUDE_FAST_MODEL in .env).")
        if r.status_code in (429, 529):
            raise ClaudeError("Claude is busy right now. Try again in a minute.")
        if r.status_code >= 400:
            raise ClaudeError(_error_message(r) or f"Claude error {r.status_code}")
        out = _extract(r.json(), tool["name"])
        if out is not None:
            return out
        if not forced and attempt < 2:
            continue    # model talked instead of calling the tool; ask again
        break
    raise ClaudeError("Claude returned no usable answer. Try again, or rephrase the request.")


def _filter_schema():
    props = {}
    for f in FILTERS:
        desc = f["label"] + (". " + f["help"] if f.get("help") else "")
        if f["kind"] == "enum_list":
            props[f["key"]] = {"type": "array", "items": {"type": "string", "enum": f["options"]},
                               "maxItems": f.get("max", 10), "description": desc}
        elif f["kind"] == "text_list":
            props[f["key"]] = {"type": "array", "items": {"type": "string"}, "maxItems": f.get("max", 10),
                               "description": desc}
        elif f["kind"] == "bool":
            props[f["key"]] = {"type": "boolean", "description": desc}
        elif f["kind"] == "int":
            props[f["key"]] = {"type": "integer", "minimum": f.get("min", 0), "maximum": f.get("max", 1000),
                               "description": desc}
        elif f["kind"] == "choice":
            if True in f["options"]:
                props[f["key"]] = {"type": "boolean", "description": desc}
            else:
                props[f["key"]] = {"type": "string", "enum": f["options"], "description": desc}
    return props


FILTER_SYSTEM = """You convert a salesperson's plain-English prospecting request into filters for the Seamless.AI \
contact search API. Rules:
- Use only filters the request supports. Fewer, accurate filters beat many guessed ones.
- jobTitle: up to 10 strings; include the common variations of each title the person asked for.
- Prefer seniority/department for broad role groups; use jobTitle for specific roles.
- contactCountry: full English country names (United States, Canada, Israel, United Kingdom, Germany ...). \
  "Europe" means list the main European countries, max 10. States: full names or US abbreviations.
- Seamless has no city filter here; use states or zip codes, or leave location at country level and say so in notes.
- industry, companySize, companyRevenue, seniority, department and the funding/news fields must use the exact listed values.
- Use the exclude* fields for anything the user wants to skip (e.g. "no staffing agencies" -> excludeIndustries \
  Staffing & Recruiting plus excludeCompanyWords staffing, recruit).
- notes: one short plain-English line for every interpretation you made that the user might disagree with \
  (e.g. "Read 'mid-size' as 51-1,000 employees."), and anything you could not express as a filter.
- Only set updatedWithinMonths, requireCompanyWebsite, requireLinkedIn, roleMinMonths/roleMaxYears,
  pastCompany, titleMustContain, excludeCountries or excludeSeniority when the request asks for it
  (the tool applies its own defaults for freshness, website and LinkedIn).
- When the user names exact roles ("only CTOs"), also put the key words in titleMustContain.
- "<role> level" (e.g. "CTO level") is ambiguous: choose one reading and say which in notes.
- target: the number of people the user asked for, or 0 if they did not say or said "all".
- name: a short name for this pull, 2-6 words."""


def prompt_to_filters(prompt, current=None):
    props = _filter_schema()
    tool = {"name": "set_search", "description": "Set the Seamless contact search for this request.",
            "input_schema": {"type": "object", "properties": {
                "name": {"type": "string"},
                "target": {"type": "integer"},
                "filters": {"type": "object", "properties": props},
                "notes": {"type": "array", "items": {"type": "string"}}},
                "required": ["name", "filters", "notes"]}}
    if current:
        user = (f"Current filters:\n{json.dumps(current, indent=1)}\n\nChange them as follows (keep everything "
                f"else the same unless the instruction says otherwise):\n{prompt}")
    else:
        user = prompt
    model = cfg.CLAUDE_MODEL
    out = _call(model, FILTER_SYSTEM, [{"role": "user", "content": user}], tool)
    filters = clean_filters(out.get("filters") or {})
    notes = [str(n) for n in (out.get("notes") or []) if str(n).strip()]
    try:
        target = int(out.get("target") or 0)
    except (TypeError, ValueError):
        target = 0
    return {"name": (out.get("name") or "New pull")[:80], "filters": filters, "notes": notes,
            "target": max(0, target)}


FIT_SYSTEM = """You check whether each person in a prospect list matches what the salesperson asked for. \
Judge only on the fields given (title, seniority, department, company, industry, size, country). \
fit = "high" (clearly the kind of person asked for), "medium" (plausible but off in one way), \
"low" (wrong role, wrong kind of company, or wrong place). reason: at most 10 words, only for medium/low."""


def score_fit(prompt, filters, people):
    """people: list of dicts with 'id' and profile fields. Returns {id: (fit, reason)}. Matched by id."""
    if not people:
        return {}
    tool = {"name": "rate_people", "description": "Rate each person's fit.",
            "input_schema": {"type": "object", "properties": {"ratings": {"type": "array", "items": {
                "type": "object", "properties": {
                    "id": {"type": "string"}, "fit": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"}}, "required": ["id", "fit"]}}}, "required": ["ratings"]}}
    lines = []
    for p in people:
        lines.append(json.dumps({k: p.get(k) for k in ("id", "title", "seniority", "department", "company",
                                                       "industry", "size", "country")}))
    user = (f"The salesperson asked for:\n{prompt}\n\nSearch filters used:\n{json.dumps(filters)}\n\n"
            f"People (one JSON per line):\n" + "\n".join(lines))
    model = cfg.CLAUDE_FAST_MODEL
    out = _call(model, FIT_SYSTEM, [{"role": "user", "content": user}], tool, max_tokens=4000)
    wanted = {str(p["id"]) for p in people}
    result = {}
    for r in out.get("ratings") or []:
        pid = str(r.get("id"))
        if pid in wanted and r.get("fit") in ("high", "medium", "low"):
            result[pid] = (r["fit"], (r.get("reason") or "")[:120])
    return result
