"""OpenLibrary enrichment lookups for audiobook metadata.

Two-step strategy:
  1. https://openlibrary.org/search.json?title=..&author=..
       -> normalized title, author_name, subject tags (genres), and a Work key.
  2. https://openlibrary.org{work_key}.json
       -> description (the synopsis).

Results are cached in-memory so repeated drawer opens don't re-query the API.
"""

from __future__ import annotations

import threading

import requests

USER_AGENT = "BookPod/1.0 (audiobook RSS library)"
SEARCH_URL = "https://openlibrary.org/search.json"
WORK_URL = "https://openlibrary.org{key}.json"
TIMEOUT = 8
MAX_GENRES = 8
_MAX_CACHE = 200

_cache: dict[str, dict | None] = {}
_cache_lock = threading.Lock()


def _get_json(url: str, params: dict | None = None) -> dict:
    resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _lookup(title: str, author: str) -> dict | None:
    try:
        search = _get_json(SEARCH_URL, {"title": title, "author": author})
    except Exception:  # noqa: BLE001 - network/API errors degrade gracefully
        return None

    docs = search.get("docs") or []
    if not docs:
        return None

    def score(doc: dict) -> tuple[int, int]:
        tq = (title or "").strip().lower()
        dt = str(doc.get("title") or "").strip().lower()
        if not tq:
            tscore = 3
        elif dt == tq:
            tscore = 0
        elif dt.startswith(tq):
            tscore = 1
        elif tq in dt:
            tscore = 2
        else:
            tscore = 3

        aq = (author or "").strip().lower()
        names = [str(n).lower() for n in (doc.get("author_name") or [])]
        if aq and names and (aq in names[0] or names[0] in aq):
            ascore = 0  # primary author matches
        else:
            ascore = 1

        return (tscore, ascore)

    # Prefer a Work whose title matches first, then whose primary author
    # matches; its key points at the endpoint with the synopsis + subjects.
    works = [d for d in docs if str(d.get("key", "")).startswith("/works/")]
    pool = works or docs
    top = min(pool, key=score)

    enriched: dict = {
        "title": top.get("title") or title,
        "author": (top.get("author_name") or [author])[0],
        "subjects": [],
        "description": "",
    }

    work_key = top.get("key")
    if work_key:
        try:
            work = _get_json(WORK_URL.format(key=work_key))
            subjects = work.get("subjects") or top.get("subject") or []
            enriched["subjects"] = [str(s) for s in subjects][:MAX_GENRES]
            desc = work.get("description")
            if isinstance(desc, dict):
                desc = desc.get("value") or ""
            enriched["description"] = desc if isinstance(desc, str) else ""
        except Exception:  # noqa: BLE001 - synopsis/subjects are best-effort
            pass

    return enriched


def enrich_book(title: str, author: str) -> dict | None:
    """Return enriched metadata (title, author, subjects, description), or None.

    The lookup is cached in-memory keyed by (title, author).
    """
    key = f"{title}\x00{author}".lower()
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    result = _lookup(title, author)

    with _cache_lock:
        if len(_cache) >= _MAX_CACHE:
            _cache.pop(next(iter(_cache), None), None)  # crude FIFO eviction
        _cache[key] = result
    return result
