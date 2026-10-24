"""Governance rules. Deterministic code, not the LLM, decides whether to spend, sleep, propose, hold or stop."""
import hashlib
import os
import re
import time

import store

BUDGET_COMPANY_HOUR = float(os.environ.get("BUDGET_COMPANY_HOUR", 0.15))  # USD per company per rolling hour
BUDGET_DAY = float(os.environ.get("BUDGET_DAY", 3.00))                    # USD across everything per rolling day
EST_CALL_USD = 0.03          # conservative estimate used before a call
MAX_ATTEMPTS = 3             # failed observes before asking the founder
FORECAST_MIN_CONF = 0.50
MATCH_MIN_SCORE = 0.70
BACKOFF_MIN = [5, 10, 20]    # stagnation sleep, minutes
ACTIVE_SLEEP_MIN = 5         # sleep after a productive cycle
MATCH_MIN_INTERVAL_S = 45    # don't re-match one company faster than this
FATIGUE_PENDING = 2          # candidate already has this many open intros -> hold
FATIGUE_WINDOW_S = 3600      # or received one within this window
MAX_PROPOSALS_PER_RUN = 2
OPEN_INTRO = ("proposed", "requested")

# Founder hiring briefs
URGENCY_CHECK_MIN = {"this week": 5, "this month": 20, "this quarter": 60}
URGENCY_OPTIONS = (*URGENCY_CHECK_MIN, "just exploring")
EXPLORING_SLEEP_MIN = 24 * 60   # "just exploring": sleep until a new candidate wakes it (store.wake_active)
STALE_DAYS = 14
MISMATCH_MIN_POSTINGS = 2       # new postings in an unrelated role family before asking the founder


def company_budget(c):
    return c["budget_hour"] if c.get("budget_hour") is not None else BUDGET_COMPANY_HOUR


def can_spend(c):
    """Return (ok, reason). Logs nothing; caller logs the decision."""
    now = time.time()
    day = store.spend_total(now - 86400)
    if day + EST_CALL_USD > BUDGET_DAY:
        return False, f"global daily cap reached (${day:.2f} of ${BUDGET_DAY:.2f})"
    hour = store.spend_total(now - 3600, c["id"])
    cap = company_budget(c)
    if hour + EST_CALL_USD > cap:
        return False, f"${hour:.2f} of ${cap:.2f}/h spent on {c['name']} in the last hour"
    return True, ""


def burn_rate_warning():
    """Projected daily spend from the last 15 minutes; returns a message if it would blow the daily cap."""
    now = time.time()
    recent = store.spend_total(now - 900)
    projected = store.spend_total(now - 86400) + recent * 4 * 24
    if recent > 0 and projected > BUDGET_DAY:
        return f"burn rate ${recent * 4:.2f}/h would hit the ${BUDGET_DAY:.2f}/day cap; slowing down"
    return None


def stagnation_sleep(c):
    """Next stagnation sleep (minutes) and the new backoff index."""
    idx = min(c.get("backoff_idx") or 0, len(BACKOFF_MIN) - 1)
    return BACKOFF_MIN[idx], min(idx + 1, len(BACKOFF_MIN) - 1)


def flip_flop(company_id):
    """True when the top forecast alternates between the same two roles across the last 4 forecasts (A,B,A,B)."""
    rows = store.q("SELECT top_role FROM forecasts WHERE company_id=? ORDER BY id DESC LIMIT 4", (company_id,))
    roles = [r["top_role"].strip().lower() for r in rows]
    return len(roles) == 4 and roles[0] == roles[2] and roles[1] == roles[3] and roles[0] != roles[1]


def check_intro(c, cand, score, role):
    """Decide PROPOSE / HOLD / LOOP / SKIP for one (company, candidate) pair, with a reason."""
    if cand["status"] != "active":
        return "SKIP", f"{cand['first_name']} paused intros"
    prior = store.one("SELECT status FROM intros WHERE company_id=? AND candidate_id=? ORDER BY id DESC LIMIT 1", (c["id"], cand["id"]))
    if prior:
        return "LOOP", f"{cand['first_name']} × {c['name']} already {prior['status']}; not re-proposing"
    if score < MATCH_MIN_SCORE:
        return "HOLD", f"{cand['first_name']} scored {score:.2f} < {MATCH_MIN_SCORE:.2f}"
    now = time.time()
    open_count = store.one(f"SELECT COUNT(*) AS n FROM intros WHERE candidate_id=? AND status IN {OPEN_INTRO}", (cand["id"],))["n"]
    if open_count >= FATIGUE_PENDING:
        return "HOLD", f"{cand['first_name']} already has {open_count} open intros (respecting their attention)"
    recent = store.one("SELECT COUNT(*) AS n FROM intros WHERE candidate_id=? AND created_at>?", (cand["id"], now - FATIGUE_WINDOW_S))["n"]
    if recent:
        return "HOLD", f"{cand['first_name']} got an intro in the last hour (respecting their attention)"
    return "PROPOSE", f"{cand['first_name']} ({score:.2f}) for {role}"


# ---------- founder hiring briefs ----------
ROLE_FAMILIES = {  # coarse, deterministic role buckets for brief-vs-board comparisons
    "engineering": r"\bengineer|\bdeveloper\b|\bswe\b|\bbackend\b|\bfrontend\b|\bfull[- ]?stack\b|\binfra|\bplatform\b|\bsre\b|\bdevops\b",
    "ML": r"\bml\b|machine learning|\bai\b|\bllms?\b|\bresearch (engineer|scientist)|\bapplied scientist",
    "data": r"\bdata\b|\banalytics\b|\banalyst\b",
    "sales": r"\bsales\b|account executive|\bae\b|\bsdr\b|\bbdr\b|business development|account manager",
    "marketing": r"\bmarketing\b|\bgrowth\b|\bcontent\b|\bseo\b|\bbrand\b|\bcommunity\b",
    "design": r"\bdesign|\bux\b|\bui\b",
    "product": r"product manager|\bpm\b|head of product|product lead",
    "devrel": r"developer advocate|\bdevrel\b|developer relations|developer experience",
    "support": r"\bsupport\b|customer success|solutions engineer|implementation|onboarding",
    "security": r"\bsecurity\b|\bsoc ?2\b|compliance",
    "recruiting": r"\brecruit|\btalent\b",
    "operations": r"\boperations\b|\bfinance\b|\blegal\b|\bpeople\b|\boffice manager|\bhr\b",
}


def role_families(text):
    t = (text or "").lower()
    return {fam for fam, pat in ROLE_FAMILIES.items() if re.search(pat, t)}


def _brief_families(b):
    scope = b.get("scope") or {}
    return role_families(" ".join([b["description"], " ".join(b["skills"]), scope.get("role", ""), scope.get("one_liner", "")]))


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def ask_founder(c, b, key, message, reason):
    """ASK_FOUNDER once per key (hashed); repeats are dropped silently. Returns True when a new ask was raised."""
    h = _hash(key)
    if store.one("SELECT id FROM asks WHERE hash=?", (h,)):
        return False
    store.ex("INSERT INTO asks(hash, company_id, brief_id, kind, message, ts) VALUES(?,?,?,?,?,?)",
             (h, c["id"], b["id"], key.split(":")[0], message, time.time()))
    store.log("ASK_FOUNDER", f"{c['name']}: \"{message}\" — {reason}", c["id"])
    return True


def brief_gate(c, briefs):
    """Founder said filled/paused on every brief → stop spending on the company. Returns (status, reason) or None."""
    if not briefs or any(b["status"] == "active" for b in briefs):
        return None
    latest = briefs[0]
    return ("stopped" if latest["status"] == "filled" else "paused",
            f"founder marked their brief {latest['status']} → no more spend on {c['name']}")


def brief_live(brief_id):
    """Re-read the brief right before spending: a filled/paused flip takes effect immediately."""
    row = store.one("SELECT status FROM hiring_briefs WHERE id=?", (brief_id,))
    if row and row["status"] == "active":
        return True, ""
    return False, f"brief #{brief_id} is {row['status'] if row else 'gone'}"


def urgency(b):
    if b.get("start_timing") in URGENCY_OPTIONS:
        return b["start_timing"]
    return (b.get("scope") or {}).get("urgency") or "this month"


def cadence(briefs):
    """Check interval (minutes) from the most urgent active brief, with the reason to log."""
    urgencies = [urgency(b) for b in briefs]
    timed = sorted((URGENCY_CHECK_MIN[u], u) for u in urgencies if u in URGENCY_CHECK_MIN)
    if timed:
        minutes, u = timed[0]
        return minutes, f"brief urgency '{u}' sets a {minutes}-min check cadence"
    return EXPLORING_SLEEP_MIN, "brief is 'just exploring', so only a new candidate wakes it"


def new_postings_since(b, postings, added):
    """Postings new since the last check, plus any published on/after the day the brief was written."""
    day = time.strftime("%Y-%m-%d", time.gmtime(b["created_at"] or 0))
    seen = {p["id"] for p in added}
    return list(added) + [p for p in postings if p["id"] not in seen and p.get("published") and p["published"] >= day]


def brief_mismatch(b, new_postings):
    """New postings in a role family the brief doesn't cover → (key, message, reason) or None."""
    mine = _brief_families(b)
    if not mine:
        return None
    other = {}
    for p in new_postings:
        fams = role_families(p["title"])
        if fams and not fams & mine:
            for fam in fams:
                other.setdefault(fam, []).append(p["title"])
    if not other:
        return None
    fam, titles = max(other.items(), key=lambda kv: len(kv[1]))
    if len(titles) < MISMATCH_MIN_POSTINGS:
        return None
    role = (b.get("scope") or {}).get("role") or "/".join(sorted(mine))
    return (f"mismatch:{b['id']}:{fam}", f"Your board now shows {fam} roles — add a brief for that?",
            f"{len(titles)} new postings ({', '.join(titles[:3])}) are {fam}, but the brief is for {role}")


def brief_stale(b):
    """Active brief older than STALE_DAYS (since written or last confirmed) with no accepted intro → (key, message, reason)."""
    since = b.get("confirmed_at") or b["created_at"]
    days = (time.time() - since) / 86400
    if days < STALE_DAYS:
        return None
    accepted = store.one("SELECT COUNT(*) AS n FROM intros WHERE company_id=? AND status='accepted' AND created_at>=?",
                         (b["company_id"], b["created_at"]))["n"]
    if accepted:
        return None
    role = (b.get("scope") or {}).get("role") or "this role"
    return (f"stale:{b['id']}:{int(since)}", f"Still hiring for {role}?",
            f"brief is {days:.0f} days old with no accepted intro")


_STOP = {"and", "the", "for", "with", "who", "can", "our", "not", "has", "have", "you", "your", "someone", "experience",
         "strong", "good", "great", "ability", "able", "plus", "years", "skills", "knowledge", "work", "working"}


def _words(text):
    return set(re.findall(r"[a-z0-9+#]+", (text or "").lower()))


def grounded(items, source):
    """Split items into (kept, dropped): an item is kept if one of its significant words appears in the source text.
    A cheap guard against requirements nobody wrote; prefix match tolerates engineer/engineering."""
    src = _words(source)
    prefixes = {w[:5] for w in src if len(w) >= 5}
    kept, dropped = [], []
    for it in items:
        sig = [w for w in _words(it) if len(w) >= 2 and w not in _STOP]
        ok = not sig or any(w in src or (len(w) >= 5 and w[:5] in prefixes) for w in sig)
        (kept if ok else dropped).append(it)
    return kept, dropped


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[\"'“”‘’.,;:!()]", " ", (text or "").lower())).strip()


def valid_cites(cites, brief_text):
    """Keep only cites whose quoted text really appears in the brief or its spec."""
    blob = _norm(brief_text)
    return [c for c in cites if len(_norm(c["brief_text"])) >= 2 and _norm(c["brief_text"]) in blob]


def forecast_suggestion(fc, briefs):
    """Job-board forecast as a secondary hint: only when it points at a role no active brief covers."""
    if not fc or not briefs or not fc["data"]["forecasts"]:
        return None
    top = fc["data"]["forecasts"][0]
    fams = role_families(top["role"])
    mine = set().union(*(_brief_families(b) for b in briefs))
    if float(top["confidence"]) >= FORECAST_MIN_CONF and fams and not fams & mine:
        return f"You may also need: {top['role']}"
    return None
