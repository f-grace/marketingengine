"""Baseline, score, and select. The analytical core.

    posts (all)
        │
        ├─ per-account baselines   median plays / saves / engagement / shape
        │
        ▼
    carousel cohort only          isSlideshow == true
        │                          mixing video and photo posts into one
        │                          distribution makes every statistic meaningless
        ▼
    scores                        rates -> reach_index -> z -> composite
        │
        ▼
    selection                     winners + losers + anomalies

Everything numeric here delegates to `scoring`, which is pure and tested. This
module's job is only to move rows in and out of SQLite.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import scoring, selection

COHORT_SLIDESHOW = "slideshow"


@dataclass
class ScoreReport:
    cohort_size: int
    rankable: bool
    selection: selection.Selection
    baselines_computed: int
    undated_posts: int

    def summary(self) -> str:
        lines = [
            f"cohort: {self.cohort_size} carousel posts",
            f"baselines: {self.baselines_computed} accounts",
            f"selected: {len(self.selection.winners)} winners, "
            f"{len(self.selection.reach_only)} reach-only, "
            f"{len(self.selection.anomalies)} anomalies, "
            f"{len(self.selection.losers)} losers",
        ]
        if self.undated_posts:
            lines.append(
                f"WARNING: {self.undated_posts} posts had an unreadable "
                "createTimeISO and were treated as brand new"
            )
        if not self.rankable:
            lines.append(f"WARNING: {self.selection.note}")
        return "\n".join("  " + line for line in lines)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_since(created_at: Optional[str], now: datetime) -> Optional[float]:
    """Age in days, or None when the timestamp is missing or unparseable."""
    if not created_at:
        return None
    text = str(created_at).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def compute_baselines(
    conn: sqlite3.Connection, history: int = 30, slideshow_only: bool = True
) -> int:
    """Per-account medians over that account's most recent posts IN THIS FORMAT.

    This is what every lift divides by, and therefore the step that stops the
    pipeline from simply learning that large accounts post large numbers.

    `slideshow_only` matters more than it looks. The cohort being scored is
    carousels, so the baseline has to be carousels too. Taking the median across
    an account's videos and carousels together compares a carousel against a
    denominator largely made of videos, and any creator whose videos outperform
    their carousels then has every carousel look like a failure.

    Observed live: an account posting 7 carousels and 13 videos had its best
    carousel (19.6x the mixed-format median on reach) fall out of the winners
    entirely, because the denominator was wrong.

    If the pipeline ever scores video as well, `account_baselines` needs a
    cohort column so the two formats keep separate denominators.
    """
    now_iso = _now().isoformat()
    accounts = conn.execute("SELECT id FROM accounts WHERE active = 1").fetchall()
    computed = 0
    cohort_filter = "AND is_slideshow = 1" if slideshow_only else ""

    for account in accounts:
        rows = conn.execute(
            f"""SELECT play_count, digg_count, comment_count, share_count,
                       collect_count
                FROM posts
                WHERE account_id = ? {cohort_filter}
                ORDER BY created_at DESC
                LIMIT ?""",
            (account["id"], history),
        ).fetchall()

        if not rows:
            continue

        plays = [r["play_count"] for r in rows]
        saves = [
            scoring.save_rate(r["collect_count"], r["play_count"]) for r in rows
        ]
        engages = [
            scoring.engage_rate(
                r["digg_count"], r["comment_count"], r["share_count"],
                r["collect_count"], r["play_count"],
            )
            for r in rows
        ]
        shapes = [
            scoring.engagement_shape(r["collect_count"], r["digg_count"],
                                     r["play_count"])
            for r in rows
        ]

        conn.execute(
            """INSERT OR REPLACE INTO account_baselines
               (account_id, computed_at, median_plays, median_saves,
                median_engage, median_shape, n_posts)
               VALUES (?,?,?,?,?,?,?)""",
            (
                account["id"],
                now_iso,
                scoring.account_median_plays(plays),
                scoring.account_median_plays(saves),
                scoring.account_median_plays(engages),
                scoring.account_median_plays(shapes),
                len(rows),
            ),
        )
        computed += 1

    conn.commit()
    return computed


def _latest_baselines(conn: sqlite3.Connection) -> Dict[int, sqlite3.Row]:
    rows = conn.execute(
        """SELECT b.* FROM account_baselines b
           JOIN (SELECT account_id, MAX(computed_at) AS mx
                 FROM account_baselines GROUP BY account_id) latest
             ON b.account_id = latest.account_id AND b.computed_at = latest.mx"""
    ).fetchall()
    return {r["account_id"]: r for r in rows}


def score_and_select(
    conn: sqlite3.Connection,
    n_winners: int = selection.DEFAULT_N_WINNERS,
    n_losers: int = selection.DEFAULT_N_LOSERS,
    n_anomalies: int = selection.DEFAULT_N_ANOMALIES,
    n_reach_only: int = selection.DEFAULT_N_REACH_ONLY,
    exclude_own: bool = True,
) -> ScoreReport:
    """Score the carousel cohort and pick the analysis batch."""
    baselines_computed = compute_baselines(conn)
    baselines = _latest_baselines(conn)
    now = _now()

    own_filter = "AND a.is_own = 0" if exclude_own else ""
    posts = conn.execute(
        f"""SELECT p.*, a.is_own
            FROM posts p JOIN accounts a ON a.id = p.account_id
            WHERE p.is_slideshow = 1 AND p.is_ad = 0 {own_filter}"""
    ).fetchall()

    if not posts:
        empty = selection.select([], rankable=False)
        return ScoreReport(0, False, empty, baselines_computed, 0)

    undated = 0
    save_rates: List[float] = []
    engage_rates: List[float] = []
    reach_indices: List[float] = []
    recencies: List[float] = []
    shapes: List[float] = []

    save_lifts: List[float] = []
    engage_lifts: List[float] = []

    for p in posts:
        baseline = baselines.get(p["account_id"])
        median_plays = baseline["median_plays"] if baseline else None
        median_saves = baseline["median_saves"] if baseline else None
        median_engage = baseline["median_engage"] if baseline else None

        # Raw rates for display and export; smoothed rates for ranking. A post
        # with 8 saves on 358 views should not out-rank one with 2,296 saves on
        # 32,000 just because 8/358 is a bigger fraction.
        save_rates.append(scoring.save_rate(p["collect_count"], p["play_count"]))
        engage_rates.append(
            scoring.engage_rate(
                p["digg_count"], p["comment_count"], p["share_count"],
                p["collect_count"], p["play_count"],
            )
        )

        sr = scoring.smoothed_rate(p["collect_count"], p["play_count"], median_saves)
        er = scoring.smoothed_rate(
            p["digg_count"] + p["comment_count"] + p["share_count"]
            + p["collect_count"],
            p["play_count"], median_engage,
        )

        # Every term is normalised against the post's OWN account. Mixing a
        # normalised reach term with absolute rate terms let one account's
        # engagement style sweep the rankings.
        save_lifts.append(scoring.lift(sr, median_saves))
        engage_lifts.append(scoring.lift(er, median_engage))
        reach_indices.append(scoring.reach_index(p["play_count"], median_plays))

        age = _days_since(p["created_at"], now)
        if age is None:
            undated += 1
            age = 0.0
        recencies.append(scoring.recency_weight(age))

        shapes.append(
            scoring.engagement_shape(p["collect_count"], p["digg_count"],
                                     p["play_count"])
        )

    composites = scoring.composite_scores(
        save_lifts, engage_lifts, reach_indices, recencies
    )
    anomalies = scoring.anomaly_scores(shapes)
    rankable = scoring.cohort_is_rankable(len(posts))

    candidates = [
        selection.Candidate(
            post_id=str(p["id"]),
            composite=composites[i],
            anomaly=anomalies[i],
            reach=reach_indices[i],
            engagement=engage_lifts[i],
        )
        for i, p in enumerate(posts)
    ]
    picked = selection.select(
        candidates, rankable=rankable,
        n_winners=n_winners, n_losers=n_losers, n_anomalies=n_anomalies,
        n_reach_only=n_reach_only,
    )

    now_iso = now.isoformat()
    conn.execute("UPDATE post_scores SET selected_as = NULL")
    for i, p in enumerate(posts):
        pid = str(p["id"])
        label = None
        if pid in picked.winners:
            label = selection.WINNER
        elif pid in picked.reach_only:
            label = selection.REACH_ONLY
        elif pid in picked.anomalies:
            label = selection.ANOMALY
        elif pid in picked.losers:
            label = selection.LOSER

        conn.execute(
            """INSERT OR REPLACE INTO post_scores
               (post_id, scored_at, save_rate, engage_rate, reach_index,
                shape, anomaly, recency, composite, cohort, selected_as)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p["id"], now_iso, save_rates[i], engage_rates[i],
                reach_indices[i], shapes[i], anomalies[i], recencies[i],
                composites[i], COHORT_SLIDESHOW, label,
            ),
        )
    conn.commit()

    return ScoreReport(
        cohort_size=len(posts),
        rankable=rankable,
        selection=picked,
        baselines_computed=baselines_computed,
        undated_posts=undated,
    )


def selected_post_urls(conn: sqlite3.Connection) -> List[str]:
    """URLs of the current analysis batch, for the Pass B enrichment run."""
    rows = conn.execute(
        """SELECT p.url FROM posts p
           JOIN post_scores s ON s.post_id = p.id
           WHERE s.selected_as IS NOT NULL AND p.url IS NOT NULL"""
    ).fetchall()
    return [r["url"] for r in rows]
