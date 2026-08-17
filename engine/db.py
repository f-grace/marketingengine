"""SQLite store and schema.

SQLite is the system of record; Google Sheets is a generated view of it. The
reason is the relational chain, which Sheets cannot join across:

    posts ─> post_scores ─> analyses ─> clusters ─> ideas ─> publications
                                                      │           │
                                                      └── UUID ───┘
                                                   the handoff join key

The whole point of Phase 3 is following that chain from a published post back to
the pattern that produced it. `ideas.id` is a UUID assigned before handoff and
returned attached to the published URL; without it the feedback loop cannot
exist. See PLAN.md section 3.

Phase 2 and 3 tables are created up front even though nothing writes to them
yet, so there is no migration step later.
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
    is_own      INTEGER NOT NULL DEFAULT 0,   -- our account vs a competitor
    active      INTEGER NOT NULL DEFAULT 1,
    added_at    TEXT NOT NULL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id                INTEGER PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    pass_name         TEXT NOT NULL,          -- 'A' wide, 'B' enrich
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

CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    digg_count  INTEGER DEFAULT 0,
    scraped_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);

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
    cohort        TEXT,                       -- e.g. 'slideshow'
    selected_as   TEXT                        -- winner | loser | anomaly | NULL
);

CREATE INDEX IF NOT EXISTS idx_scores_composite ON post_scores(composite DESC);
CREATE INDEX IF NOT EXISTS idx_scores_selected  ON post_scores(selected_as);

-- ---------- Phase 2 ----------

CREATE TABLE IF NOT EXISTS analyses (
    id                   INTEGER PRIMARY KEY,
    post_id              INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    model                TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    selected_as          TEXT,
    topic                TEXT,
    hook_text            TEXT,
    hook_shape           TEXT,
    target_audience      TEXT,
    pain_point           TEXT,
    content_angle        TEXT,
    slide_structure_json TEXT,
    visual_style         TEXT,
    cta_type             TEXT,
    cta_slide            INTEGER,
    why_it_performed     TEXT,
    relevance_to_us      REAL,
    repeatability        REAL,
    raw_json             TEXT
);

CREATE INDEX IF NOT EXISTS idx_analyses_post  ON analyses(post_id);
CREATE INDEX IF NOT EXISTS idx_analyses_shape ON analyses(hook_shape);

CREATE TABLE IF NOT EXISTS clusters (
    id           INTEGER PRIMARY KEY,
    computed_at  TEXT NOT NULL,
    label        TEXT,
    member_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id  INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    PRIMARY KEY (cluster_id, analysis_id)
);

-- Winner-vs-loser frequency comparison. An attribute present in 90% of winners
-- and 85% of losers explains nothing; this table is what makes that visible.
CREATE TABLE IF NOT EXISTS attribute_contrasts (
    id          INTEGER PRIMARY KEY,
    computed_at TEXT NOT NULL,
    attribute   TEXT NOT NULL,
    value       TEXT NOT NULL,
    winner_freq REAL,
    loser_freq  REAL,
    lift        REAL
);

CREATE TABLE IF NOT EXISTS ideas (
    id                       TEXT PRIMARY KEY,   -- UUID. the handoff join key.
    created_at               TEXT NOT NULL,
    cluster_id               INTEGER REFERENCES clusters(id),
    source_analysis_ids_json TEXT,
    concept                  TEXT,
    hook                     TEXT,
    caption_draft            TEXT,
    hashtags_json            TEXT,              -- carried from the winner
    slide_outline_json       TEXT,
    suggested_music_id       TEXT,
    rationale                TEXT,
    status                   TEXT NOT NULL DEFAULT 'proposed',
    approved_at              TEXT,
    edited_by_human          INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas(status);

-- ---------- Phase 3 ----------

CREATE TABLE IF NOT EXISTS handoffs (
    idea_id       TEXT PRIMARY KEY REFERENCES ideas(id),
    handed_off_at TEXT NOT NULL,
    external_ref  TEXT
);

CREATE TABLE IF NOT EXISTS publications (
    id           INTEGER PRIMARY KEY,
    idea_id      TEXT REFERENCES ideas(id),
    tiktok_url   TEXT NOT NULL UNIQUE,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS own_performance (
    id             INTEGER PRIMARY KEY,
    publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    measured_at    TEXT NOT NULL,
    play_count     INTEGER,
    digg_count     INTEGER,
    comment_count  INTEGER,
    share_count    INTEGER,
    collect_count  INTEGER
);
"""

# Valid transitions for ideas.status.
#
#   proposed ──> approved ──> handed_off ──> published ──> measured
#       │            ^
#       ├──> edited ─┘
#       └──> rejected (terminal)
IDEA_TRANSITIONS = {
    "proposed": {"approved", "edited", "rejected"},
    "edited": {"approved", "rejected"},
    "approved": {"handed_off", "rejected"},
    "handed_off": {"published"},
    "published": {"measured"},
    "measured": set(),
    "rejected": set(),
}


def can_transition(current: str, target: str) -> bool:
    """Whether an idea may move from `current` to `target`."""
    return target in IDEA_TRANSITIONS.get(current, set())


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
