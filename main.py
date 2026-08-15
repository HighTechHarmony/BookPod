"""FastAPI + Jinja2 + HTMX web app for browsing an audiobook RSS feed.

All blocking work (feed loading, cover downloads, refreshes) runs through
fastapi.concurrency.run_in_threadpool so it never blocks the async event loop.
Grid pagination and the book detail drawer are swapped in-place with HTMX, so
playback in the detail drawer's <audio> element is preserved across pagination
and closing the drawer (close only hides it, never removes the content).
"""

from __future__ import annotations

import hashlib
import math
import os
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from feed import Cache, cached_download, default_cache_dir, load_input, parse_feed
from enrich import enrich_book
from config import load_config

CONFIG = load_config()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COVERS_DIR = os.path.join(BASE_DIR, "covers")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# Configuration: config.toml is the base; environment variables override it.
def read_default_feed_source() -> str:
    """Resolve the default feed source.

    Priority: FEED_SOURCE env var > config.toml [feed].url (may be blank).
    """
    return os.environ.get("FEED_SOURCE") or CONFIG["feed_url"]


CACHE_DIR = os.environ.get("CACHE_DIR") or CONFIG["cache_dir"] or default_cache_dir()
_refresh_env = os.environ.get("REFRESH", "").strip().lower()
REFRESH_ON_START = (_refresh_env in ("1", "true", "yes")) if _refresh_env else CONFIG["refresh_on_start"]
PAGE_SIZE = int(os.environ.get("PAGE_SIZE") or CONFIG["page_size"] or 20)
ENRICHMENT_ENABLED = CONFIG["enrichment_enabled"]

os.makedirs(COVERS_DIR, exist_ok=True)

templates = Jinja2Templates(directory=TEMPLATES_DIR)


class Library:
    """Holds the parsed books and covers. Loaded once, and reloadable."""

    def __init__(self, source: str, cache_dir: str, refresh: bool = False) -> None:
        self.source = source
        self.cache = Cache(cache_dir, refresh=refresh)
        self.feed_key = (
            source if source.startswith(("http://", "https://")) else os.path.abspath(source)
        )
        # Covers are stored per feed so different rss_url feeds never collide
        # on index-based filenames.
        self.covers_key = hashlib.sha1(self.feed_key.encode("utf-8")).hexdigest()
        self.covers_dir = os.path.join(COVERS_DIR, self.covers_key)
        self.books: list[dict] = []
        self.channel_title = "Audiobooks"
        self.channel_image: str = ""
        self.error: str | None = None
        self._loaded = False
        self._lock = threading.RLock()

    @property
    def loaded(self) -> bool:
        return self._loaded

    def _load(self, refresh: bool) -> None:
        with self._lock:
            if refresh:
                self.cache.refresh = True
            try:
                data, _source, _origin = load_input(self.source, self.cache)
                channel_title, channel_img, books = parse_feed(data)
                # Covers are downloaded lazily per page (see ensure_covers),
                # so loading the feed never fetches the whole cover set.
                self.channel_title = channel_title
                self.channel_image = channel_img or ""
                self.books = books
                self.error = None
                self._loaded = True
            except Exception as exc:  # noqa: BLE001 - surface errors via self.error
                self.error = str(exc)
                # keep previously loaded data if a refresh fails
            finally:
                self.cache.refresh = False

    def load_sync(self) -> None:
        with self._lock:
            if self._loaded and self.error is None:
                return
            self._load(refresh=False)

    def reload_sync(self) -> None:
        with self._lock:
            self._load(refresh=True)

    def ensure_covers(self, indices: list[int]) -> None:
        """Download covers for the given 1-based book indices (lazy).

        Only the requested indices are touched. Each cover is pulled from the
        disk cache when possible (validated by feed + image URL) and written to
        COVERS_DIR; books whose cover is already local, or absent from the
        feed, are skipped. Running this in a threadpool keeps blocking I/O off
        the event loop.
        """
        if not indices:
            return
        os.makedirs(self.covers_dir, exist_ok=True)
        with self._lock:
            for idx in indices:
                if idx < 1 or idx > len(self.books):
                    continue
                book = self.books[idx - 1]
                url = book.get("cover", "")
                if not url or url.startswith("/"):
                    continue  # no cover in feed, or already downloaded locally
                rel = f"/covers/{self.covers_key}/{idx:04d}.jpg"
                dest = os.path.join(self.covers_dir, f"{idx:04d}.jpg")
                data, _origin = cached_download(self.cache, self.feed_key, url)
                if data is None:
                    book["cover"] = ""  # render placeholder instead
                    continue
                with open(dest, "wb") as f:
                    f.write(data)
                book["cover"] = rel


# One Library per feed source, created lazily; the shared Cache dir keeps a
# single on-disk cache keyed by (feed, url).
_libraries: dict[str, "Library"] = {}
_libraries_lock = threading.Lock()


def get_library(source: str) -> "Library":
    with _libraries_lock:
        lib = _libraries.get(source)
        if lib is None:
            lib = Library(source, CACHE_DIR, refresh=REFRESH_ON_START)
            _libraries[source] = lib
        return lib


app = FastAPI(title="Audiobook Feed Library")
app.mount("/covers", StaticFiles(directory=COVERS_DIR), name="covers")


# --------------------------------------------------------------------------
# View helpers
# --------------------------------------------------------------------------

def page_count(lib: "Library") -> int:
    return max(1, math.ceil(len(lib.books) / PAGE_SIZE))


def pagination_items(page: int, total: int) -> list:
    """Page numbers with '…' gaps, mirroring the old client-side logic."""
    if total <= 7:
        return list(range(1, total + 1))
    out: list = []

    def push(p):
        if not out or out[-1] != p:
            out.append(p)

    def push_range(a, b):
        for i in range(a, b + 1):
            push(i)

    push(1)
    if page > 3:
        out.append("…")
    push_range(max(2, page - 1), min(total - 1, page + 1))
    if page < total - 2:
        out.append("…")
    if total > 1:
        push(total)
    return out


def filter_books(lib: "Library", search: str | None) -> tuple[str, list[tuple[int, dict]]]:
    """Case-insensitive substring filter on title or author.

    Returns (normalized query, [(1-based original index, book), ...]) so the
    original index (used for /book/{id} and cover paths) is preserved.
    """
    q = (search or "").strip()
    entries = list(enumerate(lib.books, start=1))
    if q:
        ql = q.lower()
        entries = [
            (i, b) for i, b in entries
            if ql in b.get("title", "").lower() or ql in b.get("author", "").lower()
        ]
    return q, entries


def paginate(entries: list[tuple[int, dict]], page: int) -> tuple[int, int, list[tuple[int, dict]]]:
    """Clamp the page number and slice entries into (page, page_count, slice)."""
    total = max(1, math.ceil(len(entries) / PAGE_SIZE))
    page = max(1, min(page, total))
    start = (page - 1) * PAGE_SIZE
    return page, total, entries[start:start + PAGE_SIZE]


def grid_context(lib: "Library", page: int, rss_url: str = "", oob: bool = False,
                 search: str | None = None) -> dict:
    search, entries = filter_books(lib, search)
    page, total, page_slice = paginate(entries, page)
    page_books = [dict(book, index=i) for i, book in page_slice]
    return {
        "page": page,
        "page_count": total,
        "page_items": pagination_items(page, total),
        "page_books": page_books,
        "count": len(lib.books),
        "result_count": len(entries),
        "search": search,
        "error": lib.error,
        "channel_title": lib.channel_title,
        "channel_image": lib.channel_image or "",
        "rss_url": rss_url,
        "oob": oob,
    }


def resolve_source(rss_url: str | None) -> str | None:
    """Normalise the ?rss_url= query value; None when absent/blank."""
    value = (rss_url or "").strip()
    return value or None


def home_context(rss_url: str | None) -> dict:
    """Context for the initial page render.

    With no rss_url the app shows an empty "get started" state (menu open).
    With a URL, 'ready' tells base.html whether to render the grid inline or
    show a throbber that HTMX fills via /grid.
    """
    source = resolve_source(rss_url)
    default_url = read_default_feed_source()
    if source is None:
        return {
            "rss_url": "",
            "default_url": default_url,
            "empty": True,
            "menu_open": True,
            "ready": False,
            "error": None,
            "page": 1,
            "page_count": 1,
            "page_items": [],
            "page_books": [],
            "count": 0,
            "result_count": 0,
            "search": "",
            "channel_title": "Audiobook Feed Library",
            "channel_image": "",
            "oob": False,
        }
    lib = get_library(source)
    ctx = grid_context(lib, 1, rss_url=source)
    ctx.update({
        "default_url": default_url,
        "empty": False,
        "menu_open": False,
        "ready": lib.loaded,
    })
    return ctx


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

async def prepare_page(lib: "Library", page: int) -> int:
    """Ensure the feed is loaded and covers for the requested page exist.

    Runs the blocking work (feed load + per-page cover download) in a
    threadpool so the event loop is never blocked. Returns the clamped page.
    """
    await run_in_threadpool(lib.load_sync)
    total = page_count(lib)
    page = max(1, min(page, total))
    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, len(lib.books))
    indices = list(range(start + 1, end + 1))
    if indices:
        await run_in_threadpool(lib.ensure_covers, indices)
    return page


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, rss_url: str | None = None):
    # Render the shell immediately. Without ?rss_url= we show the empty
    # "get started" state (menu open); with a URL, /grid fills the grid.
    return templates.TemplateResponse(request, "base.html", home_context(rss_url))


@app.get("/grid", response_class=HTMLResponse)
async def grid(request: Request, page: int = 1, rss_url: str | None = None,
               search: str | None = None):
    source = resolve_source(rss_url)
    if source is None:
        return templates.TemplateResponse(request, "empty.html", {"rss_url": ""})
    lib = get_library(source)
    await run_in_threadpool(lib.load_sync)
    _search, entries = filter_books(lib, search)
    page, _total, page_slice = paginate(entries, page)
    indices = [i for i, _ in page_slice]
    if indices:
        await run_in_threadpool(lib.ensure_covers, indices)
    return templates.TemplateResponse(
        request, "grid.html",
        grid_context(lib, page, rss_url=source, oob=True, search=search),
    )


@app.get("/book/{item_id}", response_class=HTMLResponse)
async def book_detail(request: Request, item_id: int, page: int = 1, rss_url: str | None = None):
    source = resolve_source(rss_url)
    if source is None:
        raise HTTPException(status_code=404, detail="Book not found")
    lib = get_library(source)
    await run_in_threadpool(lib.load_sync)
    if item_id < 1 or item_id > len(lib.books):
        raise HTTPException(status_code=404, detail="Book not found")
    await run_in_threadpool(lib.ensure_covers, [item_id])
    book = lib.books[item_id - 1]
    return templates.TemplateResponse(
        request, "detail.html",
        {"book": book, "page": page, "rss_url": source, "index": item_id,
         "enrichment_enabled": ENRICHMENT_ENABLED},
    )


@app.get("/enrich/{item_id}", response_class=HTMLResponse)
async def enrich(request: Request, item_id: int, rss_url: str | None = None):
    if not ENRICHMENT_ENABLED:
        return templates.TemplateResponse(
            request, "enrich.html", {"disabled": True, "enriched": None, "rss_url": ""}
        )
    source = resolve_source(rss_url)
    if source is None:
        return templates.TemplateResponse(request, "enrich.html", {"enriched": None, "rss_url": ""})
    lib = get_library(source)
    await run_in_threadpool(lib.load_sync)
    if item_id < 1 or item_id > len(lib.books):
        raise HTTPException(status_code=404, detail="Book not found")
    book = lib.books[item_id - 1]
    enriched = await run_in_threadpool(enrich_book, book["title"], book["author"])
    return templates.TemplateResponse(request, "enrich.html", {"enriched": enriched, "rss_url": source})


@app.post("/refresh", response_class=HTMLResponse)
async def refresh(request: Request, rss_url: str | None = None):
    source = resolve_source(rss_url)
    if source is None:
        return templates.TemplateResponse(request, "empty.html", {"rss_url": ""})
    lib = get_library(source)
    await run_in_threadpool(lib.reload_sync)
    page = await prepare_page(lib, 1)
    return templates.TemplateResponse(request, "grid.html", grid_context(lib, page, rss_url=source, oob=True))
