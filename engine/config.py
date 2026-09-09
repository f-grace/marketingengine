"""Configuration loading.

Everything product-specific lives in config files, not in code. That is what
keeps the pipeline itself product-agnostic: swapping the niche means editing
`accounts.yml` and `brand.json`, not touching Python.

Secrets come from the environment only. Nothing here ever reads a token from a
config file that could be committed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "engine.db"
EXPORT_DIR = DATA_DIR / "exports"
PROMPTS_DIR = EXPORT_DIR / "prompts"
IMAGES_DIR = DATA_DIR / "images"

ACTOR_ID = "clockworks/tiktok-scraper"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or unusable."""


@dataclass
class Accounts:
    own: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    @property
    def all_handles(self) -> List[str]:
        """Own account first so it is never dropped by a truncated run."""
        seen = set()
        out = []
        for h in list(self.own) + list(self.sources):
            h = h.lstrip("@").strip()
            if h and h not in seen:
                seen.add(h)
                out.append(h)
        return out

    def is_own(self, handle: str) -> bool:
        norm = {h.lstrip("@").strip().lower() for h in self.own}
        return handle.lstrip("@").strip().lower() in norm


@dataclass
class ScrapeSettings:
    results_per_page: int = 100
    # Depth for accounts the store has already seen. Shallower than the first
    # deep scrape: it only needs to pick up posts added since the last run and
    # refresh metrics on recent ones so late-blooming virals get caught.
    results_per_page_refresh: int = 10
    oldest_post_date: str = "60 days"
    proxy_country_code: str = "US"
    n_winners: int = 30
    n_losers: int = 0
    n_anomalies: int = 0
    n_reach_only: int = 8


@dataclass
class GmailSettings:
    address: str
    app_password: str
    to: str


def load_env_file(path: Optional[Path] = None) -> None:
    """Load KEY=VALUE lines from .env into os.environ without overwriting.

    Deliberately tiny rather than pulling in python-dotenv. Existing environment
    variables always win, so a real shell export beats the file.
    """
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def apify_token() -> str:
    """Fetch the Apify token from the environment.

    Never read from a config file, never logged, never written to the database.
    """
    load_env_file()
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "APIFY_TOKEN is not set.\n"
            "  1. Get a token at https://console.apify.com/settings/integrations\n"
            "  2. Add it to .env as APIFY_TOKEN=... (copy .env.example first)\n"
            "     or export it in your shell.\n"
            "The token is read from the environment only and is never stored."
        )
    return token


def gmail_settings() -> Optional[GmailSettings]:
    """Gmail delivery settings, or None when not configured.

    None (rather than an exception) so email degrades to files-on-disk: the
    prompts are already written before delivery is attempted. The password is
    a Google App Password, read from the environment only and never stored.
    """
    load_env_file()
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to = os.environ.get("DIGEST_TO", "").strip() or address
    if not address or not password:
        return None
    return GmailSettings(address=address, app_password=password, to=to)


def load_accounts(path: Optional[Path] = None) -> Accounts:
    path = path or (CONFIG_DIR / "accounts.yml")
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy config/accounts.example.yml to "
            "config/accounts.yml and fill in the handles you want to track."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    own = raw.get("own") or []
    # `competitors` is the pre-rename key; accept it so old configs keep working.
    sources = raw.get("sources") or raw.get("competitors") or []

    if not isinstance(own, list) or not isinstance(sources, list):
        raise ConfigError(f"{path}: 'own' and 'sources' must both be lists")

    accounts = Accounts(own=own, sources=sources)
    if not accounts.all_handles:
        raise ConfigError(
            f"{path} lists no handles. Add at least one idea-source account "
            "under 'sources:', and your own handle under 'own:' so your own "
            "winners get rebrand prompts too."
        )
    return accounts


def load_scrape_settings(path: Optional[Path] = None) -> ScrapeSettings:
    path = path or (CONFIG_DIR / "accounts.yml")
    raw = {}
    if path.exists():
        raw = (yaml.safe_load(path.read_text()) or {}).get("scrape") or {}
    defaults = ScrapeSettings()
    return ScrapeSettings(
        results_per_page=raw.get("results_per_page", defaults.results_per_page),
        results_per_page_refresh=raw.get(
            "results_per_page_refresh", defaults.results_per_page_refresh
        ),
        oldest_post_date=raw.get("oldest_post_date", defaults.oldest_post_date),
        proxy_country_code=raw.get("proxy_country_code", defaults.proxy_country_code),
        n_winners=raw.get("n_winners", defaults.n_winners),
        n_losers=raw.get("n_losers", defaults.n_losers),
        n_anomalies=raw.get("n_anomalies", defaults.n_anomalies),
        n_reach_only=raw.get("n_reach_only", defaults.n_reach_only),
    )


def load_brand(path: Optional[Path] = None) -> Dict:
    """Product truth. The prompt builder reads voice, guardrails, and phase."""
    path = path or (CONFIG_DIR / "brand.json")
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy config/brand.example.json to "
            "config/brand.json and describe what you sell, to whom, and in what "
            "voice. Scrape and score run without it; prompt generation does not."
        )
    return json.loads(path.read_text())
