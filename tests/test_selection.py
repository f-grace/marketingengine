"""Tests for the analysis-batch selector.

The property that matters most: the three groups never overlap, because a post
analysed twice is money spent twice and a contrast computed against itself.
"""

from __future__ import annotations

from engine import selection
from engine.selection import Candidate


def _cohort(n: int) -> list:
    """n candidates with descending composite and flat anomaly."""
    return [
        Candidate(post_id=f"p{i}", composite=float(n - i), anomaly=0.0)
        for i in range(n)
    ]


class TestBasicSplit:
    def test_takes_requested_counts(self):
        sel = selection.select(_cohort(100), rankable=True)
        assert len(sel.winners) == selection.DEFAULT_N_WINNERS
        assert len(sel.losers) == selection.DEFAULT_N_LOSERS
        assert sel.total == 50

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
        # 20 posts but 30 winners + 10 losers + 10 anomalies requested.
        # Must degrade to fewer selections, never reuse a post.
        sel = selection.select(_cohort(20), rankable=True)
        assert len(set(sel.all_ids)) == sel.total
        assert sel.total <= 20

    def test_high_scoring_anomaly_is_labelled_winner(self):
        cands = [
            Candidate("top", composite=10.0, anomaly=99.0),
            Candidate("mid", composite=5.0, anomaly=0.1),
            Candidate("low", composite=1.0, anomaly=0.1),
        ]
        sel = selection.select(cands, rankable=True, n_winners=1, n_losers=1,
                               n_anomalies=1)
        assert "top" in sel.winners
        assert "top" not in sel.anomalies
        assert sel.label_for("top") == selection.WINNER


class TestAnomalySelector:
    def test_picks_odd_shapes_not_top_performers(self):
        cands = [
            Candidate("a", composite=10.0, anomaly=0.0),
            Candidate("b", composite=9.0, anomaly=0.0),
            Candidate("c", composite=1.0, anomaly=5.0),  # mid-table oddity
            Candidate("d", composite=0.5, anomaly=0.0),
        ]
        sel = selection.select(cands, rankable=True, n_winners=2, n_losers=1,
                               n_anomalies=1)
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
