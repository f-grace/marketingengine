"""Background imagery for slides.

The winning posts in this niche use real photography, not generated images. The
16.4x winner is a stock street photo; the 2.8M-view salary countdown is film
stills. So the default source here is a stock library, not an image model:
cheaper (free), higher quality, and closer to what actually performs.

    Pexels API key set  ->  real photographs, searched per slide
    no key              ->  generated gradient grounds, so the renderer
                            still produces usable slides with zero setup

An image model is a third option and deliberately not the default. Reach for it
only when a slide needs a specific composed scene that stock cannot supply.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional, Tuple

import requests

from .config import DATA_DIR, load_env_file

CACHE_DIR = DATA_DIR / "backgrounds"
PEXELS_SEARCH = "https://api.pexels.com/v1/search"

# Portrait, matching TikTok photo mode.
SLIDE_W, SLIDE_H = 1080, 1920


class ImageryError(RuntimeError):
    pass


def pexels_key() -> Optional[str]:
    """Free key from pexels.com/api. No card, instant, 200 requests/hour."""
    load_env_file()
    return os.environ.get("PEXELS_API_KEY", "").strip() or None


def _cache_path(query: str, index: int) -> Path:
    digest = hashlib.sha256(f"{query}|{index}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{digest}.jpg"


def fetch_background(query: str, index: int = 0) -> Optional[Path]:
    """Download a portrait photo for `query`, cached on disk.

    Returns None when no key is configured or nothing matches, so the caller can
    fall back rather than fail. Caching matters: re-rendering a deck while
    tweaking type should not re-hit the API every time.
    """
    key = pexels_key()
    if not key:
        return None

    path = _cache_path(query, index)
    if path.exists():
        return path

    try:
        response = requests.get(
            PEXELS_SEARCH,
            headers={"Authorization": key},
            params={
                "query": query,
                "orientation": "portrait",
                "per_page": 15,
                "size": "large",
            },
            timeout=30,
        )
        response.raise_for_status()
        photos = response.json().get("photos") or []
    except (requests.RequestException, ValueError):
        return None

    if not photos:
        return None

    photo = photos[index % len(photos)]
    src = (photo.get("src") or {}).get("portrait") or (photo.get("src") or {}).get("large")
    if not src:
        return None

    try:
        blob = requests.get(src, timeout=60).content
    except requests.RequestException:
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


# Query hints per slide role. Deliberately generic and anonymous: no
# recognisable faces, which sidesteps both the right-of-publicity problem and
# the uncanny-face failure mode of generated imagery.
ROLE_QUERIES = {
    "HOOK": "city skyscrapers business district",
    "TENSION": "crowded office people working",
    "TURN": "empty office corridor",
    "PAYOFF": "financial district street",
    "PROOF": "laptop desk documents",
    "HOWTO": "person typing laptop",
    "OBJECTION": "office window city view",
    "CONCEDE": "business meeting silhouette",
    "ENTRY": "trading floor monitors finance",
}


def query_for(role: str, fallback: str = "business office city") -> str:
    role = (role or "").upper()
    for key, query in ROLE_QUERIES.items():
        if role.startswith(key):
            return query
    return fallback
