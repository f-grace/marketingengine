"""SQLite store and schema.

SQLite is the system of record. The relational chain the engine cares about:

    accounts ─> posts ─> post_scores ─> recreation_prompts
                  │
                  └─> post_images (single image + local download)

A post that scores as a winner gets exactly one recreation prompt
(`recreation_prompts.post_id` is UNIQUE), which is also what makes reruns
idempotent: a post that already has a prompt is skipped, and `emailed_at`
records whether that prompt has left the machine yet.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    handle      TEXT NOT NULL UNIQUE,
    is_own      INTEGER NOT NULL DEFAULT 0,   -- our account vs an idea source
    active      INTEGER NOT NULL DEFAULT 1,
    added_at    TEXT NOT NULL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id                INTEGER PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    pass_name         TEXT NOT NULL,          -- 'A' wide sweep
    actor_input_json  TEXT NOT NULL,
    raw_path          TEXT,                   -- raw dataset on disk, pre-parse
    result_count      INTEGER,
    apify_run_id      TEXT,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY,
    tiktok_id       TEXT NOT NULL UNIQUE,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    scrape_run_id   INTEGER REFERENCES scrape_runs(id),
    url             TEXT,
    caption         TEXT,
    created_at      TEXT,                     -- createTimeISO
    play_count      INTEGER DEFAULT 0,
    digg_count      INTEGER DEFAULT 0,
    comment_count   INTEGER DEFAULT 0,
    share_count     INTEGER DEFAULT 0,
    collect_count   INTEGER DEFAULT 0,        -- SAVES. primary quality signal.
    is_slideshow    INTEGER DEFAULT 0,
    slide_count     INTEGER DEFAULT 0,
    music_id        TEXT,
    music_name      TEXT,
    hashtags_json   TEXT,
    cover_url       TEXT,
    is_ad           INTEGER DEFAULT 0,
    is_sponsored    INTEGER DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_account   ON posts(account_id);
CREATE INDEX IF NOT EXISTS idx_posts_slideshow ON posts(is_slideshow);
CREATE INDEX IF NOT EXISTS idx_posts_created   ON posts(created_at);

CREATE TABLE IF NOT EXISTS post_images (
    id          INTEGER PRIMARY KEY,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    slide_index INTEGER NOT NULL,
    url         TEXT,
    local_path  TEXT,
    UNIQUE(post_id, slide_index)
);

CREATE TABLE IF NOT EXISTS account_baselines (
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    computed_at     TEXT NOT NULL,
    median_plays    REAL,
    median_saves    REAL,
    median_engage   REAL,
    median_shape    REAL,                     -- saves per like
    n_posts         INTEGER NOT NULL,
    PRIMARY KEY (account_id, computed_at)
);

CREATE TABLE IF NOT EXISTS post_scores (
    post_id       INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
    scored_at     TEXT NOT NULL,
    save_rate     REAL,
    engage_rate   REAL,
    reach_index   REAL,
    shape         REAL,
    anomaly       REAL,
    recency       REAL,
    composite     REAL,
    cohort        TEXT,                       -- e.g. 'single_image'
    selected_as   TEXT                        -- winner | reach_only | NULL
);

CREATE INDEX IF NOT EXISTS idx_scores_composite ON post_scores(composite DESC);
CREATE INDEX IF NOT EXISTS idx_scores_selected  ON post_scores(selected_as);

-- One recreation prompt per winning post. UNIQUE post_id is the rerun-dedup
-- mechanism; emailed_at is NULL until a digest containing the prompt sends.
CREATE TABLE IF NOT EXISTS recreation_prompts (
    id           INTEGER PRIMARY KEY,
    post_id      INTEGER NOT NULL UNIQUE REFERENCES posts(id) ON DELETE CASCADE,
    pathway      TEXT NOT NULL,        -- 'own_rebrand' | 'source_recreate'
    selected_as  TEXT,                 -- winner | reach_only, at generation time
    prompt_path  TEXT,
    image_path   TEXT,                 -- local image copy attached to the email
    created_at   TEXT NOT NULL,
    emailed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_prompts_unemailed
    ON recreation_prompts(emailed_at) WHERE emailed_at IS NULL;
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open the store, creating the schema if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_account(
    conn: sqlite3.Connection, handle: str, is_own: bool, now: str,
    notes: Optional[str] = None,
) -> int:
    """Insert an account if new, returning its id either way."""
    handle = handle.lstrip("@").strip()
    cur = conn.execute("SELECT id FROM accounts WHERE handle = ?", (handle,))
    row = cur.fetchone()
    if row:
        conn.execute(
            "UPDATE accounts SET is_own = ?, active = 1 WHERE id = ?",
            (1 if is_own else 0, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO accounts (handle, is_own, active, added_at, notes) "
        "VALUES (?, ?, 1, ?, ?)",
        (handle, 1 if is_own else 0, now, notes),
    )
    return cur.lastrowid
