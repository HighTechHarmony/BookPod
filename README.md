# BookPod — Audiobook RSS Feed Library

A small self-hosted web app that turns an audiobook RSS feed into a browsable,
searchable library of covers. Built with **FastAPI + Jinja2 + HTMX** — no Node,
no frontend build tools.

- Paste any audiobook RSS URL (or point it at a local feed) and get a paged grid
  of covers with title + author.
- Search by **title or author** (partial, case-insensitive); author names are
  clickable to filter by that author.
- Click a book to open a detail drawer with metadata and an embedded `<audio>`
  player. The player keeps playing across pagination and when the drawer is
  closed.
- Feeds and cover images are **cached on disk**, so repeat loads are fast and
  offline-friendly. Covers are fetched lazily — only for the page you view.

## Screenshot

![BookPod cover grid](screenshot.png)

## Project layout

```
main.py                 FastAPI application (routes, search, per-feed library)
feed.py                 RSS parsing + SHA-1 disk caching (shared, stdlib only)
rss_to_html.py          Standalone CLI that renders the feed to a static page
templates/              Jinja2 templates (base, grid, detail, empty)
requirements.txt        Python dependencies
feed_url.txt            Private default feed source (git-ignored)
feed_url.txt.example    Committed template for feed_url.txt
bookpod.service.example Example systemd unit
startup.sh              Convenience: runs uvicorn locally
```

## Requirements

- Python 3.10+ (3.14 used in dev). On Debian/Ubuntu, install the venv module if
  missing: `sudo apt install python3.14-venv` (adjust the version).
- Packages in `requirements.txt` (FastAPI, Uvicorn, Jinja2, python-multipart).
- Internet access on first load (to fetch the feed and cover images), unless you
  point everything at local files and a warm cache.

## Quick start (local dev)

```bash
cd <project-dir>

# One-time setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Configure your default feed (optional; see "Configuration")
cp feed_url.txt.example feed_url.txt   # then edit feed_url.txt with your URL

# Run
./startup.sh                           # or: .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. With no `?rss_url=` in the URL you'll see the "get
started" state; use the ☰ menu to enter a feed URL, or load one directly:

```
http://127.0.0.1:8000/?rss_url=https://example.com/feed.rss
```

## Configuration

The app is configured via environment variables and the private `feed_url.txt`
file.

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEED_SOURCE` | *(see below)* | Overrides the default feed source entirely |
| `CACHE_DIR` | `~/.cache/audiobook_feed_library` | Where feeds + covers are cached |
| `PAGE_SIZE` | `20` | Books per page |
| `REFRESH` | `0` | `1`/`true` forces re-fetch on startup |

**Default feed source resolution** (`read_default_feed_source` in `main.py`):

1. `FEED_SOURCE` env var, if set
2. first non-comment line of `feed_url.txt` (git-ignored, so it can hold a
   private URL safely)
3. fallback to `podbook_reduced.rss` in the app directory

Covers are written to `covers/<sha1(feed)>/NNNN.jpg` (one subdirectory per feed)
and served at `/covers/...`.

## Standalone CLI

The original one-shot generator is still available:

```bash
python3 rss_to_html.py <input> [output_dir] [--page-size N] [--no-download] [--title TEXT] [--cache-dir DIR] [--refresh]
```

It shares the same parsing/caching code (`feed.py`) and produces a single
self-contained `index.html` with a paged cover grid.

## Deploying as a systemd service

An example unit is provided in `bookpod.service.example`. Copy it to
`/etc/systemd/system/bookpod.service`, then adjust it to **your** install:

```ini
User=<your-user>                     # the user that owns the app
Group=<your-user>

WorkingDirectory=/path/to/BookPod    # where main.py lives
ExecStart=/path/to/BookPod/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000

# If the app lives under /home/<user>/..., hardening MUST allow access:
ProtectHome=false
ReadWritePaths=/path/to/BookPod
```

> **Important (the 203/EXEC gotcha):** with `ProtectHome=true`, systemd makes
> `/home` unreadable to the service — so if your install is under `/home`, set
> `ProtectHome=false`, or the process fails with `status=203/EXEC`.
> Also, the venv must be created **on the server** (never copied from another
> machine), otherwise the `bin/uvicorn` shebang points at a Python that doesn't
> exist there.

Then:

```bash
sudo cp bookpod.service.example /etc/systemd/system/bookpod.service
sudo systemctl daemon-reload
sudo systemctl enable --now bookpod
journalctl -u bookpod -f     # follow the logs
```

The service binds to `127.0.0.1:8000`; put a reverse proxy (nginx/Caddy) in
front if you want to expose it. If you set `CACHE_DIR` to a custom path, add it
to `ReadWritePaths`.

## Caching

- **Feeds**: a URL-sourced feed is cached under `<CACHE_DIR>/feeds/` and reused
  on later runs. The browser "Refresh feed" button (in the ☰ menu) re-fetches.
- **Covers**: cached under `<CACHE_DIR>/images/`, keyed by `SHA-1(feed + image
  URL)` with a JSON sidecar so each entry is validated against both the feed and
  the image URL. Serving comes from the disk cache when available, so repeat
  loads don't hit the network.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `status=203/EXEC` | systemd couldn't run `ExecStart`. Check the venv path exists on the server, the `User` can read/execute it, and `ProtectHome=false` if the app is under `/home`. |
| `Could not import module "main"` | Wrong working directory. Run uvicorn from the app dir (or set `WorkingDirectory=`), or use `--app-dir`. |
| Blank page / throbber never resolves | Feed unreachable or covers failing — check `journalctl -u bookpod -f` for the error, and that the feed URL in `feed_url.txt`/`?rss_url=` is reachable. |
