#!/usr/bin/env python3
"""
Transfer Rumors Wire — fetcher + local server

Pulls soccer transfer content from:
  - ESPN FC's soccer RSS feed (direct, filtered to transfer-flavored stories)
  - Google News, queried for articles that reference/report on:
      Fabrizio Romano, David Ornstein, Ben Jacobs, Indy Kaila, Deadline Day Live

Why Google News instead of X directly: X's API no longer has a free read
tier (pay-per-read since Feb 2026), so this script tracks what gets
*reported* about these journalists' posts rather than pulling tweets
themselves. It's not a perfect substitute for their live feeds, but it's
free and needs no API keys.

Usage:
  python3 transfer_rumors_fetch.py                  # run forever, refresh every 15 min
  python3 transfer_rumors_fetch.py --interval 600    # refresh every 10 min
  python3 transfer_rumors_fetch.py --once            # single fetch, no server, then exit
  python3 transfer_rumors_fetch.py --port 8888       # use a different local port

No third-party packages required — standard library only.
"""

import argparse
import html as html_lib
import json
import os
import re
import sys
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "rumors_data.json")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

NS = {"dc": "http://purl.org/dc/elements/1.1/"}

# Keywords used to keep general feeds (ESPN, Flashscore) limited to transfer-flavored stories
TRANSFER_KEYWORDS = [
    "transfer", "sign", "signing", "signs", "signed", "loan", "medical",
    "here we go", "deal", "bid", "fee", "valuation", "release clause",
    "contract", "target", "linked", "join", "move to", "agent", "rumour",
    "rumor", "exclusive", "swap", "agree", "confirm", "official", "unveil",
    "announce", "complete", "verbal agreement", "reject", "reportedly",
]

FEEDS = [
    {"id": "espn_fc", "name": "ESPN FC", "kind": "google_news",
     "query": "site:espn.com transfer", "filter": True},
    {"id": "romano", "name": "Fabrizio Romano", "kind": "google_news",
     "query": '"Fabrizio Romano" transfer'},
    {"id": "ornstein", "name": "David Ornstein", "kind": "google_news",
     "query": '"David Ornstein" transfer'},
    {"id": "jacobs", "name": "Ben Jacobs", "kind": "google_news",
     "query": '"Ben Jacobs" football transfer'},
    {"id": "ddl", "name": "Deadline Day Live", "kind": "site",
     "feed_url": "https://www.transfernewslive.com/transfer-news/feed/",
     "fallback_urls": [
         "https://www.transfernewslive.com/transfer-news/",
         "https://www.transfernewslive.com/transfer-news/page/2/",
     ],
     "scraper": "ddl",
     "google_fallback_query": "site:transfernewslive.com transfer"},
    {"id": "flashscore", "name": "Flashscore", "kind": "site",
     "fallback_urls": [
         "https://www.flashscore.com/news/transfer-news/C8OAR7gMWSzc94ws/",
     ],
     "scraper": "flashscore",
     "filter": True,
     "google_fallback_query": "site:flashscore.com transfer"},
]


def fetch_url(url, timeout=15):
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        status = resp.status
        content_type = resp.headers.get("Content-Type", "")
    return raw, status, content_type


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_rss(xml_bytes):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items
    for item in root.findall(".//item"):
        title = strip_html(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        description = strip_html(item.findtext("description") or "")
        creator = strip_html(item.findtext("dc:creator", namespaces=NS) or "")
        items.append({
            "title": title, "link": link, "pubDate": pub_date,
            "description": description, "creator": creator,
        })
    return items


def to_epoch(pub_date):
    try:
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0


def matches_keywords(text, keywords):
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def _scrape_articles_by_pattern(html_bytes, link_pattern, source_label, base_url=""):
    """Generic scraper: finds article links matching link_pattern, dedupes,
    and assigns descending synthetic timestamps to preserve page order.
    Exact per-article publish times aren't reliably present in scraped
    markup, so this doesn't claim false precision on timing -- just order.

    Handles both quote styles (href="..." and href='...') and both relative
    (/path/) and absolute (https://site.com/path/) link forms -- relative
    links get normalized to full URLs using base_url so they're still
    clickable from the dashboard, which is served from a different origin."""
    html_text = html_bytes.decode("utf-8", errors="replace")
    pattern = re.compile(
        r'<a[^>]+href=["\'](' + link_pattern + r')["\'][^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    seen = {}
    order = []
    for href, inner in pattern.findall(html_text):
        text = strip_html(inner)
        if len(text) < 8:  # skips empty/image-only anchor matches
            continue
        full_href = href if href.startswith("http") else base_url + href
        if full_href not in seen or len(text) > len(seen[full_href]):
            seen[full_href] = text
        if full_href not in order:
            order.append(full_href)

    now = time.time()
    items = []
    for i, href in enumerate(order):
        items.append({
            "title": seen[href],
            "link": href,
            "description": "",
            "approx_time": True,  # order-derived timestamp, not a real publish date --
                                   # some pages (e.g. Flashscore) mix old and new stories
                                   # in a way that doesn't correlate with page position
            "creator": source_label,
            "pubDate": "",
            "_epoch_override": now - (i * 300),  # 5 min apart, preserves page order
        })
    return items


def scrape_transfer_news_live(html_bytes):
    """Fallback for transfernewslive.com if their RSS feed isn't reachable.
    Pulls (title, link) pairs straight off the /transfer-news/ archive page."""
    return _scrape_articles_by_pattern(
        html_bytes,
        r"https://www\.transfernewslive\.com/transfer-news/[a-z0-9\-]+/",
        "Transfer News Live",
    )


def scrape_flashscore(html_bytes):
    """Scraper for flashscore.com's transfer news page. Their article URLs
    look like /news/<slug>/<8-char-id>/ -- the 8-char length is what
    distinguishes real articles from category/nav pages, which use a
    longer 16-char id (e.g. the Transfer News category page itself).
    Their internal links appear to be relative (no domain prefix), so
    matches get normalized to full URLs via base_url."""
    return _scrape_articles_by_pattern(
        html_bytes,
        r"(?:https://www\.flashscore\.com)?/news/[a-z0-9\-]+/[A-Za-z0-9]{8}/",
        "Flashscore",
        base_url="https://www.flashscore.com",
    )


SCRAPERS = {
    "ddl": scrape_transfer_news_live,
    "flashscore": scrape_flashscore,
}


def _fetch_rss_items(url, name, quiet=False):
    try:
        raw, status, content_type = fetch_url(url)
    except HTTPError as e:
        if not quiet:
            print(f"  [warn] {name}: fetch failed (HTTP {e.code} {e.reason})", file=sys.stderr)
        return []
    except (URLError, TimeoutError) as e:
        if not quiet:
            print(f"  [warn] {name}: fetch failed ({e})", file=sys.stderr)
        return []

    raw_items = parse_rss(raw)
    if not raw_items and not quiet:
        preview = raw[:200].decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)[:200]
        print(f"  [warn] {name}: HTTP {status}, Content-Type: {content_type!r}, "
              f"but found 0 <item> entries. Response starts with: {preview!r}", file=sys.stderr)
    return raw_items


def _fetch_scrape_items(urls, name, scraper_fn):
    all_items = []
    seen_links = set()
    for url in urls:
        try:
            raw, status, content_type = fetch_url(url)
        except (HTTPError, URLError, TimeoutError) as e:
            print(f"  [warn] {name}: fallback scrape fetch failed for {url} ({e})", file=sys.stderr)
            continue

        page_items = scraper_fn(raw)
        if not page_items:
            preview = raw[:300].decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)[:300]
            print(f"  [warn] {name}: {url} -> HTTP {status}, Content-Type: {content_type!r}, "
                  f"body length {len(raw)}, but matched 0 article links. "
                  f"Response starts with: {preview!r}", file=sys.stderr)

        for it in page_items:
            if it["link"] in seen_links:
                continue
            seen_links.add(it["link"])
            all_items.append(it)
        time.sleep(0.5)  # be polite between page requests

    if not all_items:
        print(f"  [warn] {name}: fallback scrape found 0 articles across {len(urls)} page(s)", file=sys.stderr)
        return []

    # Re-assign synthetic epochs across the *combined, already-ordered* list so
    # page 2's articles consistently sort behind page 1's rather than each
    # page resetting its own clock.
    now = time.time()
    for i, it in enumerate(all_items):
        it["_epoch_override"] = now - (i * 300)
    return all_items


def _google_news_url(query):
    return (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=en-GB&gl=GB&ceid=GB:en"
    )


def fetch_feed(feed):
    kind = feed["kind"]
    if kind == "rss":
        raw_items = _fetch_rss_items(feed["url"], feed["name"])
    elif kind == "google_news":
        raw_items = _fetch_rss_items(_google_news_url(feed["query"]), feed["name"])
    elif kind == "site":
        raw_items = []
        if feed.get("feed_url"):
            raw_items = _fetch_rss_items(feed["feed_url"], feed["name"], quiet=True)
        if not raw_items:
            print(f"  [note] {feed['name']}: RSS feed unavailable, "
                  f"scraping {len(feed['fallback_urls'])} page(s) instead", file=sys.stderr)
            scraper_fn = SCRAPERS[feed.get("scraper", "ddl")]
            raw_items = _fetch_scrape_items(feed["fallback_urls"], feed["name"], scraper_fn)
        if not raw_items and feed.get("google_fallback_query"):
            print(f"  [note] {feed['name']}: direct scrape also unavailable "
                  f"(likely blocked from this network), falling back to Google News", file=sys.stderr)
            raw_items = _fetch_rss_items(_google_news_url(feed["google_fallback_query"]), feed["name"])
    else:
        raw_items = []

    if not raw_items:
        return []

    selected = raw_items
    if feed.get("filter"):
        filtered = [
            it for it in raw_items
            if matches_keywords(it["title"] + " " + it["description"], TRANSFER_KEYWORDS)
        ]
        if filtered:
            selected = filtered
        else:
            print(f"  [note] {feed['name']}: keyword filter matched 0 of {len(raw_items)} items "
                  f"this cycle -- showing unfiltered items instead so the section isn't empty",
                  file=sys.stderr)
            selected = raw_items

    out = []
    for it in selected:
        epoch = it.get("_epoch_override")
        if epoch is None:
            epoch = to_epoch(it["pubDate"])
        out.append({
            "title": it["title"],
            "link": it["link"],
            "description": it["description"][:280],
            "source_id": feed["id"],
            "source_name": feed["name"],
            "byline": it["creator"],
            "pub_date_raw": it["pubDate"],
            "epoch": epoch,
            "approx_time": it.get("approx_time", False),
        })
    return out


def run_fetch_cycle():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching feeds...")
    all_items = []
    for feed in FEEDS:
        items = fetch_feed(feed)
        print(f"  {feed['name']}: {len(items)} items")
        all_items.extend(items)
        time.sleep(0.5)  # be polite between requests

    # Dedupe by link, keep first occurrence
    seen_links = set()
    deduped = []
    for it in sorted(all_items, key=lambda x: x["epoch"], reverse=True):
        key = it["link"] or it["title"].lower()
        if key in seen_links:
            continue
        seen_links.add(key)
        deduped.append(it)

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "items": deduped,
    }
    with open(DATA_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  -> wrote {len(deduped)} items to {os.path.basename(DATA_FILE)}")


def serve_dashboard(port):
    handler = partial(SimpleHTTPRequestHandler, directory=SCRIPT_DIR)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def main():
    parser = argparse.ArgumentParser(description="Transfer rumors fetcher + local dashboard server")
    parser.add_argument("--interval", type=int, default=900, help="Seconds between fetches (default 900 = 15 min)")
    parser.add_argument("--port", type=int, default=8420, help="Local server port (default 8420)")
    parser.add_argument("--once", action="store_true", help="Fetch once and exit, no server")
    args = parser.parse_args()

    if args.once:
        run_fetch_cycle()
        return

    run_fetch_cycle()  # populate data before first page load

    httpd = serve_dashboard(args.port)
    url = f"http://127.0.0.1:{args.port}/dashboard.html"
    print(f"\nDashboard running at {url}")
    print(f"Refreshing every {args.interval} seconds. Press Ctrl+C to stop.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        while True:
            time.sleep(args.interval)
            run_fetch_cycle()
    except KeyboardInterrupt:
        print("\nStopping...")
        httpd.shutdown()


if __name__ == "__main__":
    main()
