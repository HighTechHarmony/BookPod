"""Feed loading, XML parsing, and on-disk caching for audiobook RSS feeds.

Extracted from the original rss_to_html.py CLI so the logic can be shared
between the command-line tool and the FastAPI web app. Stdlib only.

Key pieces:
- load_input(): read a feed from a local path or http(s) URL (URLs are cached).
- parse_feed(): namespace-agnostic RSS parsing into a list of book dicts.
- Cache: SHA-1 disk cache for feeds and cover images. Every entry is validated
  by BOTH the feed (its URL, or absolute path for local feeds) and the image
  URL/filename, via a hash key plus a JSON sidecar cross-check.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) audiobook-feed/1.0"


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


def desc_text(elem: ET.Element | None) -> str:
    """Return the full inner content of a <description> element.

    Podcast descriptions are usually CDATA-wrapped HTML, in which case
    ``elem.text`` already holds the entire string (tags included). When the
    HTML is instead embedded as real child elements, the inner markup is
    reconstructed so the description isn't truncated to just the leading text.
    """
    if elem is None:
        return ""
    out = elem.text or ""
    for child in elem:
        out += ET.tostring(child, encoding="unicode")
    return out.strip()


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
# Feed loading & parsing
# --------------------------------------------------------------------------

def load_input(source: str, cache: Cache) -> tuple[bytes, str, str]:
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
    """Parse feed XML into (channel_title, channel_image, [book dicts]).

    Each book dict has: title, author, cover, link, pubDate, description, and
    the enclosure fields audio, audio_type, audio_length.
    """
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

        # Enclosure: the actual audio media
        enc = find_child(item_el, "enclosure")
        audio = ""
        audio_type = ""
        audio_length = ""
        if enc is not None:
            audio = enc.attrib.get("url", "") or ""
            audio_type = enc.attrib.get("type", "") or ""
            audio_length = enc.attrib.get("length", "") or ""

        link = text_of(find_child(item_el, "link")) or ""
        if not audio:
            audio = link  # fall back to <link> when no enclosure is present

        books.append({
            "title": title,
            "author": author,
            "cover": cover or "",
            "link": link,
            "pubDate": text_of(find_child(item_el, "pubDate")) or "",
            "description": desc_text(find_child(item_el, "description")),
            "audio": audio,
            "audio_type": audio_type,
            "audio_length": audio_length,
        })

    return channel_title, channel_img, books
