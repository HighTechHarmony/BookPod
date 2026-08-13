#!/usr/bin/env python3
"""Generate a paged cover-grid web page from an audiobook RSS feed.

Reads a podcast/audiobook RSS feed from a local file path or an http(s) URL,
extracts each item's title, author and cover image, downloads the cover images
into a local folder, and writes a single self-contained HTML file that shows
the books as a paginated grid of cover thumbnails with title and author below.

Stdlib only - no third-party dependencies.

Remote feeds (http/https) and cover images are cached on disk so repeat runs
are fast and work offline. Every cache entry is validated by BOTH the feed
(its URL, or absolute path for local feeds) and the image URL/filename; a JSON
sidecar records the exact feed + url for each cached file.

Usage:
    python3 rss_to_html.py <input> [output_dir] [--page-size N]
                           [--no-download] [--title TEXT]
                           [--cache-dir DIR] [--refresh]

Examples:
    python3 rss_to_html.py podbook.rss
    python3 rss_to_html.py podbook.rss my_site --page-size 24
    python3 rss_to_html.py https://example.com/feed.rss --no-download
    python3 rss_to_html.py https://example.com/feed.rss --refresh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) rss-to-html/1.0"


# --------------------------------------------------------------------------
# XML helpers (namespace-agnostic: itunes tags can use http or https URIs)
# --------------------------------------------------------------------------

def localname(tag: str) -> str:
    """Strip any {namespace} prefix from an XML tag name."""
    return tag.rsplit("}", 1)[-1]


def find_child(elem: ET.Element, name: str) -> ET.Element | None:
    """Return the first direct child whose local tag name == name, or None."""
    for child in elem:
        if localname(child.tag) == name:
            return child
    return None


def find_children(elem: ET.Element, name: str) -> list[ET.Element]:
    """Return all direct children whose local tag name == name."""
    return [c for c in elem if localname(c.tag) == name]


def text_of(elem: ET.Element | None) -> str:
    """Return trimmed element text, or an empty string when absent."""
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


# --------------------------------------------------------------------------
# Feed loading
# --------------------------------------------------------------------------

def load_input(source: str, cache: "Cache") -> tuple[bytes, str, str]:
    """Load feed bytes from a local path or http(s) URL.

    URL feeds are cached on disk and reused on later runs unless --refresh.
    Returns (data, source, origin) with origin in {'file', 'fetched', 'cached'}.
    """
    if source.startswith("http://") or source.startswith("https://"):
        cached = cache.get_feed(source)
        if cached is not None:
            return cached, source, "cached"
        data, err = fetch_bytes(source, timeout=60)
        if err is not None:
            stale = cache.get_feed(source, ignore_refresh=True)  # fallback on failed refresh
            if stale is not None:
                print(
                    f"warning: refresh of '{source}' failed ({err}); using cached copy",
                    file=sys.stderr,
                )
                return stale, source, "cached"
            raise urllib.error.URLError(err)
        cache.put_feed(source, data)
        return data, source, "fetched"
    with open(source, "rb") as f:
        return f.read(), source, "file"


def parse_feed(data: bytes) -> tuple[str, str | None, list[dict]]:
    """Parse feed XML into (channel_title, channel_image, [book dicts])."""
    root = ET.fromstring(data)

    channel = None
    for child in root:
        if localname(child.tag) == "channel":
            channel = child
            break
    if channel is None:
        raise ValueError("no <channel> element found in feed")

    channel_title = text_of(find_child(channel, "title"))

    # Channel-level cover: <itunes:image href="..."/> or <image><url>...</url></image>
    channel_img = None
    for child in find_children(channel, "image"):
        href = child.attrib.get("href")
        if href:
            channel_img = href
            break
    if channel_img is None:
        img_el = find_child(channel, "image")
        url_el = find_child(img_el, "url") if img_el is not None else None
        if url_el is not None:
            channel_img = text_of(url_el)

    books: list[dict] = []
    for item_el in find_children(channel, "item"):
        title = text_of(find_child(item_el, "title")) or "Untitled"
        author = text_of(find_child(item_el, "author")) or "Unknown"

        # Item cover: <itunes:image href="..."/> (fall back to channel image)
        cover = None
        for child in find_children(item_el, "image"):
            href = child.attrib.get("href")
            if href:
                cover = href
                break
        if cover is None:
            cover = channel_img

        books.append({
            "title": title,
            "author": author,
            "cover": cover or "",
            "link": text_of(find_child(item_el, "link")) or "",
            "pubDate": text_of(find_child(item_el, "pubDate")) or "",
            "description": text_of(find_child(item_el, "description")) or "",
        })

    return channel_title, channel_img, books


# --------------------------------------------------------------------------
# Fetching & on-disk caching
# --------------------------------------------------------------------------

def fetch_bytes(url: str, timeout: int = 30) -> tuple[bytes | None, str | None]:
    """Fetch url over HTTP(S). Returns (data, None) or (None, error message)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), None
    except Exception as exc:  # noqa: BLE001 - keep the run going on any failure
        return None, str(exc)


def default_cache_dir() -> str:
    """XDG-compatible default cache location (~/.cache/audiobook_feed_library)."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "audiobook_feed_library")


class Cache:
    """On-disk cache for the RSS feed and cover images.

    Every entry is keyed by a SHA-1 of (feed key, url), so cached data is
    validated by BOTH the feed (its URL, or absolute path for local feeds) and
    the image URL/filename. A JSON sidecar records the exact feed + url and is
    cross-checked on read, so a file that does not belong to the requested
    feed/url is never served.
    """

    def __init__(self, cache_dir: str, refresh: bool = False) -> None:
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.images_dir = os.path.join(cache_dir, "images")
        self.feeds_dir = os.path.join(cache_dir, "feeds")

    @staticmethod
    def _hash(*parts: str) -> str:
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()

    # --- images ----------------------------------------------------------

    def image_files(self, feed_key: str, url: str) -> tuple[str, str]:
        key = self._hash(feed_key, url)
        return (
            os.path.join(self.images_dir, key + ".jpg"),
            os.path.join(self.images_dir, key + ".json"),
        )

    def get_image(self, feed_key: str, url: str) -> bytes | None:
        img, meta = self.image_files(feed_key, url)
        if not os.path.exists(img):
            return None
        if os.path.exists(meta):  # sidecar must match feed + url
            try:
                with open(meta, encoding="utf-8") as f:
                    record = json.load(f)
                if record.get("feed") != feed_key or record.get("url") != url:
                    return None
            except (OSError, ValueError):
                return None
        with open(img, "rb") as f:
            return f.read()

    def put_image(self, feed_key: str, url: str, data: bytes) -> None:
        img, meta = self.image_files(feed_key, url)
        os.makedirs(self.images_dir, exist_ok=True)
        with open(img, "wb") as f:
            f.write(data)
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"feed": feed_key, "url": url}, f, indent=2)

    # --- feeds -----------------------------------------------------------

    def feed_files(self, url: str) -> tuple[str, str]:
        key = self._hash(url)
        return (
            os.path.join(self.feeds_dir, key + ".rss"),
            os.path.join(self.feeds_dir, key + ".json"),
        )

    def get_feed(self, url: str, ignore_refresh: bool = False) -> bytes | None:
        feed, meta = self.feed_files(url)
        if (self.refresh and not ignore_refresh) or not os.path.exists(feed):
            return None
        if os.path.exists(meta):
            try:
                with open(meta, encoding="utf-8") as f:
                    if json.load(f).get("url") != url:
                        return None
            except (OSError, ValueError):
                return None
        with open(feed, "rb") as f:
            return f.read()

    def put_feed(self, url: str, data: bytes) -> None:
        feed, meta = self.feed_files(url)
        os.makedirs(self.feeds_dir, exist_ok=True)
        with open(feed, "wb") as f:
            f.write(data)
        with open(meta, "w", encoding="utf-8") as f:
            json.dump(
                {"url": url, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                f,
                indent=2,
            )


def cached_download(cache: Cache, feed_key: str, url: str) -> tuple[bytes | None, str]:
    """Return (image bytes, origin) where origin is 'cache' or 'network'.

    Serves from the cache unless --refresh. If a refresh download fails, falls
    back to the existing cached copy. Returns (None, error) on total failure.
    """
    cached = cache.get_image(feed_key, url)
    if cached is not None and not cache.refresh:
        return cached, "cache"
    data, err = fetch_bytes(url)
    if err is None:
        cache.put_image(feed_key, url, data)
        return data, "network"
    if cached is not None:
        return cached, "cache"  # refresh failed -> keep old copy
    return None, err


# --------------------------------------------------------------------------
# HTML generation
# --------------------------------------------------------------------------

CSS = """
:root {
  --bg: #12141c;
  --panel: #1b1f2b;
  --text: #e8eaf0;
  --muted: #9aa3b5;
  --accent: #7c8cff;
  --radius: 10px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.4;
}
header {
  position: sticky;
  top: 0;
  z-index: 10;
  background: rgba(18, 20, 28, 0.92);
  backdrop-filter: blur(6px);
  border-bottom: 1px solid #2a2f3d;
  padding: 18px 24px;
}
header h1 { margin: 0; font-size: 1.35rem; letter-spacing: 0.3px; }
header .meta { margin: 4px 0 0; font-size: 0.85rem; color: var(--muted); }
main { max-width: 1500px; margin: 0 auto; padding: 24px; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 22px 18px;
}
.card { display: flex; flex-direction: column; text-decoration: none; color: inherit; }
.card .thumb {
  display: block;
  width: 100%;
  aspect-ratio: 1 / 1;
  object-fit: cover;
  border-radius: var(--radius);
  background: #232838;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.card:hover .thumb { transform: translateY(-3px); box-shadow: 0 10px 22px rgba(0, 0, 0, 0.45); }
.card .title {
  margin-top: 10px;
  font-size: 0.9rem;
  font-weight: 600;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.card .author { margin-top: 2px; font-size: 0.78rem; color: var(--muted); }
.pagination {
  display: flex;
  justify-content: center;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: 36px 0 12px;
}
.pagination button {
  min-width: 34px;
  padding: 7px 11px;
  border: 1px solid #333a4d;
  border-radius: 7px;
  background: var(--panel);
  color: var(--text);
  font: inherit;
  font-size: 0.85rem;
  cursor: pointer;
}
.pagination button:hover:not(:disabled) { border-color: var(--accent); }
.pagination button.active { background: var(--accent); border-color: var(--accent); color: #0b0d16; font-weight: 600; }
.pagination button:disabled { opacity: 0.4; cursor: default; }
.pagination .dots { color: var(--muted); padding: 0 2px; }
footer { text-align: center; padding: 18px; color: var(--muted); font-size: 0.75rem; }
@media (max-width: 520px) {
  .grid { grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); }
}
"""

JS_TEMPLATE = """
'use strict';
const PAGE_SIZE = __PAGE_SIZE__;
const data = JSON.parse(document.getElementById('book-data').textContent);
const total = data.length;
const pageCount = () => Math.max(1, Math.ceil(total / PAGE_SIZE));
let currentPage = 1;

const grid = document.getElementById('grid');
const pagination = document.getElementById('pagination');
const meta = document.getElementById('meta');

const PLACEHOLDER = 'data:image/svg+xml;utf8,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">' +
  '<rect width="200" height="200" fill="#232838"/>' +
  '<text x="100" y="104" fill="#5a6378" font-family="sans-serif" font-size="14" text-anchor="middle">No cover</text></svg>'
);
window.PLACEHOLDER = PLACEHOLDER;

function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function pageItems() {
  const totalPages = pageCount();
  const cur = currentPage;
  const out = [];
  const push = (p) => { if (out[out.length - 1] !== p) out.push(p); };
  const pushRange = (a, b) => { for (let i = a; i <= b; i++) push(i); };
  push(1);
  if (cur > 3) out.push('\u2026');
  pushRange(Math.max(2, cur - 1), Math.min(totalPages - 1, cur + 1));
  if (cur < totalPages - 2) out.push('\u2026');
  if (totalPages > 1) push(totalPages);
  return out;
}

function renderPagination() {
  const totalPages = pageCount();
  const cur = currentPage;
  let html = '<button id="prev" ' + (cur === 1 ? 'disabled' : '') + '>&lsaquo; Prev</button>';
  const items = totalPages <= 7
    ? Array.from({ length: totalPages }, (_, i) => i + 1)
    : pageItems();
  for (const it of items) {
    if (it === '\u2026') {
      html += '<span class="dots">\u2026</span>';
    } else {
      html += '<button class="' + (it === cur ? 'active' : '') + '" data-page="' + it + '">' + it + '</button>';
    }
  }
  html += '<button id="next" ' + (cur === totalPages ? 'disabled' : '') + '>Next &rsaquo;</button>';
  pagination.innerHTML = html;
}

function render() {
  const start = (currentPage - 1) * PAGE_SIZE;
  const slice = data.slice(start, start + PAGE_SIZE);
  let cards = '';
  for (const b of slice) {
    const cover = b.cover ? esc(b.cover) : PLACEHOLDER;
    const inner =
      '<img class="thumb" loading="lazy" src="' + cover + '" alt="' + esc(b.title) + ' cover" onerror="this.onerror=null;this.src=window.PLACEHOLDER">' +
      '<span class="title">' + esc(b.title) + '</span>' +
      '<span class="author">' + esc(b.author) + '</span>';
    cards += b.link
      ? '<a class="card" href="' + esc(b.link) + '" target="_blank" rel="noopener">' + inner + '</a>'
      : '<div class="card">' + inner + '</div>';
  }
  grid.innerHTML = cards;
  renderPagination();
  meta.textContent = total + ' audiobooks \u00b7 page ' + currentPage + ' of ' + pageCount();
}

pagination.addEventListener('click', function (e) {
  const btn = e.target.closest('button');
  if (!btn || btn.disabled) return;
  const totalPages = pageCount();
  if (btn.id === 'prev') currentPage = Math.max(1, currentPage - 1);
  else if (btn.id === 'next') currentPage = Math.min(totalPages, currentPage + 1);
  else currentPage = Number(btn.dataset.page);
  render();
  window.scrollTo({ top: 0, behavior: 'smooth' });
});

render();
"""


def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_html(title: str, books: list[dict], page_size: int) -> str:
    # ensure_ascii keeps output 7-bit; escaping "</" prevents </script> breakout
    data_json = json.dumps(books, ensure_ascii=True).replace("</", "<\\/")
    js = JS_TEMPLATE.replace("__PAGE_SIZE__", str(page_size))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape_html(title)}</title>
<style>
{CSS}
</style>
</head>
<body>
<header>
  <h1>{escape_html(title)}</h1>
  <p class="meta" id="meta"></p>
</header>
<main>
  <div class="grid" id="grid"></div>
  <nav class="pagination" id="pagination"></nav>
</main>
<footer>Generated from an RSS feed by rss_to_html.py</footer>
<script type="application/json" id="book-data">{data_json}</script>
<script>
{js}
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rss_to_html.py",
        description=(
            "Generate a paged cover-grid web page from an audiobook RSS feed "
            "(local file path or http(s) URL)."
        ),
    )
    parser.add_argument(
        "input",
        help="Path to a local RSS file, or an http(s):// URL of an RSS feed.",
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output directory (default: '<input basename>_site' in the current directory).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=20,
        metavar="N",
        help="Books per page (default: 20).",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Link cover images to their original remote URLs instead of downloading them.",
    )
    parser.add_argument(
        "--title",
        default=None,
        metavar="TEXT",
        help="Override the page title (default: the feed's channel title).",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        metavar="DIR",
        help="Directory for cached feeds and cover images "
        "(default: ~/.cache/audiobook_feed_library).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached data and re-fetch the feed and cover images.",
    )
    args = parser.parse_args(argv)

    if args.page_size < 1:
        print("error: --page-size must be at least 1", file=sys.stderr)
        return 2

    cache = Cache(args.cache_dir or default_cache_dir(), refresh=args.refresh)

    # 1. Load feed
    try:
        data, source_name, origin = load_input(args.input, cache)
    except (OSError, urllib.error.URLError) as exc:
        print(f"error: could not read '{args.input}': {exc}", file=sys.stderr)
        return 1

    # Identity used for cache keys: the URL for remote feeds, abs path otherwise
    feed_key = (
        source_name
        if source_name.startswith(("http://", "https://"))
        else os.path.abspath(source_name)
    )

    # 2. Parse feed
    try:
        channel_title, _channel_img, books = parse_feed(data)
    except ET.ParseError as exc:
        print(f"error: '{args.input}' is not valid XML: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not books:
        print(f"error: no <item> entries found in feed '{args.input}'", file=sys.stderr)
        return 1

    # 3. Resolve output directory
    if args.output:
        out_dir = args.output
    else:
        base = os.path.basename(source_name).split("?", 1)[0]
        stem = os.path.splitext(base)[0] or "feed"
        out_dir = stem + "_site"
    covers_dir = os.path.join(out_dir, "covers")

    # 4. Download covers (unless --no-download); cache-backed
    downloaded = 0
    from_cache = 0
    failed: list[tuple[int, str, str]] = []
    if args.no_download:
        pass  # book["cover"] already holds the remote URL
    else:
        os.makedirs(covers_dir, exist_ok=True)
        for idx, book in enumerate(books, start=1):
            cover_url = book.get("cover", "")
            if not cover_url:
                continue
            dest = os.path.join(covers_dir, f"{idx:04d}.jpg")
            data, result = cached_download(cache, feed_key, cover_url)
            if data is None:
                failed.append((idx, cover_url, result))
                book["cover"] = ""  # render placeholder instead
                continue
            with open(dest, "wb") as f:
                f.write(data)
            if result == "cache":
                from_cache += 1
            else:
                downloaded += 1
            book["cover"] = f"covers/{idx:04d}.jpg"

    # 5. Render and write HTML
    page_title = args.title if args.title is not None else (channel_title or "Audiobooks")
    html = build_html(page_title, books, args.page_size)
    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 6. Summary
    pages = -(-len(books) // args.page_size)  # ceiling division
    print(f"Parsed {len(books)} items from {source_name}")
    if origin == "cached":
        print("Feed: used cached copy (use --refresh to update)")
    elif origin == "fetched":
        print("Feed: fetched from network and cached")
    if args.no_download:
        print("Covers: linked to original remote URLs (--no-download)")
    else:
        ok = downloaded + from_cache
        print(
            f"Covers: {ok} OK ({from_cache} from cache, {downloaded} downloaded), "
            f"failed {len(failed)}"
        )
        for idx, url, err in failed[:10]:
            print(f"  [{idx:04d}] {err}  <- {url}")
        if len(failed) > 10:
            print(f"  ... and {len(failed) - 10} more failures")
    print(f"Cache: {cache.cache_dir}")
    print(f"Wrote {html_path} (page size {args.page_size}, {pages} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
