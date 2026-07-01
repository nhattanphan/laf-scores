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

FIREBASE_URLS = [u.strip().rstrip("/") for u in os.environ.get(
    "FIREBASE_URLS",
    "https://laf-wc2026-default-rtdb.firebaseio.com,"
    "https://wc2026fr-default-rtdb.firebaseio.com"
).split(",") if u.strip()]
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


def load_teams_iso():
    """iso → {id: FIFA code, group: letter} (matches the site's TEAMS object)."""
    path = Path(__file__).parent / "teams_iso.json"
    return json.loads(path.read_text())


def http_json(url, headers=None, method="GET", body=None):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_api_matches():
    """World Cup matches around today (UTC), any status.
    Window is ±1 day to catch matches that started yesterday (late kickoffs)
    or are scheduled tomorrow (early UTC kickoffs in Asia/Pacific slots).
    Extra time / penalty shootouts can push a match >2h past kickoff, so we
    always include yesterday to avoid dropping a match mid-ET at midnight UTC."""
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=1)).isoformat()
    date_to   = (today + timedelta(days=1)).isoformat()
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
        if f.get("stage") == "knockout" or "home" not in f or "away" not in f:
            continue  # knockout fixtures carry no teams here — handled via koFixtures
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




# ── KNOCKOUT support ──────────────────────────────────────────────────────────
# Admin enters knockout matchups in Firebase node `koFixtures`:
#   koFixtures/{matchN} = {"home": <teamId>, "away": <teamId>}
# where teamId is the site's FIFA-style id (TEAMS key). We map teamId → iso via
# teams_iso.json (iso → {id, group}) inverted, then match API results by team pair.

def load_team_id_to_iso():
    """Return {teamId: iso} by inverting teams_iso.json (iso → {id,...})."""
    try:
        teams_iso = load_teams_iso()
    except Exception:
        return {}
    out = {}
    for iso, info in teams_iso.items():
        tid = info.get("id")
        if tid:
            out[tid] = iso
    return out


def fetch_ko_fixtures():
    """Read admin-entered knockout matchups from the first Firebase DB."""
    return firebase_get("koFixtures") or {}


def ko_match_to_site_id(api_match, fixtures, ko_fixtures, id_to_iso):
    """Map an API knockout result to a site matchId using admin-entered koFixtures.
    Returns (matchId, flipped) or (None, False)."""
    if not ko_fixtures:
        return None, False
    h_iso = api_name_to_iso(api_match["homeTeam"]["name"])
    a_iso = api_name_to_iso(api_match["awayTeam"]["name"])
    if not h_iso or not a_iso:
        return None, False
    # knockout fixtures by matchN → date
    ko_dates = {f["matchN"]: f for f in fixtures if f.get("stage") == "knockout"}
    for mn, pair in ko_fixtures.items():
        if not isinstance(pair, dict):
            continue
        ph, pa = pair.get("home"), pair.get("away")
        if not ph or not pa:
            continue
        set_iso = {id_to_iso.get(ph), id_to_iso.get(pa)}
        if set_iso == {h_iso, a_iso}:
            fx = ko_dates.get(mn)
            if not fx:
                continue
            # build matchId exactly like the site does for a resolved knockout fixture:
            #   dailyMatchId = `${date}_${homeISO}_${awayISO}`
            # The site's home/away order = admin's koFixtures order (home first).
            home_iso = id_to_iso.get(ph)
            away_iso = id_to_iso.get(pa)
            mid = f"{fx['date']}_{home_iso}_{away_iso}"
            flipped = (home_iso, away_iso) != (h_iso, a_iso)
            return mid, flipped
    return None, False


def extract_score(api_match):
    """Return (home, away, won_iso) for a match.

    football-data.org **v4** semantics (docs/general/v4/overtime.html):
      fullTime    = RUNNING TOTAL score. Set to 0 at kickoff, keeps counting
                    through ET *and* the penalty shootout. A match decided on
                    pens shows e.g. 1-1 (120') + 6-5 (pens) as fullTime 7-6.
      regularTime = score after 90' (v4 only; appears for knockout matches)
      extraTime   = ONLY the goals scored during extra time (starts at 0)
      penalties   = ONLY the goals of the shootout
      duration    = REGULAR | EXTRA_TIME | PENALTY_SHOOTOUT

    Display score = open-play goals (90' or 120'), never penalties:
      → fullTime minus penalties whenever a shootout is/was in progress,
        cross-checked against regularTime + extraTime when available.
    won_iso is set only when a knockout is decided on penalties.
    Returns None if no usable score is available yet.
    """
    status = api_match.get("status", "")
    if status in ("SCHEDULED", "TIMED", "POSTPONED", "CANCELLED"):
        return None

    sc   = api_match.get("score", {}) or {}
    ft   = sc.get("fullTime",    {}) or {}
    rt   = sc.get("regularTime", {}) or {}
    et   = sc.get("extraTime",   {}) or {}
    pens = sc.get("penalties",   {}) or {}
    duration = sc.get("duration") or "REGULAR"

    ft_h, ft_a = ft.get("home"), ft.get("away")
    if ft_h is None or ft_a is None:
        return None  # API glitch mid-poll (e.g. VAR review) — skip this poll
    h, a = int(ft_h), int(ft_a)

    p_h, p_a = pens.get("home"), pens.get("away")
    in_shootout = (duration == "PENALTY_SHOOTOUT" or status == "PENALTIES"
                   or (p_h is not None and p_a is not None))
    if in_shootout:
        # Strip shootout goals out of the running total → true 120' score.
        h -= int(p_h or 0)
        a -= int(p_a or 0)
        # Cross-check with regularTime + extraTime (authoritative when present).
        rt_h, rt_a = rt.get("home"), rt.get("away")
        if rt_h is not None and rt_a is not None:
            alt_h = int(rt_h) + int(et.get("home") or 0)
            alt_a = int(rt_a) + int(et.get("away") or 0)
            if (alt_h, alt_a) != (h, a):
                h, a = alt_h, alt_a

    if h < 0 or a < 0:
        return None  # inconsistent transient API state — skip this poll

    # Penalty shootout winner (bracket needs this to auto-advance).
    won_iso = None
    winner  = sc.get("winner")
    if duration == "PENALTY_SHOOTOUT" and status == "FINISHED":
        if winner == "HOME_TEAM" or (p_h is not None and p_a is not None and p_h > p_a):
            won_iso = api_name_to_iso(api_match["homeTeam"]["name"])
        elif winner == "AWAY_TEAM" or (p_h is not None and p_a is not None and p_a > p_h):
            won_iso = api_name_to_iso(api_match["awayTeam"]["name"])
    return h, a, won_iso, in_shootout


def firebase_get(path, base=None):
    try:
        return http_json(f"{base or FIREBASE_URLS[0]}/{path}.json")
    except Exception:
        return None


def firebase_put(path, value):
    """Write to ALL configured databases (LAF + family share the same fixtures)."""
    for base in FIREBASE_URLS:
        try:
            http_json(f"{base}/{path}.json", method="PUT", body=value)
        except Exception as e:
            print(f"  ⚠ write failed on {base}: {e}", flush=True)



# Statuses that count as "live" (match clock running or paused between halves/ET)
_LIVE_STATUSES = {"IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTIES"}
# Statuses where the match might still change (broader than live — used for ET/pen decisions)
_ACTIVE_STATUSES = _LIVE_STATUSES | {"SUSPENDED"}


def _is_var_cancel(prev, h, a):
    """Return True if the new score is exactly 1 lower on one side — a VAR
    cancelled goal.  Returns False (= skip write) for anything else that looks
    like a decrease, because a drop of >1 goal during a live match is always
    an API glitch (e.g. transient null→halfTime fallback that was fixed in
    extract_score, but belt-and-suspenders here).
    prev is the cache dict {"h": int, "a": int, ...}."""
    if prev is None:
        return False
    ph, pa = int(prev.get("h", 0)), int(prev.get("a", 0))
    h_drop = ph - h
    a_drop = pa - a
    # Legitimate VAR cancel: exactly one side drops by exactly 1
    if h_drop == 1 and a_drop == 0:
        return True
    if a_drop == 1 and h_drop == 0:
        return True
    # Any other decrease → API glitch, do NOT write
    return False


def _is_api_glitch(prev, h, a):
    """Return True when the score decreased in a way that is impossible legitimately:
    - Either side drops by more than 1 goal, OR
    - Both sides drop simultaneously (e.g. 2-1 → 1-0: home -1, away -1).
    A single-side drop of exactly 1 is a legitimate VAR cancel."""
    if prev is None:
        return False
    ph, pa = int(prev.get("h", 0)), int(prev.get("a", 0))
    h_drop = ph - h
    a_drop = pa - a
    if h_drop > 1 or a_drop > 1:
        return True
    # Both sides dropped simultaneously → impossible, must be a glitch
    if h_drop > 0 and a_drop > 0:
        return True
    return False


def sync_once(fixtures, cache):
    """One API poll → Firebase writes for changed scores.
    Returns (#live, #updates, has_et) where has_et signals an ET/penalty match."""
    matches = fetch_api_matches()
    live    = sum(1 for m in matches if m.get("status") in _LIVE_STATUSES)
    has_et  = any(m.get("status") in {"EXTRA_TIME", "PENALTIES"} for m in matches)
    updates = 0

    for m in matches:
        score = extract_score(m)
        if score is None:
            continue
        mid, flipped = match_to_site_id(m, fixtures)
        if not mid:
            mid, flipped = ko_match_to_site_id(m, fixtures, KO_FIXTURES, ID_TO_ISO)
        if not mid:
            continue

        h, a, won_iso, in_shootout = score
        if flipped:
            h, a = a, h

        status = m.get("status", "")
        minute = None
        if status in _ACTIVE_STATUSES:
            minute = m.get("minute") or m.get("score", {}).get("minute")

        # ── VAR / glitch guard ────────────────────────────────────────────────
        # Guard is DISABLED while/after a shootout: the corrected 120' score is
        # legitimately lower than an inflated fullTime (e.g. 7–6 → 1–1) that an
        # older run may have cached, and pens-in-progress never add open-play
        # goals anyway.
        prev       = cache.get(mid)
        var_cancel = (not in_shootout) and _is_var_cancel(prev, h, a)
        api_glitch = (not in_shootout) and _is_api_glitch(prev, h, a)

        if api_glitch:
            # Score dropped by >1 goal — impossible legitimately. API is
            # returning a transient bad value (e.g. fullTime momentarily null
            # during VAR review, previously caught by halfTime fallback).
            # Skip this poll entirely; do NOT update Firebase or cache.
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts} UTC] 🚫 API glitch skipped for {mid}: "
                  f"cache={prev} → API={h}–{a} (drop >1, ignoring)", flush=True)
            continue

        if var_cancel:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts} UTC] ⚠ VAR cancel detected for {mid}: "
                  f"{prev} → {h}–{a}", flush=True)

        new_val = {"h": h, "a": a}
        if minute is not None:
            new_val["min"] = int(minute)
        if won_iso:
            new_val["won"] = won_iso

        # change detection (h, a, won); also force-write on VAR cancel or live
        cur_cmp = {"h": h, "a": a}
        if won_iso:
            cur_cmp["won"] = won_iso

        force_write = var_cancel or status in _ACTIVE_STATUSES
        if prev == cur_cmp and not force_write:
            continue  # no change, not live → skip

        firebase_put(f"dailyResults/{mid}", new_val)
        cache[mid] = cur_cmp
        updates += 1
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        pen_note = f" (pen → {won_iso})" if won_iso else ""
        et_note  = " [ET]" if status in {"EXTRA_TIME", "PENALTIES"} else ""
        var_note = " ⚠VAR" if var_cancel else ""
        print(f"  [{ts} UTC] ✅ {mid} ← {h}–{a}{pen_note}{et_note}{var_note} ({status})",
              flush=True)

    # mark finished matches (needed to know when a group is truly complete)
    for m in matches:
        if m.get("status") != "FINISHED":
            continue
        mid, _ = match_to_site_id(m, fixtures)
        if not mid:
            mid, _ = ko_match_to_site_id(m, fixtures, KO_FIXTURES, ID_TO_ISO)
        if mid and not FINISHED_CACHE.get(mid):
            firebase_put(f"dailyFinished/{mid}", True)
            FINISHED_CACHE[mid] = True

    if updates or any(m.get("status") == "FINISHED" for m in matches):
        finalize_groups(fixtures, cache)

    return live, updates, has_et


FINISHED_CACHE = {}
KO_FIXTURES = {}
ID_TO_ISO = {}


def finalize_groups(fixtures, scores):
    """When all 6 matches of a group are FINISHED, write the final ranking to
    results/groups/{g} (FIFA codes, positions 1..3) — the node the bracket-game
    scoring reads. Never overwrites an existing (admin-entered) ranking, so
    manual corrections always win."""
    teams_iso = load_teams_iso()
    existing_by_db = {b: (firebase_get("results/groups", b) or {}) for b in FIREBASE_URLS}
    by_group = {}
    for f in fixtures:
        if f.get("stage") == "knockout" or "home" not in f:
            continue  # group standings only use group-stage fixtures
        by_group.setdefault(f.get("group") or teams_iso.get(f["home"], {}).get("group"), []).append(f)
    for g, ms in by_group.items():
        if not g or all(g in ex for ex in existing_by_db.values()):
            continue  # unknown group or already finalized everywhere (admin wins)
        mids = [f"{f['date']}_{f['home']}_{f['away']}" for f in ms]
        if len(mids) < 6 or not all(FINISHED_CACHE.get(mid) for mid in mids):
            continue
        # compute standings: pts → goal diff → goals for
        st = {}
        for f, mid in zip(ms, mids):
            r = scores.get(mid)
            if not r:
                break
            for t in (f["home"], f["away"]):
                st.setdefault(t, {"pts": 0, "gf": 0, "ga": 0})
            h, a = int(r["h"]), int(r["a"])
            st[f["home"]]["gf"] += h; st[f["home"]]["ga"] += a
            st[f["away"]]["gf"] += a; st[f["away"]]["ga"] += h
            if h > a: st[f["home"]]["pts"] += 3
            elif a > h: st[f["away"]]["pts"] += 3
            else: st[f["home"]]["pts"] += 1; st[f["away"]]["pts"] += 1
        else:
            order = sorted(st, key=lambda t: (-st[t]["pts"], -(st[t]["gf"] - st[t]["ga"]), -st[t]["gf"]))
            ids = [teams_iso[t]["id"] for t in order if t in teams_iso]
            if len(ids) >= 3:
                val = {"1": ids[0], "2": ids[1], "3": ids[2]}
                for base in FIREBASE_URLS:
                    if g not in existing_by_db[base]:
                        try:
                            http_json(f"{base}/results/groups/{g}.json", method="PUT", body=val)
                        except Exception as e:
                            print(f"  ⚠ ranking write failed on {base}: {e}", flush=True)
                print(f"  🏁 Group {g} complete → final ranking saved: "
                      f"{ids[0]} / {ids[1]} / {ids[2]}", flush=True)


def main():
    if not API_TOKEN:
        sys.exit("FOOTBALL_DATA_TOKEN env var is missing.")
    if datetime.now(timezone.utc).date() > datetime(2026, 7, 20, tzinfo=timezone.utc).date():
        print("Tournament over — nothing to do.")
        return
    fixtures = load_fixtures()
    cache = firebase_get("dailyResults") or {}
    FINISHED_CACHE.update(firebase_get("dailyFinished") or {})
    global KO_FIXTURES, ID_TO_ISO
    ID_TO_ISO  = load_team_id_to_iso()
    KO_FIXTURES = fetch_ko_fixtures()
    if KO_FIXTURES:
        n = len([k for k, v in KO_FIXTURES.items()
                 if isinstance(v, dict) and v.get("home") and v.get("away")])
        print(f"Loaded {n} knockout matchup(s) from admin.")

    # POLL_SECONDS: interval when live match is running (default 30s for faster updates)
    poll_live = int(os.environ.get("POLL_SECONDS", "30"))
    # POLL_IDLE_SECONDS: interval when no live match (default 60s to save rate-limit quota)
    poll_idle = int(os.environ.get("POLL_IDLE_SECONDS", "60"))
    max_minutes = int(os.environ.get("MAX_MINUTES", "0"))  # 0 = single pass

    if max_minutes <= 0:
        live, updates, _ = sync_once(fixtures, cache)
        print(f"Done. {updates} update(s) written.")
        return

    # ── LIVE polling mode ────────────────────────────────────────────────────
    # Polls every poll_live seconds when a match is in progress, poll_idle
    # otherwise.  The deadline is extended automatically when a match enters
    # extra time or a penalty shootout so we never stop mid-ET.
    import time
    deadline = datetime.now(timezone.utc) + timedelta(minutes=max_minutes)
    idle_polls = 0
    et_active  = False          # True while any match is in ET/penalties
    et_grace   = timedelta(minutes=45)   # max extra grace when ET detected

    print(f"Live mode: poll {poll_live}s (live) / {poll_idle}s (idle) "
          f"until {deadline:%H:%M} UTC.", flush=True)

    def next_kickoff_utc():
        """Earliest future kickoff (fixtures are CEST = UTC+2 during the tournament)."""
        now = datetime.now(timezone.utc)
        best = None
        for f in fixtures:
            ko = datetime.fromisoformat(f"{f['date']}T{f['time']}:00+02:00")
            if ko > now and (best is None or ko < best):
                best = ko
        return best

    while True:
        now = datetime.now(timezone.utc)

        # ── Deadline check (with ET extension) ──────────────────────────────
        # If we're past the scheduled deadline but a match is still in ET /
        # penalties, extend deadline by et_grace so we don't drop mid-match.
        if now >= deadline:
            if et_active:
                deadline = now + et_grace
                print(f"  ⏱ ET/Pen in progress — extending deadline to "
                      f"{deadline:%H:%M} UTC.", flush=True)
                et_active = False   # will be re-set next poll if still active
            else:
                break

        try:
            globals()["KO_FIXTURES"] = fetch_ko_fixtures()
            live, _, has_et = sync_once(fixtures, cache)
            et_active  = has_et
            idle_polls = 0 if live else idle_polls + 1
        except Exception as e:
            print(f"  ⚠ poll failed: {e}", flush=True)

        if idle_polls >= 30:
            ko = next_kickoff_utc()
            if ko is None or ko > deadline:
                print("No live matches and no kickoff before session end — stopping early.",
                      flush=True)
                break
            wait = (ko - datetime.now(timezone.utc)).total_seconds()
            if wait > 360:
                print(f"Next kickoff {ko:%H:%M} UTC — idling until then.", flush=True)
                time.sleep(min(wait - 300, 300))
                continue
            idle_polls = 0  # kickoff imminent — resume normal polling

        # Adaptive sleep: fast when live, slow when idle
        interval = poll_live if live else poll_idle
        time.sleep(interval)

    print("Live polling session finished.")





if __name__ == "__main__":
    main()
