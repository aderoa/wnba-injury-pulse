# wnba-injury-pulse

Live tracking of the WNBA's official injury report PDFs. Polls every 15 minutes,
parses each report, diffs against the previous state per-team, and surfaces only
the **changes** — adds, clears, status flips, reason updates.

## 📊 Live dashboard

**[aderoa.github.io/wnba-injury-pulse](https://aderoa.github.io/wnba-injury-pulse/)** — auto-refreshing dashboard with two views: current state grouped by team, and a chronological feed of recent changes.

For browsing the raw artifacts:

- **[INJURY_CHANGES.md](INJURY_CHANGES.md)** — newest-first markdown log of fired changes
- **[data/team_state.json](data/team_state.json)** — current best-known state per team
- **[data/changes_log.json](data/changes_log.json)** — structured ledger of all changes

## How the diff handles team absences

The WNBA only includes a team in the report if they have a game **today or
tomorrow**. So a team can disappear for 2-3 days between back-to-backs. The
naive interpretation — "player not in current report = available" — is wrong
when the player's team isn't reporting at all.

This system tracks per-team state. A change event only fires when:

- A team is in the current report (so they're filing an update we can compare against)
- AND the team's state for the player differs from the last submitted state

Concrete example:

- **Mon 5/4** — NYL plays. Stewart listed OUT.
- **Tue 5/5 – Thu 5/7** — NYL has no games. Team absent from reports. Stewart's state preserved as "OUT (last submitted Mon)."
- **Fri 5/8** — NYL plays again. Stewart not on the new report → **cleared event fires**, exactly once, at the moment NYL re-emerges.

There's also a third state: **NOT YET SUBMITTED**. Teams in scope (game today/tomorrow) but who haven't filed yet show up this way. We treat those polls as non-updates — neither adding nor clearing players — and wait until they actually submit.

## Architecture

```
Cloudflare Worker (cron every minute, fires every 15 min for injuries)
        │
        │ repository_dispatch: injury_pulse_tick
        ▼
GitHub Actions (.github/workflows/poll.yml)
        │
        ▼
scripts/poll_injuries.py
   ├── compute latest URL from current ET time
   ├── fetch PDF (with 1-slot fallback if 404)
   ├── parse with pdfplumber
   ├── collapse → per-team view (handles B2B duplicates)
   ├── diff against data/team_state.json (only for teams w/ submissions)
   └── write outputs:
       ├── data/team_state.json        ← carries forward across polls
       ├── data/current_report.json    ← raw parse of most recent PDF
       ├── data/changes_log.json       ← structured event log (last 2000)
       ├── data/injury_pulse_live.json ← compact dashboard payload
       └── INJURY_CHANGES.md           ← human-readable change log

GitHub Pages serves index.html + data/*
```

The Worker (`worker.js`) is shared with the milestones tracker — it dispatches both `live_tracker_tick` (every minute, to wnba-milestones) and `injury_pulse_tick` (every 15 minutes, to this repo).

## Setup

1. **Create the repo** `aderoa/wnba-injury-pulse` and push these files.
2. **Enable GitHub Pages** — Settings → Pages → Branch: `main`, Folder: `/ (root)`. Dashboard live at `https://aderoa.github.io/wnba-injury-pulse/` after ~1 min.
3. **Update the Cloudflare Worker** — replace your existing `wnba-milestones-trigger` worker code with the `worker.js` from this repo. Add two new env variables in Settings → Variables:
   - `MILESTONES_REPO` = `wnba-milestones`
   - `INJURY_PULSE_REPO` = `wnba-injury-pulse`
   
   Keep your existing `GITHUB_OWNER` and `GITHUB_TOKEN` as-is.
4. **Smoke test** — Actions tab → "WNBA Injury Pulse" → Run workflow with `dry_run = true`. Expected output: PDF fetched, table parsed, "X teams in report" printed, no commit.
5. **Live test** — re-trigger without dry_run. Should commit `team_state.json` and the dashboard should populate within ~2 min.
6. **Cron takeover** — The Worker fires every 15 min on the :00/:15/:30/:45 marks. A schedule cron is also configured in `poll.yml` as a fallback safety net.

## Embedding in Presto / HoopsHype articles

The dashboard supports two URL parameters for chrome-less iframe embedding:

- `?embed=1` — hides the brand header and footer, tightens padding
- `?view=log` — shows just the recent-changes feed, hiding the team grid

Combine for a clean live ticker:

```html
<iframe
  src="https://aderoa.github.io/wnba-injury-pulse/?embed=1&view=log"
  width="100%"
  height="600"
  frameborder="0"
  style="border:1px solid #e5e7eb;border-radius:8px"
  title="WNBA Injury Pulse">
</iframe>
```

## Files

| Path | Purpose |
|------|---------|
| `index.html` | Live dashboard |
| `INJURY_CHANGES.md` | Newest-first change log |
| `worker.js` | Cloudflare Worker (shared with milestones tracker) |
| `.github/workflows/poll.yml` | GH Actions workflow, dispatched by Worker |
| `scripts/poll_injuries.py` | PDF fetch + parse + diff + write |
| `data/team_state.json` | Persistent per-team state |
| `data/changes_log.json` | Structured ledger of all changes |
| `data/current_report.json` | Raw parse of most recent PDF |
| `data/injury_pulse_live.json` | Compact dashboard payload |

## Known limitations

- **First ~2 days will be noisy** — every player listed will fire as an `added` event because we have no prior state. Self-resolves after a couple polls.
- **PDF format drift** — if the WNBA changes the table layout, `pdfplumber` may extract differently and the parser will need adjustment. Watch for "0 teams in report" in workflow logs.
- **Player name matching** — the report uses "Last, First" format. We flip to "First Last" for cross-system matching, but unusual names (suffixes, hyphens, unicode characters) may need manual overrides.
- **CDN access** — the WNBA's CDN may rate-limit or block certain IP ranges. GitHub Actions runners work fine; some cloud providers don't. If you ever see persistent 403s in workflow logs, that's the cause.
- **Time zone** — slot URLs use 12-hour Eastern time. The poller computes ET from UTC; DST changes are handled via Python's `zoneinfo`.

## Status state machine

Five known statuses (in increasing severity):

| Status | Meaning |
|--------|---------|
| Available | Will play |
| Probable | Likely to play |
| Questionable | Game-time decision |
| Doubtful | Unlikely to play |
| Out | Will not play |

Plus the meta-state:

| Marker | Meaning |
|--------|---------|
| NOT YET SUBMITTED | Team in scope, no update filed yet — non-event, state preserved |
