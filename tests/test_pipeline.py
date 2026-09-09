"""End-to-end test over synthetic actor output.

Runs the real ingest, scoring, selection, and export code against a fabricated
dataset shaped like clockworks/tiktok-scraper's real output. No network, no
Apify spend, no API keys.

The case that matters most is `test_small_account_outranks_large_account`: it is
the whole reason `reach_index` exists, and the bug it guards against produces
plausible-looking output rather than an error.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from engine import db, export, ingest, pipeline, selection


def _item(
    tiktok_id: str,
    handle: str,
    plays: int,
    saves: int,
    likes: int = 100,
    comments: int = 10,
    shares: int = 5,
    days_ago: int = 1,
    slideshow: bool = True,
    slides: int = 1,
):
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "id": tiktok_id,
        "text": f"caption for {tiktok_id}",
        "createTimeISO": created.isoformat(),
        "authorMeta": {"name": handle, "fans": 10_000},
        "playCount": plays,
        "collectCount": saves,
        "diggCount": likes,
        "commentCount": comments,
        "shareCount": shares,
        "isSlideshow": slideshow,
        "slideshowImageLinks": (
            [{"downloadLink": f"https://x/{tiktok_id}/{i}.jpg"} for i in range(slides)]
            if slideshow else []
        ),
        "webVideoUrl": f"https://www.tiktok.com/@{handle}/video/{tiktok_id}",
        "musicMeta": {"musicId": "m1", "musicName": "sound"},
        "hashtags": [{"name": "fyp"}, {"name": "niche"}],
        "videoMeta": {"coverUrl": f"https://x/{tiktok_id}/cover.jpg"},
        "isAd": False,
    }


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


class TestIngest:
    def test_ingests_a_clean_batch(self, conn):
        items = [_item(f"p{i}", "creator_a", 10_000, 100) for i in range(5)]
        report = ingest.ingest_items(conn, items, own_handles=[])
        assert report.inserted == 5
        assert report.skipped == 0
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 5

    def test_records_slide_images(self, conn):
        ingest.ingest_items(conn, [_item("p1", "a", 1000, 10, slides=7)], [])
        assert conn.execute("SELECT COUNT(*) FROM post_images").fetchone()[0] == 7

    def test_rescrape_updates_metrics_without_duplicating(self, conn):
        ingest.ingest_items(conn, [_item("p1", "a", 1_000, 10)], [])
        ingest.ingest_items(conn, [_item("p1", "a", 50_000, 900)], [])

        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
        row = conn.execute("SELECT play_count, collect_count FROM posts").fetchone()
        assert row["play_count"] == 50_000
        assert row["collect_count"] == 900

    def test_own_account_is_flagged(self, conn):
        ingest.ingest_items(conn, [_item("p1", "me", 1000, 10)], own_handles=["@me"])
        assert conn.execute("SELECT is_own FROM accounts").fetchone()[0] == 1

    # ---- shadow paths ----

    def test_missing_id_is_skipped_not_fatal(self, conn):
        items = [{"authorMeta": {"name": "a"}}, _item("ok", "a", 1000, 10)]
        report = ingest.ingest_items(conn, items, [])
        assert report.inserted == 1
        assert report.reasons["missing id"] == 1

    def test_missing_author_is_skipped_not_fatal(self, conn):
        report = ingest.ingest_items(conn, [{"id": "x"}], [])
        assert report.reasons["missing author handle"] == 1

    def test_null_counts_coerce_to_zero(self, conn):
        item = _item("p1", "a", 0, 0)
        item["playCount"] = None
        item["collectCount"] = None
        report = ingest.ingest_items(conn, [item], [])
        assert report.inserted == 1
        assert conn.execute("SELECT play_count FROM posts").fetchone()[0] == 0

    def test_junk_entries_do_not_abort_the_batch(self, conn):
        items = ["not a dict", None, _item("good", "a", 1000, 10)]
        report = ingest.ingest_items(conn, items, [])
        assert report.inserted == 1
        assert report.skipped == 2


class TestScoringPipeline:
    def test_small_account_outranks_large_account(self, conn):
        """The reason reach_index exists.

        `small` normally gets 1k views and posted a 20k banger (20x).
        `big` normally gets 500k views and posted a 200k dud (0.4x).
        Ranking on raw plays puts the dud on top. That is the failure mode.
        """
        items = []
        for i in range(15):
            items.append(_item(f"s{i}", "small", 1_000, 10, likes=50))
        for i in range(15):
            items.append(_item(f"b{i}", "big", 500_000, 5_000, likes=25_000))

        items.append(_item("small_hit", "small", 20_000, 600, likes=900))
        items.append(_item("big_dud", "big", 200_000, 800, likes=9_000))

        ingest.ingest_items(conn, items, [])
        report = pipeline.score_and_select(conn, n_winners=5, n_losers=3, n_anomalies=3)

        scores = {
            r["tiktok_id"]: r["composite"]
            for r in conn.execute(
                "SELECT p.tiktok_id, s.composite FROM post_scores s "
                "JOIN posts p ON p.id = s.post_id"
            )
        }
        assert scores["small_hit"] > scores["big_dud"]
        assert report.cohort_size == 32

    def test_one_account_cannot_sweep_on_engagement_style_alone(self, conn):
        """Regression: the first live run put all 8 winners on one account.

        `firmscope.co` has a structurally high save rate (3-7%) versus
        `streetsmartcareers` (0-0.6%). Because `save_rate` and `engage_rate`
        were scored in absolute terms while only `reach_index` was normalised,
        70% of the composite weight sat on an account-level trait. The high-save
        account swept every slot, including posts that underperformed its own
        baseline.

        Here `generous` saves at ~10x the rate of `stingy`, but each account has
        one genuine standout. Both standouts must place above their own
        stablemates.
        """
        items = []
        # generous: high absolute save rate, flat performance
        for i in range(15):
            items.append(_item(f"g{i}", "generous", 10_000, 700, likes=500))
        # stingy: low absolute save rate, flat performance
        for i in range(15):
            items.append(_item(f"s{i}", "stingy", 10_000, 70, likes=500))

        # One genuine standout each: 3x their own account's usual save rate.
        items.append(_item("generous_hit", "generous", 30_000, 6_300, likes=1_500))
        items.append(_item("stingy_hit", "stingy", 30_000, 630, likes=1_500))

        ingest.ingest_items(conn, items, [])
        pipeline.score_and_select(conn, n_winners=4, n_losers=3, n_anomalies=2)

        winners = [
            r["tiktok_id"] for r in conn.execute(
                "SELECT p.tiktok_id FROM post_scores s JOIN posts p ON p.id=s.post_id "
                "WHERE s.selected_as='winner'"
            )
        ]
        # The standout from the LOW-save-rate account must still make the batch.
        assert "stingy_hit" in winners
        assert "generous_hit" in winners

        # And the batch must not be a single account's roster.
        handles = {
            r["handle"] for r in conn.execute(
                "SELECT a.handle FROM post_scores s "
                "JOIN posts p ON p.id=s.post_id JOIN accounts a ON a.id=p.account_id "
                "WHERE s.selected_as='winner'"
            )
        }
        assert len(handles) > 1, "winners came from a single account"

    def test_baseline_ignores_other_formats(self, conn):
        """Regression: posts were scored against a mixed-format baseline.

        An account posting mostly high-view video (and multi-slide carousels)
        alongside a few lower-view single images had its single-image baseline
        inflated by the other formats, so even a standout single image scored
        as an underperformer.
        """
        items = []
        # 8 videos and 5 multi-slide carousels at 500k. These must NOT enter
        # the single-image baseline.
        for i in range(8):
            items.append(_item(f"v{i}", "mixed", 500_000, 5_000, slideshow=False))
        for i in range(5):
            items.append(_item(f"m{i}", "mixed", 500_000, 5_000, slides=6))
        # 7 single images at ~10k, one standout at 200k.
        for i in range(6):
            items.append(_item(f"c{i}", "mixed", 10_000, 300, likes=600))
        items.append(_item("single_hit", "mixed", 200_000, 6_000, likes=12_000))

        ingest.ingest_items(conn, items, [])
        pipeline.compute_baselines(conn)

        median_plays = conn.execute(
            "SELECT median_plays FROM account_baselines"
        ).fetchone()["median_plays"]
        # Median of the 7 single images, not of all 20 posts.
        assert median_plays == pytest.approx(10_000), (
            f"baseline {median_plays} was polluted by other formats"
        )

        pipeline.score_and_select(conn, n_winners=2, n_losers=2, n_anomalies=1)
        label = conn.execute(
            "SELECT s.selected_as FROM post_scores s JOIN posts p ON p.id=s.post_id "
            "WHERE p.tiktok_id='single_hit'"
        ).fetchone()["selected_as"]
        assert label == selection.WINNER

    def test_underperforming_post_is_not_a_winner(self, conn):
        """A post below its own account's median must never rank as a winner."""
        items = [_item(f"n{i}", "acct", 10_000, 500, likes=800) for i in range(20)]
        # Same account, far below its own norm on every axis.
        items.append(_item("dud", "acct", 300, 8, likes=15))
        ingest.ingest_items(conn, items, [])
        pipeline.score_and_select(conn, n_winners=5, n_losers=3, n_anomalies=2)

        label = conn.execute(
            "SELECT s.selected_as FROM post_scores s JOIN posts p ON p.id=s.post_id "
            "WHERE p.tiktok_id='dud'"
        ).fetchone()["selected_as"]
        assert label != selection.WINNER

    def test_video_posts_are_excluded_from_the_cohort(self, conn):
        items = [_item(f"c{i}", "a", 1000, 10) for i in range(12)]
        items += [_item(f"v{i}", "a", 9_999_999, 99_999, slideshow=False)
                  for i in range(5)]
        ingest.ingest_items(conn, items, [])
        report = pipeline.score_and_select(conn)
        assert report.cohort_size == 12

    def test_multi_slide_carousels_are_excluded_from_the_cohort(self, conn):
        items = [_item(f"c{i}", "a", 1000, 10) for i in range(12)]
        items += [_item(f"m{i}", "a", 9_999_999, 99_999, slides=6)
                  for i in range(5)]
        ingest.ingest_items(conn, items, [])
        report = pipeline.score_and_select(conn)
        assert report.cohort_size == 12

    def test_slideshow_with_unknown_image_count_is_excluded(self, conn):
        # isSlideshow true but the actor returned no image links: slide_count
        # is 0 and an unknown image count is not "one".
        items = [_item(f"c{i}", "a", 1000, 10) for i in range(12)]
        items.append(_item("unknown", "a", 1000, 10, slides=0))
        ingest.ingest_items(conn, items, [])
        report = pipeline.score_and_select(conn)
        assert report.cohort_size == 12

    def test_own_posts_are_included_by_default(self, conn):
        """Own winners feed the own_rebrand pathway, so they must be scored."""
        items = [_item(f"c{i}", "rival", 1000, 10) for i in range(12)]
        items += [_item(f"m{i}", "me", 1000, 10) for i in range(5)]
        ingest.ingest_items(conn, items, own_handles=["me"])
        report = pipeline.score_and_select(conn)
        assert report.cohort_size == 17

    def test_exclude_own_still_works_when_asked(self, conn):
        items = [_item(f"c{i}", "rival", 1000, 10) for i in range(12)]
        items += [_item(f"m{i}", "me", 1000, 10) for i in range(5)]
        ingest.ingest_items(conn, items, own_handles=["me"])
        report = pipeline.score_and_select(conn, exclude_own=True)
        assert report.cohort_size == 12

    def test_groups_never_overlap(self, conn):
        items = [_item(f"p{i}", "a", 1000 * (i + 1), 10 * (i + 1)) for i in range(60)]
        ingest.ingest_items(conn, items, [])
        pipeline.score_and_select(conn)
        rows = conn.execute(
            "SELECT selected_as, COUNT(*) c FROM post_scores "
            "WHERE selected_as IS NOT NULL GROUP BY selected_as"
        ).fetchall()
        counts = {r["selected_as"]: r["c"] for r in rows}
        assert counts[selection.WINNER] == 30

        # Every selected post carries exactly one label, so the labelled total
        # must equal the number of distinct selected rows.
        labelled = conn.execute(
            "SELECT COUNT(*) FROM post_scores WHERE selected_as IS NOT NULL"
        ).fetchone()[0]
        assert labelled == sum(counts.values())

    def test_rescore_clears_stale_selection_labels(self, conn):
        ingest.ingest_items(
            conn, [_item(f"p{i}", "a", 1000 * (i + 1), 10) for i in range(40)], []
        )
        pipeline.score_and_select(conn, n_winners=30, n_losers=5, n_anomalies=5,
                                  n_reach_only=0)
        first = conn.execute(
            "SELECT COUNT(*) FROM post_scores WHERE selected_as IS NOT NULL"
        ).fetchone()[0]
        pipeline.score_and_select(conn, n_winners=3, n_losers=2, n_anomalies=2,
                                  n_reach_only=0)
        second = conn.execute(
            "SELECT COUNT(*) FROM post_scores WHERE selected_as IS NOT NULL"
        ).fetchone()[0]
        assert second < first, "stale labels from the larger batch survived"
        assert second <= 7

    def test_thin_cohort_warns_instead_of_pretending(self, conn):
        ingest.ingest_items(conn, [_item(f"p{i}", "a", 1000, 10) for i in range(4)], [])
        report = pipeline.score_and_select(conn)
        assert report.rankable is False
        assert "below the minimum" in report.selection.note

    def test_empty_store_does_not_crash(self, conn):
        report = pipeline.score_and_select(conn)
        assert report.cohort_size == 0
        assert report.selection.total == 0

    def test_unparseable_timestamps_are_counted_not_fatal(self, conn):
        items = [_item(f"p{i}", "a", 1000, 10) for i in range(12)]
        items[0]["createTimeISO"] = "not-a-date"
        items[1]["createTimeISO"] = None
        ingest.ingest_items(conn, items, [])
        report = pipeline.score_and_select(conn)
        assert report.undated_posts == 2
        assert report.cohort_size == 12

    def test_identical_posts_produce_no_nan(self, conn):
        """Zero-variance cohort. Review finding F4, at the pipeline level."""
        items = [_item(f"p{i}", "a", 10_000, 100, likes=200) for i in range(20)]
        ingest.ingest_items(conn, items, [])
        pipeline.score_and_select(conn)
        composites = [
            r["composite"] for r in conn.execute("SELECT composite FROM post_scores")
        ]
        assert all(c == c for c in composites)  # NaN != NaN
        assert len(composites) == 20


class TestPerformanceFloors:
    """Regression: a 263-play, 0-like post was selected as a 'winner' because
    rank-based selection pads a thin cohort with whatever exists."""

    def test_tiny_posts_never_selected_even_in_a_thin_cohort(self, conn):
        # Whole cohort is small-time; nothing clears min_plays.
        items = [_item(f"p{i}", "a", 200 + i, 2) for i in range(13)]
        ingest.ingest_items(conn, items, [])
        report = pipeline.score_and_select(conn, min_plays=10_000)
        assert report.selection.total == 0
        assert report.below_floor == 13

    def test_below_baseline_post_fails_reach_floor(self, conn):
        # 20k plays clears min_plays, but 0.9x the account median is not viral.
        items = [_item(f"p{i}", "a", 22_000, 200) for i in range(15)]
        items.append(_item("meh", "a", 20_000, 180))
        ingest.ingest_items(conn, items, [])
        report = pipeline.score_and_select(
            conn, min_plays=10_000, min_reach_multiple=2.0
        )
        selected = {
            r["tiktok_id"] for r in conn.execute(
                "SELECT p.tiktok_id FROM post_scores s "
                "JOIN posts p ON p.id=s.post_id WHERE s.selected_as IS NOT NULL"
            )
        }
        assert "meh" not in selected
        assert selected == set()  # nobody in a flat cohort is 2x their median

    def test_genuine_viral_post_clears_the_floors(self, conn):
        items = [_item(f"p{i}", "a", 10_000, 100) for i in range(15)]
        items.append(_item("banger", "a", 80_000, 2_000))
        ingest.ingest_items(conn, items, [])
        pipeline.score_and_select(
            conn, min_plays=10_000, min_reach_multiple=2.0, max_age_days=30
        )
        selected = {
            r["tiktok_id"] for r in conn.execute(
                "SELECT p.tiktok_id FROM post_scores s "
                "JOIN posts p ON p.id=s.post_id WHERE s.selected_as IS NOT NULL"
            )
        }
        assert selected == {"banger"}

    def test_old_viral_post_fails_age_floor(self, conn):
        items = [_item(f"p{i}", "a", 10_000, 100) for i in range(15)]
        items.append(_item("ancient", "a", 500_000, 9_000, days_ago=75))
        ingest.ingest_items(conn, items, [])
        report = pipeline.score_and_select(
            conn, min_plays=10_000, min_reach_multiple=2.0, max_age_days=30
        )
        assert report.selection.total == 0

    def test_floors_off_preserves_rank_based_behaviour(self, conn):
        items = [_item(f"p{i}", "a", 200 + i, 2) for i in range(13)]
        ingest.ingest_items(conn, items, [])
        report = pipeline.score_and_select(conn)  # floors default to 0 here
        assert report.selection.total > 0
        assert report.below_floor == 0

    def test_floors_still_score_everything_for_export(self, conn):
        items = [_item(f"p{i}", "a", 200 + i, 2) for i in range(13)]
        ingest.ingest_items(conn, items, [])
        pipeline.score_and_select(conn, min_plays=10_000)
        scored = conn.execute("SELECT COUNT(*) FROM post_scores").fetchone()[0]
        assert scored == 13


class TestKnownAccounts:
    def test_only_accounts_with_posts_count_as_known(self, conn):
        ingest.ingest_items(conn, [_item("p1", "Seen", 1000, 10)], [])
        # An account row without posts (e.g. deactivated or pre-registered)
        # must still get a deep first scrape.
        db.upsert_account(conn, "empty", is_own=False,
                          now="2026-01-01T00:00:00+00:00")
        known = pipeline.known_account_handles(conn)
        assert known == {"seen"}  # lowercased for case-insensitive matching

    def test_empty_store_knows_nothing(self, conn):
        assert pipeline.known_account_handles(conn) == set()


class TestSelectedPosts:
    def test_returns_only_the_batch(self, conn):
        ingest.ingest_items(
            conn, [_item(f"p{i}", "a", 1000 * (i + 1), 10) for i in range(40)], []
        )
        pipeline.score_and_select(conn, n_winners=5, n_losers=2, n_anomalies=2,
                                  n_reach_only=2)
        rows = pipeline.selected_posts(conn)
        selected = conn.execute(
            "SELECT COUNT(*) FROM post_scores WHERE selected_as IS NOT NULL"
        ).fetchone()[0]
        assert len(rows) == selected
        assert all(r["url"].startswith("https://www.tiktok.com/") for r in rows)
        assert all(r["selected_as"] is not None for r in rows)
        assert {r["is_own"] for r in rows} == {0}


class TestExport:
    def test_writes_every_view(self, conn, tmp_path):
        ingest.ingest_items(conn, [_item(f"p{i}", "a", 1000, 10) for i in range(15)], [])
        pipeline.score_and_select(conn)
        written = export.to_csv(conn, tmp_path / "exports")
        names = {p.stem for p in written}
        assert "ranked_posts" in names
        assert "analysis_batch" in names
        assert "account_baselines" in names
        for p in written:
            assert p.exists()

    def test_ranked_posts_csv_has_rows(self, conn, tmp_path):
        ingest.ingest_items(conn, [_item(f"p{i}", "a", 1000 * (i + 1), 10)
                                   for i in range(15)], [])
        pipeline.score_and_select(conn)
        export.to_csv(conn, tmp_path / "exports")
        text = (tmp_path / "exports" / "ranked_posts.csv").read_text()
        assert "composite" in text
        assert len(text.strip().splitlines()) == 16  # header + 15

    def test_export_on_empty_store_writes_headers_only(self, conn, tmp_path):
        written = export.to_csv(conn, tmp_path / "exports")
        assert len(written) == len(export.VIEWS)
