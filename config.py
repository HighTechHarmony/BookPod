"""Load configuration from config.toml.

config.toml is the single source of truth for BookPod settings (it is
git-ignored; see config.toml.example for the supported keys). Individual
settings can still be overridden with environment variables.
"""

from __future__ import annotations

import os
import tomllib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("BOOKPOD_CONFIG") or os.path.join(BASE_DIR, "config.toml")

_DEFAULTS = {
    "feed_url": "",
    "enrichment_enabled": True,
    "page_size": 20,
    "cache_dir": "",
    "refresh_on_start": False,
}


def load_config(path: str | None = None) -> dict:
    """Return the merged configuration (defaults + config.toml)."""
    cfg = dict(_DEFAULTS)
    p = path or CONFIG_PATH
    if os.path.isfile(p):
        with open(p, "rb") as f:
            data = tomllib.load(f)

        feed = data.get("feed") or {}
        cfg["feed_url"] = str(feed.get("url", "") or "").strip()

        enrich = data.get("enrichment") or {}
        cfg["enrichment_enabled"] = bool(enrich.get("enabled", True))

        app = data.get("app") or {}
        cfg["page_size"] = int(app.get("page_size", _DEFAULTS["page_size"]))
        cfg["cache_dir"] = str(app.get("cache_dir", "") or "").strip()
        cfg["refresh_on_start"] = bool(app.get("refresh_on_start", False))
    return cfg
