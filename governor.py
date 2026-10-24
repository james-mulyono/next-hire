"""Governance rules. Deterministic code, not the LLM, decides whether to spend, sleep, propose, hold or stop."""
import os
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
