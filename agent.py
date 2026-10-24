"""The agent loop: observe -> forecast -> match -> decide, one company at a time, every step through the governor.

app.py reloads this module before every cycle, so logic changes ship without restarting the process.
"""
import hashlib
import json
import time

import governor
import llm
import signals
import store

PER_CYCLE = 2            # companies handled per cycle, so the log keeps moving
OBSERVE_EVERY_S = 300    # re-fetch public signals at most this often per company
RETRY_SOON_S = 20


def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def _sleep(c, minutes, reason, backoff_idx=None):
    fields = {"sleep_until": time.time() + minutes * 60}
    if backoff_idx is not None:
        fields["backoff_idx"] = backoff_idx
    store.update_company(c["id"], **fields)
    if reason:
        store.log("SLEEP", f"{c['name']}: {reason} → sleeping {minutes:g} min", c["id"])


def cycle():
    now = time.time()
    warn = governor.burn_rate_warning()
    if warn and now - float(store.get_meta("last_burn_warn", 0)) > 600:
        store.set_meta("last_burn_warn", now)
        store.log("BUDGET", warn)
    due = store.q("SELECT * FROM companies WHERE status='active' AND sleep_until<=? ORDER BY sleep_until, id LIMIT ?", (now, PER_CYCLE))
    for c in due:
        try:
            process(c)
        except Exception as e:  # one company must never take the loop down
            store.log("ERROR", f"{c['name']}: {type(e).__name__}: {str(e)[:160]} → retrying later", c["id"])
            store.update_company(c["id"], sleep_until=time.time() + 120)


def observe(c, has_brief=False):
    """Fetch public signals if due. Returns (postings, added, removed, site_text) or None if the agent is stuck.
    With an active founder brief, public signals are background only, so a missing board never blocks."""
    snap = store.snapshot(c["id"])
    if snap and time.time() - c["last_observed"] < OBSERVE_EVERY_S:
        return snap["postings"], [], [], snap["site_text"] or ""

    # Find a job board if we don't know one yet.
    if not c["board_type"]:
        slug = c["board_slug"] or signals.slugify(c["name"])
        board_type, _ = signals.detect_board(slug)
        if board_type:
            store.update_company(c["id"], board_type=board_type, board_slug=slug, attempts=0)
            c["board_type"], c["board_slug"] = board_type, slug
            store.log("OBSERVE", f"{c['name']}: found a public {board_type} board ('{slug}')", c["id"])
        else:
            attempts = c["attempts"] + 1
            if attempts < governor.MAX_ATTEMPTS:
                store.update_company(c["id"], attempts=attempts, sleep_until=time.time() + RETRY_SOON_S)
                store.log("OBSERVE", f"{c['name']}: no public job board at '{slug}' on Ashby/Greenhouse/Lever (attempt {attempts}/{governor.MAX_ATTEMPTS})", c["id"])
                return None
            if c["domain"] or has_brief:
                store.update_company(c["id"], board_type="none", attempts=0)
                c["board_type"] = "none"
                source = "the website" if c["domain"] else "the founder's brief"
                store.log("OBSERVE", f"{c['name']}: no public job board after {attempts} tries → working from {source} only", c["id"])
            else:
                store.update_company(c["id"], status="asking", attempts=attempts)
                store.log("ASK", f"{c['name']}: stuck — tried '{slug}' on Ashby, Greenhouse and Lever {attempts}× and no website is known. "
                                 "Asking the founder for their job board or website.", c["id"])
                return None

    postings, errors = [], []
    if c["board_type"] != "none":
        try:
            postings = signals.fetch_postings(c["board_type"], c["board_slug"])
        except Exception as e:
            errors.append(f"job board: {e}")
    site_text = (snap or {}).get("site_text") or ""
    if c["domain"] and not site_text:
        try:
            site_text = signals.fetch_site_text(c["domain"])
        except Exception as e:
            errors.append(f"website: {type(e).__name__}")

    if errors and not postings and not site_text:
        if has_brief:  # keep the last snapshot; the brief carries on without fresh signals
            store.log("OBSERVE", f"{c['name']}: fetch failed ({'; '.join(errors)}) → continuing from the founder's brief", c["id"])
            return None
        attempts = c["attempts"] + 1
        if attempts >= governor.MAX_ATTEMPTS:
            store.update_company(c["id"], status="asking", attempts=attempts)
            store.log("ASK", f"{c['name']}: stuck — {'; '.join(errors)} after {attempts} tries. Asking the founder.", c["id"])
        else:
            store.update_company(c["id"], attempts=attempts, sleep_until=time.time() + RETRY_SOON_S)
            store.log("OBSERVE", f"{c['name']}: fetch failed ({'; '.join(errors)}), attempt {attempts}/{governor.MAX_ATTEMPTS}", c["id"])
        return None

    old = snap["postings"] if snap else []
    added, removed = signals.diff(old, postings) if snap else ([], [])
    store.save_snapshot(c["id"], postings, site_text)
    store.update_company(c["id"], last_observed=time.time(), attempts=0)
    if not snap:
        store.log("OBSERVE", f"{c['name']}: first look — {len(postings)} open postings" + (", website read" if site_text else ""), c["id"])
    elif added or removed:
        parts = []
        if added:
            parts.append(f"{len(added)} new ({', '.join(p['title'] for p in added[:3])})")
        if removed:
            parts.append(f"{len(removed)} closed ({', '.join(p['title'] for p in removed[:3])})")
        store.log("SIGNAL", f"{c['name']}: postings changed — {'; '.join(parts)}", c["id"])
    return postings, added, removed, site_text


def process(c):
    # Role source priority: (a) an active founder brief, used directly; (b) the job-board forecast only when there is none.
    briefs = store.briefs(c["id"])
    gate = governor.brief_gate(c, briefs)
    if gate:
        status, why = gate
        store.update_company(c["id"], status=status)
        store.log("STOP", f"{c['name']}: {why}", c["id"])
        return
    active = [b for b in briefs if b["status"] == "active"]
    obs = observe(c, bool(active))
    if obs is None:
        if not active:
            return
        snap = store.snapshot(c["id"])
        obs = (snap["postings"], [], [], snap["site_text"] or "") if snap else ([], [], [], "")
    postings, added, removed, site_text = obs
    c = store.company(c["id"])
    if active:
        process_briefs(c, active, postings, added, site_text)
        return

    # Forecast only when the inputs changed (input hashing: never pay twice for the same question).
    f_inputs = {"postings": sorted(p["title"] for p in postings), "site": site_text[:2000], "note": c["judge_note"] or ""}
    f_hash = _hash(f_inputs)
    fc = store.latest_forecast(c["id"])
    if f_hash != c["forecast_hash"] or fc is None:
        ok, why = governor.can_spend(c)
        if not ok:
            store.log("PAUSE", f"{c['name']}: budget — {why}. Pausing this company until budget frees up.", c["id"])
            _sleep(c, 10, None)
            return
        if not llm.available():
            store.log("HOLD", f"{c['name']}: no LLM key configured — holding instead of guessing", c["id"])
            _sleep(c, 2, None)
            return
        try:
            data, usd = llm.forecast(c, postings, added, removed, site_text)
        except llm.LLMError as e:
            store.log("ERROR", f"{c['name']}: forecast failed ({e}) → holding, will retry", c["id"])
            _sleep(c, 2, None)
            return
        if not data["forecasts"]:
            store.update_company(c["id"], forecast_hash=f_hash)
            store.log("HOLD", f"{c['name']}: no forecast possible from current signals (${usd:.3f})", c["id"])
            minutes, idx = governor.stagnation_sleep(c)
            _sleep(c, minutes, "no forecast possible yet", idx)
            return
        top = data["forecasts"][0]
        store.ex("INSERT INTO forecasts(company_id, ts, top_role, confidence, data_json) VALUES(?,?,?,?,?)",
                 (c["id"], time.time(), top["role"], float(top["confidence"]), json.dumps(data)))
        store.update_company(c["id"], forecast_hash=f_hash, one_liner=data["company_one_liner"][:200])
        ev = "; ".join(top["evidence"][:2])
        store.log("FORECAST", f"{c['name']}: next hire → {top['role']} ({float(top['confidence']):.2f}) — {ev} (${usd:.3f})", c["id"])
        fc = store.latest_forecast(c["id"])
        if governor.flip_flop(c["id"]):
            store.log("LOOP", f"{c['name']}: forecast keeps flipping between two roles → holding and asking the founder which it is", c["id"])
            store.update_company(c["id"], status="asking")
            return

    top = fc["data"]["forecasts"][0]
    conf = float(top["confidence"])
    if conf < governor.FORECAST_MIN_CONF and not c["judge_note"]:
        if f_hash != c["match_hash"]:  # log the hold once per forecast, not every cycle
            store.update_company(c["id"], match_hash=f_hash)
            store.log("HOLD", f"{c['name']}: forecast confidence {conf:.2f} < {governor.FORECAST_MIN_CONF:.2f} — not matching yet. "
                              f"Would change my mind: {fc['data']['what_would_raise_confidence']}", c["id"])
        minutes, idx = governor.stagnation_sleep(c)
        _sleep(c, minutes, "waiting for a stronger signal", idx)
        return

    # Match only when the forecast or the pool changed.
    pool = store.q("SELECT * FROM candidates WHERE status='active' ORDER BY id")
    if not pool:
        _sleep(c, 1, None)
        return
    m_hash = _hash({"forecast": fc["id"], "pool": [p["id"] for p in pool]})
    if m_hash == c["match_hash"]:
        minutes, idx = governor.stagnation_sleep(c)
        _sleep(c, minutes, "no new signals or candidates", idx)
        return
    wait = governor.MATCH_MIN_INTERVAL_S - (time.time() - c["last_match_at"])
    if wait > 0:
        store.update_company(c["id"], sleep_until=time.time() + wait)
        return
    ok, why = governor.can_spend(c)
    if not ok:
        store.log("PAUSE", f"{c['name']}: budget — {why}. Pausing matching until budget frees up.", c["id"])
        _sleep(c, 10, None)
        return
    spec = fc["data"]["top_role_spec"]
    try:
        data, usd = llm.match(c, spec, pool)
    except llm.LLMError as e:
        store.log("ERROR", f"{c['name']}: match failed ({e}) → holding, will retry", c["id"])
        _sleep(c, 2, None)
        return
    store.update_company(c["id"], match_hash=m_hash, last_match_at=time.time(), backoff_idx=0)
    decide(c, spec, data["matches"], pool, usd)
    _sleep(c, governor.ACTIVE_SLEEP_MIN, None)


def decide(c, spec, matches, pool, usd, brief_id=None):
    by_id = {p["id"]: p for p in pool}
    role = spec["role"]
    proposed, best_held = 0, None
    for m in matches:
        cand = by_id.get(m["candidate_id"])
        if not cand:
            continue
        score = float(m["score"])
        verdict, why = governor.check_intro(c, cand, score, role)
        if verdict == "PROPOSE" and m.get("uncited"):
            verdict, why = "HOLD", f"{cand['first_name']} ({score:.2f}) — match reason didn't cite any part of the brief"
        if verdict == "PROPOSE" and brief_id:
            live, stop_why = governor.brief_live(brief_id)
            if not live:
                store.log("STOP", f"{c['name']}: {stop_why} → not proposing", c["id"])
                return
        if verdict == "PROPOSE" and proposed < governor.MAX_PROPOSALS_PER_RUN:
            now = time.time()
            store.ex("INSERT INTO intros(company_id, candidate_id, role, score, reason, status, created_at, updated_at, brief_id) VALUES(?,?,?,?,?,?,?,?,?)",
                     (c["id"], cand["id"], role, score, m["reason"], "proposed", now, now, brief_id))
            store.ex("DELETE FROM holds WHERE company_id=? AND candidate_id=?", (c["id"], cand["id"]))
            store.log("PROPOSE", f"{c['name']}: intro {why} — {m['reason']}", c["id"])
            proposed += 1
        elif verdict == "HOLD":
            store.ex("INSERT INTO holds(company_id, candidate_id, role, score, reason, ts) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(company_id, candidate_id) DO UPDATE SET role=excluded.role, score=excluded.score, reason=excluded.reason, ts=excluded.ts",
                     (c["id"], cand["id"], role, score, f"{why}; gap: {m['gap']}", time.time()))
            store.log("HOLD", f"{c['name']}: held {why} — gap: {m['gap']}", c["id"])
            if best_held is None or score > best_held[0]:
                best_held = (score, cand["first_name"], m["gap"])
        elif verdict == "LOOP":
            store.log("LOOP", f"{c['name']}: {why}", c["id"])
    if proposed == 0:
        msg = f"{c['name']}: no intro sent for {role}{' (founder brief)' if brief_id else ''} (${usd:.3f})"
        if best_held:
            msg += f" — best was {best_held[1]} at {best_held[0]:.2f} (gap: {best_held[2]})"
        store.log("MATCH", msg, c["id"])


# ---------- founder hiring briefs ----------
CITE_LABEL = {"must_have": "must-have", "nice_to_have": "nice-to-have", "description": "brief", "someone_like": "someone-like",
              "seniority": "seniority", "location": "location", "jd": "JD"}


def _brief_text(b):
    """Everything the founder wrote (plus the spec derived from it): what match reasons may cite."""
    scope = b["scope"] or {}
    parts = [b[k] or "" for k in ("description", "someone_like", "seniority", "location", "jd_text")] + b["skills"]
    parts += [qa.get("a", "") for qa in b["answers"] or []]
    parts += scope.get("must_haves", []) + scope.get("nice_to_haves", []) + [scope.get(k, "") for k in ("role", "one_liner", "seniority", "location")]
    return " | ".join(parts)


def process_briefs(c, briefs, postings, added, site_text):
    pool = store.q("SELECT * FROM candidates WHERE status='active' ORDER BY id")
    retry_s = None
    for b in briefs:
        ok, why = governor.brief_live(b["id"])
        if not ok:
            store.log("STOP", f"{c['name']}: {why} → skipping, no spend", c["id"])
            continue
        b, hold_min = scope_brief(c, b, postings, site_text)
        if hold_min:
            _sleep(c, hold_min, None)
            return
        # Deterministic asks, raised once each (hashed).
        for ask in (governor.brief_mismatch(b, governor.new_postings_since(b, postings, added)), governor.brief_stale(b)):
            if ask:
                governor.ask_founder(c, b, *ask)
        wait = match_brief(c, b, pool)
        if wait:
            retry_s = wait if retry_s is None else min(retry_s, wait)
    minutes, why = governor.cadence(briefs)
    if retry_s is not None and retry_s < minutes * 60:
        store.update_company(c["id"], sleep_until=time.time() + retry_s)
        return
    _sleep(c, minutes, why)


def scope_brief(c, b, postings, site_text):
    """Turn the brief into a role spec when it (or the founder's answers) changed. Returns (brief, hold_minutes)."""
    answers = [qa for qa in b["answers"] or [] if qa.get("a")]
    s_hash = _hash({**{k: b[k] for k in store.BRIEF_FIELDS}, "answers": answers})
    if b["scope"] and b["scope_hash"] == s_hash:
        return b, None
    ok, why = governor.can_spend(c)
    if not ok:
        store.log("PAUSE", f"{c['name']}: budget — {why}. Pausing brief scoping until budget frees up.", c["id"])
        return b, 10
    if not llm.available():
        store.log("HOLD", f"{c['name']}: no LLM key configured — holding the brief instead of guessing", c["id"])
        return b, 2
    try:
        data, usd = llm.scope_from_brief({**b, "company_name": c["name"], "domain": c["domain"]}, site_text, postings)
    except llm.LLMError as e:
        store.log("ERROR", f"{c['name']}: scoping the brief failed ({e}) → holding, will retry", c["id"])
        return b, 2

    # Never invent requirements: drop anything not traceable to the founder's words (dealbreakers) or their public site/postings.
    founder_text = " ".join([b[k] or "" for k in ("description", "someone_like", "dealbreakers", "seniority", "location", "jd_text")]
                            + b["skills"] + [qa["a"] for qa in answers])
    public_text = " ".join([site_text or ""] + [p["title"] for p in postings])
    dropped = []
    for key, source in (("must_haves", founder_text + " " + public_text), ("nice_to_haves", founder_text + " " + public_text),
                        ("dealbreakers", founder_text)):
        data[key], gone = governor.grounded(data[key], source)
        dropped += gone
    # One clarification round at most: once the founder answered (or skipped), don't ask again.
    data["clarifying_questions"] = [] if b["answers"] is not None else data["clarifying_questions"][:2]

    store.update_brief(b["id"], scope_json=json.dumps(data), scope_hash=s_hash, scoped_at=time.time(), match_hash=None)
    re_ = "re-scoped" if b["scope"] else "scoped"
    store.log("SCOPE", f"{c['name']}: founder brief {re_} → {data['role']} — {data['one_liner']} · must-haves: {', '.join(data['must_haves'][:4]) or 'none'}"
                       f" · dealbreakers (hard filter): {', '.join(data['dealbreakers'][:3]) or 'none'} (${usd:.3f})", c["id"])
    if dropped:
        store.log("SCOPE", f"{c['name']}: dropped {len(dropped)} requirement(s) the founder never wrote: {'; '.join(dropped[:3])}", c["id"])
    if data["clarifying_questions"]:
        store.log("ASK_FOUNDER", f"{c['name']}: {len(data['clarifying_questions'])} clarifying question(s) on the brief page — "
                                 f"\"{data['clarifying_questions'][0]}\" (matching on the current scope meanwhile)", c["id"])
    return store.brief(b["id"]), None


def match_brief(c, b, pool):
    """Match one scoped brief against the pool. Returns seconds to wait before retrying, or None."""
    if not pool or not b["scope"]:
        return None
    m_hash = _hash({"scope": b["scope_hash"], "pool": [p["id"] for p in pool]})
    if m_hash == b["match_hash"]:
        return None
    wait = governor.MATCH_MIN_INTERVAL_S - (time.time() - (b["last_match_at"] or 0))
    if wait > 0:
        return wait
    ok, why = governor.can_spend(c)
    if not ok:
        store.log("PAUSE", f"{c['name']}: budget — {why}. Pausing brief matching until budget frees up.", c["id"])
        return 600
    ok, why = governor.brief_live(b["id"])
    if not ok:
        store.log("STOP", f"{c['name']}: {why} → not matching", c["id"])
        return None
    try:
        data, usd = llm.match_brief(c, b, pool)
    except llm.LLMError as e:
        store.log("ERROR", f"{c['name']}: brief match failed ({e}) → holding, will retry", c["id"])
        return 120
    now = time.time()
    store.update_brief(b["id"], match_hash=m_hash, last_match_at=now)
    store.update_company(c["id"], last_match_at=now, backoff_idx=0)

    # Dealbreakers are hard filters: anyone flagged is removed outright, whatever their score.
    by_id = {p["id"]: p for p in pool}
    # An exclusion must name one of the founder's own dealbreakers, so the model can't invent a filter.
    dealbreakers = " ".join(b["scope"]["dealbreakers"] + [b["dealbreakers"] or ""])
    excluded = {x["candidate_id"]: x for x in data["excluded"]
                if x["candidate_id"] in by_id and b["scope"]["dealbreakers"] and governor.grounded([x["dealbreaker"]], dealbreakers)[0]}
    if excluded:
        names = [f"{by_id[i]['first_name']} (\u201c{x['dealbreaker']}\u201d: {x['evidence']})" for i, x in excluded.items()]
        store.log("EXCLUDE", f"{c['name']}: dealbreaker filter removed {len(names)} — " + "; ".join(names[:4])
                             + (f" +{len(names) - 4} more" if len(names) > 4 else ""), c["id"])
        for i in excluded:
            store.ex("DELETE FROM holds WHERE company_id=? AND candidate_id=?", (c["id"], i))
    matches = [m for m in data["matches"] if m["candidate_id"] not in excluded]

    # Each reason must cite the part of the brief it satisfies; uncited matches are held, never proposed.
    text = _brief_text(b)
    for m in matches:
        cites = governor.valid_cites(m["cites"], text)
        if cites:
            m["reason"] = f"{m['reason']} — satisfies " + "; ".join(f"{CITE_LABEL.get(x['brief_part'], x['brief_part'])} \u201c{x['brief_text']}\u201d" for x in cites[:3])
        else:
            m["uncited"] = True
    decide(c, b["scope"], matches, pool, usd, brief_id=b["id"])
    return None
