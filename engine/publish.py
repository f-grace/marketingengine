"""Push rendered slide decks to TikTok drafts via Upload-Post.

Upload-Post runs an approved TikTok Content Posting API integration, which
removes three things we would otherwise have to build and wait on: our own
developer app, the OAuth flow, and TikTok's 2-4 week audit. It also accepts
multipart file uploads, so slides do not need to be hosted at a public URL.

    rendered PNGs ──> POST /api/upload_photos ──> TikTok inbox
                      post_mode=MEDIA_UPLOAD          │
                                                      ▼
                                            you finish and post in-app

MEDIA_UPLOAD is the only mode this module will use by default. Publishing
straight to a live account is the one irreversible action in the whole system,
and on a new account a bad post costs distribution that does not come back.
DIRECT_POST requires passing allow_direct=True explicitly.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .config import load_env_file

ENDPOINT = "https://api.upload-post.com/api/upload_photos"

DRAFT = "MEDIA_UPLOAD"
DIRECT = "DIRECT_POST"

# TikTok caps a photo post at 10 images.
MAX_SLIDES = 10

# Upload-Post plan note: TikTok is excluded from the free tier. Basic is $24/mo.
PLAN_NOTE = "TikTok uploads need a paid Upload-Post plan (Basic, $24/mo)."


class PublishError(RuntimeError):
    """Upload failed, or configuration is missing."""


@dataclass
class PublishResult:
    idea_id: str
    ok: bool
    mode: str
    slides: int
    external_ref: Optional[str]
    detail: str


def api_key() -> str:
    load_env_file()
    key = os.environ.get("UPLOAD_POST_API_KEY", "").strip()
    if not key:
        raise PublishError(
            "UPLOAD_POST_API_KEY is not set.\n"
            "  1. Create an account at upload-post.com and connect @lynkoai\n"
            f"  2. {PLAN_NOTE}\n"
            "  3. Put the key in .env as UPLOAD_POST_API_KEY=...\n"
            "The key is read from the environment only and never stored."
        )
    return key


def profile_user() -> str:
    """The Upload-Post profile identifier the TikTok account is connected under."""
    load_env_file()
    user = os.environ.get("UPLOAD_POST_USER", "").strip()
    if not user:
        raise PublishError(
            "UPLOAD_POST_USER is not set. It is the profile name you connected "
            "your TikTok account under in Upload-Post. Add it to .env."
        )
    return user


def caption_for(idea: Dict) -> str:
    """Caption plus hashtags, as one string.

    Upload-Post takes a single `title`. TikTok reads hashtags out of the caption
    body, so they are appended rather than sent separately.
    """
    caption = (idea.get("caption_draft") or "").strip()
    tags = json.loads(idea.get("hashtags_json") or "[]")
    if tags:
        caption = f"{caption}\n\n" + " ".join(f"#{t.lstrip('#')}" for t in tags)
    return caption.strip()


def upload_deck(
    slide_paths: List[Path],
    caption: str,
    mode: str = DRAFT,
    timeout: int = 180,
    allow_direct: bool = False,
) -> Dict:
    """POST the deck. Returns the parsed response body."""
    if not slide_paths:
        raise PublishError("No slides to upload. Run `python -m engine render` first.")
    if len(slide_paths) > MAX_SLIDES:
        raise PublishError(
            f"{len(slide_paths)} slides exceeds TikTok's {MAX_SLIDES}-image limit "
            "for a photo post. Trim the deck."
        )
    missing = [p for p in slide_paths if not p.exists()]
    if missing:
        raise PublishError(f"Missing rendered slides: {missing[0]} (and {len(missing)-1} more)")

    if mode == DIRECT and not allow_direct:
        raise PublishError(
            "Refusing DIRECT_POST without allow_direct=True. Publishing to a live "
            "account is irreversible, and on a new account a bad post costs "
            "distribution permanently. Use draft mode."
        )

    files = [("photos[]", (p.name, p.read_bytes(), "image/png")) for p in slide_paths]
    data = {
        "title": caption,
        "user": profile_user(),
        "platform[]": "tiktok",
        "post_mode": mode,
        # Our slides are photographs composited with drawn type, not model
        # output, so this is false. Flip it if backgrounds ever come from an
        # image model: TikTok requires AI-generated content to be disclosed.
        "is_aigc": "false",
    }

    try:
        response = requests.post(
            ENDPOINT,
            headers={"Authorization": f"Apikey {api_key()}"},
            data=data,
            files=files,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PublishError(f"Upload-Post request failed: {exc}") from exc

    if response.status_code >= 400:
        raise PublishError(
            f"Upload-Post returned {response.status_code}: {response.text[:400]}"
        )

    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:400]}


def record_handoff(conn, idea_id: str, external_ref: Optional[str]) -> None:
    """Mark the idea handed off and store the join key.

    This is the Phase 3 contract from PLAN.md section 3: the idea UUID has to
    survive into TikTok and come back attached to the published URL, or results
    can never be traced to the pattern that produced them.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO handoffs (idea_id, handed_off_at, external_ref) "
        "VALUES (?, ?, ?)",
        (idea_id, now, external_ref),
    )
    conn.execute("UPDATE ideas SET status = 'handed_off' WHERE id = ?", (idea_id,))
    conn.commit()


def already_handed_off(conn, idea_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM handoffs WHERE idea_id = ?", (idea_id,)
    ).fetchone()
    return row is not None
