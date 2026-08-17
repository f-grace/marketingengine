"""Tests for the pure scoring math.

The failure these exist to prevent: silently producing confident-looking
rankings from data that cannot support them.
"""

from __future__ import annotations

import math

import pytest

from engine import scoring


class TestRates:
    def test_save_rate(self):
        assert scoring.save_rate(50, 1000) == pytest.approx(0.05)

    def test_save_rate_survives_zero_views(self):
        # A brand new post can genuinely have zero views. Must not divide by zero.
        assert scoring.save_rate(0, 0) == 0.0

    def test_engage_rate_sums_all_interactions(self):
        rate = scoring.engage_rate(
            digg_count=100, comment_count=20, share_count=10, collect_count=70,
            play_count=1000,
        )
        assert rate == pytest.approx(0.2)


class TestReachIndex:
    def test_outperformer_on_small_account(self):
        # 40k views on an account that normally gets 8k is a 5x hit.
        assert scoring.reach_index(40_000, 8_000) == pytest.approx(5.0)

    def test_underperformer_on_large_account(self):
        # 200k views on an account that normally gets 400k is a flop, even
        # though the absolute number dwarfs the example above. This is the
        # whole reason the function exists.
        assert scoring.reach_index(200_000, 400_000) == pytest.approx(0.5)

    def test_missing_baseline_is_neutral_not_zero(self):
        assert scoring.reach_index(50_000, None) == 1.0
        assert scoring.reach_index(50_000, 0) == 1.0


class TestRecency:
    def test_today_is_full_weight(self):
        assert scoring.recency_weight(0) == pytest.approx(1.0)

    def test_one_tau_decays_to_1_over_e(self):
        assert scoring.recency_weight(21.0) == pytest.approx(1 / math.e)

    def test_old_posts_are_nearly_worthless(self):
        assert scoring.recency_weight(90) < 0.02

    def test_negative_age_from_clock_skew_is_clamped(self):
        # Must not amplify a post above 1.0 because of a bad timestamp.
        assert scoring.recency_weight(-5) == 1.0


class TestZScoresGuard:
    """Review finding F4."""

    def test_normal_cohort(self):
        z = scoring.zscores([1.0, 2.0, 3.0])
        assert z[1] == pytest.approx(0.0)
        assert z[0] < 0 < z[2]

    def test_zero_variance_returns_neutral_not_nan(self):
        # Every post performed identically. The textbook formula divides by
        # zero here and poisons everything downstream with NaN.
        z = scoring.zscores([0.05, 0.05, 0.05, 0.05])
        assert z == [0.0, 0.0, 0.0, 0.0]
        assert not any(math.isnan(v) for v in z)

    def test_single_post_cohort(self):
        assert scoring.zscores([0.42]) == [0.0]

    def test_empty_cohort(self):
        assert scoring.zscores([]) == []

    def test_never_returns_nan_for_near_identical_values(self):
        z = scoring.zscores([0.05, 0.05 + 1e-15, 0.05])
        assert not any(math.isnan(v) for v in z)


class TestCohortRankability:
    def test_thin_cohort_is_not_rankable(self):
        assert scoring.cohort_is_rankable(3) is False

    def test_healthy_cohort_is_rankable(self):
        assert scoring.cohort_is_rankable(50) is True

    def test_boundary(self):
        assert scoring.cohort_is_rankable(scoring.MIN_COHORT_SIZE) is True
        assert scoring.cohort_is_rankable(scoring.MIN_COHORT_SIZE - 1) is False


class TestEngagementShape:
    def test_saves_relative_to_likes(self):
        # 100 saves, 200 likes -> shape 0.5
        assert scoring.engagement_shape(100, 200, 10_000) == pytest.approx(0.5)

    def test_no_likes_is_defined(self):
        assert scoring.engagement_shape(100, 0, 10_000) == 0.0


class TestAnomalyScores:
    def test_typical_post_scores_near_zero(self):
        shapes = [0.5, 0.5, 0.5, 0.5]
        assert scoring.anomaly_scores(shapes) == pytest.approx([0.0, 0.0, 0.0, 0.0])

    def test_outlier_in_both_directions_is_flagged(self):
        # 2x and 0.5x the norm should be equally unusual.
        shapes = [0.5, 0.5, 0.5, 1.0, 0.25]
        scores = scoring.anomaly_scores(shapes)
        assert scores[3] == pytest.approx(scores[4], rel=1e-6)
        assert scores[3] > scores[0]

    def test_all_zero_shapes_do_not_explode(self):
        assert scoring.anomaly_scores([0.0, 0.0]) == [0.0, 0.0]


class TestComposite:
    def test_ordering_follows_performance(self):
        saves = [0.01, 0.05, 0.10]
        engage = [0.05, 0.10, 0.20]
        reach = [0.5, 1.0, 4.0]
        recency = [1.0, 1.0, 1.0]

        scores = scoring.composite_scores(saves, engage, reach, recency)
        assert scores[0] < scores[1] < scores[2]

    def test_recency_penalises_stale_winners(self):
        saves = [0.10, 0.10]
        engage = [0.20, 0.20]
        reach = [4.0, 4.0]
        # Identical performance, but the second post is three weeks older.
        recency = [1.0, scoring.recency_weight(21)]

        scores = scoring.composite_scores(saves, engage, reach, recency)
        assert scores[0] >= scores[1]

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError, match="equal-length"):
            scoring.composite_scores([0.1], [0.1, 0.2], [1.0], [1.0])

    def test_degenerate_cohort_produces_no_nan(self):
        # Every post identical: z-scores all collapse, composite must stay finite.
        scores = scoring.composite_scores(
            [0.05] * 4, [0.1] * 4, [1.0] * 4, [1.0] * 4
        )
        assert all(math.isfinite(s) for s in scores)


class TestAccountMedian:
    def test_median_of_history(self):
        assert scoring.account_median_plays([100, 200, 300]) == 200

    def test_no_history_returns_none(self):
        assert scoring.account_median_plays([]) is None
