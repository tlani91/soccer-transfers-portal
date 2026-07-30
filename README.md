# Transfer Wire

A live football transfer aggregator. Two pages, no framework, no build step,
no dependencies — vanilla HTML/CSS/JS on the front end, Python standard library
on the back.

**[Rumour wire](https://tlani91.github.io/soccer-transfers-portal/dashboard.html)** ·
**[Completed deals](https://tlani91.github.io/soccer-transfers-portal/transfers.html)**

## What it does

**Rumour wire** pulls transfer reporting from ESPN FC, Fabrizio Romano, David
Ornstein, Ben Jacobs, Deadline Day Live and Flashscore into one feed. Each item
is auto-classified by how solid it looks — *here we go*, *done deal*, *medical
booked*, *denied*, *exclusive*, or plain *rumour* — and a 24-hour activity rail
shows how busy the window is right now. Refreshes every 30 minutes.

**Completed deals** is a ledger of confirmed moves across the Premier League,
LaLiga, Serie A, Bundesliga and Ligue 1 over a rolling 30-day window, with fees,
dates, and both clubs. Filter by league or deal type, sort by date or fee.
Refreshes daily.

## How it works

    transfer_rumors_fetch.py  →  rumors_data.json     →  dashboard.html
    besoccer_fetch.py         →  transfers_data.json  →  transfers.html

Both pages are static and fetch their JSON client-side. Everything is served by
GitHub Pages straight from the repo root.

The rumour fetch runs in GitHub Actions every 30 minutes, triggered externally
via the workflow dispatch API rather than Actions' own `schedule:` cron, which
is too unreliable to depend on.

The completed-deals fetch runs daily on a local machine via `launchd`, wrapped
by `run_besoccer_daily.sh`. It sanity-checks the parse before committing, so a
failed or blocked fetch restores the previous data instead of publishing an
empty page.

## Running locally

The pages fetch JSON over HTTP, so opening the files directly won't work —
serve them:

    python3 besoccer_fetch.py --dry-run --league premier_league   # check the parse
    python3 besoccer_fetch.py                                     # write the JSON
    python3 -m http.server 8000

Then open <http://localhost:8000/dashboard.html>.

Python 3 only. Nothing to install.

## Data and attribution

Rumour headlines are brief attributed snippets that link back to the publisher —
the feed is an index, not a replacement for reading the source.

Completed transfer data comes from [BeSoccer](https://www.besoccer.com/transfers).
Every row links to the corresponding BeSoccer player page, and the source is
credited on the page itself. Requests identify themselves honestly and respect
`robots.txt`.

## Status

A personal project, built in the open. Not affiliated with any of the sources
it links to.
