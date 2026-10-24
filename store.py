"""SQLite storage. One shared connection guarded by a lock (agent thread + API threads)."""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("BRIDGE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.db"))

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, domain TEXT, board_type TEXT, board_slug TEXT,
  added_by TEXT DEFAULT 'seed', status TEXT DEFAULT 'active', sleep_until REAL DEFAULT 0,
  backoff_idx INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, forecast_hash TEXT, match_hash TEXT,
  last_observed REAL DEFAULT 0, last_match_at REAL DEFAULT 0, budget_hour REAL,
  judge_note TEXT, one_liner TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS snapshots (
  company_id INTEGER PRIMARY KEY, ts REAL, postings_json TEXT, site_text TEXT);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY, token TEXT UNIQUE, first_name TEXT, strengths TEXT, roles TEXT,
  seniority TEXT, location TEXT, availability TEXT, contact TEXT, link TEXT,
  seeded INTEGER DEFAULT 0, status TEXT DEFAULT 'active', created_at REAL);
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY, company_id INTEGER, ts REAL, top_role TEXT, confidence REAL, data_json TEXT);
CREATE TABLE IF NOT EXISTS intros (
  id INTEGER PRIMARY KEY, company_id INTEGER, candidate_id INTEGER, role TEXT, score REAL, reason TEXT,
  status TEXT, founder_contact TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS holds (
  company_id INTEGER, candidate_id INTEGER, role TEXT, score REAL, reason TEXT, ts REAL,
  PRIMARY KEY (company_id, candidate_id));
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, ts REAL, company_id INTEGER, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS spend (
  id INTEGER PRIMARY KEY, ts REAL, company_id INTEGER, model TEXT, purpose TEXT,
  input_tokens INTEGER, output_tokens INTEGER, usd REAL);
"""

with _lock:
    _conn.executescript(SCHEMA)
    for col in ("last_name", "linkedin", "resume_text", "resume_file"):  # added after launch; migrate in place
        try:
            _conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    _conn.commit()


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


def get_meta(key, default=None):
    row = one("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else default


def set_meta(key, value):
    ex("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def log(kind, message, company_id=None):
    ex("INSERT INTO decisions(ts, company_id, kind, message) VALUES(?, ?, ?, ?)", (time.time(), company_id, kind, message))
    print(f"{time.strftime('%H:%M:%S')} [{kind}] {message}", flush=True)


def update_company(company_id, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE companies SET {cols} WHERE id=?", (*fields.values(), company_id))


def company(company_id):
    return one("SELECT * FROM companies WHERE id=?", (company_id,))


def spend_total(since=0.0, company_id=None):
    if company_id is None:
        row = one("SELECT COALESCE(SUM(usd), 0) AS s FROM spend WHERE ts>=?", (since,))
    else:
        row = one("SELECT COALESCE(SUM(usd), 0) AS s FROM spend WHERE ts>=? AND company_id=?", (since, company_id))
    return row["s"]


def snapshot(company_id):
    row = one("SELECT * FROM snapshots WHERE company_id=?", (company_id,))
    if row:
        row["postings"] = json.loads(row["postings_json"] or "[]")
    return row


def save_snapshot(company_id, postings, site_text):
    ex("INSERT INTO snapshots(company_id, ts, postings_json, site_text) VALUES(?, ?, ?, ?) "
       "ON CONFLICT(company_id) DO UPDATE SET ts=excluded.ts, postings_json=excluded.postings_json, site_text=excluded.site_text",
       (company_id, time.time(), json.dumps(postings), site_text))


def latest_forecast(company_id):
    row = one("SELECT * FROM forecasts WHERE company_id=? ORDER BY id DESC LIMIT 1", (company_id,))
    if row:
        row["data"] = json.loads(row["data_json"])
    return row


def wake_active():
    """Wake every active company so it re-checks on the next cycle. Returns how many were woken."""
    rows = q("SELECT id FROM companies WHERE status='active' AND sleep_until>?", (time.time(),))
    for r in rows:
        update_company(r["id"], sleep_until=0)
    return len(rows)
