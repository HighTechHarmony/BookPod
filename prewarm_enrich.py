#!/usr/bin/env python3
"""Pre-warm the on-disk enrichment cache for every book in the feed.

The web app falls back to this disk cache (see ``enrich.py``) when OpenLibrary
is unreachable, so books enriched here keep showing their genres/synopsis even
during an outage. Run it once while OpenLibrary is reachable:

    .venv/bin/python prewarm_enrich.py

It skips books that already have a saved enrichment on disk, so re-running is
safe, fast, and resumable. Only successful lookups are persisted (books that
return ``not_found``/``unavailable`` write nothing).

Options:
    --feed URL|PATH   Feed to pre-warm (default: FEED_SOURCE env > config.toml)
    --cache-dir DIR   Cache directory (default: CACHE_DIR env > config.toml >
                      default_cache_dir()). Use the same dir the web app uses
                      so the app can read the results.
    --timeout SECS    Per-request timeout in seconds (default: 15, same as the
                      web app). If OpenLibrary is down, lower this (e.g. 5) so
                      the run doesn't crawl — those books simply won't be
                      cached, and you can re-run later once the service is back.
    --refresh         Force re-fetch of the feed instead of using the cached
                      copy.
    --limit N         Only pre-warm the first N books (handy for smoke tests).
"""

from __future__ import annotations

import argparse
import os
import sys

from config import load_config
from feed import Cache, default_cache_dir, load_input, parse_feed
import enrich
from enrich import enrich_book

STATUS_LABELS = {
    enrich.ENRICH_OK: "ok",
    enrich.ENRICH_NO_MATCH: "not_found",
    enrich.ENRICH_UNAVAILABLE: "unavailable",
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pre-warm the on-disk enrichment cache for every book in the feed."
    )
    ap.add_argument("--feed", default=None, help="Feed URL or local path")
    ap.add_argument("--cache-dir", default=None, help="Cache directory")
    ap.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout (s)")
    ap.add_argument("--refresh", action="store_true", help="Re-fetch the feed")
    ap.add_argument("--limit", type=int, default=0, help="Only pre-warm the first N books")
    args = ap.parse_args()

    cfg = load_config()

    # Mirror the web app's feed/cache resolution (main.py).
    feed_source = (
        args.feed or os.environ.get("FEED_SOURCE") or cfg.get("feed_url") or ""
    ).strip()
    if not feed_source:
        print(
            "error: no feed source. Pass --feed, set FEED_SOURCE, or set "
            "[feed] url in config.toml.",
            file=sys.stderr,
        )
        return 2

    cache_dir = (
        args.cache_dir or os.environ.get("CACHE_DIR") or cfg.get("cache_dir") or default_cache_dir()
    )

    # Bulk runs must not stall 15s per request when OpenLibrary is down.
    enrich.TIMEOUT = args.timeout

    # Load the feed through the same machinery the app uses (disk-cached).
    try:
        data, _source, _origin = load_input(
            feed_source, Cache(cache_dir, refresh=args.refresh)
        )
        channel_title, _img, books = parse_feed(data)
    except Exception as exc:  # noqa: BLE001 - surface a clear error
        print(f"error: could not load feed '{feed_source}': {exc}", file=sys.stderr)
        return 1

    if args.limit > 0:
        books = books[: args.limit]

    total = len(books)
    counts = {label: 0 for label in STATUS_LABELS.values()}
    counts["skipped"] = 0
    print(f"channel: {channel_title or '(untitled)'}  books: {total}  cache: {cache_dir}")
    print(f"timeout: {args.timeout}s  (feed refresh: {'yes' if args.refresh else 'no'})")

    for i, book in enumerate(books, start=1):
        title = book.get("title") or "Untitled"
        author = book.get("author") or "Unknown"

        if enrich._load_disk(cache_dir, title, author) is not None:
            counts["skipped"] += 1
            print(f"[{i}/{total}] {'skipped (already cached)':<22} - {title} - {author}")
            continue

        status, _data = enrich_book(title, author, cache_dir=cache_dir)
        label = STATUS_LABELS.get(status, status)
        counts[label] = counts.get(label, 0) + 1
        print(f"[{i}/{total}] {label:<22} - {title} - {author}")

    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    print(f"\ndone: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
