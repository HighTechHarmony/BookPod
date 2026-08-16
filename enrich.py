"""OpenLibrary enrichment lookups for audiobook metadata.

Two-step strategy:
  1. https://openlibrary.org/search.json?title=..&author=..
       -> normalized title, author_name, subject tags (genres), and a Work key.
  2. https://openlibrary.org{work_key}.json
       -> description (the synopsis).

Audiobook feed titles are noisy in ways that break naive lookups:

  * subtitles ("Foo: A Novel") make OpenLibrary's literal ``title=`` search
    return zero docs, so we retry with progressively looser title variants;
  * curly apostrophes/dashes differ between feeds and OpenLibrary records;
  * the top-scoring match is often a boxed set / audiobook record with no
    description, so we walk the scored candidates and return the first Work
    that actually has a synopsis (falling back to the best match otherwise).

Results are cached in-memory so repeated drawer opens don't re-query the API.
"""

from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

USER_AGENT = "BookPod/1.0 (audiobook RSS library)"
SEARCH_URL = "https://openlibrary.org/search.json"
WORK_URL = "https://openlibrary.org{key}.json"
TIMEOUT = 15
MAX_GENRES = 8
MAX_WORKS = 4  # candidate works probed per title variant for a synopsis
_MAX_CACHE = 200
_ENRICH_UNAVAILABLE_TTL = 300  # seconds to remember OpenLibrary being unreachable

# Statuses returned alongside enriched data so the UI can distinguish a genuine
# "no match" from "the enrichment service is down/unreachable".
ENRICH_OK = "ok"
ENRICH_NO_MATCH = "not_found"
ENRICH_UNAVAILABLE = "unavailable"

_cache: dict[str, tuple[str, dict | None, float]] = {}  # key -> (status, data, expiry)
_cache_lock = threading.Lock()


def _get_json(url: str, params: dict | None = None) -> dict:
    resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _norm(s: str) -> str:
    """Lowercase and normalize punctuation that differs between feeds & OpenLibrary."""
    return (
        (s or "")
        .lower()
        .replace("\u2019", "'")   # right single quote ’
        .replace("\u2018", "'")   # left single quote ‘
        .replace("\u2014", "-")   # em dash —
        .replace("\u2013", "-")   # en dash –
        .replace("\u00a0", " ")   # non-breaking space
        .strip()
    )


def _candidate_titles(title: str) -> list[str]:
    """Progressively looser search-title variants, deduplicated, in order.

    Audiobook feed titles carry subtitle noise ("Foo: A Novel", ", Book 2")
    that OpenLibrary's literal ``title=`` parameter returns no docs for. We
    always try the full title first, then the part before the first colon,
    then the head with a trailing series marker (Book/Vol/Part + number)
    stripped.
    """
    t = (title or "").strip()
    variants = [t]
    if not t:
        return variants

    head = re.split(r"\s*:\s*", t, maxsplit=1)[0].strip()
    if head and head != t:
        variants.append(head)

    stripped = re.sub(
        r"(?i)[,\s]*(?:book|vol(?:ume)?|pt\.?|part|episode|#)\s*[0-9]+[a-z]?\s*$",
        "", head,
    ).strip()
    if stripped and stripped != head:
        variants.append(stripped)

    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        k = v.lower()
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _lookup(title: str, author: str) -> tuple[str, dict | None]:
    nt = _norm(title)
    aq = _norm(author)
    fallback: dict | None = None
    reachable = False  # True once any search API call returns a response

    def score(doc: dict) -> tuple[int, int]:
        dt = _norm(doc.get("title"))
        if not nt:
            tscore = 3
        elif dt == nt:
            tscore = 0
        elif dt.startswith(nt):
            tscore = 1
        elif nt in dt:
            tscore = 2
        else:
            tscore = 3
        names = [_norm(n) for n in (doc.get("author_name") or [])]
        ascore = 0 if aq and names and (aq in names[0] or names[0] in aq) else 1
        return (tscore, ascore)

    def fetch_work(doc: dict) -> dict | None:
        work_key = doc.get("key")
        if not work_key:
            return None
        try:
            return _get_json(WORK_URL.format(key=work_key))
        except Exception:  # noqa: BLE001 - synopsis/subjects are best-effort
            return None

    def enrich_from(doc: dict, work: dict | None) -> dict:
        subjects: list = []
        desc = ""
        if work is not None:
            subjects = work.get("subjects") or doc.get("subject") or []
            d = work.get("description")
            if isinstance(d, dict):
                d = d.get("value") or ""
            desc = d if isinstance(d, str) else ""
        return {
            "title": doc.get("title") or title,
            "author": (doc.get("author_name") or [author])[0],
            "subjects": [str(s) for s in subjects][:MAX_GENRES],
            "description": desc,
        }

    # Try each progressively looser title variant. For each one, walk the
    # scored candidates and return the first Work that actually carries a
    # synopsis (boxed sets / audiobook records often score high but have none).
    # The best-scored match from the first variant is kept as a fallback so we
    # still return title/author/subjects when no synopsis exists anywhere.
    for tq in _candidate_titles(title):
        try:
            search = _get_json(SEARCH_URL, {"title": tq, "author": author})
        except Exception:  # noqa: BLE001 - network/API errors degrade gracefully
            continue
        reachable = True
        docs = search.get("docs") or []
        if not docs:
            continue

        works = [d for d in docs if str(d.get("key", "")).startswith("/works/")]
        pool = works or docs
        ordered = sorted(pool, key=score)
        candidates = ordered[:MAX_WORKS]

        # Fast path: the best-scored Work usually carries the synopsis, so
        # probe it alone and avoid needless parallel requests to OpenLibrary.
        results: dict[int, dict] = {
            0: enrich_from(candidates[0], fetch_work(candidates[0])),
        }
        if results[0]["description"]:
            return ENRICH_OK, results[0]
        if fallback is None:
            fallback = results[0]

        # Slow path: the top match is a synopsis-less boxed set / audiobook
        # record. Probe the remaining candidates concurrently — the work JSON
        # endpoint is slow (~5s+), so serial probes would stall the drawer.
        if len(candidates) > 1:
            with ThreadPoolExecutor(max_workers=len(candidates) - 1) as ex:
                fut_map = {
                    ex.submit(fetch_work, doc): i
                    for i, doc in enumerate(candidates[1:], start=1)
                }
                for fut in as_completed(fut_map):
                    i = fut_map[fut]
                    results[i] = enrich_from(candidates[i], fut.result())
            for i in range(1, len(candidates)):
                if results[i]["description"]:
                    return ENRICH_OK, results[i]

    if fallback is not None:
        return ENRICH_OK, fallback
    if not reachable:
        # Every search attempt failed at the network/HTTP layer, so we can't
        # tell "no match" from "service unreachable".
        return ENRICH_UNAVAILABLE, None
    return ENRICH_NO_MATCH, None


def enrich_book(title: str, author: str) -> tuple[str, dict | None]:
    """Return (status, data) for enriched metadata.

    status is one of ENRICH_OK, ENRICH_NO_MATCH, or ENRICH_UNAVAILABLE (see the
    module docstring); data is the enriched dict when status is ENRICH_OK and
    None otherwise.

    Lookups are cached in-memory keyed by (title, author). "Unavailable"
    results are remembered only briefly (see _ENRICH_UNAVAILABLE_TTL) so the
    app retries once the service recovers, while "ok"/"no match" results are
    stable and cached indefinitely.
    """
    key = f"{title}\x00{author}".lower()
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            status, data, expires = hit
            if status != ENRICH_UNAVAILABLE or now < expires:
                return status, data
            # stale "unavailable" marker -> retry the lookup
            del _cache[key]

    status, result = _lookup(title, author)

    with _cache_lock:
        if len(_cache) >= _MAX_CACHE:
            _cache.pop(next(iter(_cache), None), None)  # crude FIFO eviction
        expires = (
            time.monotonic() + _ENRICH_UNAVAILABLE_TTL
            if status == ENRICH_UNAVAILABLE
            else math.inf
        )
        _cache[key] = (status, result, expires)
    return status, result
