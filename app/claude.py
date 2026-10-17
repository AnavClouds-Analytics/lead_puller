"""Claude helpers: turn a plain-English request into Seamless filters, and rate how well people fit it."""
import json
import os

import httpx

from . import db
from .filters import FILTERS, clean_filters

API_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages"
TRANSPORT = None  # tests swap in a MockTransport
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_FAST_MODEL = "claude-haiku-5-5"


class ClaudeError(Exception):
    pass


def api_key():
    return os.environ.get("ANTHROPIC_API_KEY") or db.get_setting("anthropic_api_key") or ""


def _call(model, system, messages, tool, max_tokens=2000):
    key = api_key()
    if not key:
        raise ClaudeError("No Claude API key yet. Add it in Settings.")
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages,
            "tools": [tool], "tool_choice": {"type": "tool", "name": tool["name"]}}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        with httpx.Client(timeout=120, transport=TRANSPORT) as c:
            r = c.post(API_URL, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise ClaudeError(f"Could not reach Claude: {e}")
    if r.status_code == 401:
        raise ClaudeError("Claude did not accept the API key. Check it in Settings.")
    if r.status_code == 429:
        raise ClaudeError("Claude is rate-limiting right now. Try again in a minute.")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message")
        except Exception:
            msg = None
        raise ClaudeError(msg or f"Claude error {r.status_code}")
    for block in r.json().get("content", []):
        if block.get("type") == "tool_use":
            return block.get("input") or {}
    raise ClaudeError("Claude returned no answer.")


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
- target: the number of people the user asked for, or 0 if they did not say.
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
    model = db.get_setting("claude_model") or DEFAULT_MODEL
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
    model = db.get_setting("claude_fast_model") or DEFAULT_FAST_MODEL
    out = _call(model, FIT_SYSTEM, [{"role": "user", "content": user}], tool, max_tokens=4000)
    wanted = {str(p["id"]) for p in people}
    result = {}
    for r in out.get("ratings") or []:
        pid = str(r.get("id"))
        if pid in wanted and r.get("fit") in ("high", "medium", "low"):
            result[pid] = (r["fit"], (r.get("reason") or "")[:120])
    return result
