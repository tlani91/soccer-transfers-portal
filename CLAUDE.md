# Transfer Wire

A three-page static football site on GitHub Pages. No build step, no framework,
no dependencies. Vanilla HTML/CSS/JS front end, Python standard library back end.

Live: https://tlani91.github.io/soccer-transfers-portal/

## Layout

    dashboard.html            Rumour wire (live feed, refreshes client-side every 60s)
    transfers.html            Completed deals ledger (top 5 leagues, current transfer window)
    scores.html               Fixtures and results (7 leagues, 9 days back to 10 ahead)
    index.html                Redirect to dashboard.html so the Pages root doesn't 404
    rumors_data.json          Written by transfer_rumors_fetch.py
    transfers_data.json       Written by besoccer_fetch.py
    scores_data.json          Written by scores_fetch.py
    transfer_rumors_fetch.py  Rumour sources -> rumors_data.json
    besoccer_fetch.py         BeSoccer -> transfers_data.json
    scores_fetch.py           football-data.org -> scores_data.json
    run_besoccer_daily.sh     launchd wrapper: pull, fetch, check, commit, push
    com.tlaniado.transferwire.besoccer.plist   Backup copy; live one is in ~/Library/LaunchAgents
    .github/workflows/        Rumour workflow and scores workflow

All HTML and JSON live flat in the repo root. The pages fetch their JSON as
siblings, so nothing here can move into a subfolder.

## Three pipelines, deliberately different

**Rumours** run in GitHub Actions every 30 minutes, triggered externally by
cron-job.org POSTing to the workflow dispatch API. GitHub's native `schedule:`
cron is unreliable and is not used.

**Completed deals** run on the Mac at 07:15 daily via launchd, NOT in Actions.
BeSoccer serves datacenter IPs a JavaScript bot challenge (`<title>Client
Challenge</title>`, ~3KB page), so Actions runners get nothing. See hard rules.

**Scores** run in Actions hourly, same cron-job.org dispatch pattern. Two API
requests per run against football-data.org, which is a licensed API with no
bot challenge, so Actions is fine here.

## Commands

    python3 besoccer_fetch.py --dry-run --league premier_league   # parse check, writes nothing
    python3 besoccer_fetch.py --dry-run --since 2026-06-01        # override the window start
    python3 besoccer_fetch.py --show-html --league premier_league # dump raw row markup
    python3 besoccer_fetch.py                                     # write transfers_data.json
    bash run_besoccer_daily.sh                                    # full daily cycle by hand

    export FOOTBALL_DATA_KEY=...        # never commit this; session only
    python3 scores_fetch.py --dry-run   # print the fixture table, writes nothing
    python3 scores_fetch.py             # write scores_data.json

    python3 -m http.server 8000         # serve locally; pages need HTTP, not file://

    launchctl kickstart -k gui/$(id -u)/com.tlaniado.transferwire.besoccer
    cat ~/Library/Logs/transferwire-besoccer.log

Always run `--dry-run` and eyeball the output before letting a parser change
write to a JSON file.

## Hard rules

1. **Never work around BeSoccer's bot challenge.** No headless browsers, no
   challenge solvers, no residential proxies, no rotating IPs. If the challenge
   starts reaching the home IP too, the answer is to switch to a licensed API
   (API-Football's transfers endpoint is the identified candidate), not to
   defeat the check.
2. **Keep the honest User-Agent.** BeSoccer's 406 was a missing `Accept`
   header, not the User-Agent. Requests identify as
   `TransferWire/1.0 (+https://tlani91.github.io/soccer-transfers-portal/)`.
   Do not reintroduce browser impersonation.
3. **Attribution stays.** Every ledger row links to its BeSoccer player page and
   the sidebar credits BeSoccer; the scores page credits football-data.org.
   Brief attributed snippets that link back, never wholesale republication.
4. **The API key never enters the repo.** `FOOTBALL_DATA_KEY` is an Actions
   secret in CI and a session env var locally. A committed key stays in git
   history even after deletion, and this repo is public.
5. **Standard library only** in the Python. No pip installs.
6. **No ads or monetization** without a legal review first. BeSoccer licenses
   its data commercially and is an EU company, so database rights apply on top
   of copyright — and football-data.org's free tier is non-commercial use only,
   so ads would require a paid plan there as well.

## Conventions

- Design system is shared across all three pages: same CSS variables, Archivo
  (variable width) for display, IBM Plex Mono for metadata, same two-column
  desktop shell at `min-width: 1000px`.
- Every page carries the `Transfer Wire` wordmark. The nav shows which page
  you're on, not the `<h1>`. Adding a page means updating the nav in all three.
- `--gutter` drives horizontal padding at all breakpoints.
- Sidebar is `display: contents` below 1000px so the filter bar can stick to the
  page rather than the header block. Don't give `.side` a background or padding
  at mobile widths.
- Signal colours are functional, never decorative. Wire: green/blue/red/amber by
  rumour credibility. Ledger: amber for paid, blue for loans, grey for frees.
  Scores: green for live, red for postponed, nothing else.
- No club crests anywhere. The pages are deliberately typographic, and crests
  are trademarks — displaying via a licensed CDN is defensible, copying them
  into the repo is not. Rejected once already; don't reintroduce.

## Gotchas

- **Always `git pull --rebase` before pushing.** The rumour workflow commits
  every 30 minutes, so the local clone is stale within the hour and pushes get
  rejected constantly.
- **The repo must stay out of `~/Desktop`.** macOS TCC blocks launchd agents
  from reading protected folders, and background agents can't show a permission
  prompt — they just fail with "Operation not permitted".
- **launchd doesn't read `.zshrc`.** `run_besoccer_daily.sh` sets `PATH`
  explicitly. Keep it that way.
- **The daily job aborts if the working tree is dirty.** `git pull --rebase`
  refuses to run over uncommitted changes, which has silently killed a morning
  run before. `--autostash` is the fix — verify it's on that command.
- **`besoccer_fetch.py` rewrites `last_updated` every run**, so the JSON always
  differs and the daily commit always fires, even when no transfer changed.
- **The scores workflow deliberately does NOT do that.** It diffs
  `scores_data.json` ignoring `last_updated` and skips the commit when only the
  timestamp moved, so quiet weekdays produce no commits at all. That is working
  correctly, not stalling.
- **The plist in the repo is a backup.** launchd only reads
  `~/Library/LaunchAgents/`. Edit there, then copy across.
- **The ledger window is derived, not stored.** `current_window()` returns
  1 Jan or 1 Jul of the current year, so it rolls over on its own and the
  ledger grows across a window instead of ageing rows out. Nothing to
  maintain; `--since` overrides it for testing.
- **Pre-agreed moves dated ahead of today are dropped.** BeSoccer lists
  deals that haven't happened yet (a Jul 2027 free, a Sep 2026 loan); they
  sorted above everything and the date badge has no year, so they read as a
  broken sort. The horizon carries one day of slack because BeSoccer dates
  in European local time.
- **BeSoccer's club pages only carry the current season**, roughly back to
  the January window, so a `--since` earlier than that silently returns
  whatever the page happens to hold rather than a complete history.
- **Intra-league moves appear twice**, once per club. `dedupe()` keys on
  (player_url, date) and keeps the incoming record.
- **The opposing club is in an image `alt` attribute**, not a text node.
  `flatten()` preserves alt text with control-character markers before stripping
  tags; naive tag-stripping loses every club name.
- **`besoccer_fetch.py` exits 0 even when it parses nothing.** The sanity check
  in `run_besoccer_daily.sh` is what stops an empty file overwriting good data.
  Don't remove it.
- **football-data.org rejects any date range over 10 days.** `scores_fetch.py`
  therefore makes two requests, results and fixtures, and merges them.
- **Kickoffs are stored in UTC but grouped by the reader's local day**, so a
  20:00 UTC Saturday match doesn't land on Sunday for a European reader.
  Group on `epoch`, never on the `date` string.
- **Bundesliga showing 0 matches is usually correct** — they start later than
  the other leagues, so an August window can legitimately be empty. The scores
  sanity check fails on malformed output, not on low counts, because a check
  that cries wolf every close season is worse than no check.

## Open items

- `transfer_rumors_fetch.py` still sends a spoofed Chrome User-Agent. Swap it
  for the honest string plus proper `Accept` headers, then verify each source
  (Google News RSS, transfernewslive, Flashscore) still returns rows.
- The rumour pipeline has no sanity check before it commits, unlike the other
  two. A source silently returning zero rows would thin the feed without
  failing anything.
- Custom domain, needed before AdSense would be plausible and for SEO.
- Belgium is missing from the scores page. Not on football-data.org's free
  tier at any point; would need a different provider.
- ~6% of ledger rows have no opposing club. Probably genuine gaps in BeSoccer's
  data; unverified.
