"""Bridge: FastAPI routes plus the background agent worker. Run: uvicorn app:app --host 0.0.0.0 --port 8000"""
import base64
import importlib
import io
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
import pypdf

import agent
import governor
import llm
import signals
import store

TICK_S = 10
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SEED_COMPANIES = [  # public company info only
    ("Resend", "resend.com", "ashby", "resend", "seed"),
    ("Browserbase", "browserbase.com", "ashby", "browserbase", "seed"),
    ("PostHog", "posthog.com", "ashby", "posthog", "seed"),
    ("Linear", "linear.app", "ashby", "linear", "seed"),
    ("Clerk", "clerk.com", "ashby", "clerk", "seed"),
    # Judges' companies: names only until the founder gives us a board or website.
    ("Agentin", None, None, None, "judge"),
    ("Clean", None, None, None, "judge"),
    ("Conduit", None, None, None, "judge"),
    ("Corgi", None, None, None, "judge"),
    ("Meadow", None, None, None, "judge"),
    ("Gutgutgoose", None, None, None, "judge"),
    ("Latted", None, None, None, "judge"),
]

SEED_CANDIDATES = [  # clearly labeled sample profiles so matches exist before the room joins
    ("Priya", "Built retrieval and search infra at a Series B; Python, Postgres, vector DBs", "founding ML / infra engineer", "senior", "SF / remote"),
    ("Marco", "Developer advocate; wrote SDK docs and ran 40+ workshops; TypeScript", "devrel, developer experience", "mid", "remote"),
    ("Aisha", "Security engineer: SOC 2 from scratch, cloud IAM, pen testing", "security / platform engineer", "senior", "NYC / remote"),
    ("Tom", "First sales hire at two devtools startups; closed first $1M ARR", "founding AE, sales", "senior", "SF"),
    ("Lena", "Product designer for B2B dashboards; design systems in Figma", "product designer", "mid", "remote"),
    ("Diego", "SRE: Kubernetes, Terraform, on-call for 99.99% API", "SRE / infrastructure", "senior", "remote (US)"),
    ("Sam", "New grad, full-stack React + Go, two hackathon wins", "software engineer", "junior", "SF / NYC"),
    ("Mei", "Data engineer: dbt, Airflow, warehouse for 200-person company", "data engineer / analytics", "mid", "remote"),
    ("Jonah", "Support engineer turned solutions engineer; customer onboarding for APIs", "solutions engineer, support eng", "mid", "remote"),
    ("Fatima", "Growth marketer for PLG devtools; SEO, lifecycle email", "growth / marketing", "mid", "remote"),
    ("Kenji", "Browser automation and scraping at scale; Playwright, Chrome internals", "backend / browser infra engineer", "senior", "remote"),
    ("Rosa", "Recruiter for early-stage engineering teams (0 to 30 hires)", "talent / recruiting", "senior", "SF"),
    ("Ben", "Email deliverability engineer; SMTP, DKIM/SPF, high-volume sending", "backend / deliverability engineer", "senior", "remote"),
    ("Hana", "Frontend engineer focused on auth UX and React component libraries", "frontend engineer", "mid", "remote"),
    ("Omar", "Engineering manager, grew analytics team from 3 to 12", "engineering manager", "senior", "remote (EU)"),
]


def seed():
    if store.one("SELECT id FROM companies LIMIT 1") is None:
        for name, domain, board_type, slug, by in SEED_COMPANIES:
            store.ex("INSERT INTO companies(name, domain, board_type, board_slug, added_by, created_at) VALUES(?,?,?,?,?,?)",
                     (name, domain, board_type, slug, by, time.time()))
    if store.one("SELECT id FROM candidates LIMIT 1") is None:
        for first, strengths, roles, seniority, location in SEED_CANDIDATES:
            store.ex("INSERT INTO candidates(token, first_name, strengths, roles, seniority, location, availability, contact, seeded, created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,1,?)",
                     (secrets.token_urlsafe(12), first, strengths, roles, seniority, location, "open to talk",
                      f"{first.lower()}@example.com (sample)", time.time()))


_stop = threading.Event()


def worker():
    if store.get_meta("agent_started_at") is None:
        store.set_meta("agent_started_at", time.time())
        store.log("SYSTEM", "Agent started — running unsupervised")
    else:
        store.log("SYSTEM", "Server restarted — agent resumed (uptime counted from first start)")
    while not _stop.is_set():
        try:
            for m in (signals, llm, governor, agent):  # hot-reload logic without restarting the process
                importlib.reload(m)
            agent.cycle()
        except Exception as e:
            store.log("ERROR", f"cycle failed: {type(e).__name__}: {str(e)[:160]}")
        _stop.wait(TICK_S)


@asynccontextmanager
async def lifespan(_app):
    seed()
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    yield
    _stop.set()


app = FastAPI(lifespan=lifespan)


# ---------- pages ----------
@app.get("/")
def root():
    return RedirectResponse("/console")


@app.get("/console")
def console_page():
    return FileResponse(os.path.join(STATIC, "console.html"))


@app.get("/join")
def join_page():
    return FileResponse(os.path.join(STATIC, "join.html"))


@app.get("/inbox")
def inbox_page():
    return FileResponse(os.path.join(STATIC, "inbox.html"))


# ---------- candidate ----------
def _clean(v, n=300):
    return (str(v or "")).strip()[:n]


def _url(v):
    """Only http(s) links ever reach an href."""
    if not v:
        return ""
    return v if v.lower().startswith(("http://", "https://")) else "https://" + v


UPLOADS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
MAX_RESUME_BYTES = 5 * 1024 * 1024


def _save_resume(token, b64):
    """Optional PDF resume: keep the file (shared only after an accepted intro) and its text (used for matching)."""
    if not b64:
        return None, None
    try:
        data = base64.b64decode(b64.split(",")[-1], validate=True)
    except Exception:
        raise HTTPException(400, "Couldn't read the resume file")
    if len(data) > MAX_RESUME_BYTES:
        raise HTTPException(400, "Resume must be under 5 MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Resume must be a PDF")
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        text = " ".join((pg.extract_text() or "") for pg in reader.pages[:4])
        text = " ".join(text.split())[:3000]
    except Exception:
        text = ""
    os.makedirs(UPLOADS, exist_ok=True)
    name = f"{token}.pdf"
    with open(os.path.join(UPLOADS, name), "wb") as f:
        f.write(data)
    return text, name


@app.post("/api/join")
def join(p: dict = Body(...)):
    if not p.get("consent"):
        raise HTTPException(400, "Consent is required")
    first, last, strengths = _clean(p.get("first_name"), 40), _clean(p.get("last_name"), 40), _clean(p.get("strengths"), 400)
    linkedin = _clean(p.get("linkedin"), 200)
    if not first or not last or not strengths:
        raise HTTPException(400, "First name, last name and what you're great at are required")
    if "linkedin.com/" not in linkedin.lower():
        raise HTTPException(400, "A LinkedIn profile URL is required (linkedin.com/in/...)")
    linkedin, link = _url(linkedin), _url(_clean(p.get("link"), 200))
    token = secrets.token_urlsafe(16)
    resume_text, resume_file = _save_resume(token, p.get("resume_b64"))
    store.ex("INSERT INTO candidates(token, first_name, last_name, strengths, roles, seniority, location, availability, contact, link, "
             "linkedin, resume_text, resume_file, seeded, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
             (token, first, last, strengths, _clean(p.get("roles")), _clean(p.get("seniority"), 40), _clean(p.get("location"), 80),
              _clean(p.get("availability"), 80), _clean(p.get("contact"), 120), link,
              linkedin, resume_text, resume_file, time.time()))
    woken = store.wake_active()
    store.log("SIGNAL", f"New candidate {first} joined ({strengths[:60]}) → re-checking {woken} sleeping companies")
    return {"token": token}


def _candidate(token):
    c = store.one("SELECT * FROM candidates WHERE token=?", (token or "",))
    if not c:
        raise HTTPException(404, "Unknown link")
    return c


@app.get("/api/inbox")
def inbox(t: str):
    c = _candidate(t)
    intros = store.q("SELECT i.*, co.name AS company, co.one_liner FROM intros i JOIN companies co ON co.id=i.company_id "
                     "WHERE i.candidate_id=? AND i.status IN ('requested','accepted','declined') ORDER BY i.id DESC", (c["id"],))
    for i in intros:
        if i["status"] != "accepted":
            i["founder_contact"] = None
    profile = {k: c[k] for k in ("first_name", "last_name", "strengths", "roles", "seniority", "location", "availability", "contact", "linkedin", "status")}
    profile["has_resume"] = bool(c["resume_file"])
    return {"profile": profile, "intros": intros}


@app.post("/api/inbox/respond")
def respond(p: dict = Body(...)):
    c = _candidate(p.get("t"))
    intro = store.one("SELECT * FROM intros WHERE id=? AND candidate_id=?", (p.get("intro_id"), c["id"]))
    if not intro or intro["status"] != "requested":
        raise HTTPException(400, "This intro is not waiting for you")
    status = "accepted" if p.get("accept") else "declined"
    store.ex("UPDATE intros SET status=?, updated_at=? WHERE id=?", (status, time.time(), intro["id"]))
    co = store.company(intro["company_id"])
    if status == "accepted":
        store.log("INTRO", f"{co['name']} × {c['first_name']}: both said yes → contact details shared", co["id"])
    else:
        store.log("INTRO", f"{co['name']} × {c['first_name']}: candidate declined → recorded, will never re-propose", co["id"])
    return {"ok": True}


@app.post("/api/inbox/pause")
def pause_me(p: dict = Body(...)):
    c = _candidate(p.get("t"))
    new = "paused" if c["status"] == "active" else "active"
    store.ex("UPDATE candidates SET status=? WHERE id=?", (new, c["id"]))
    store.log("STOP" if new == "paused" else "SIGNAL", f"{c['first_name']} {'paused' if new == 'paused' else 'resumed'} intros")
    return {"status": new}


@app.get("/api/intro/{intro_id}/resume")
def intro_resume(intro_id: int):
    row = store.one("SELECT i.status, ca.resume_file, ca.first_name, ca.last_name FROM intros i JOIN candidates ca ON ca.id=i.candidate_id "
                    "WHERE i.id=?", (intro_id,))
    if not row or row["status"] != "accepted" or not row["resume_file"]:
        raise HTTPException(404, "Resume is shared only after both sides accept")
    return FileResponse(os.path.join(UPLOADS, row["resume_file"]), media_type="application/pdf",
                        filename=f"{row['first_name']}-{row['last_name'] or ''}-resume.pdf")


@app.post("/api/inbox/delete")
def delete_me(p: dict = Body(...)):
    c = _candidate(p.get("t"))
    for table in ("intros", "holds"):
        store.ex(f"DELETE FROM {table} WHERE candidate_id=?", (c["id"],))
    if c["resume_file"]:
        try:
            os.remove(os.path.join(UPLOADS, c["resume_file"]))
        except OSError:
            pass
    store.ex("DELETE FROM candidates WHERE id=?", (c["id"],))
    store.log("STOP", "A candidate deleted their data")
    return {"ok": True}


# ---------- console actions ----------
def _company_or_404(cid):
    c = store.company(cid)
    if not c:
        raise HTTPException(404, "Unknown company")
    return c


@app.post("/api/intro/{intro_id}/request")
def request_intro(intro_id: int, p: dict = Body(...)):
    intro = store.one("SELECT * FROM intros WHERE id=?", (intro_id,))
    if not intro or intro["status"] != "proposed":
        raise HTTPException(400, "Intro is not in proposed state")
    contact = _clean(p.get("contact"), 120)
    if not contact:
        raise HTTPException(400, "Founder contact is required")
    store.ex("UPDATE intros SET status='requested', founder_contact=?, updated_at=? WHERE id=?", (contact, time.time(), intro_id))
    co, cand = store.company(intro["company_id"]), store.one("SELECT first_name FROM candidates WHERE id=?", (intro["candidate_id"],))
    store.log("INTRO", f"{co['name']} requested the intro to {cand['first_name']} → waiting for {cand['first_name']}'s yes", co["id"])
    return {"ok": True}


@app.post("/api/intro/{intro_id}/repropose")
def repropose(intro_id: int):
    """Demo of the loop guard: try to propose the same pair again; the governor refuses."""
    intro = store.one("SELECT * FROM intros WHERE id=?", (intro_id,))
    if not intro:
        raise HTTPException(404, "Unknown intro")
    co = store.company(intro["company_id"])
    cand = store.one("SELECT * FROM candidates WHERE id=?", (intro["candidate_id"],))
    verdict, why = governor.check_intro(co, cand, intro["score"], intro["role"])
    store.log(verdict, f"{co['name']}: re-proposal attempt — {why}", co["id"])
    return {"verdict": verdict, "reason": why}


@app.post("/api/company/{cid}/feedback")
def feedback(cid: int, p: dict = Body(...)):
    c = _company_or_404(cid)
    note = _clean(p.get("note"), 400)
    if not note:
        raise HTTPException(400, "Note is required")
    store.update_company(cid, judge_note=note, status="active", sleep_until=0, forecast_hash=None, match_hash=None, last_match_at=0)
    store.log("SIGNAL", f"{c['name']}: founder feedback — \"{note[:100]}\" → re-forecasting", cid)
    return {"ok": True}


@app.post("/api/company/{cid}/answer")
def answer(cid: int, p: dict = Body(...)):
    c = _company_or_404(cid)
    domain = _clean(p.get("domain"), 120).replace("https://", "").replace("http://", "").strip("/") or c["domain"]
    slug = _clean(p.get("slug"), 80) or None
    if not domain and not slug:
        raise HTTPException(400, "Give a website or a job board slug")
    store.update_company(cid, domain=domain, board_slug=slug, board_type=None, attempts=0, status="active",
                         sleep_until=0, last_observed=0, forecast_hash=None)
    store.ex("DELETE FROM snapshots WHERE company_id=?", (cid,))
    store.log("SIGNAL", f"{c['name']}: founder answered — " + ", ".join(x for x in (domain and f"website {domain}", slug and f"board '{slug}'") if x) + " → resuming", cid)
    return {"ok": True}


@app.post("/api/company/{cid}/run")
def run_now(cid: int):
    c = _company_or_404(cid)
    if c["status"] != "active":
        raise HTTPException(400, f"Company is {c['status']}")
    store.update_company(cid, sleep_until=0, match_hash=None, last_match_at=0)
    store.log("SIGNAL", f"{c['name']}: manual check requested → running next cycle", cid)
    return {"ok": True}


@app.post("/api/company/{cid}/filled")
def filled(cid: int):
    c = _company_or_404(cid)
    store.update_company(cid, status="stopped")
    store.log("STOP", f"{c['name']}: founder marked the role filled → stopped watching, no more spend", cid)
    return {"ok": True}


@app.post("/api/company/{cid}/squeeze")
def squeeze(cid: int):
    c = _company_or_404(cid)
    new = None if c["budget_hour"] is not None else 0.01
    store.update_company(cid, budget_hour=new)
    cap = new if new is not None else governor.BUDGET_COMPANY_HOUR
    store.log("BUDGET", f"{c['name']}: hourly budget set to ${cap:.2f}", cid)
    return {"budget_hour": cap}


@app.post("/api/company")
def add_company(p: dict = Body(...)):
    name = _clean(p.get("name"), 60)
    if not name:
        raise HTTPException(400, "Name is required")
    domain = _clean(p.get("domain"), 120).replace("https://", "").replace("http://", "").strip("/") or None
    cid = store.ex("INSERT INTO companies(name, domain, board_slug, added_by, created_at) VALUES(?,?,?,?,?)",
                   (name, domain, _clean(p.get("slug"), 80) or None, "founder", time.time()))
    store.log("SIGNAL", f"{name}: added to the watchlist", cid)
    return {"id": cid}


# ---------- state for the console ----------
@app.get("/api/state")
def state():
    now = time.time()
    started = float(store.get_meta("agent_started_at", now))
    names = {c["id"]: c["name"] for c in store.q("SELECT id, name FROM companies")}
    companies = []
    for c in store.q("SELECT * FROM companies ORDER BY CASE added_by WHEN 'judge' THEN 0 WHEN 'founder' THEN 0 ELSE 1 END, id"):
        fc = store.latest_forecast(c["id"])
        snap = store.snapshot(c["id"])
        intros = store.q("SELECT i.id, i.role, i.score, i.reason, i.status, i.founder_contact, ca.first_name, "
                         "CASE WHEN i.status='accepted' THEN ca.contact END AS candidate_contact, "
                         "CASE WHEN i.status='accepted' THEN ca.last_name END AS last_name, "
                         "CASE WHEN i.status='accepted' THEN ca.linkedin END AS linkedin, "
                         "CASE WHEN i.status='accepted' THEN ca.link END AS link, "
                         "CASE WHEN i.status='accepted' AND ca.resume_file IS NOT NULL THEN 1 ELSE 0 END AS has_resume "
                         "FROM intros i JOIN candidates ca ON ca.id=i.candidate_id WHERE i.company_id=? ORDER BY i.id DESC", (c["id"],))
        companies.append({
            "id": c["id"], "name": c["name"], "domain": c["domain"], "added_by": c["added_by"], "status": c["status"],
            "board": f"{c['board_type']}:{c['board_slug']}" if c["board_type"] and c["board_type"] != "none" else None,
            "postings": len(snap["postings"]) if snap else None,
            "sleeping_s": max(0, int(c["sleep_until"] - now)), "one_liner": c["one_liner"], "judge_note": c["judge_note"],
            "forecast": fc["data"] if fc else None, "forecast_at": fc["ts"] if fc else None,
            "intros": intros, "held": store.one("SELECT COUNT(*) AS n FROM holds WHERE company_id=?", (c["id"],))["n"],
            "spend_hour": round(store.spend_total(now - 3600, c["id"]), 4), "spend_total": round(store.spend_total(0, c["id"]), 4),
            "budget_hour": governor.company_budget(c),
        })
    decisions = store.q("SELECT * FROM decisions ORDER BY id DESC LIMIT 120")
    for d in decisions:
        d["company"] = names.get(d["company_id"])
    pool = store.q("SELECT first_name, strengths, seeded, created_at FROM candidates WHERE status='active' ORDER BY id DESC LIMIT 60")
    spend_rows = store.q("SELECT ts, usd FROM spend ORDER BY ts")
    cum, series = 0.0, []
    for r in spend_rows:
        cum += r["usd"]
        series.append([r["ts"], round(cum, 4)])
    return {
        "now": now, "uptime_s": int(now - started), "llm": llm.available(),
        "counts": {
            "companies": len(companies),
            "watching": sum(1 for c in companies if c["status"] == "active"),
            "candidates": store.one("SELECT COUNT(*) AS n FROM candidates WHERE status='active'")["n"],
            "joined": store.one("SELECT COUNT(*) AS n FROM candidates WHERE seeded=0")["n"],
            "proposed": store.one("SELECT COUNT(*) AS n FROM intros")["n"],
            "held": store.one("SELECT COUNT(*) AS n FROM holds")["n"],
            "accepted": store.one("SELECT COUNT(*) AS n FROM intros WHERE status='accepted'")["n"],
            "decisions": store.one("SELECT COUNT(*) AS n FROM decisions")["n"],
        },
        "spend": {"total": round(cum, 4), "day_cap": governor.BUDGET_DAY, "series": series},
        "companies": companies, "decisions": decisions, "pool": pool,
    }
