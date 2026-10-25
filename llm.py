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


WEB_SEARCH_USD = 0.01  # per search request

RESEARCH_SYSTEM = """You research a company's recent public news to sharpen a forecast of its next hire.
Search for company-level news only: funding rounds, product launches, new markets or offices, big customers,
partnerships, changelog or blog posts, leadership announcements. Never research, name or profile individual
employees or candidates. Prefer the last 90 days and the company's own site, blog and changelog, then reputable press.
Reply with 2-5 short bullets, each '- YYYY-MM or "undated": fact (source domain)', then one line starting
'Implication:' saying which hire this points to, or 'Implication: nothing new' if you found nothing useful."""


def research(company, top_role, why_unsure):
    """Web research when the agent is unsure. Returns (notes, usd). Plain text: server tools + free text, no schema."""
    key = _load_key()
    if not key:
        raise LLMError("no ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=key, timeout=120.0, max_retries=1)
    who = company["name"] + (f" ({company['domain']})" if company.get("domain") else "")
    user = (f"Company: {who}\nCurrent best guess for next hire: {top_role}\n"
            f"What would raise confidence: {why_unsure}\nFind recent public news that confirms or changes this.")
    messages = [{"role": "user", "content": user}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    usd, resp = 0.0, None
    try:
        for _ in range(3):  # resume server-side loop on pause_turn, bounded
            resp = client.messages.create(model=FORECAST_MODEL, max_tokens=3000, system=RESEARCH_SYSTEM,
                                          tools=tools, messages=messages, output_config={"effort": "low"})
            u = resp.usage
            searches = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
            p_in, p_out = PRICES[FORECAST_MODEL]
            call_usd = u.input_tokens / 1e6 * p_in + u.output_tokens / 1e6 * p_out + searches * WEB_SEARCH_USD
            usd += call_usd
            store.ex("INSERT INTO spend(ts, company_id, model, purpose, input_tokens, output_tokens, usd) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), company["id"], FORECAST_MODEL, "research", u.input_tokens, u.output_tokens, call_usd))
            if resp.stop_reason != "pause_turn":
                break
            messages = [{"role": "user", "content": user}, {"role": "assistant", "content": resp.content}]
    except anthropic.APIError as e:
        raise LLMError(f"{type(e).__name__}: {str(e)[:160]}") from e
    if resp.stop_reason == "refusal":
        raise LLMError("model declined the request")
    text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise LLMError("no research notes returned")
    return text[:1500], usd


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
        "what_they_value": {
            "type": "object",
            "properties": {"values": _STRS, "key_skills": _STRS, "source": _STR},
            "required": ["values", "key_skills", "source"], "additionalProperties": False},
    },
    "required": ["company_one_liner", "forecasts", "top_role_spec", "what_would_raise_confidence", "what_they_value"],
    "additionalProperties": False,
}

FORECAST_SYSTEM = """You forecast a startup's NEXT hire from public signals only (its job board and website, plus any note from the founder).
Rules:
- Predict roles the company is likely to need in the next 1-3 months. A role that is already posted can be a forecast only if the signals suggest they need more of it or a more senior version.
- Every piece of evidence must quote or name a specific posting title, a change in postings, or a phrase from the website/founder note. No generic startup advice.
- Calibrate confidence honestly: above 0.7 only with at least two independent pieces of evidence pointing the same way. Thin signals mean low confidence.
- A founder note is first-hand information and outweighs inference.
- Return 1-3 forecasts sorted by confidence (highest first). top_role_spec describes the top forecast as a short role spec.
- what_would_raise_confidence: the one signal that would most change your mind.
- what_they_value: from the company's own careers/about/values pages (and postings), the 3-5 values or working styles they say they care about (keep their wording, e.g. "ship fast", "high ownership") and the 3-6 skills that come up most across their postings and pages. source: which pages these came from, or "postings only" / "not stated" if the pages say nothing. Never invent values the pages don't state."""

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

MATCH_SYSTEM = """You match a startup role spec against a pool of people who opted in to intros. Use only what each person wrote about themselves, plus github_verified: facts read from the public GitHub profile they chose to link. Treat it as evidence of real work; never penalize someone for not having it.
Return the 3 best candidates (fewer if the pool is smaller), best first.
Score calibration (0-1): 0.85+ clearly meets every must-have and wants this kind of role; 0.70-0.84 meets the must-haves with minor gaps; below 0.70 misses a must-have or doesn't want this kind of role.
Be strict: most people in a general pool should score below 0.70 for any specific role. A weak intro wastes a founder's and a candidate's time.
reason: one sentence citing what the person wrote. gap: the biggest missing must-have, or "none"."""


def forecast(company, postings, added, removed, site_text):
    lines = [f"Company: {company['name']}" + (f" ({company['domain']})" if company.get("domain") else "")]
    if company.get("judge_note"):
        lines.append(f"FOUNDER NOTE (first-hand): {company['judge_note']}")
    if company.get("research_note"):
        lines.append(f"RECENT PUBLIC NEWS (agent's own web research; dated, weigh by recency): {company['research_note']}")
    lines.append(f"Website text: {site_text[:2000] if site_text else '(no website text available)'}")
    if company.get("pages_text"):
        lines.append(f"Careers / about / values pages: {company['pages_text']}")
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


def _pool(candidates):
    return [{"candidate_id": c["id"], "great_at": c["strengths"], "wants_roles": c["roles"], "seniority": c["seniority"],
             "location": c["location"], "availability": c["availability"],
             **({"school": c["school"]} if c.get("school") else {}),
             **({"github_verified": c["github_summary"]} if c.get("github_summary") else {}),
             **({"profile": json.loads(c["profile_json"])} if c.get("profile_json") else {}),
             **({"resume_excerpt": c["resume_text"][:1500]} if c.get("resume_text") else {})} for c in candidates]


def match(company, spec, candidates):
    pool = _pool(candidates)
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
- must_haves: 3-5 qualifications a candidate must already have (skills, experience, degree), from the tags and what the founder said the person needs. Job duties and responsibilities ("present your work", "communicate status") are not qualifications: fold the important ones into one skill (e.g. "A/B testing and experiment analysis") and drop generic soft skills. nice_to_haves: things mentioned as a plus, or qualities implied by their "someone like" example that they did not make essential.
- dealbreakers: only what the founder listed as dealbreakers / not looking for, as short phrases describing the person to EXCLUDE (e.g. "Bachelor's only, no PhD"). Never add your own. If the founder's dealbreaker text also states a requirement (e.g. "need PhD"), put that requirement in must_haves and keep only the excluded case here. A dealbreaker must never contradict a must-have.
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
    pool = _pool(candidates)
    spec = {k: scope[k] for k in ("role", "one_liner", "must_haves", "nice_to_haves", "dealbreakers", "seniority", "location")}
    user = (f"Company: {company['name']}\nRole spec (from the founder's brief): {json.dumps(spec)}\n"
            "Founder's own words:\n" + "\n".join(_brief_lines(brief)) + f"\n\nPool:\n{json.dumps(pool)}")
    return call_json(MATCH_MODEL, MATCH_BRIEF_SYSTEM, user, MATCH_BRIEF_SCHEMA, "match", company["id"], effort="low")


PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": _STR, "skills": _STRS, "specializations": _STRS,
        "internships": {"type": "array", "items": {"type": "object", "properties": {"title": _STR, "company": _STR, "when": _STR},
                                                    "required": ["title", "company", "when"], "additionalProperties": False}},
        "projects": {"type": "array", "items": {"type": "object", "properties": {"name": _STR, "what": _STR},
                                                 "required": ["name", "what"], "additionalProperties": False}},
    },
    "required": ["headline", "skills", "specializations", "internships", "projects"],
    "additionalProperties": False,
}
PROFILE_SYSTEM = """Summarize one candidate's profile from what they shared: their signup answers, resume, and the GitHub and LinkedIn
profiles they linked and consented to us reading. Use only facts in the input; never guess.
headline: one line, e.g. "Backend engineer, 3 yrs, Go and Postgres". skills: up to 12 concrete skills or tools.
specializations: up to 4 areas. internships: past internships only (title, company, when). projects: up to 4 notable
projects from GitHub repos or LinkedIn, each with a short 'what'."""


def build_profile(cand, linkedin_text):
    parts = [f"Signup: strengths: {cand.get('strengths')}; wants: {cand.get('roles')}; seniority: {cand.get('seniority')}; school: {cand.get('school') or 'n/a'}"]
    if cand.get("resume_text"):
        parts.append(f"Resume: {cand['resume_text'][:2500]}")
    if cand.get("github_summary"):
        parts.append(f"GitHub (verified): {cand['github_summary']}")
    if linkedin_text:
        parts.append(f"LinkedIn (verified): {linkedin_text}")
    return call_json(MATCH_MODEL, PROFILE_SYSTEM, "\n".join(parts), PROFILE_SCHEMA, "profile", effort="low", max_tokens=1500)
