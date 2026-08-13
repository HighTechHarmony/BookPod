"""FastAPI + Jinja2 + HTMX web app for browsing an audiobook RSS feed.

All blocking work (feed loading, cover downloads, refreshes) runs through
fastapi.concurrency.run_in_threadpool so it never blocks the async event loop.
Grid pagination and the book detail drawer are swapped in-place with HTMX, so
playback in the detail drawer's <audio> element is preserved across pagination
and closing the drawer (close only hides it, never removes the content).
"""

from __future__ import annotations

import math
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from feed import Cache, cached_download, default_cache_dir, load_input, parse_feed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COVERS_DIR = os.path.join(BASE_DIR, "covers")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# Configuration (all overridable via environment variables)
def read_default_feed_source() -> str:
    """Resolve the default feed source.

    Priority: FEED_SOURCE env var > private feed_url.txt > local reduced feed.

    feed_url.txt is git-ignored so a personal/private feed URL is never
    committed to the repo. Lines starting with '#' are treated as comments.
    """
    env = os.environ.get("FEED_SOURCE")
    if env:
        return env
    url_file = os.path.join(BASE_DIR, "feed_url.txt")
    if os.path.isfile(url_file):
        with open(url_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    return os.path.join(BASE_DIR, "podbook_reduced.rss")


FEED_SOURCE = read_default_feed_source()
CACHE_DIR = os.environ.get("CACHE_DIR") or default_cache_dir()
REFRESH_ON_START = os.environ.get("REFRESH", "").strip().lower() in ("1", "true", "yes")
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "20") or 20)

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
        self.books: list[dict] = []
        self.channel_title = "Audiobooks"
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
                channel_title, _img, books = parse_feed(data)
                # Covers are downloaded lazily per page (see ensure_covers),
                # so loading the feed never fetches the whole cover set.
                self.channel_title = channel_title
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
        os.makedirs(COVERS_DIR, exist_ok=True)
        with self._lock:
            for idx in indices:
                if idx < 1 or idx > len(self.books):
                    continue
                book = self.books[idx - 1]
                url = book.get("cover", "")
                if not url or url.startswith("/"):
                    continue  # no cover in feed, or already downloaded locally
                rel = f"/covers/{idx:04d}.jpg"
                dest = os.path.join(COVERS_DIR, f"{idx:04d}.jpg")
                data, _origin = cached_download(self.cache, self.feed_key, url)
                if data is None:
                    book["cover"] = ""  # render placeholder instead
                    continue
                with open(dest, "wb") as f:
                    f.write(data)
                book["cover"] = rel


library = Library(FEED_SOURCE, CACHE_DIR, refresh=REFRESH_ON_START)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Kick off feed loading off the event loop so the first request never
    # blocks it; routes await load_sync() (a no-op once loaded). Covers are
    # downloaded lazily per page by the routes themselves.
    threading.Thread(target=library.load_sync, daemon=True).start()
    yield


app = FastAPI(title="Audiobook Feed Library", lifespan=lifespan)
app.mount("/covers", StaticFiles(directory=COVERS_DIR), name="covers")


# --------------------------------------------------------------------------
# View helpers
# --------------------------------------------------------------------------

def page_count() -> int:
    return max(1, math.ceil(len(library.books) / PAGE_SIZE))


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


def grid_context(page: int) -> dict:
    total = page_count()
    page = max(1, min(page, total))
    start = (page - 1) * PAGE_SIZE
    slice_ = library.books[start:start + PAGE_SIZE]
    page_books = [dict(book, index=i) for i, book in enumerate(slice_, start=start + 1)]
    return {
        "page": page,
        "page_count": total,
        "page_items": pagination_items(page, total),
        "page_books": page_books,
        "count": len(library.books),
        "error": library.error,
        "channel_title": library.channel_title,
    }


def home_context() -> dict:
    """Context for the initial page render.

    'ready' tells base.html whether the grid can be rendered inline. When the
    feed is still loading (not ready), base.html shows a throbber and HTMX
    fetches /grid into #grid, which awaits the load + per-page covers.
    """
    ctx = grid_context(1)
    ctx["ready"] = library.loaded
    return ctx


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

async def prepare_page(page: int) -> int:
    """Ensure the feed is loaded and covers for the requested page exist.

    Runs the blocking work (feed load + per-page cover download) in a
    threadpool so the event loop is never blocked. Returns the clamped page.
    """
    await run_in_threadpool(library.load_sync)
    total = page_count()
    page = max(1, min(page, total))
    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, len(library.books))
    indices = list(range(start + 1, end + 1))
    if indices:
        await run_in_threadpool(library.ensure_covers, indices)
    return page


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # Render the shell immediately: if the feed is already loaded the grid is
    # rendered inline; otherwise base.html shows a throbber and HTMX fetches
    # /grid (which awaits the load + per-page covers) into #grid.
    return templates.TemplateResponse(request, "base.html", home_context())


@app.get("/grid", response_class=HTMLResponse)
async def grid(request: Request, page: int = 1):
    page = await prepare_page(page)
    return templates.TemplateResponse(request, "grid.html", grid_context(page))


@app.get("/book/{item_id}", response_class=HTMLResponse)
async def book_detail(request: Request, item_id: int, page: int = 1):
    await run_in_threadpool(library.load_sync)
    if item_id < 1 or item_id > len(library.books):
        raise HTTPException(status_code=404, detail="Book not found")
    await run_in_threadpool(library.ensure_covers, [item_id])
    book = library.books[item_id - 1]
    return templates.TemplateResponse(request, "detail.html", {"book": book, "page": page})


@app.post("/refresh", response_class=HTMLResponse)
async def refresh(request: Request):
    await run_in_threadpool(library.reload_sync)
    page = await prepare_page(1)
    return templates.TemplateResponse(request, "grid.html", grid_context(page))
