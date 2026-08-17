"""Tests for the analysis-batch selector.

The property that matters most: the three groups never overlap, because a post
analysed twice is money spent twice and a contrast computed against itself.
"""

from __future__ import annotations

from engine import selection
from engine.selection import Candidate


def _cohort(n: int) -> list:
    """n candidates with descending composite, reach, and engagement.

    Reach and engagement track the composite so the quadrant logic has a real
    gradient to work with rather than every post looking identical on both axes.
    """
    return [
        Candidate(
            post_id=f"p{i}",
            composite=float(n - i),
            anomaly=0.0,
            reach=float(n - i) / n * 3.0,
            engagement=float(n - i) / n * 3.0,
        )
        for i in range(n)
    ]


class TestBasicSplit:
    def test_takes_requested_counts(self):
        sel = selection.select(_cohort(100), rankable=True)
        assert len(sel.winners) == selection.DEFAULT_N_WINNERS
        assert len(sel.losers) == selection.DEFAULT_N_LOSERS

    def test_winners_are_the_top_scorers(self):
        sel = selection.select(_cohort(100), rankable=True)
        assert sel.winners[0] == "p0"

    def test_losers_are_the_bottom_scorers(self):
        sel = selection.select(_cohort(100), rankable=True)
        assert sel.losers[0] == "p99"


class TestNoOverlap:
    def test_groups_are_disjoint(self):
        sel = selection.select(_cohort(100), rankable=True)
        assert len(set(sel.all_ids)) == sel.total

    def test_small_cohort_does_not_double_count(self):
        # 20 posts but far more slots than that requested. Must degrade to
        # fewer selections, never reuse a post.
        sel = selection.select(_cohort(20), rankable=True)
        assert len(set(sel.all_ids)) == sel.total
        assert sel.total <= 20

    def test_high_scoring_anomaly_is_labelled_winner(self):
        cands = [
            Candidate("top", composite=10.0, anomaly=99.0, reach=5.0, engagement=5.0),
            Candidate("mid", composite=5.0, anomaly=0.1, reach=1.0, engagement=1.0),
            Candidate("low", composite=1.0, anomaly=0.1, reach=0.2, engagement=0.2),
        ]
        sel = selection.select(cands, rankable=True, n_winners=1, n_losers=1,
                               n_anomalies=1, n_reach_only=0)
        assert "top" in sel.winners
        assert "top" not in sel.anomalies
        assert sel.label_for("top") == selection.WINNER


class TestAnomalySelector:
    def test_picks_odd_shapes_not_top_performers(self):
        cands = [
            Candidate("a", composite=10.0, anomaly=0.0, reach=3.0, engagement=3.0),
            Candidate("b", composite=9.0, anomaly=0.0, reach=2.5, engagement=2.5),
            Candidate("c", composite=1.0, anomaly=5.0, reach=1.0, engagement=1.0),
            Candidate("d", composite=0.5, anomaly=0.0, reach=0.2, engagement=0.2),
        ]
        sel = selection.select(cands, rankable=True, n_winners=2, n_losers=1,
                               n_anomalies=1, n_reach_only=0)
        assert sel.anomalies == ["c"]


class TestThinData:
    def test_empty_cohort(self):
        sel = selection.select([], rankable=False)
        assert sel.total == 0
        assert "no candidates" in sel.note

    def test_unrankable_cohort_carries_a_warning(self):
        sel = selection.select(_cohort(5), rankable=False)
        assert sel.rankable is False
        assert "below the minimum" in sel.note
        # Still returns posts. Looking at thin data beats looking at nothing,
        # as long as the caller is told.
        assert sel.total > 0

    def test_rankable_cohort_has_no_warning(self):
        sel = selection.select(_cohort(100), rankable=True)
        assert sel.note == ""


class TestLabelling:
    def test_label_for_each_group(self):
        sel = selection.select(_cohort(100), rankable=True)
        assert sel.label_for(sel.winners[0]) == selection.WINNER
        assert sel.label_for(sel.losers[0]) == selection.LOSER

    def test_unselected_post_raises(self):
        sel = selection.select(_cohort(100), rankable=True)
        try:
            sel.label_for("not-in-batch")
        except KeyError:
            pass
        else:
            raise AssertionError("expected KeyError for unselected post")


class TestQuadrants:
    """The two-axis model. A post can succeed on reach OR engagement and still
    be worth analysing; only failing BOTH makes it a control-group member."""

    def _mixed(self):
        # 12 posts spread across the quadrants.
        out = []
        for i in range(4):        # high reach, high engagement
            out.append(Candidate(f"both{i}", 5.0 - i, 0.0, reach=4.0, engagement=4.0))
        for i in range(4):        # high reach, low engagement
            out.append(Candidate(f"reach{i}", 1.0 - i, 0.0, reach=4.0, engagement=0.1))
        for i in range(4):        # low on both
            out.append(Candidate(f"dud{i}", -5.0 - i, 0.0, reach=0.1, engagement=0.1))
        return out

    def test_high_reach_low_engagement_is_not_a_loser(self):
        sel = selection.select(self._mixed(), rankable=True, n_winners=4,
                               n_losers=4, n_anomalies=0, n_reach_only=4)
        for i in range(4):
            assert f"reach{i}" not in sel.losers

    def test_high_reach_low_engagement_is_labelled_reach_only(self):
        sel = selection.select(self._mixed(), rankable=True, n_winners=4,
                               n_losers=4, n_anomalies=0, n_reach_only=4)
        assert set(sel.reach_only) == {f"reach{i}" for i in range(4)}
        assert sel.label_for("reach0") == selection.REACH_ONLY

    def test_losers_failed_on_both_axes(self):
        sel = selection.select(self._mixed(), rankable=True, n_winners=4,
                               n_losers=4, n_anomalies=0, n_reach_only=4)
        assert set(sel.losers) == {f"dud{i}" for i in range(4)}

    def test_control_group_shortfall_is_reported(self):
        # Nothing fails on both axes, so there is no valid control group.
        cands = [Candidate(f"p{i}", float(10 - i), 0.0, reach=3.0, engagement=3.0)
                 for i in range(12)]
        sel = selection.select(cands, rankable=True, n_winners=3, n_losers=4,
                               n_anomalies=0, n_reach_only=0)
        assert len(sel.losers) < 4
        assert "control group is smaller" in sel.note

    def test_quadrants_stay_disjoint(self):
        sel = selection.select(self._mixed(), rankable=True, n_winners=4,
                               n_losers=4, n_anomalies=2, n_reach_only=4)
        assert len(set(sel.all_ids)) == sel.total
