"""Backtest the next-hire forecast on real public job boards.

For each company: hide postings from the last HOLDOUT_DAYS, forecast from the older ones only (no website text, so
nothing from "the future" leaks in), then check the forecast against the role families actually posted afterwards.
Baseline: guess the most common role family among the visible postings.

Run: BRIDGE_DB=/tmp/backtest.db .venv/bin/python backtest.py   (separate DB so spend doesn't hit the live agent)
Caveat: boards only list postings still open, so roles that opened and closed inside the window are invisible.
"""
import collections
import concurrent.futures
import json
import sys
import time

import governor
import llm
import signals

HOLDOUT_DAYS = 21
SLUGS = ["resend", "browserbase", "posthog", "linear", "clerk", "supabase", "modal", "replit", "cursor", "perplexity",
         "mercor", "vercel", "ramp", "notion", "openai", "anthropic", "figma", "stripe", "airtable", "deel", "retool",
         "brex", "plaid", "elevenlabs", "harvey", "cohere", "runway", "scale", "datadog", "discord"]


def families(text):
    return governor.role_families(text)


def run_one(slug):
    board_type, postings = signals.detect_board(slug)
    if not board_type or board_type == "lever":  # Lever has no publish dates
        return None
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - HOLDOUT_DAYS * 86400))
    dated = [p for p in postings if p.get("published")]
    visible = [p for p in dated if p["published"] < cutoff]
    later = [p for p in dated if p["published"] >= cutoff]
    later_fams = collections.Counter(f for p in later for f in families(p["title"]))
    vis_fams = collections.Counter(f for p in visible for f in families(p["title"]))
    if len(visible) < 3 or sum(later_fams.values()) < 2:
        return None
    data, usd = llm.forecast({"name": slug.capitalize(), "id": None}, visible, [], [], "")
    preds = [(f["role"], float(f["confidence"])) for f in data["forecasts"]]
    pred_fams = [families(r) for r, _ in preds]
    baseline = vis_fams.most_common(1)[0][0] if vis_fams else None
    return {
        "company": slug, "board": board_type, "visible": len(visible), "later": len(later),
        "later_titles": [p["title"] for p in later][:6], "later_families": dict(later_fams),
        "predictions": preds,
        "hit1": bool(pred_fams and pred_fams[0] & set(later_fams)),
        "hit3": any(f & set(later_fams) for f in pred_fams[:3]),
        "baseline": baseline, "baseline_hit": baseline in later_fams, "usd": usd,
    }


def main():
    if not llm.available():
        sys.exit("No ANTHROPIC_API_KEY")
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        results = []
        for slug, r in zip(SLUGS, pool.map(lambda s: _safe(s), SLUGS)):
            if r:
                results.append(r)
    for r in results:
        mark = "HIT " if r["hit1"] else ("hit3" if r["hit3"] else "miss")
        top = r["predictions"][0] if r["predictions"] else ("(none)", 0)
        print(f"{mark}  {r['company']:<12} predicted: {top[0]} ({top[1]:.2f})")
        print(f"      actually posted in last {HOLDOUT_DAYS}d: {', '.join(r['later_titles'])}")
        print(f"      baseline guess '{r['baseline']}': {'hit' if r['baseline_hit'] else 'miss'}")
    n = len(results)
    if n:
        conf = [(r["predictions"][0][1], r["hit1"]) for r in results if r["predictions"]]
        hi = [h for c, h in conf if c >= governor.FORECAST_MIN_CONF]
        print(f"\n{n} companies · top-1 hit {sum(r['hit1'] for r in results)}/{n} · top-3 hit {sum(r['hit3'] for r in results)}/{n}"
              f" · baseline {sum(r['baseline_hit'] for r in results)}/{n}"
              f" · top-1 when confidence ≥ {governor.FORECAST_MIN_CONF:.2f}: {sum(hi)}/{len(hi)}"
              f" · cost ${sum(r['usd'] for r in results):.3f}")
    json.dump(results, open("backtest_results.json", "w"), indent=1)


JUDGE_SCHEMA = {"type": "object", "properties": {"same_role": {"type": "boolean"}, "posting": {"type": "string"}},
                "required": ["same_role", "posting"], "additionalProperties": False}
JUDGE_SYSTEM = """Decide whether a predicted hire was actually posted. same_role is true only if one of the postings is
essentially the predicted role: same job function and similar scope (a different city or region is fine; a different
function is not, e.g. Solutions Engineer vs Account Executive is false). posting: the matching title, or "" if none."""


def judge(slug_results):
    """Strict pass: did the company post the specific predicted role (not just the same broad family)?
    Baseline: the most recently posted visible title, i.e. 'they'll hire more of what they just hired'."""
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - HOLDOUT_DAYS * 86400))

    def one(r):
        _, postings = signals.detect_board(r["company"])
        dated = [p for p in postings if p.get("published")]
        later = [p["title"] for p in dated if p["published"] >= cutoff]
        recent_visible = max((p for p in dated if p["published"] < cutoff), key=lambda p: p["published"])["title"]
        out = {}
        for name, guess in (("model", r["predictions"][0][0]), ("baseline", recent_visible)):
            d, _ = llm.call_json(llm.MATCH_MODEL, JUDGE_SYSTEM, f"Predicted hire: {guess}\nPostings that appeared later:\n- " + "\n- ".join(later),
                                 JUDGE_SCHEMA, "backtest-judge", effort="low", max_tokens=500)
            out[name] = (guess, d["same_role"], d["posting"])
        return r["company"], r["predictions"][0][1], out

    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        rows = list(pool.map(one, slug_results))
    for company, conf, out in rows:
        m, b = out["model"], out["baseline"]
        print(f"{'HIT ' if m[1] else 'miss'} {company:<12} ({conf:.2f}) {m[0][:70]}" + (f"  → {m[2]}" if m[1] else ""))
        print(f"     baseline {'HIT ' if b[1] else 'miss'} {b[0][:70]}" + (f"  → {b[2]}" if b[1] else ""))
    n = len(rows)
    hi = [out["model"][1] for _, conf, out in rows if conf >= governor.FORECAST_MIN_CONF]
    print(f"\nSTRICT · model {sum(o['model'][1] for _, _, o in rows)}/{n} · baseline {sum(o['baseline'][1] for _, _, o in rows)}/{n}"
          f" · model when confidence ≥ {governor.FORECAST_MIN_CONF:.2f}: {sum(hi)}/{len(hi)}")


def _safe(slug):
    try:
        return run_one(slug)
    except Exception as e:
        print(f"skip {slug}: {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
        return None


if __name__ == "__main__":
    if "--judge" in sys.argv:  # strict pass over the saved forecasts
        judge(json.load(open("backtest_results.json")))
    else:
        main()
