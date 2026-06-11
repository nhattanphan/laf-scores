# LAF WC2026 — automatic live score updater

Streams LIVE World Cup scores: during match windows a GitHub Actions job polls
football-data.org every 60 seconds and writes any score change to Firebase
`dailyResults/{matchId}` immediately — goals appear on the site within ~1 minute. The site
(lafwc2026.fr) picks them up live through its existing listeners: home-page
LIVE scores, daily-game points, group tables — no site changes needed.

## Setup (5 minutes)

1. Get a free API key: https://www.football-data.org/client/register
   (free tier includes the FIFA World Cup; 10 req/min is far more than needed).
2. Create a GitHub repo (private is fine) with these 3 files, keeping the
   `.github/workflows/` path.
3. Repo → Settings → Secrets and variables → Actions → New repository secret:
   Name: FOOTBALL_DATA_TOKEN  ·  Value: your API key
4. Test: Actions tab → "Update WC scores" → Run workflow. Check the log —
   you should see "✅ 2026-06-11_mx_za ← ..." lines once a match is live.

## Notes

- Writes only on change (no spam writes); admin manual entry on the site
  still works and will simply be overwritten by the next API sync if they differ.
- If the log shows "UNMATCHED team name", add that spelling to API_ALIASES
  in update_scores.py.
- Group stage only (matches the daily game). After June 27 you can disable
  the workflow or extend fixtures.json for the knockout rounds.
- Use a PUBLIC repo: Actions minutes are free and unlimited there (private
  repos have a 2000 min/month cap, which live polling would exceed). The repo
  contains no secrets — the API key lives in GitHub Secrets.
- Sessions auto-stop after ~30 min without any live match, and GitHub cron
  start times can drift a few minutes — the windows start 5 min early to absorb that.
- Free API tier allows 10 requests/min; we use 1/min. Plenty of headroom.
