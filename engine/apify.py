"""Apify actor client for clockworks/tiktok-scraper.

Two passes, deliberately:

    Pass A  wide + cheap    all accounts, no comments, no downloads   ~$1.70/1k
    Pass B  narrow + rich   only selected posts, + comments + images

Comments and image downloads are per-post multipliers on billed results.
Applying them to all 1,000 posts instead of the ~50 you actually analyse is the
difference between a $1.70 run and a $40 run.

Every field below comes from the actor's published input schema. Two values are
NOT verified and are flagged inline: the allowed set for `profileSorting`, and
the accepted format of `oldestPostDateUnified`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from apify_client import ApifyClient

from .config import ACTOR_ID

# Roughly $1.70 per 1,000 results, per the actor's pay-per-event pricing.
# Directional only. Apify documents pre-run cost estimation as unreliable, so
# treat this as a sanity check, not a bill.
USD_PER_1000_RESULTS = 1.70


class ApifyError(RuntimeError):
    """Actor run failed or returned nothing usable."""


@dataclass
class RunResult:
    items: List[Dict]
    run_id: Optional[str]
    dataset_id: Optional[str]
    raw_path: Optional[Path]

    @property
    def estimated_cost_usd(self) -> float:
        return len(self.items) / 1000.0 * USD_PER_1000_RESULTS


def pass_a_input(
    handles: List[str],
    results_per_page: int = 100,
    oldest_post_date: str = "60 days",
    proxy_country_code: str = "US",
) -> Dict:
    """Wide sweep. No comments, no media downloads, no AI enrichment.

    `aiVideoDescription` / `aiVideoSummary` stay false. Unverified, but per-item
    AI enrichment on a pay-per-event actor is very likely billed extra, and we
    run our own analysis downstream anyway.
    """
    return {
        "profiles": [h.lstrip("@") for h in handles],
        "profileScrapeSections": ["videos"],
        # NOTE: "latest" is the documented default. The README does not
        # enumerate the allowed values, so do not assume "popular" exists.
        "profileSorting": "latest",
        "resultsPerPage": results_per_page,
        "excludePinnedPosts": False,
        # NOTE: typed `string` with no documented format. Verify in Apify
        # Console before relying on the relative form.
        "oldestPostDateUnified": oldest_post_date,
        "maxFollowersPerProfile": 0,
        "maxFollowingPerProfile": 0,
        "commentsPerPost": 0,
        "topLevelCommentsPerPost": 0,
        "maxRepliesPerComment": 0,
        "scrapeRelatedVideos": False,
        "scrapeRelatedSearchWords": False,
        "scrapeAdditionalAuthorMeta": False,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadAvatars": False,
        "shouldDownloadMusicCovers": False,
        "downloadSubtitlesOptions": "NEVER_DOWNLOAD_SUBTITLES",
        "aiVideoDescription": False,
        "aiVideoSummary": False,
        "proxyCountryCode": proxy_country_code,
    }


def pass_b_input(
    post_urls: List[str],
    top_level_comments_per_post: int = 30,
    proxy_country_code: str = "US",
) -> Dict:
    """Enrichment sweep over selected posts only.

    `maxRepliesPerComment` is 0 on purpose: top-level comments are the questions
    the market is asking, replies are mostly commenters talking to each other,
    and you pay per comment.
    """
    return {
        "postURLs": post_urls,
        "topLevelCommentsPerPost": top_level_comments_per_post,
        "maxRepliesPerComment": 0,
        "commentsPerPost": 0,
        "shouldDownloadSlideshowImages": True,
        "shouldDownloadCovers": True,
        "shouldDownloadVideos": False,
        "downloadSubtitlesOptions": "NEVER_DOWNLOAD_SUBTITLES",
        "aiVideoDescription": False,
        "aiVideoSummary": False,
        "proxyCountryCode": proxy_country_code,
    }


def run_actor(
    token: str,
    run_input: Dict,
    raw_dir: Optional[Path] = None,
    pass_name: str = "A",
) -> RunResult:
    """Call the actor and return its dataset items.

    The raw dataset is written to disk BEFORE anything parses it. That makes
    scoring re-runnable without paying to re-scrape, and turns an actor schema
    change into a diff you can read instead of a mystery downstream.
    """
    client = ApifyClient(token)

    try:
        run = client.actor(ACTOR_ID).call(run_input=run_input)
    except Exception as exc:  # apify_client raises several distinct types
        raise ApifyError(
            f"Apify actor {ACTOR_ID} failed to run: {exc}\n"
            "Check that APIFY_TOKEN is valid and that the input matches the "
            "actor's schema."
        ) from exc

    if not run:
        raise ApifyError(f"Apify actor {ACTOR_ID} returned no run object")

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise ApifyError(
            f"Apify run {run.get('id')} produced no dataset. "
            f"Run status: {run.get('status')}"
        )

    items = list(client.dataset(dataset_id).iterate_items())

    raw_path = None
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        raw_path = raw_dir / f"pass{pass_name}-{stamp}.json"
        raw_path.write_text(json.dumps(items, indent=2, ensure_ascii=False))

    return RunResult(
        items=items,
        run_id=run.get("id"),
        dataset_id=dataset_id,
        raw_path=raw_path,
    )


def load_raw(path: Path) -> List[Dict]:
    """Re-read a saved dataset so scoring can be re-run for free."""
    return json.loads(path.read_text())
