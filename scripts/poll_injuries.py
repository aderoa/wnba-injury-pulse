"""
poll_injuries.py
----------------
Polls the WNBA's official injury report PDFs every 15 minutes and detects
state changes per team.

Critical insight: a team only appears in a report if they have a game today
or tomorrow. So "player not in current report" only means "cleared" if the
player's TEAM is also in the current report. If the team is absent, the
player's last-known state is preserved.

A third state exists: "NOT YET SUBMITTED." This means the team is in the
report's scope (has a game soon) but hasn't filed their submission yet.
We preserve last-known state in this case too — it's a non-update, not a
clear.

Outputs:
  data/team_state.json       Current best-known state per team
  data/changes_log.json      Chronological list of detected changes
  data/current_report.json   Raw parsed contents of the most recent PDF
  INJURY_CHANGES.md          Human-readable change log (newest at top)

Flags:
  --slot YYYY-MM-DD_HH_MMA/PM   Override the slot to fetch (testing)
  --dry-run                     Don't write outputs
  --no-fallback                 Don't try previous slot if current 404s
"""
import argparse
import datetime as dt
import io
import json
import re
import sys
import unicodedata
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TEAM_STATE_PATH = DATA / "team_state.json"
CHANGES_LOG_PATH = DATA / "changes_log.json"
CURRENT_REPORT_PATH = DATA / "current_report.json"
LIVE_JSON_PATH = DATA / "injury_pulse_live.json"
INJURY_CHANGES_MD = ROOT / "INJURY_CHANGES.md"

URL_BASE = "https://ak-static.cms.nba.com/referee/wnba_injury"

# WNBA publish times are Eastern. Slot format: YYYY-MM-DD_HH_MMA/PM (12-hour clock).
SLOT_TZ = dt.timezone(dt.timedelta(hours=-5))  # ET (will adjust for DST below)

# Team name → 3-letter abbreviation (matches matchup column abbreviations)
TEAM_ABBR = {
    "Atlanta Dream": "ATL",
    "Chicago Sky": "CHI",
    "Connecticut Sun": "CON",
    "Dallas Wings": "DAL",
    "Golden State Valkyries": "GSV",
    "Indiana Fever": "IND",
    "Las Vegas Aces": "LVA",
    "Los Angeles Sparks": "LAS",
    "Minnesota Lynx": "MIN",
    "New York Liberty": "NYL",
    "Phoenix Mercury": "PHX",
    "Portland Fire": "PDX",
    "Seattle Storm": "SEA",
    "Washington Mystics": "WAS",
}


def _build_team_variants():
    """
    Build (variant_text, canonical_full_name) pairs sorted by length descending.
    pdfplumber's extract_text() on this PDF drops spaces inside multi-word names,
    so we match against both "Phoenix Mercury" and "PhoenixMercury" forms.
    """
    variants = set()
    for full in TEAM_ABBR.keys():
        variants.add((full, full))
        variants.add((full.replace(" ", ""), full))
    return sorted(variants, key=lambda t: -len(t[0]))


TEAM_VARIANTS = _build_team_variants()


def normalize_extracted_text(s):
    """
    Add spaces back into text where pdfplumber's extraction dropped them.
    Handles common patterns:
      - camelCase → "camel Case"  (sW → 's W' inside "Coach'sDecision")
      - "a-b" → "a - b"  (between letters)
      - ",X" or ";X" → ", X" / "; X"  (after punctuation, before letter)
      - "X(" → "X ("  (before opening paren)
    Does NOT touch single-token compound names like "DiJonai" because they
    aren't reasons. Player names are normalized separately (just the comma).
    """
    if not s:
        return s
    s = re.sub(r'([A-Za-z])-([A-Za-z])', r'\1 - \2', s)
    s = re.sub(r'([,;])([A-Za-z])', r'\1 \2', s)
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', s)
    s = re.sub(r'([A-Za-z0-9])\(', r'\1 (', s)
    return s


def normalize_player_name_lf(name_lf):
    """Insert a space after the comma if it's missing: 'Last,First' → 'Last, First'."""
    return re.sub(r',(\S)', r', \1', name_lf or "")

# Statuses we expect from the report. Order matters — earlier = more available.
STATUS_ORDER = ["Available", "Probable", "Questionable", "Doubtful", "Out"]
_STATUSES_RE = "|".join(re.escape(s) for s in STATUS_ORDER)
_STATUS_PATTERN = re.compile(rf"\b({_STATUSES_RE})\b")

# Reason categories (rough buckets used for filtering / display)
REASON_CATEGORIES = {
    "injury": ["injury/illness"],
    "rest_coach": ["coach's decision"],
    "offcourt": ["personal reasons", "not with team"],
    "procedural": ["concussion protocol", "return to competition", "g league", "g-league"],
}

USER_AGENT = "wnba-injury-pulse/1.0 (HoopsMatic)"

# Pre-compiled patterns for line parsing
_DATE_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.*)$")
_TIME_RE = re.compile(r"^(\d{1,2}:\d{2}\s*\(ET\))\s+(.*)$")
_MATCHUP_RE = re.compile(r"^([A-Z]{2,4}@[A-Z]{2,4})\s+(.*)$")
# Player name is "Last, First" — the comma is the anchor. Allow hyphens, apostrophes,
# accented chars, periods (for initials). Player runs from the comma-bearing token to
# the next whitespace before the status keyword.
_PLAYER_RE = re.compile(
    r"^(?P<player>[\w\.\-\'\u00C0-\u017F]+(?:\s[\w\.\-\'\u00C0-\u017F]+)*,\s*"
    r"[\w\.\-\'\u00C0-\u017F]+(?:\s[\w\.\-\'\u00C0-\u017F]+)*?)\s+"
    rf"(?P<status>{_STATUSES_RE})(?:\s+(?P<reason>.*))?$"
)


# ---------- slot helpers ----------

def _eastern_now():
    """Now in ET, naive (DST-aware via system tzdata)."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: assume EDT (UTC-4) — May through November
        return dt.datetime.now(dt.timezone.utc).astimezone(
            dt.timezone(dt.timedelta(hours=-4))
        )


def latest_slot(now=None):
    """Round down to the nearest 15-min mark in Eastern Time."""
    now = now or _eastern_now()
    minute = (now.minute // 15) * 15
    return now.replace(minute=minute, second=0, microsecond=0)


def previous_slot(slot):
    return slot - dt.timedelta(minutes=15)


def slot_to_url(slot):
    """Format slot to the URL filename. Slot must be tz-aware in ET."""
    return f"{URL_BASE}/Injury-Report_{slot.strftime('%Y-%m-%d_%I_%M%p')}.pdf"


def parse_slot_arg(s):
    """Parse a CLI --slot value like '2026-05-09_06_30PM'."""
    return dt.datetime.strptime(s, "%Y-%m-%d_%I_%M%p").replace(tzinfo=_eastern_now().tzinfo)


# ---------- PDF download ----------

def fetch_pdf(slot, allow_fallback=True, max_attempts=2):
    """
    Fetch the PDF for the given slot. Returns (bytes, slot_used) on success,
    raises RuntimeError if no slot in fallback range responds with 200.
    """
    last_err = None
    for attempt in range(max_attempts):
        candidate = slot if attempt == 0 else previous_slot(slot)
        url = slot_to_url(candidate)
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("application/pdf"):
                return resp.content, candidate, url
            last_err = f"HTTP {resp.status_code} for {url}"
        except requests.RequestException as exc:
            last_err = f"request failed for {url}: {exc}"
        if not allow_fallback:
            break
    raise RuntimeError(f"Failed to fetch any recent slot. Last error: {last_err}")


# ---------- PDF parsing ----------

def parse_pdf(pdf_bytes):
    """
    Extract structured rows from the injury report PDF using line-based
    text parsing. The PDF doesn't have visible table borders, so
    pdfplumber.extract_tables() doesn't reliably detect them.

    Strategy:
      1. Get all text lines from each page
      2. Skip page headers and column headers
      3. Each line falls into one of:
         - "main line" — has a status keyword → starts a new player record
         - "NOT YET SUBMITTED" line — team has no players reported yet
         - "continuation line" — text without a status, appended to previous reason

    Date / time / matchup / team are forward-filled because they only appear
    on the first line of each grouping.

    Returns: {report_ts, page_count, teams: [...]}
    """
    import pdfplumber

    raw_lines = []
    report_ts = None
    page_count = 0

    page_header_re = re.compile(r'^Page\s*\d+\s*of\s*\d+$')

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if report_ts is None:
                    m = re.search(r"Injury Report:\s*([\d/]+\s+[\d:]+\s*[AP]M)", line)
                    if m:
                        report_ts = m.group(1)
                # Skip page chrome — both spaced and no-space forms (pdfplumber
                # may strip whitespace in extracted text)
                if line.startswith("Injury Report:") or line.startswith("InjuryReport:"):
                    continue
                if page_header_re.match(line):
                    continue
                if line.startswith("Game Date") or line.startswith("GameDate"):
                    continue
                raw_lines.append(line)

    teams = _parse_lines_to_teams(raw_lines)

    # Diagnostic — print the first 30 raw lines if parsing produced nothing.
    # Lets us see exactly what pdfplumber returned in the workflow log so we
    # can adjust the parser if the format differs from expectations.
    if not teams:
        print("WARNING: parse produced 0 teams. Dumping first 30 raw lines for debug:")
        for i, line in enumerate(raw_lines[:30]):
            print(f"  L{i:>3}: {line!r}")
        if len(raw_lines) > 30:
            print(f"  ...({len(raw_lines) - 30} more lines)")

    return {
        "report_ts": report_ts,
        "page_count": page_count,
        "raw_line_count": len(raw_lines),
        "teams": teams,
    }


def _strip_prefix_fields(line, cur):
    """
    Strip leading {date} {time} {matchup} {team} from a line, updating cur dict.
    Returns the remaining text (which should be the player+status+reason portion).

    Team matching uses TEAM_VARIANTS to handle both "Dallas Wings" and the
    no-space "DallasWings" form pdfplumber returns when font spacing is tight.
    """
    rest = line
    m = _DATE_RE.match(rest)
    if m:
        cur["date"] = m.group(1)
        rest = m.group(2)
    m = _TIME_RE.match(rest)
    if m:
        cur["time"] = m.group(1)
        rest = m.group(2)
    m = _MATCHUP_RE.match(rest)
    if m:
        cur["matchup"] = m.group(1)
        rest = m.group(2)
    # Try to strip a team name (variant-aware) from the start
    for variant, canonical in TEAM_VARIANTS:
        if rest.startswith(variant):
            cur["team_full"] = canonical
            rest = rest[len(variant):].lstrip()
            break
    return rest


def _parse_lines_to_teams(lines):
    """
    Walk through cleaned lines and produce a list of team-game entries.
    Handles forward-fill of date/time/matchup/team and reason wrapping.

    Reason wrapping in this PDF is unusual: when a reason is too long to fit on
    one line, pdfplumber returns the FIRST wrap line BEFORE the player's row
    and the SECOND wrap line AFTER the player's row. We handle this by
    buffering "orphan" lines (text without a status keyword) and routing them:

      - If buffered before a player whose own line has NO inline reason →
        the orphan(s) become that player's reason precursor.
      - If buffered after a player whose own line HAS an inline reason →
        flush them onto the LAST player as a reason continuation when the
        next player line arrives.
    """
    teams = []
    seen_keys = {}  # (date, matchup, team_full) -> team_entry index
    cur = {"date": None, "time": None, "matchup": None, "team_full": None}
    last_player = None
    orphan_buffer = []  # raw text fragments awaiting assignment

    def get_or_make_team():
        key = (cur["date"], cur["matchup"], cur["team_full"])
        if key in seen_keys:
            return teams[seen_keys[key]]
        entry = {
            "team_full": cur["team_full"],
            "team_abbr": TEAM_ABBR.get(cur["team_full"], (cur["team_full"] or "")[:3].upper()),
            "game_date": cur["date"],
            "game_time": cur["time"],
            "matchup": cur["matchup"],
            "submitted": True,
            "players": [],
        }
        teams.append(entry)
        seen_keys[key] = len(teams) - 1
        return entry

    def flush_orphans_to_last():
        """Append buffered orphans onto the last player's reason."""
        nonlocal orphan_buffer
        if orphan_buffer and last_player is not None:
            last_player["reason"] = (last_player["reason"] + " " + " ".join(orphan_buffer)).strip()
        orphan_buffer = []

    for line in lines:
        # NOT YET SUBMITTED — special team-only marker, no player. pdfplumber
        # may return this with or without internal spaces.
        if "NOTYETSUBMITTED" in line.replace(" ", ""):
            stripped = re.sub(r"\s*NOT\s*YET\s*SUBMITTED\s*", " ", line).strip()
            _strip_prefix_fields(stripped, cur)
            if cur["team_full"]:
                key = (cur["date"], cur["matchup"], cur["team_full"])
                if key not in seen_keys:
                    teams.append({
                        "team_full": cur["team_full"],
                        "team_abbr": TEAM_ABBR.get(cur["team_full"], cur["team_full"][:3].upper()),
                        "game_date": cur["date"],
                        "game_time": cur["time"],
                        "matchup": cur["matchup"],
                        "submitted": False,
                        "players": [],
                    })
                    seen_keys[key] = len(teams) - 1
            flush_orphans_to_last()
            last_player = None
            continue

        rest = _strip_prefix_fields(line, cur)
        m = _PLAYER_RE.match(rest)

        if m:
            player_lf = m.group("player").strip()
            status = m.group("status").strip()
            inline_reason = (m.group("reason") or "").strip()

            if cur["team_full"] is None:
                # Couldn't determine team; skip this line (and any orphans)
                orphan_buffer = []
                last_player = None
                continue

            # Convert "Last, First" → "First Last" (after normalizing comma spacing)
            if "," in player_lf:
                last_n, first_n = [p.strip() for p in player_lf.split(",", 1)]
                name = f"{first_n} {last_n}".strip()
            else:
                name = player_lf

            entry = get_or_make_team()
            if inline_reason:
                # Inline reason present — orphans (if any) belong to LAST player as continuation
                flush_orphans_to_last()
                player_record = {
                    "name_last_first": player_lf,
                    "name": name,
                    "status": status,
                    "reason": inline_reason,
                }
            else:
                # No inline reason — orphans (if any) are THIS player's reason precursor
                player_record = {
                    "name_last_first": player_lf,
                    "name": name,
                    "status": status,
                    "reason": " ".join(orphan_buffer).strip() if orphan_buffer else "",
                }
                orphan_buffer = []

            entry["players"].append(player_record)
            last_player = player_record
        else:
            # No status keyword — orphan reason fragment
            if rest.strip():
                orphan_buffer.append(rest.strip())

    # End of input — flush any trailing orphans onto the last player
    flush_orphans_to_last()

    # Normalize player names + reasons for display, then categorize.
    # Categorization uses normalized text so keyword matching works.
    for t in teams:
        for p in t["players"]:
            p["reason"] = normalize_extracted_text(p["reason"])
            p["reason_category"] = categorize_reason(p["reason"])
            p["name_last_first"] = normalize_player_name_lf(p["name_last_first"])
        t["players"] = dedupe_players(t["players"])

    return teams


def categorize_reason(reason):
    if not reason:
        return "unknown"
    lower = reason.lower()
    for cat, keywords in REASON_CATEGORIES.items():
        for kw in keywords:
            if kw in lower:
                return cat
    return "other"


def dedupe_players(players):
    """Collapse duplicate (player) entries, preferring the more restrictive status."""
    by_name = {}
    for p in players:
        name = p["name"]
        if name in by_name:
            existing = by_name[name]
            # Pick the more restrictive of the two statuses
            if STATUS_ORDER.index(p["status"] if p["status"] in STATUS_ORDER else "Out") > \
               STATUS_ORDER.index(existing["status"] if existing["status"] in STATUS_ORDER else "Out"):
                by_name[name] = p
        else:
            by_name[name] = p
    return list(by_name.values())


# ---------- state diff ----------

def collapse_team_view(report):
    """
    Convert a parsed report into a flat dict {team_abbr: {submitted, players}}
    so per-team diffing is easy. If a team appears multiple times (today + tomorrow
    games), merge the player lists; submitted=True overrides False.
    """
    by_team = {}
    for t in report.get("teams", []):
        abbr = t["team_abbr"]
        if abbr not in by_team:
            by_team[abbr] = {
                "team_full": t["team_full"],
                "submitted": t["submitted"],
                "players": list(t["players"]),
                "game_dates": [t["game_date"]] if t["game_date"] else [],
                "matchups": [t["matchup"]] if t["matchup"] else [],
            }
        else:
            existing = by_team[abbr]
            existing["submitted"] = existing["submitted"] or t["submitted"]
            for p in t["players"]:
                if not any(q["name"] == p["name"] for q in existing["players"]):
                    existing["players"].append(p)
            if t["game_date"] and t["game_date"] not in existing["game_dates"]:
                existing["game_dates"].append(t["game_date"])
            if t["matchup"] and t["matchup"] not in existing["matchups"]:
                existing["matchups"].append(t["matchup"])
    # Re-dedupe merged player lists
    for v in by_team.values():
        v["players"] = dedupe_players(v["players"])
    return by_team


def diff_team(prev_state, current, ts_iso):
    """
    Produce a list of change events between prev_state.players (list) and
    current.players (list). Both expected to be deduped already.
    """
    changes = []
    prev_by_name = {p["name"]: p for p in (prev_state or {}).get("players", [])}
    curr_by_name = {p["name"]: p for p in current["players"]}

    # Adds
    for name, p in curr_by_name.items():
        if name not in prev_by_name:
            changes.append({
                "ts": ts_iso, "type": "added", "team_abbr": current.get("team_abbr"),
                "player": name, "status": p["status"], "reason": p["reason"],
                "reason_category": p["reason_category"],
            })

    # Removes (only valid when current.submitted=True)
    if current.get("submitted"):
        for name, p in prev_by_name.items():
            if name not in curr_by_name:
                changes.append({
                    "ts": ts_iso, "type": "cleared", "team_abbr": current.get("team_abbr"),
                    "player": name,
                    "previous_status": p["status"], "previous_reason": p["reason"],
                })

    # Status / reason changes
    for name in (set(prev_by_name) & set(curr_by_name)):
        prev_p = prev_by_name[name]
        curr_p = curr_by_name[name]
        if prev_p["status"] != curr_p["status"]:
            changes.append({
                "ts": ts_iso, "type": "status_change", "team_abbr": current.get("team_abbr"),
                "player": name,
                "from_status": prev_p["status"], "to_status": curr_p["status"],
                "reason": curr_p["reason"], "reason_category": curr_p["reason_category"],
            })
        elif prev_p["reason"] != curr_p["reason"]:
            changes.append({
                "ts": ts_iso, "type": "reason_change", "team_abbr": current.get("team_abbr"),
                "player": name, "status": curr_p["status"],
                "from_reason": prev_p["reason"], "to_reason": curr_p["reason"],
                "reason_category": curr_p["reason_category"],
            })

    return changes


# ---------- IO ----------

def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        print(f"WARN: {path} malformed — treating as empty", file=sys.stderr)
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


# ---------- markdown rendering ----------

def format_change_md(change):
    t = change["type"]
    team = change.get("team_abbr", "")
    p = change["player"]
    if t == "added":
        return (f"**{p}** ({team}) newly listed as **{change['status']}** "
                f"— {change.get('reason', '') or '_no reason_'}")
    if t == "cleared":
        return (f"**{p}** ({team}) cleared "
                f"(was {change.get('previous_status', '?')})")
    if t == "status_change":
        return (f"**{p}** ({team}): "
                f"**{change['from_status']}** → **{change['to_status']}** "
                f"— {change.get('reason', '') or '_no reason_'}")
    if t == "reason_change":
        return (f"**{p}** ({team}, {change['status']}): reason updated "
                f"({change['from_reason']} → {change['to_reason']})")
    return json.dumps(change)


INTRO = (
    "# WNBA Injury Pulse — Change Log\n\n"
    "Auto-updated by the injury-pulse workflow. Newest entries at the top. "
    "Tracks the WNBA's official injury report PDFs (published every 15 min) "
    "and surfaces only the changes — adds, clears, status flips, reason updates.\n\n"
    "Per-team state is preserved when a team is absent from the current report "
    "(no game today or tomorrow). A 'cleared' event only fires when the team "
    "is submitting an updated report and the player isn't on it.\n\n"
)


def prepend_change_block(changes, polled_at_utc):
    if not changes:
        return
    timestamp = polled_at_utc.strftime("%Y-%m-%d %H:%M UTC")
    block_lines = [f"## {timestamp}", ""]
    for c in changes:
        block_lines.append(f"- {format_change_md(c)}")
    block_lines.append("")
    new_block = "\n".join(block_lines)

    if INJURY_CHANGES_MD.exists():
        existing = INJURY_CHANGES_MD.read_text()
        if existing.startswith("# WNBA Injury Pulse"):
            idx = existing.find("\n## ")
            body = existing[idx + 1:] if idx != -1 else ""
        else:
            body = existing
    else:
        body = ""

    INJURY_CHANGES_MD.write_text(INTRO + new_block + "\n" + body)


def write_job_summary(changes, polled_at_utc, report_meta, used_url):
    import os
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        f"## Injury Pulse — {polled_at_utc.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"PDF: `{used_url}`",
        f"Report timestamp: {report_meta.get('report_ts', 'unknown')}",
        "",
    ]
    if changes:
        lines.append(f"### {len(changes)} change(s)")
        lines.append("")
        for c in changes:
            lines.append(f"- {format_change_md(c)}")
    else:
        lines.append("_No changes detected this poll._")
    lines.append("")
    with open(summary_path, "a") as f:
        f.write("\n".join(lines))


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", help="Override slot (e.g. 2026-05-09_06_30PM)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-fallback", action="store_true")
    args = ap.parse_args()

    polled_at_utc = dt.datetime.now(dt.timezone.utc)

    if args.slot:
        slot = parse_slot_arg(args.slot)
    else:
        slot = latest_slot()

    print(f"Target slot: {slot.isoformat()}")
    pdf_bytes, used_slot, used_url = fetch_pdf(slot, allow_fallback=not args.no_fallback)
    print(f"Fetched {len(pdf_bytes)} bytes from {used_url}")

    report = parse_pdf(pdf_bytes)
    report["fetched_url"] = used_url
    report["fetched_slot"] = used_slot.isoformat()
    report["polled_at_utc"] = polled_at_utc.isoformat()

    teams_by_abbr = collapse_team_view(report)
    print(f"Teams in report: {sorted(teams_by_abbr.keys())}")

    # Load previous state
    prev_state = load_json(TEAM_STATE_PATH, {})
    prev_changes = load_json(CHANGES_LOG_PATH, [])

    # Compute changes
    changes = []
    for abbr, current in teams_by_abbr.items():
        # Skip diffing if NOT YET SUBMITTED — non-update, preserve prev state
        if not current["submitted"]:
            continue
        prev = prev_state.get(abbr)
        team_changes = diff_team(prev, {**current, "team_abbr": abbr}, polled_at_utc.isoformat())
        changes.extend(team_changes)

    print(f"Detected {len(changes)} changes")
    for c in changes:
        print(f"  - {format_change_md(c).replace('**', '')}")

    # Update team state for teams that submitted in this report.
    # Teams not in the report (or NOT YET SUBMITTED) keep their prior state.
    new_state = dict(prev_state)
    for abbr, current in teams_by_abbr.items():
        if not current["submitted"]:
            # Mark that we saw them but they hadn't submitted yet
            if abbr in new_state:
                new_state[abbr]["last_seen_pending_ts"] = polled_at_utc.isoformat()
            continue
        new_state[abbr] = {
            "team_full": current["team_full"],
            "submitted": True,
            "last_submitted_ts": polled_at_utc.isoformat(),
            "last_submitted_report_url": used_url,
            "game_dates": current.get("game_dates", []),
            "matchups": current.get("matchups", []),
            "players": current["players"],
        }

    if args.dry_run:
        print("--dry-run: skipping writes")
        write_job_summary(changes, polled_at_utc, report, used_url)
        return 0

    # Write outputs
    save_json(TEAM_STATE_PATH, new_state)
    save_json(CURRENT_REPORT_PATH, report)
    if changes:
        prev_changes.extend(changes)
        prev_changes = prev_changes[-2000:]  # cap to last 2000 events
        save_json(CHANGES_LOG_PATH, prev_changes)
        prepend_change_block(changes, polled_at_utc)

    # Live JSON snapshot for the dashboard (compact, contains everything needed)
    save_json(LIVE_JSON_PATH, build_live_json(
        new_state, prev_changes, polled_at_utc, used_url, report.get("report_ts")
    ))

    write_job_summary(changes, polled_at_utc, report, used_url)
    return 0


def build_live_json(team_state, changes_log, polled_at_utc, used_url, report_ts):
    """
    Compact dashboard payload. Includes:
      - per-team currently-listed players (with staleness)
      - flat list of currently injured/listed players (sorted, useful for filters)
      - recent changes (newest first, capped)
    """
    # Flat list of all currently listed players across all teams
    flat = []
    for abbr, info in team_state.items():
        last_ts = info.get("last_submitted_ts")
        for p in info.get("players", []):
            flat.append({
                "name": p["name"],
                "team_abbr": abbr,
                "team_full": info.get("team_full"),
                "status": p["status"],
                "reason": p["reason"],
                "reason_category": p.get("reason_category", "unknown"),
                "last_updated": last_ts,
            })
    # Sort by status severity (Out first), then name
    severity = {"Out": 0, "Doubtful": 1, "Questionable": 2, "Probable": 3, "Available": 4}
    flat.sort(key=lambda r: (severity.get(r["status"], 99), r["name"]))

    # Per-team grouping
    teams_view = []
    for abbr, info in sorted(team_state.items()):
        teams_view.append({
            "team_abbr": abbr,
            "team_full": info.get("team_full"),
            "last_submitted_ts": info.get("last_submitted_ts"),
            "last_seen_pending_ts": info.get("last_seen_pending_ts"),
            "matchups": info.get("matchups", []),
            "players": info.get("players", []),
        })

    # Recent changes — newest first, last 250
    recent = list(reversed(changes_log[-250:])) if changes_log else []

    return {
        "schema_version": 1,
        "polled_at_utc": polled_at_utc.isoformat(),
        "fetched_url": used_url,
        "report_ts": report_ts,
        "all_players": flat,
        "teams": teams_view,
        "recent_changes": recent,
    }


if __name__ == "__main__":
    sys.exit(main())
