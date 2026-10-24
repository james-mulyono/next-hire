"""Claude calls with JSON-schema outputs and per-call cost tracking."""
import json
import os
import time

import anthropic

import store

FORECAST_MODEL = "claude-sonnet-5-5"
MATCH_MODEL = "claude-sonnet-5-5"
SCOPE_MODEL = FORECAST_MODEL
PRICES = {  # USD per million tokens (input, output)
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


class LLMError(Exception):
    pass


def _load_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def available():
    return bool(_load_key())


def call_json(model, system, user, schema, purpose, company_id=None, effort="medium", max_tokens=8000):
    key = _load_key()
    if not key:
        raise LLMError("no ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=key, timeout=90.0, max_retries=1)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIError as e:
        raise LLMError(f"{type(e).__name__}: {str(e)[:160]}") from e

    p_in, p_out = PRICES.get(model, (2.0, 10.0))
    usd = resp.usage.input_tokens / 1e6 * p_in + resp.usage.output_tokens / 1e6 * p_out
    store.ex("INSERT INTO spend(ts, company_id, model, purpose, input_tokens, output_tokens, usd) VALUES(?,?,?,?,?,?,?)",
             (time.time(), company_id, model, purpose, resp.usage.input_tokens, resp.usage.output_tokens, usd))

    if resp.stop_reason == "refusal":
        raise LLMError("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise LLMError("response truncated (max_tokens)")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise LLMError("no text in response")
    try:
        return json.loads(text), usd
    except json.JSONDecodeError as e:
        raise LLMError("invalid JSON") from e


_STR = {"type": "string"}
_NUM = {"type": "number"}
_STRS = {"type": "array", "items": _STR}

FORECAST_SCHEMA = {
    "type": "object",
    "properties": {
        "company_one_liner": _STR,
        "forecasts": {"type": "array", "items": {
            "type": "object",
            "properties": {"role": _STR, "why": _STR, "evidence": _STRS, "confidence": _NUM},
            "required": ["role", "why", "evidence", "confidence"], "additionalProperties": False}},
        "top_role_spec": {
            "type": "object",
            "properties": {"role": _STR, "one_liner": _STR, "must_haves": _STRS, "nice_to_haves": _STRS,
                           "seniority": _STR, "location": _STR},
            "required": ["role", "one_liner", "must_haves", "nice_to_haves", "seniority", "location"],
            "additionalProperties": False},
        "what_would_raise_confidence": _STR,
    },
    "required": ["company_one_liner", "forecasts", "top_role_spec", "what_would_raise_confidence"],
    "additionalProperties": False,
}

FORECAST_SYSTEM = """You forecast a startup's NEXT hire from public signals only (its job board and website, plus any note from the founder).
Rules:
- Predict roles the company is likely to need in the next 1-3 months. A role that is already posted can be a forecast only if the signals suggest they need more of it or a more senior version.
- Every piece of evidence must quote or name a specific posting title, a change in postings, or a phrase from the website/founder note. No generic startup advice.
- Calibrate confidence honestly: above 0.7 only with at least two independent pieces of evidence pointing the same way. Thin signals mean low confidence.
- A founder note is first-hand information and outweighs inference.
- Return 1-3 forecasts sorted by confidence (highest first). top_role_spec describes the top forecast as a short role spec.
- what_would_raise_confidence: the one signal that would most change your mind."""

MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {"type": "array", "items": {
            "type": "object",
            "properties": {"candidate_id": {"type": "integer"}, "score": _NUM, "reason": _STR, "gap": _STR},
            "required": ["candidate_id", "score", "reason", "gap"], "additionalProperties": False}},
    },
    "required": ["matches"],
    "additionalProperties": False,
}

MATCH_SYSTEM = """You match a startup role spec against a pool of people who opted in to intros. Use only what each person wrote about themselves.
Return the 3 best candidates (fewer if the pool is smaller), best first.
Score calibration (0-1): 0.85+ clearly meets every must-have and wants this kind of role; 0.70-0.84 meets the must-haves with minor gaps; below 0.70 misses a must-have or doesn't want this kind of role.
Be strict: most people in a general pool should score below 0.70 for any specific role. A weak intro wastes a founder's and a candidate's time.
reason: one sentence citing what the person wrote. gap: the biggest missing must-have, or "none"."""


def forecast(company, postings, added, removed, site_text):
    lines = [f"Company: {company['name']}" + (f" ({company['domain']})" if company.get("domain") else "")]
    if company.get("judge_note"):
        lines.append(f"FOUNDER NOTE (first-hand): {company['judge_note']}")
    lines.append(f"Website text: {site_text[:2000] if site_text else '(no website text available)'}")
    if postings:
        lines.append(f"Open postings ({len(postings)}):")
        for p in postings[:60]:
            lines.append(f"- {p['title']}" + (f" [{p['team']}]" if p.get("team") else "") + (f" ({p['location']})" if p.get("location") else "")
                         + (f" posted {p['published']}" if p.get("published") else ""))
    else:
        lines.append("Open postings: none found on a public job board.")
    if added:
        lines.append("NEW since last check: " + "; ".join(p["title"] for p in added[:20]))
    if removed:
        lines.append("REMOVED since last check: " + "; ".join(p["title"] for p in removed[:20]))
    return call_json(FORECAST_MODEL, FORECAST_SYSTEM, "\n".join(lines), FORECAST_SCHEMA, "forecast", company["id"], effort="medium")


def match(company, spec, candidates):
    pool = [{"candidate_id": c["id"], "great_at": c["strengths"], "wants_roles": c["roles"], "seniority": c["seniority"],
             "location": c["location"], "availability": c["availability"]} for c in candidates]
    user = f"Company: {company['name']}\nRole spec: {json.dumps(spec)}\n\nPool:\n{json.dumps(pool)}"
    return call_json(MATCH_MODEL, MATCH_SYSTEM, user, MATCH_SCHEMA, "match", company["id"], effort="low")


URGENCIES = ["this week", "this month", "this quarter", "just exploring"]

SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "role": _STR, "one_liner": _STR, "must_haves": _STRS, "nice_to_haves": _STRS, "dealbreakers": _STRS,
        "seniority": _STR, "location": _STR, "urgency": {"type": "string", "enum": URGENCIES},
        "clarifying_questions": _STRS,
    },
    "required": ["role", "one_liner", "must_haves", "nice_to_haves", "dealbreakers", "seniority", "location",
                 "urgency", "clarifying_questions"],
    "additionalProperties": False,
}

SCOPE_SYSTEM = """You turn a founder's hiring brief into a short role spec used to match opted-in candidates.
The founder's own words are the source of truth. Their public website and job postings are background only.
Rules:
- Never invent requirements. Every must_have, nice_to_have and dealbreaker must trace back to something the founder wrote (brief, pasted JD, answers) or, for must/nice-to-haves only, their public website or postings. Keep the founder's wording where you can.
- must_haves: skills the founder tagged plus anything they said the person needs. nice_to_haves: things mentioned as a plus, or qualities implied by their "someone like" example that they did not make essential.
- dealbreakers: only what the founder listed as dealbreakers / not looking for, as short phrases. Never add your own.
- "Someone like" describes a past great hire: extract qualities, never names.
- seniority and location: from the brief or pasted JD; "unspecified" if not given. urgency: from start timing.
- Postings may clarify the stack but must not override or add to what the founder asked for.
- one_liner: one plain sentence a candidate would read describing the role.
- clarifying_questions: 0-2 short questions, only when the answer would materially change who you would match (e.g. two plausible roles, contradictory signals). Do not ask about things that can stay unspecified. If the founder already answered your questions, return []."""

_BRIEF_LABELS = [
    ("description", "Describe the person who'd make your next 3 months easier"),
    ("someone_like", "Someone like (a past great hire/teammate)"),
    ("dealbreakers", "Dealbreakers / not looking for"),
    ("seniority", "Seniority"),
    ("location", "Location / remote"),
    ("start_timing", "Start timing"),
]


def _brief_lines(brief):
    lines = [f"- {label}: {brief[k]}" for k, label in _BRIEF_LABELS if brief.get(k)]
    if brief.get("skills"):
        lines.append(f"- Must-have skills (tagged by founder): {', '.join(brief['skills'])}")
    if brief.get("jd_text"):
        lines.append(f"- Pasted JD / doc: {brief['jd_text'][:4000]}")
    for qa in brief.get("answers") or []:
        if qa.get("a"):
            lines.append(f"- Founder answered \"{qa['q']}\": {qa['a']}")
    return lines


def scope_from_brief(brief, website_text, postings):
    """Founder brief (+ public site/postings as background) -> role spec. brief carries company_name and answers."""
    lines = [f"Company: {brief.get('company_name', '')}" + (f" ({brief['domain']})" if brief.get("domain") else ""),
             "FOUNDER HIRING BRIEF (first-hand, highest priority):", *_brief_lines(brief),
             f"Public website text (background): {website_text[:2000] if website_text else '(none)'}"]
    if postings:
        lines.append(f"Open postings (background, {len(postings)}): " + "; ".join(p["title"] for p in postings[:40]))
    else:
        lines.append("Open postings: none found on a public job board.")
    return call_json(SCOPE_MODEL, SCOPE_SYSTEM, "\n".join(lines), SCOPE_SCHEMA, "scope", brief["company_id"], effort="medium")


BRIEF_PARTS = ["must_have", "nice_to_have", "description", "someone_like", "seniority", "location", "jd"]

MATCH_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "excluded": {"type": "array", "items": {
            "type": "object",
            "properties": {"candidate_id": {"type": "integer"}, "dealbreaker": _STR, "evidence": _STR},
            "required": ["candidate_id", "dealbreaker", "evidence"], "additionalProperties": False}},
        "matches": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer"}, "score": _NUM, "reason": _STR, "gap": _STR,
                "cites": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"brief_part": {"type": "string", "enum": BRIEF_PARTS}, "brief_text": _STR},
                    "required": ["brief_part", "brief_text"], "additionalProperties": False}}},
            "required": ["candidate_id", "score", "reason", "gap", "cites"], "additionalProperties": False}},
    },
    "required": ["excluded", "matches"],
    "additionalProperties": False,
}

MATCH_BRIEF_SYSTEM = MATCH_SYSTEM + """
The role comes from the founder's own hiring brief.
- excluded: every person whose own words hit one of the founder's dealbreakers. dealbreaker: the dealbreaker text; evidence: what the person wrote. Excluded people must NOT appear in matches, however strong otherwise.
- cites: for each match, 1-3 parts of the brief the person satisfies. brief_text must be copied word for word from the brief or the role spec (e.g. a must-have, a phrase from the description)."""


def match_brief(company, brief, candidates):
    scope = brief["scope"]
    pool = [{"candidate_id": c["id"], "great_at": c["strengths"], "wants_roles": c["roles"], "seniority": c["seniority"],
             "location": c["location"], "availability": c["availability"]} for c in candidates]
    spec = {k: scope[k] for k in ("role", "one_liner", "must_haves", "nice_to_haves", "dealbreakers", "seniority", "location")}
    user = (f"Company: {company['name']}\nRole spec (from the founder's brief): {json.dumps(spec)}\n"
            "Founder's own words:\n" + "\n".join(_brief_lines(brief)) + f"\n\nPool:\n{json.dumps(pool)}")
    return call_json(MATCH_MODEL, MATCH_BRIEF_SYSTEM, user, MATCH_BRIEF_SCHEMA, "match", company["id"], effort="low")
