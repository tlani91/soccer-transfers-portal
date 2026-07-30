#!/bin/bash
#
# run_besoccer_daily.sh -- daily completed-transfers fetch, run by launchd.
#
# Does the whole cycle: pull (the rumour bot commits every 30 min, so the
# local clone is always behind), fetch, sanity-check, commit, push.
# If the fetch comes back empty or malformed, the previous good
# transfers_data.json is restored and nothing is pushed.
#
# Test by hand before letting launchd near it:
#     bash run_besoccer_daily.sh
#

set -uo pipefail

REPO="/Users/tlaniado/Projects/soccer-transfers-portal"
LOCK="/tmp/transferwire-besoccer.lock"

# launchd does NOT read .zshrc, so PATH must be set explicitly or
# python3 and git won't be found.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Don't let a slow run overlap the next one.
if ! mkdir "$LOCK" 2>/dev/null; then
  log "another run is already in progress, skipping"
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

cd "$REPO" || { log "FAIL: repo not found at $REPO"; exit 1; }

log "=== daily BeSoccer fetch ==="

# The rumour workflow commits rumors_data.json every 30 minutes, so a push
# from here will be rejected unless we rebase onto it first.
if ! git pull --rebase --quiet; then
  log "FAIL: git pull --rebase failed (conflict or no network)"
  exit 1
fi

if ! python3 besoccer_fetch.py; then
  log "FAIL: besoccer_fetch.py errored"
  git checkout -- transfers_data.json 2>/dev/null
  exit 1
fi

# The fetcher exits 0 even when it parses nothing, so check the output
# before it can overwrite good data.
if ! python3 - <<'PY'
import json, sys

with open('transfers_data.json') as f:
    data = json.load(f)

items = data.get('items', [])
n = len(items)
no_club = sum(1 for i in items if not i.get('other_club'))
no_date = sum(1 for i in items if not i.get('date'))
print(f'{n} items | {no_club} missing a club | {no_date} missing a date')

if n < 20:
    sys.exit(f'FAIL: only {n} items. Fetch is broken or being challenged.')
if no_club / n > 0.3:
    sys.exit(f'FAIL: {no_club}/{n} rows have no opposing club.')
if no_date:
    sys.exit(f'FAIL: {no_date} rows have no date.')
print('Looks healthy.')
PY
then
  log "FAIL: sanity check rejected the fetch, restoring previous data"
  git checkout -- transfers_data.json
  exit 1
fi

if git diff --quiet -- transfers_data.json; then
  log "no change today, nothing to commit"
  log "=== done ==="
  exit 0
fi

git add transfers_data.json
git commit -q -m "Update completed transfers ($(date -u '+%Y-%m-%d'))"

if git push --quiet; then
  log "pushed"
else
  log "FAIL: git push failed (credentials or network)"
  exit 1
fi

log "=== done ==="
