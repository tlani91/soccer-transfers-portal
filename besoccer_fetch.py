#!/usr/bin/env python3
"""
besoccer_fetch.py -- completed transfers for the top five leagues.

Runs ONCE A DAY. Five requests total, spaced out. Writes transfers_data.json.

    python3 besoccer_fetch.py --dry-run --league premier_league
    python3 besoccer_fetch.py --show-html --league premier_league
    python3 besoccer_fetch.py                        # write transfers_data.json

Standard library only, to match transfer_rumors_fetch.py.

HEADERS: BeSoccer returns 406 when the Accept header is missing, not because
of the User-Agent. So we send an honest, self-identifying User-Agent with
normal Accept headers. No browser impersonation needed.

PARSING: the opposing club is a crest IMAGE whose name is in its alt
attribute, so alt text must be preserved before tags are stripped or the
club vanishes. Alt values are marked with brackets so their position
relative to the date is still known after flattening.
"""

import argparse
import gzip
import html as html_mod
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── Configuration ─────────────────────────────────────────────────────

BASE = "https://www.besoccer.com/transfers/players-in-players-out/"

LEAGUES = [
    ("Premier League", "premier_league"),
    ("LaLiga",         "primera_division"),
    ("Serie A",        "serie_a"),
    ("Bundesliga",     "bundesliga"),
    ("Ligue 1",        "ligue_1"),
]

# The ledger covers a transfer window, not a rolling span of days: it starts
# on a fixed date and grows until the window shuts. A rolling window shed a
# fifth of the ledger the morning 1 Jul aged out, which is the opposite of
# what a window ledger should do.
#
# The start is derived from the date rather than pinned, so it rolls over on
# its own: the winter window on 1 Jan, the summer window on 1 Jul. Between
# windows the ledger keeps showing the one that just closed, which is what
# you want to read in October.
SUMMER_OPENS_MONTH = 7
PAUSE_SECONDS = 2
OUTPUT = "transfers_data.json"

EXCLUDE_TYPES = {"Released", "Retired"}

# Youth and reserve sides. The trailing-B rule is the loose one --
# delete it if it ever eats a real club.
YOUTH_PATTERN = re.compile(r"(\bU\d{2}\b|\bU\d{2}$|\s(B|II)$)")

# Longest first, so "Free transfer" matches before "Transfer".
TYPE_WORDS = [
    "Free transfer", "Loan return", "Contract renewal",
    "Transfer", "Loan", "Released", "Retired",
]

# alt values that are decoration, not club names.
JUNK_ALT = re.compile(r"icon|logo|badge|shield|escudo|nofoto|no-?photo|avatar|"
                      r"player|jugador|ads?|banner|arrow|flag", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

DATE_RE = re.compile(r"\b(\d{2})\s+([A-Z]{3})\s+(\d{4})\b")
FEE_RE = re.compile(r"([\d.,]+)\s*M\.\s*€")
IMG_ALT_RE = re.compile(r'<img\b[^>]*?\balt="([^"]*)"[^>]*>', re.I)
ALT_TOKEN_RE = re.compile(r"\x01([^\x02]*)\x02")   # control chars: never in real text

# The per-club "See more" link, which is what divides a league page into
# club blocks. BeSoccer localises these paths by host, so accept the
# other language variants too rather than only the English one.
CLUB_LINK_RE = re.compile(
    r'href="([^"]*/(?:team|equipo|equipe|squadra|equipa|time)'
    r'/(?:transfers|fichajes|transferts|trasferimenti|transferencias)/[^"]+)"'
)

HEADERS = {
    "User-Agent": "TransferWire/1.0 (+https://tlani91.github.io/soccer-transfers-portal/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip",
}

# ── HTTP ──────────────────────────────────────────────────────────────


def get(url, timeout=25):
    """-> (page text, final URL after any redirects)"""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw.decode("utf-8", "replace"), resp.geturl()

# ── Text helpers ──────────────────────────────────────────────────────


def flatten(fragment):
    """Strip tags, but first convert <img alt="X"> into a marked token so
    crest names survive. Markers keep alt position relative to the date."""
    marked = IMG_ALT_RE.sub(lambda m: f" \x01{m.group(1)}\x02 ", fragment)
    text = re.sub(r"<[^>]+>", " ", marked)
    return re.sub(r"[ \t]+", " ", html_mod.unescape(text)).strip()


def split_alts(text):
    """-> (visible text with alt tokens removed, [alt values in order])"""
    alts = [a.strip() for a in ALT_TOKEN_RE.findall(text)]
    plain = re.sub(r"\s+", " ", ALT_TOKEN_RE.sub(" ", text)).strip()
    return plain, alts


def undouble(name):
    """Guard against a name appearing twice ("Luis Vazquez Luis Vazquez")."""
    words = name.split()
    half = len(words) // 2
    if words and len(words) % 2 == 0 and words[:half] == words[half:]:
        return " ".join(words[:half])
    return name


def clean_alts(alts):
    return [a for a in alts if len(a) > 1 and not JUNK_ALT.search(a)]


def parse_fee(text):
    m = FEE_RE.search(text)
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(".", "").replace(",", ".")), 2)
    except ValueError:
        return None


def parse_date(day, mon, year):
    month = MONTHS.get(mon.upper())
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day), tzinfo=timezone.utc)
    except ValueError:
        return None


def is_youth(club):
    return bool(club) and bool(YOUTH_PATTERN.search(club))


def pick_club(tail, tail_alts, kind):
    """The opposing club: normally a crest alt sitting after the date."""
    good = clean_alts(tail_alts)
    if good:
        return good[0]
    parts = re.split(rf"\b{re.escape(kind)}\s*\.", tail, maxsplit=1)
    before = parts[0].strip(" .,-")
    if before:
        return before
    if len(parts) > 1:                      # club printed after the type
        rest = FEE_RE.sub("", parts[1]).strip(" .,-")
        if rest:
            return rest
    return ""

# ── Parsing ───────────────────────────────────────────────────────────


def parse_row(anchor_html, href, direction, club, club_url,
              league_name, league_id, cutoff):
    marked = flatten(anchor_html)

    m = DATE_RE.search(marked)
    if not m:
        return None
    date = parse_date(*m.groups())
    if date is None or date < cutoff:
        return None

    # Player's own photo alt can hold a stale name on lazy-loaded rows,
    # so the name comes from visible text only.
    head, _ = split_alts(marked[:m.start()])
    tail, tail_alts = split_alts(marked[m.end():])

    kind = None
    for word in TYPE_WORDS:
        if re.search(rf"\b{re.escape(word)}\s*\.", tail):
            kind = word
            break
    if kind is None:
        for word in TYPE_WORDS:
            if head.startswith(word):
                kind = word
                break
    if kind is None or kind in EXCLUDE_TYPES:
        return None

    for word in TYPE_WORDS:
        if head.startswith(word):
            head = head[len(word):].strip()
            break
    player = undouble(head)
    if not player:
        return None

    other = pick_club(tail, tail_alts, kind)
    if is_youth(club) or is_youth(other):
        return None

    return {
        "player": player,
        "player_url": href if href.startswith("http")
                      else "https://www.besoccer.com" + href,
        "league": league_name,
        "league_id": league_id,
        "club": club,
        "club_url": club_url,
        "direction": direction,
        "other_club": other,
        "type": kind,
        "fee_eur_m": parse_fee(tail),
        "date": date.strftime("%Y-%m-%d"),
        "epoch": int(date.timestamp()),
    }


def club_blocks(page):
    """Split a league page into one chunk per club."""
    marks = [m.start() for m in CLUB_LINK_RE.finditer(page)]
    bounds = marks + [len(page)]
    for i in range(len(marks)):
        yield page[max(0, bounds[i] - 1500): bounds[i + 1]]


def parse_club_block(block, league_name, league_id, cutoff):
    club_m = CLUB_LINK_RE.search(block)
    club_url = club_m.group(1) if club_m else ""
    head_m = re.search(r"<h[23][^>]*>(.*?)</h[23]>", block, re.S)
    club, _ = split_alts(flatten(head_m.group(1))) if head_m else ("", [])

    split = re.split(r"Player\s*out", block, maxsplit=1, flags=re.I)
    sections = [("in", split[0])] + ([("out", split[1])] if len(split) > 1 else [])

    rows = []
    for direction, section in sections:
        for m in re.finditer(
            r'<a\s[^>]*href="([^"]*/player/[^"]*)"[^>]*>(.*?)</a>', section, re.S
        ):
            row = parse_row(m.group(2), m.group(1), direction, club, club_url,
                            league_name, league_id, cutoff)
            if row:
                rows.append(row)
    return rows


def parse_league(page, league_name, league_id, cutoff):
    rows = []
    found_any = False
    for block in club_blocks(page):
        found_any = True
        rows += parse_club_block(block, league_name, league_id, cutoff)
    if not found_any:
        print(f"[warn] no club blocks found for {league_name}", file=sys.stderr)
    return rows

# ── Diagnostics ───────────────────────────────────────────────────────

_diagnosed = False


def diagnose(page, final_url, name):
    """Print enough to identify WHICH page we were served, when a league
    parses to nothing. Runs once per process, not once per league."""
    global _diagnosed
    if _diagnosed:
        return
    _diagnosed = True

    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S)
    lang = re.search(r'<html[^>]*\blang="([^"]*)"', page, re.I)
    title_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title.group(1))).strip() \
        if title else "?"

    print("\n" + "=" * 60, file=sys.stderr)
    print(f"[diag] {name} parsed to nothing. What did we actually get?",
          file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"[diag] requested host : www.besoccer.com", file=sys.stderr)
    print(f"[diag] final URL      : {final_url}", file=sys.stderr)
    print(f"[diag] page length    : {len(page):,} chars", file=sys.stderr)
    print(f"[diag] <html lang>    : {lang.group(1) if lang else '?'}",
          file=sys.stderr)
    print(f"[diag] <title>        : {title_text[:90]}", file=sys.stderr)

    # A real league page is hundreds of KB. Anything small is a stub,
    # a challenge, or an error page -- so just show it.
    if len(page) < 20000:
        print("[diag] page is tiny; full body follows", file=sys.stderr)
        print("-" * 60, file=sys.stderr)
        print(page[:4000], file=sys.stderr)
        print("-" * 60, file=sys.stderr)

    markers = [
        "/team/transfers/", "/equipo/fichajes/", "/equipe/transferts/",
        "/player/", "/jugador/", "/joueur/",
        "New signing", "Player out", "Altas", "Bajas",
        "Transfer.", "Traspaso", "Free transfer", "Libre",
        "captcha", "Just a moment", "cf-browser-verification",
        "Access denied", "cookie",
    ]
    print("[diag] markers present:", file=sys.stderr)
    for mark in markers:
        n = page.count(mark)
        if n:
            print(f"[diag]   {n:5d}  {mark}", file=sys.stderr)

    # The single most useful clue: what link shapes does this page contain?
    paths = {}
    for href in re.findall(r'href="(/[^"?#]*)"', page):
        parts = [p for p in href.split("/") if p][:2]
        if parts:
            key = "/" + "/".join(parts)
            paths[key] = paths.get(key, 0) + 1
    print("[diag] most common link prefixes:", file=sys.stderr)
    for key, n in sorted(paths.items(), key=lambda kv: -kv[1])[:15]:
        print(f"[diag]   {n:5d}  {key}", file=sys.stderr)
    print("=" * 60 + "\n", file=sys.stderr)

# ── Dedupe ────────────────────────────────────────────────────────────


def dedupe(rows):
    """An intra-league move is listed by both clubs. Keep the incoming
    record, which names the club the player actually joined."""
    best = {}
    for row in rows:
        key = (row["player_url"], row["date"])
        current = best.get(key)
        if current is None or (current["direction"] == "out"
                               and row["direction"] == "in"):
            best[key] = row
    return sorted(best.values(), key=lambda r: (-r["epoch"], r["player"]))

# ── Main ──────────────────────────────────────────────────────────────


def show_html(slug, count=2):
    """Dump raw markup for the first few transfer anchors, for debugging."""
    page, _ = get(BASE + slug)
    shown = 0
    for m in re.finditer(r'<a\s[^>]*href="[^"]*/player/[^"]*"[^>]*>.*?</a>',
                         page, re.S):
        chunk = m.group(0)
        if not DATE_RE.search(flatten(chunk)):
            continue
        print("=" * 64)
        print(chunk)
        print("-" * 64)
        print("flattened:", flatten(chunk).replace("\x01", "[ALT ")
                                          .replace("\x02", "]"))
        shown += 1
        if shown >= count:
            return
    print("No dated transfer anchors found.")


def current_window(now=None):
    """Start and label of the transfer window in progress, or the last one
    to close. Rolls on 1 Jan and 1 Jul so nothing needs maintaining."""
    now = now or datetime.now(timezone.utc)
    summer = now.month >= SUMMER_OPENS_MONTH
    start = datetime(now.year, SUMMER_OPENS_MONTH if summer else 1, 1,
                     tzinfo=timezone.utc)
    return start, f"{'summer' if summer else 'winter'} {now.year}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print parsed rows, write nothing")
    ap.add_argument("--show-html", action="store_true",
                    help="dump raw markup of the first rows and exit")
    ap.add_argument("--league", help="one league slug only, for testing")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d")
                                           .replace(tzinfo=timezone.utc),
                    help="override the window start "
                         f"(default {current_window()[0]:%Y-%m-%d})")
    args = ap.parse_args()

    if args.show_html:
        show_html(args.league or "premier_league")
        return

    cutoff, label = current_window()
    if args.since:
        cutoff, label = args.since, f"since {args.since:%d %b %Y}"
    targets = [(n, s) for n, s in LEAGUES if not args.league or s == args.league]
    if not targets:
        raise SystemExit(f"Unknown league: {args.league}")

    rows = []
    for i, (name, slug) in enumerate(targets):
        if i:
            time.sleep(PAUSE_SECONDS)
        try:
            page, final_url = get(BASE + slug)
        except Exception as e:
            print(f"[error] {name}: {e}", file=sys.stderr)
            continue
        found = parse_league(page, name, slug, cutoff)
        print(f"[ok] {name}: {len(found)} rows since {cutoff:%Y-%m-%d}",
              file=sys.stderr)
        if not found:
            diagnose(page, final_url, name)
        rows += found

    items = dedupe(rows)
    print(f"[total] {len(rows)} parsed -> {len(items)} after dedupe",
          file=sys.stderr)

    missing = sum(1 for r in items if not r["other_club"])
    if items and missing / len(items) > 0.2:
        print(f"[warn] {missing}/{len(items)} rows have no opposing club -- "
              f"run with --show-html to inspect the markup", file=sys.stderr)

    if args.dry_run:
        print(f"\n{'DATE':<12}{'PLAYER':<24}{'CLUB':<24}{'DIR':<5}"
              f"{'OTHER CLUB':<24}{'TYPE':<15}FEE")
        print("-" * 120)
        for r in items[:60]:
            fee = f"{r['fee_eur_m']:.1f}M" if r["fee_eur_m"] else ""
            print(f"{r['date']:<12}{r['player'][:23]:<24}{r['club'][:23]:<24}"
                  f"{r['direction']:<5}{r['other_club'][:23]:<24}"
                  f"{r['type'][:14]:<15}{fee}")
        if len(items) > 60:
            print(f"... and {len(items) - 60} more")
        return

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "window_start": cutoff.date().isoformat(),
        "window_label": label,
        "source": "BeSoccer",
        "leagues": [{"id": s, "name": n} for n, s in LEAGUES],
        "items": items,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[written] {OUTPUT} ({len(items)} items)", file=sys.stderr)


if __name__ == "__main__":
    main()
