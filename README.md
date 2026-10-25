# Bridge

**An unsupervised agent that predicts a startup's next hire from public signals and makes intros only when both sides say yes.**

Built at c0mpiled-16 @ UCSD (Oct 24, 2026) for the **Unsupervised** track.

## The problem
A 6-person startup needs hire #7. The founder has no recruiter and no time to write a job description, post it, and sort through 400 cold applicants. Hiring is the slowest thing on their plate, and a late or wrong hire costs months.

## What Bridge does
1. **Watches** each company's public job board (Ashby, Greenhouse, Lever) and website.
2. **Forecasts the next hire** with evidence and a confidence score, often before the job is posted.
   Example: *"5 APAC roles posted in two weeks → Head of APAC is next."*
3. **Researches on its own when unsure.** Below the confidence bar, it searches recent company news (funding, launches, new markets) and re-forecasts. No founder needs to be online.
4. **Matches an opt-in talent pool.** People join in 30 seconds by QR code and can add a resume, LinkedIn, GitHub and school. With their consent, the agent reads the GitHub and LinkedIn profiles they linked (LinkedIn through Bright Data) and builds one profile from everything: skills, internships and projects. Founders filter on it the way they would on a job board.
5. **Proposes intros only when confident.** Everyone else is held back, and the agent says why.
6. **Double opt-in.** The founder swipes right, then the candidate accepts. Only then are contact details, LinkedIn and resume shared.

Founders can also post a **hiring brief** in 60 seconds: who they need, dealbreakers and urgency. The agent scopes it into a role spec and treats dealbreakers as hard filters. Every match reason quotes the part of the brief it satisfies, and requirements the founder never wrote are dropped.

## Live candidate data: LinkedIn and GitHub
Every candidate profile is built from live data, not only what someone typed into a form.

- **Live LinkedIn data (Bright Data):** when a candidate joins with their LinkedIn URL, the agent pulls their live profile through Bright Data's scrape API (`POST /datasets/v3/scrape`, LinkedIn profiles dataset, about 45 s per profile). That gives current position, work history, internships, education and certifications. It runs in a background thread, so joining stays instant.
- **GitHub profiles (GitHub REST API):** when a candidate links their GitHub, the agent reads their public profile and repositories: languages actually used, top repos by stars, and recent activity. The matcher treats this as evidence of real work.
- **One profile from all sources:** Claude combines signup answers, resume, LinkedIn and GitHub into one structured profile: headline, skills, specializations, internships and projects. The profile:
  - is used in matching;
  - shows on founders' swipe cards ("GitHub (verified)", "Internships", "Projects");
  - drives the review filters (skills, roles, seniority, school, has GitHub, has resume);
  - appears in the candidate's own inbox, so they can see what founders see.
- **Matching against companies:** each company's needs come from its live job board, its careers, about and values pages ("what they look for"), the agent's own web research, and any founder brief. Both sides are compared in one scored match, with a reason and the gap for every candidate.

Candidate data is read only for people who joined and linked the profiles themselves; the consent checkbox at signup says so.

## Why it's unsupervised
The LLM never decides whether to act. A deterministic **governor** does:

| Situation | What the governor does |
|---|---|
| Nothing changed | Skips the model call (input hashing) and sleeps with backoff: 5 → 10 → 20 min |
| Low confidence (< 0.65) | Holds, logs what would change its mind, researches at most once every 3 hours |
| Budget | Caps spend per company per hour and per day; pauses when hit |
| Loops | Never re-proposes a pair that was already accepted, declined or passed; flags forecasts that flip-flop |
| Stuck | Tries 3 times, then asks the founder instead of guessing |
| Candidate fatigue | At most 2 open intros per person, and one per hour |
| Filled or paused | Stops spending immediately |

Every decision appears in a live activity log. In its first 1.5 hours the agent made 246 decisions on 40 model calls, for **$0.79 total**.

## Does the forecast work? A backtest
For 23 real companies (Anthropic, OpenAI, Stripe, Figma, Notion, Datadog, Linear and others), we hid the last 21 days of job postings, forecast from the older ones only, and checked whether the specific predicted role was posted afterwards. Code is in `backtest.py`.

| | Exact role predicted |
|---|---|
| Bridge, all forecasts | **9 / 23 (39%)** |
| Baseline: "they'll hire what they just hired" | 2 / 23 (9%) |
| Bridge at confidence ≥ 0.65 | **5 / 7 (71%)** |
| Bridge at confidence < 0.50 | 0 / 4 |

Confidence tracks accuracy, which is why the match bar is set at 0.65.

Caveats:
- 23 companies is a small sample.
- The "same role" check is also done by Claude.
- Job boards only list postings that are still open.

## Privacy rules (enforced in code)
- **Opt-in only.** Candidate LinkedIn and GitHub data is pulled only for profiles the candidate linked at signup, with consent. Nobody is contacted without joining.
- **Company data only from public company pages.** Web research is limited to company-level news, never individuals.
- **Contact details, LinkedIn and resume stay hidden** until both sides accept.
- **Candidates can pause or delete themselves at any time.** Delete also removes their resume file.
- **Third-party links and emails are stripped** from founders' "someone like…" examples.

## Next steps
- **Bright Data company signals:** LinkedIn job posts and headcount for startups with no public job board.
- **Bright Data public-profile sourcing:** an optional second pool, clearly labeled "not opted in". The agent never contacts these people; a founder who saves one gets the public LinkedIn link and reaches out themselves. The API is verified; the searches run as background jobs.
- **Partner pools:** career centers, clubs, bootcamps and hackathons share our join link or upload lists of members who opted in.
- **Learning from swipes:** use a founder's right and left swipes to re-weight their future matches.

## Run it
```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
# public link: cloudflared tunnel --url http://localhost:8000
```

Pages:
- `/console`: dashboard
- `/join`: candidates
- `/founder`: hiring briefs
- `/inbox?t=…`: each candidate's private inbox

Backtest: `BRIDGE_DB=/tmp/bt.db .venv/bin/python backtest.py`, then `backtest.py --judge`.

## Stack
- Python, FastAPI and SQLite.
- One background worker that hot-reloads the agent logic between cycles, so the agent never restarts and its uptime survives deploys.
- Plain HTML and JavaScript, with no build step.

## AI tools used
- **Claude Code (Claude Opus 5.5)** wrote most of the code with us: architecture, implementation, debugging and this README.
- **The Claude API (`claude-sonnet-5-5`)** runs inside the product for:
  - forecasting next hires;
  - turning founder briefs into role specs;
  - matching candidates;
  - web research using the server-side web search tool;
  - the backtest's role check.
- All model output goes through deterministic code (the governor, input hashing, grounding checks for requirements and quotes) before the agent acts on it.
