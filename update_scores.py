#!/usr/bin/env python3
"""
LAF WC2026 — live score updater
Fetches World Cup scores from football-data.org and writes them to the
Firebase Realtime Database node `dailyResults/{matchId}` used by lafwc2026.fr.

matchId format (must match dailyMatchId() in the site):
    {date}_{home_iso}_{away_iso}   e.g. 2026-06-11_mx_za
    (date/time in the site's fixtures are CET-based)

Run via GitHub Actions cron (see .github/workflows/scores.yml).
Required env var: FOOTBALL_DATA_TOKEN  (free key from https://www.football-data.org/client/register)
Optional env var: FIREBASE_URL (defaults to the LAF database)
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIREBASE_URL = os.environ.get(
    "FIREBASE_URL", "https://laf-wc2026-default-rtdb.firebaseio.com"
).rstrip("/")
API_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "")
API_BASE = "https://api.football-data.org/v4"

# ── Site team names (FIXTURES_DATA homeN/awayN) → ISO codes used in match IDs ──
NAME_TO_ISO = {
    "Algeria": "dz", "Argentina": "ar", "Australia": "au", "Austria": "at",
    "Belgium": "be", "Bosnia-Herzegovina": "ba", "Brazil": "br", "Canada": "ca",
    "Cape Verde": "cv", "Colombia": "co", "Congo DR": "cd", "Croatia": "hr",
    "Curacao": "cw", "Czechia": "cz", "Ecuador": "ec", "Egypt": "eg",
    "England": "gb-eng", "France": "fr", "Germany": "de", "Ghana": "gh",
    "Haiti": "ht", "Iran": "ir", "Iraq": "iq", "Ivory Coast": "ci",
    "Japan": "jp", "Jordan": "jo", "Mexico": "mx", "Morocco": "ma",
    "Netherlands": "nl", "New Zealand": "nz", "Norway": "no", "Panama": "pa",
    "Paraguay": "py", "Portugal": "pt", "Qatar": "qa", "Saudi Arabia": "sa",
    "Scotland": "gb-sct", "Senegal": "sn", "South Africa": "za",
    "South Korea": "kr", "Spain": "es", "Sweden": "se", "Switzerland": "ch",
    "Tunisia": "tn", "Turkiye": "tr", "USA": "us", "Uruguay": "uy",
    "Uzbekistan": "uz",
}

# football-data.org name variants → site name (extend if logs show UNMATCHED)
API_ALIASES = {
    "korea republic": "South Korea",
    "south korea": "South Korea",
    "czech republic": "Czechia",
    "bosnia and herzegovina": "Bosnia-Herzegovina",
    "bosnia & herzegovina": "Bosnia-Herzegovina",
    "united states": "USA",
    "usa": "USA",
    "côte d'ivoire": "Ivory Coast",
    "cote d'ivoire": "Ivory Coast",
    "dr congo": "Congo DR",
    "congo dr": "Congo DR",
    "democratic republic of the congo": "Congo DR",
    "ir iran": "Iran",
    "iran": "Iran",
    "türkiye": "Turkiye",
    "turkiye": "Turkiye",
    "turkey": "Turkiye",
    "curaçao": "Curacao",
    "cabo verde": "Cape Verde",
    "cape verde islands": "Cape Verde",
    "korea dpr": None,  # not qualified — guard against accidental match
}


def norm(name: str) -> str:
    """Normalize an API team name for matching."""
    n = name.lower().strip()
    n = re.sub(r"\s+(national team|nt)$", "", n)
    return n


def api_name_to_iso(api_name: str):
    n = norm(api_name)
    if n in API_ALIASES:
        target = API_ALIASES[n]
        return NAME_TO_ISO.get(target) if target else None
    # direct match against site names
    for site_name, iso in NAME_TO_ISO.items():
        if norm(site_name) == n:
            return iso
    return None


def load_fixtures():
    path = Path(__file__).parent / "fixtures.json"
    return json.loads(path.read_text())


def http_json(url, headers=None, method="GET", body=None):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_api_matches():
    """World Cup matches around today (UTC), any status."""
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=1)).isoformat()
    date_to = (today + timedelta(days=1)).isoformat()
    url = f"{API_BASE}/competitions/WC/matches?dateFrom={date_from}&dateTo={date_to}"
    data = http_json(url, headers={"X-Auth-Token": API_TOKEN})
    return data.get("matches", [])


def match_to_site_id(api_match, fixtures):
    """Map an API match to the site's matchId by team pair + date proximity."""
    h_iso = api_name_to_iso(api_match["homeTeam"]["name"])
    a_iso = api_name_to_iso(api_match["awayTeam"]["name"])
    if not h_iso or not a_iso:
        print(f"  UNMATCHED team name: {api_match['homeTeam']['name']!r} vs "
              f"{api_match['awayTeam']['name']!r} — add to API_ALIASES")
        return None, False
    api_date = datetime.fromisoformat(api_match["utcDate"].replace("Z", "+00:00")).date()
    for f in fixtures:
        site_date = datetime.fromisoformat(f["date"]).date()
        if abs((site_date - api_date).days) > 1:
            continue
        if {f["home"], f["away"]} == {h_iso, a_iso}:
            # API order may be flipped vs site order (rare, but cheap to handle)
            flipped = (f["home"], f["away"]) != (h_iso, a_iso)
            mid = f"{f['date']}_{f['home']}_{f['away']}"
            return mid, flipped
    print(f"  UNMATCHED fixture: {h_iso} vs {a_iso} on {api_date}")
    return None, False


def extract_score(api_match):
    """Current score for live matches, full-time for finished. None if not started."""
    status = api_match.get("status", "")
    if status in ("SCHEDULED", "TIMED", "POSTPONED", "CANCELLED"):
        return None
    sc = api_match.get("score", {})
    ft = sc.get("fullTime", {}) or {}
    h, a = ft.get("home"), ft.get("away")
    if h is None or a is None:
        # live matches sometimes only populate halves
        ht = sc.get("halfTime", {}) or {}
        h = h if h is not None else ht.get("home")
        a = a if a is not None else ht.get("away")
    if h is None or a is None:
        return None
    return int(h), int(a)


def firebase_get(path):
    try:
        return http_json(f"{FIREBASE_URL}/{path}.json")
    except Exception:
        return None


def firebase_put(path, value):
    http_json(f"{FIREBASE_URL}/{path}.json", method="PUT", body=value)


def sync_once(fixtures, cache):
    """One API poll → Firebase writes for changed scores. Returns (#live, #updates)."""
    matches = fetch_api_matches()
    live = sum(1 for m in matches if m.get("status") in ("IN_PLAY", "PAUSED"))
    updates = 0
    for m in matches:
        score = extract_score(m)
        if score is None:
            continue
        mid, flipped = match_to_site_id(m, fixtures)
        if not mid:
            continue
        h, a = score
        if flipped:
            h, a = a, h
        new_val = {"h": h, "a": a}
        if cache.get(mid) == new_val:
            continue  # no change → no write
        firebase_put(f"dailyResults/{mid}", new_val)
        cache[mid] = new_val
        updates += 1
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts} UTC] ✅ {mid} ← {h}–{a} ({m.get('status')})", flush=True)
    return live, updates


def main():
    if not API_TOKEN:
        sys.exit("FOOTBALL_DATA_TOKEN env var is missing.")
    fixtures = load_fixtures()
    cache = firebase_get("dailyResults") or {}

    poll = int(os.environ.get("POLL_SECONDS", "60"))
    max_minutes = int(os.environ.get("MAX_MINUTES", "0"))  # 0 = single pass

    if max_minutes <= 0:
        live, updates = sync_once(fixtures, cache)
        print(f"Done. {updates} update(s) written.")
        return

    # ── LIVE polling mode: poll every `poll` seconds for up to `max_minutes` ──
    import time
    deadline = datetime.now(timezone.utc) + timedelta(minutes=max_minutes)
    idle_polls = 0
    print(f"Live mode: polling every {poll}s until {deadline:%H:%M} UTC.", flush=True)
    while datetime.now(timezone.utc) < deadline:
        try:
            live, _ = sync_once(fixtures, cache)
            idle_polls = 0 if live else idle_polls + 1
        except Exception as e:  # API hiccup — keep going
            print(f"  ⚠ poll failed: {e}", flush=True)
        # If no live match for 30 consecutive polls (~30 min), stop early
        if idle_polls >= 30:
            print("No live matches for a while — stopping early.", flush=True)
            break
        time.sleep(poll)
    print("Live polling session finished.")


if __name__ == "__main__":
    main()
