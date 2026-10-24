"""Public company signals: job boards (Greenhouse, Lever, Ashby) and homepage text. Company pages only."""
import html
import re

import requests

UA = {"User-Agent": "Bridge-hackathon-agent/0.1 (public job boards only)"}
TIMEOUT = 10

BOARD_URLS = {
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
}


class SignalError(Exception):
    pass


def _get_json(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    if r.status_code != 200:
        raise SignalError(f"HTTP {r.status_code}")
    return r.json()


def fetch_postings(board_type, slug):
    """Return a normalized list of {id, title, team, location, published} for one board."""
    if board_type not in BOARD_URLS:
        raise SignalError(f"unknown board type {board_type}")
    data = _get_json(BOARD_URLS[board_type].format(slug=slug))
    out = []
    if board_type == "ashby":
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            out.append({"id": str(j.get("id")), "title": j.get("title", ""), "team": j.get("department") or j.get("team") or "",
                        "location": j.get("location") or "", "published": (j.get("publishedAt") or "")[:10]})
    elif board_type == "greenhouse":
        for j in data.get("jobs", []):
            out.append({"id": str(j.get("id")), "title": j.get("title", ""), "team": "",
                        "location": (j.get("location") or {}).get("name", ""),
                        "published": (j.get("first_published") or j.get("updated_at") or "")[:10]})
    elif board_type == "lever":
        if not isinstance(data, list):
            raise SignalError("unexpected lever payload")
        for j in data:
            cats = j.get("categories") or {}
            out.append({"id": str(j.get("id")), "title": j.get("text", ""), "team": cats.get("team") or cats.get("department") or "",
                        "location": cats.get("location") or "", "published": ""})
    return out


def detect_board(slug):
    """Try each board provider for this slug. Returns (board_type, postings) or (None, None)."""
    for board_type in BOARD_URLS:
        try:
            postings = fetch_postings(board_type, slug)
        except (SignalError, requests.RequestException, ValueError):
            continue
        if postings:
            return board_type, postings
    return None, None


def fetch_site_text(domain, limit=2000):
    if not domain:
        return ""
    url = domain if domain.startswith("http") else f"https://{domain}"
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", r.text)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    meta = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', text)
    body = re.sub(r"(?s)<[^>]+>", " ", text)
    parts = [title.group(1) if title else "", meta.group(1) if meta else "", body]
    clean = html.unescape(" ".join(parts))
    return re.sub(r"\s+", " ", clean).strip()[:limit]


def diff(old, new):
    old_ids = {p["id"] for p in old}
    new_ids = {p["id"] for p in new}
    added = [p for p in new if p["id"] not in old_ids]
    removed = [p for p in old if p["id"] not in new_ids]
    return added, removed


def slugify(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())
