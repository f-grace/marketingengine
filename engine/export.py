"""Export the store to inspectable views.

SQLite is the system of record. This module produces the human-facing copy:
CSVs that always work and need no setup or credentials.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

# One query per file. These are views, not tables: they join the numbers back
# to the handles and captions so a human can actually read them.
VIEWS: Dict[str, str] = {
    "ranked_posts": """
        SELECT
            a.handle,
            a.is_own,
            p.tiktok_id,
            p.url,
            substr(COALESCE(p.caption, ''), 1, 200) AS caption,
            p.created_at,
            p.play_count,
            p.collect_count AS saves,
            p.digg_count AS likes,
            p.comment_count,
            p.share_count,
            p.slide_count,
            ROUND(s.save_rate, 5)   AS save_rate,
            ROUND(s.engage_rate, 5) AS engage_rate,
            ROUND(s.reach_index, 3) AS reach_index,
            ROUND(s.anomaly, 3)     AS anomaly,
            ROUND(s.recency, 3)     AS recency,
            ROUND(s.composite, 4)   AS composite,
            s.selected_as
        FROM posts p
        JOIN post_scores s ON s.post_id = p.id
        JOIN accounts a    ON a.id = p.account_id
        ORDER BY s.composite DESC
    """,
    "analysis_batch": """
        SELECT
            s.selected_as,
            a.handle,
            a.is_own,
            p.url,
            substr(COALESCE(p.caption, ''), 1, 200) AS caption,
            ROUND(s.reach_index, 3) AS reach_index,
            ROUND(s.save_rate, 5)   AS save_rate,
            ROUND(s.composite, 4)   AS composite
        FROM post_scores s
        JOIN posts p    ON p.id = s.post_id
        JOIN accounts a ON a.id = p.account_id
        WHERE s.selected_as IS NOT NULL
        ORDER BY s.selected_as, s.composite DESC
    """,
    "account_baselines": """
        SELECT
            a.handle,
            a.is_own,
            b.n_posts,
            ROUND(b.median_plays, 1)  AS median_plays,
            ROUND(b.median_saves, 5)  AS median_save_rate,
            ROUND(b.median_engage, 5) AS median_engage_rate,
            ROUND(b.median_shape, 4)  AS median_saves_per_like,
            b.computed_at
        FROM account_baselines b
        JOIN accounts a ON a.id = b.account_id
        WHERE b.computed_at = (
            SELECT MAX(computed_at) FROM account_baselines
            WHERE account_id = b.account_id
        )
        ORDER BY b.median_plays DESC
    """,
}


def _fetch(conn: sqlite3.Connection, sql: str) -> Tuple[List[str], List[tuple]]:
    cur = conn.execute(sql)
    headers = [d[0] for d in cur.description]
    return headers, cur.fetchall()


def to_csv(conn: sqlite3.Connection, out_dir: Path) -> List[Path]:
    """Write one CSV per view. Always available, no credentials needed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, sql in VIEWS.items():
        headers, rows = _fetch(conn, sql)
        path = out_dir / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            writer.writerows(rows)
        written.append(path)
    return written
