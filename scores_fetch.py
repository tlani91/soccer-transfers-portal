#!/usr/bin/env python3
"""
scores_fetch.py -- fixtures and results for seven European leagues.

One request covers all seven, so this is cheap enough to run every 30 minutes
alongside the rumour fetch. Writes scores_data.json.

    export FOOTBALL_DATA_KEY=your_key
    python3 scores_fetch.py --dry-run     # print, write nothing
    python3 scores_fetch.py               # write scores_data.json

The key comes from the environment. NEVER hardcode it -- this repo is public
and a committed key stays in git history even after it's deleted. In Actions
it comes from a repository secret.

Standard library only, to match the other fetchers.

Note: free tier scores are delayed, so this is a results-and-fixtures page,
not a live ticker. Matches in play are flagged but their scores may lag.
"""

import argparse
import gzip
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = "https://api.football-data.org/v4"
OUTPUT = "scores_data.json"

# Free-tier competition codes. Belgium isn't available on this plan.
# Display names are ours -- the API calls LaLiga "Primera Division".
LEAGUES = [
    ("PL",  "Premier League"),
    ("PD",  "LaLiga"),
    ("SA",  "Serie A"),
    ("BL1", "Bundesliga"),
    ("FL1", "Ligue 1"),
    ("DED", "Eredivisie"),
    ("PPL", "Primeira Liga"),
]

# The API rejects any range longer than 10 days, so results and fixtures are
# fetched as two separate windows. Two requests per refresh is still trivial
# against a 10-per-minute limit, and it buys a much wider span than one
# cramped window would.
DAYS_BACK = 9      # results: today-9 .. today
DAYS_AHEAD = 10    # fixtures: today+1 .. today+10

# The API's status vocabulary, collapsed into what the page actually shows.
STATE = {
    "FINISHED":  "finished",
    "AWARDED":   "finished",
    "IN_PLAY":   "live",
    "PAUSED":    "live",
    "TIMED":     "upcoming",
    "SCHEDULED": "upcoming",
    "POSTPONED": "off",
    "SUSPENDED": "off",
    "CANCELLED": "off",
}


def get(path, key):
    req = urllib.request.Request(
        BASE + path,
        headers={"X-Auth-Token": key, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return json.loads(raw.decode("utf-8"))


def side(team):
    """Trim a team object down to what the page renders."""
    return {
        "name": team.get("shortName") or team.get("name") or "",
        "full": team.get("name") or "",
        "tla": team.get("tla") or "",
        "crest": team.get("crest") or "",
    }


def shape(match, names):
    utc = match.get("utcDate") or ""
    try:
        epoch = int(datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None

    score = match.get("score") or {}
    full = score.get("fullTime") or {}
    half = score.get("halfTime") or {}
    code = (match.get("competition") or {}).get("code", "")

    return {
        "id": match.get("id"),
        "league_id": code,
        "league": names.get(code, (match.get("competition") or {}).get("name", "")),
        "matchday": match.get("matchday"),
        "utc": utc,
        "epoch": epoch,
        "date": utc[:10],
        "status": match.get("status"),
        "state": STATE.get(match.get("status"), "upcoming"),
        "home": side(match.get("homeTeam") or {}),
        "away": side(match.get("awayTeam") or {}),
        "ft": [full.get("home"), full.get("away")],
        "ht": [half.get("home"), half.get("away")],
        "winner": score.get("winner"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print a summary, write nothing")
    args = ap.parse_args()

    key = os.environ.get("FOOTBALL_DATA_KEY", "").strip()
    if not key:
        sys.exit("FOOTBALL_DATA_KEY is not set.")

    names = dict(LEAGUES)
    today = date.today()
    codes = ",".join(code for code, _ in LEAGUES)

    windows = [
        ((today - timedelta(days=DAYS_BACK)).isoformat(), today.isoformat()),
        ((today + timedelta(days=1)).isoformat(),
         (today + timedelta(days=DAYS_AHEAD)).isoformat()),
    ]

    raw = []
    for frm, to in windows:
        try:
            data = get(f"/matches?competitions={codes}&dateFrom={frm}&dateTo={to}", key)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            sys.exit(f"[error] HTTP {e.code} for {frm}..{to}: {body}")
        except Exception as e:
            sys.exit(f"[error] {type(e).__name__} for {frm}..{to}: {e}")
        raw += data.get("matches", [])

    frm, to = windows[0][0], windows[1][1]

    # Windows shouldn't overlap, but dedupe on id so they safely could.
    seen = {}
    for m in raw:
        shaped = shape(m, names)
        if shaped:
            seen[shaped["id"]] = shaped
    matches = sorted(seen.values(), key=lambda m: (m["epoch"], m["league"]))

    by_league = {}
    by_state = {}
    for m in matches:
        by_league[m["league"]] = by_league.get(m["league"], 0) + 1
        by_state[m["state"]] = by_state.get(m["state"], 0) + 1

    print(f"[ok] {len(matches)} matches from {frm} to {to}", file=sys.stderr)
    for code, name in LEAGUES:
        print(f"   {name:16s} {by_league.get(name, 0):3d}", file=sys.stderr)
    for state in ("finished", "live", "upcoming", "off"):
        print(f"   {state:10s} {by_state.get(state, 0):3d}", file=sys.stderr)

    if args.dry_run:
        print(f"\n{'DATE':<12}{'TIME':<7}{'LEAGUE':<16}"
              f"{'HOME':<22}{'':<7}{'AWAY':<22}STATE")
        print("-" * 96)
        for m in matches[:40]:
            ft = (f"{m['ft'][0]}-{m['ft'][1]}"
                  if m["ft"][0] is not None else "  v  ")
            print(f"{m['date']:<12}{m['utc'][11:16]:<7}{m['league'][:15]:<16}"
                  f"{m['home']['name'][:21]:<22}{ft:^7}"
                  f"{m['away']['name'][:21]:<22}{m['state']}")
        if len(matches) > 40:
            print(f"... and {len(matches) - 40} more")
        return

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "date_from": frm,
        "date_to": to,
        "source": "football-data.org",
        "leagues": [{"id": c, "name": n} for c, n in LEAGUES],
        "matches": matches,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[written] {OUTPUT} ({len(matches)} matches)", file=sys.stderr)


if __name__ == "__main__":
    main()
