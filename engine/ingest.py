"""Turn raw actor dataset items into rows in the store.

Actor output is third-party data and every field is treated as optional. A post
missing `authorMeta`, or carrying a null `playCount`, or arriving with a
malformed timestamp, must not abort a 1,000-item ingest. Bad items are counted
and reported, never silently dropped.

    dataset item ──> validate ──> upsert post ──> upsert images/comments
                        │
                        ├─ no tiktok id     -> skipped, counted
                        ├─ no author handle -> skipped, counted
                        └─ null counts      -> coerced to 0, kept
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from . import db


@dataclass
class IngestReport:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    images: int = 0
    comments: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def summary(self) -> str:
        parts = [
            f"{self.inserted} new",
            f"{self.updated} updated",
            f"{self.skipped} skipped",
        ]
        if self.images:
            parts.append(f"{self.images} slide images")
        if self.comments:
            parts.append(f"{self.comments} comments")
        line = ", ".join(parts)
        if self.reasons:
            detail = "; ".join(f"{k}: {v}" for k, v in sorted(self.reasons.items()))
            line += f"  (skipped -> {detail})"
        return line


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(value, default: int = 0) -> int:
    """Coerce an actor numeric field, tolerating None and strings."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _handle_of(item: Dict) -> Optional[str]:
    author = item.get("authorMeta") or {}
    handle = author.get("name") or author.get("nickName") or author.get("uniqueId")
    if not handle:
        return None
    return str(handle).lstrip("@").strip() or None


def _hashtags_of(item: Dict) -> List[str]:
    raw = item.get("hashtags") or []
    out = []
    for h in raw:
        if isinstance(h, dict):
            name = h.get("name")
        else:
            name = h
        if name:
            out.append(str(name).lstrip("#"))
    return out


def _slideshow_links(item: Dict) -> List[str]:
    links = item.get("slideshowImageLinks") or []
    out = []
    for entry in links:
        if isinstance(entry, dict):
            url = entry.get("downloadLink") or entry.get("url")
        else:
            url = entry
        if url:
            out.append(str(url))
    return out


MIN_COMMENT_CHARS = 15


def ingest_comments(conn: sqlite3.Connection, rows: Iterable[Dict]) -> int:
    """Write comment rows from the actor's separate comments dataset.

    Joins to posts on the video URL. Rows for posts we never ingested are
    dropped silently, which is correct: the comments dataset covers the whole
    run and we only keep comments for posts in the store.

    Sub-15-character comments are filtered out here rather than downstream.
    They are emoji, tags, and "first", and paying an LLM to cluster them is
    waste. Replies are excluded at the actor level (maxRepliesPerComment=0),
    so everything arriving here should already be top-level.
    """
    now = _now()
    url_to_post: Dict[str, int] = {}
    for row in conn.execute("SELECT id, url FROM posts WHERE url IS NOT NULL"):
        url_to_post[row["url"]] = row["id"]

    written = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = (row.get("videoWebUrl") or row.get("submittedVideoUrl")
               or row.get("input"))
        post_id = url_to_post.get(url)
        if post_id is None:
            continue

        text = (row.get("text") or "").strip()
        if len(text) < MIN_COMMENT_CHARS:
            continue

        # cid is the platform's stable comment id; use it to avoid duplicating
        # comments across repeated enrich runs on the same post.
        cid = row.get("cid")
        if cid:
            existing = conn.execute(
                "SELECT 1 FROM comments WHERE post_id = ? AND text = ?",
                (post_id, text),
            ).fetchone()
            if existing:
                continue

        conn.execute(
            "INSERT INTO comments (post_id, text, digg_count, scraped_at) "
            "VALUES (?, ?, ?, ?)",
            (post_id, text, _int(row.get("diggCount")), now),
        )
        written += 1

    conn.commit()
    return written


def ingest_items(
    conn: sqlite3.Connection,
    items: Iterable[Dict],
    own_handles: Iterable[str],
    scrape_run_id: Optional[int] = None,
) -> IngestReport:
    """Write actor items into `posts`, `post_images`, and `comments`."""
    report = IngestReport()
    now = _now()
    own_norm = {h.lstrip("@").strip().lower() for h in own_handles}
    account_ids: Dict[str, int] = {}

    for item in items:
        if not isinstance(item, dict):
            report.skip("not an object")
            continue

        tiktok_id = item.get("id")
        if not tiktok_id:
            report.skip("missing id")
            continue
        tiktok_id = str(tiktok_id)

        handle = _handle_of(item)
        if not handle:
            report.skip("missing author handle")
            continue

        if handle not in account_ids:
            account_ids[handle] = db.upsert_account(
                conn, handle, is_own=handle.lower() in own_norm, now=now
            )
        account_id = account_ids[handle]

        music = item.get("musicMeta") or {}
        slide_links = _slideshow_links(item)
        is_slideshow = bool(item.get("isSlideshow")) or bool(slide_links)

        row = (
            tiktok_id,
            account_id,
            scrape_run_id,
            item.get("webVideoUrl"),
            item.get("text"),
            item.get("createTimeISO"),
            _int(item.get("playCount")),
            _int(item.get("diggCount")),
            _int(item.get("commentCount")),
            _int(item.get("shareCount")),
            _int(item.get("collectCount")),
            1 if is_slideshow else 0,
            len(slide_links),
            music.get("musicId"),
            music.get("musicName"),
            json.dumps(_hashtags_of(item)),
            (item.get("videoMeta") or {}).get("coverUrl"),
            1 if item.get("isAd") else 0,
            1 if item.get("isSponsored") else 0,
            now,
            now,
        )

        existing = conn.execute(
            "SELECT id FROM posts WHERE tiktok_id = ?", (tiktok_id,)
        ).fetchone()

        if existing:
            post_id = existing["id"]
            # Refresh the metrics; a re-scrape of the same post is the point.
            conn.execute(
                """UPDATE posts SET
                     scrape_run_id = ?, url = ?, caption = ?, created_at = ?,
                     play_count = ?, digg_count = ?, comment_count = ?,
                     share_count = ?, collect_count = ?, is_slideshow = ?,
                     slide_count = ?, music_id = ?, music_name = ?,
                     hashtags_json = ?, cover_url = ?, is_ad = ?,
                     is_sponsored = ?, last_seen_at = ?
                   WHERE id = ?""",
                row[2:19] + (now, post_id),
            )
            report.updated += 1
        else:
            cur = conn.execute(
                """INSERT INTO posts (
                     tiktok_id, account_id, scrape_run_id, url, caption,
                     created_at, play_count, digg_count, comment_count,
                     share_count, collect_count, is_slideshow, slide_count,
                     music_id, music_name, hashtags_json, cover_url, is_ad,
                     is_sponsored, first_seen_at, last_seen_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            post_id = cur.lastrowid
            report.inserted += 1

        for idx, url in enumerate(slide_links):
            conn.execute(
                "INSERT OR IGNORE INTO post_images (post_id, slide_index, url) "
                "VALUES (?, ?, ?)",
                (post_id, idx, url),
            )
            report.images += 1

        for comment in item.get("comments") or []:
            text = comment.get("text") if isinstance(comment, dict) else comment
            if not text or len(str(text).strip()) < 15:
                # Sub-15-character comments are emoji, tags, and "first".
                # Filtering here keeps the clustering step honest and cheap.
                continue
            conn.execute(
                "INSERT INTO comments (post_id, text, digg_count, scraped_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    post_id,
                    str(text).strip(),
                    _int((comment or {}).get("diggCount")) if isinstance(comment, dict) else 0,
                    now,
                ),
            )
            report.comments += 1

    conn.commit()
    return report
