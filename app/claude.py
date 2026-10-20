"""Claude helper for the optional "Describe it" box: plain English → Seamless filters (Include / Exclude)."""
import json
import os

import httpx

from .config import cfg
from .filters import FIELDS, clean

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
                              "(CLAUDE_MODEL in .env).")
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
    for f in FIELDS:
        desc = f["label"] + (". " + f["help"] if f.get("help") else "")
        if f["kind"] == "list":
            item = {"type": "string", "enum": f["options"]} if f.get("options") else {"type": "string"}
            side = {"type": "array", "items": item}
            sides = {"in": dict(side, maxItems=f.get("max", 10))}
            if f.get("exclude", "none") != "none":
                sides["out"] = side
            props[f["key"]] = {"type": "object", "properties": sides, "description": desc}
        elif f["kind"] == "bool":
            props[f["key"]] = {"type": "boolean", "description": desc}
        elif f["kind"] == "int":
            props[f["key"]] = {"type": "integer", "minimum": f.get("min", 0), "maximum": f.get("max", 1000),
                               "description": desc}
        elif f["kind"] == "choice":
            props[f["key"]] = {"type": "string", "enum": [str(o) for o in f["options"]], "description": desc}
    return props


SYSTEM = """You convert a salesperson's plain-English prospecting request into Seamless.AI contact-search filters.
Every list filter has "in" (include) and, where allowed, "out" (exclude). Rules:
- Use only filters the request supports. Fewer, accurate filters beat many guessed ones.
- jobTitle.in: up to 10 titles, with the common variations of each role asked for (CTO, Chief Technology Officer).
- Prefer seniority/department for broad role groups; jobTitle for specific roles.
- contactCountry: full English country names. "Europe" = the main European countries, max 10.
- Cities go in locations ("Austin, Texas", "Tel Aviv, Israel").
- locationType: leave unset unless the user says the place is where the COMPANY is ("companies headquartered in
  Israel" -> company).
- industry, companySize, companyRevenue, seniority, department, funding and news fields: exact listed values only.
- Employee ranges map to companySize values (e.g. "1-500 employees" -> 0 - 1 (Self-employed), 2 - 10, 11 - 50,
  51 - 200, 201 - 500).
- Anything to skip goes in "out" (e.g. "no staffing agencies" -> industry.out Staffing & Recruiting and
  companyName.out staffing, recruiting; "not interns" -> jobTitle.out intern).
- notes: one short line for every interpretation the user might disagree with, and anything you couldn't express.
- target: the number of contacts the user asked for, 0 if not said.
- name: a short name for this pull, 2-6 words."""


def describe(prompt):
    tool = {"name": "set_search", "description": "Set the Seamless contact search for this request.",
            "input_schema": {"type": "object", "properties": {
                "name": {"type": "string"}, "target": {"type": "integer"},
                "filters": {"type": "object", "properties": _filter_schema()},
                "notes": {"type": "array", "items": {"type": "string"}}},
                "required": ["name", "filters", "notes"]}}
    out = _call(cfg.CLAUDE_MODEL, SYSTEM, [{"role": "user", "content": prompt}], tool)
    try:
        target = max(0, int(out.get("target") or 0))
    except (TypeError, ValueError):
        target = 0
    return {"name": (out.get("name") or "")[:80], "filters": clean(out.get("filters") or {}),
            "notes": [str(n) for n in (out.get("notes") or []) if str(n).strip()], "target": target}
