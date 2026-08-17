"""Pick which posts are worth paying a multimodal model to look at.

This is the cost-control step. The scrape returns ~1,000 posts; the AI analysis
batch is ~50. Running vision analysis over everything instead costs roughly 20x
more for signal you would have to filter afterwards anyway.

Selection works on two axes that are deliberately NOT collapsed into one number,
because a post can succeed on either and be worth learning from:

                  high engagement        low engagement
                ┌──────────────────────┬──────────────────────┐
      high      │  WINNER              │  REACH-ONLY          │
      reach     │  worked on both      │  travels but does    │
                │                      │  not stick           │
                ├──────────────────────┼──────────────────────┤
      low       │  ANOMALY             │  LOSER               │
      reach     │  quietly good,       │  failed on both      │
                │  poor distribution   │  -> control group    │
                └──────────────────────┴──────────────────────┘

REACH-ONLY exists because a post that gets 78x an account's normal views with a
0.09% save rate is not a failure. It is a format that travels. Filing it as a
loser would teach the winner/loser contrast that a highly effective reach
mechanism is a failure pattern.

LOSER requires failing on BOTH axes. A control group made of posts that
succeeded on one axis is not a control group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

WINNER = "winner"
REACH_ONLY = "reach_only"
ANOMALY = "anomaly"
LOSER = "loser"

DEFAULT_N_WINNERS = 30
DEFAULT_N_REACH_ONLY = 8
DEFAULT_N_LOSERS = 10
DEFAULT_N_ANOMALIES = 10

# Fraction of the cohort treated as "high" or "low" on an axis when assigning
# quadrants. A third each way leaves a neutral middle that belongs to neither.
_TERCILE = 1.0 / 3.0


@dataclass(frozen=True)
class Candidate:
    """A scored post awaiting selection.

    `reach` and `engagement` are lifts over the post's own account (1.0 means
    exactly typical for that creator), kept separate so the quadrant logic can
    ask about each axis independently.
    """

    post_id: str
    composite: float
    anomaly: float
    reach: float = 1.0
    engagement: float = 1.0


@dataclass(frozen=True)
class Selection:
    """The analysis batch, plus why it is the size it is."""

    winners: List[str]
    losers: List[str]
    anomalies: List[str]
    reach_only: List[str]
    rankable: bool
    note: str = ""

    @property
    def all_ids(self) -> List[str]:
        return (list(self.winners) + list(self.reach_only)
                + list(self.anomalies) + list(self.losers))

    @property
    def total(self) -> int:
        return len(self.all_ids)

    def label_for(self, post_id: str) -> str:
        if post_id in self.winners:
            return WINNER
        if post_id in self.reach_only:
            return REACH_ONLY
        if post_id in self.anomalies:
            return ANOMALY
        if post_id in self.losers:
            return LOSER
        raise KeyError(f"{post_id} is not in this selection")


_NO_SPREAD = 1e-12


def _threshold(values: Sequence[float], fraction: float, *, high: bool) -> float:
    """Value at the given fraction through a sorted sequence.

    When every value is identical there is no meaningful "high" or "low", so the
    threshold degenerates to one nothing can satisfy: -inf for a low cut, +inf
    for a high cut. Without this, a flat cohort makes every post simultaneously
    low-reach and low-engagement, and the entire batch lands in the control
    group.
    """
    if not values:
        return float("-inf") if high else float("inf")
    if max(values) - min(values) < _NO_SPREAD:
        return float("inf") if high else float("-inf")
    ordered = sorted(values)
    idx = min(int(len(ordered) * fraction), len(ordered) - 1)
    return ordered[idx]


def select(
    candidates: Sequence[Candidate],
    rankable: bool,
    n_winners: int = DEFAULT_N_WINNERS,
    n_losers: int = DEFAULT_N_LOSERS,
    n_anomalies: int = DEFAULT_N_ANOMALIES,
    n_reach_only: int = DEFAULT_N_REACH_ONLY,
) -> Selection:
    """Split a scored cohort into the four quadrants.

    Groups never overlap. Order of claim is winners, then reach-only, then
    anomalies, then losers, so a post that qualifies for more than one gets the
    more actionable label.

    `rankable` comes from `scoring.cohort_is_rankable`. When False the selection
    still returns posts, because looking at them beats looking at nothing, but
    `note` carries the warning so the caller can surface it rather than
    presenting thin data as authoritative.
    """
    if not candidates:
        return Selection([], [], [], [], rankable=False,
                         note="no candidates in cohort")

    reaches = [c.reach for c in candidates]
    engagements = [c.engagement for c in candidates]
    high_reach = _threshold(reaches, 1 - _TERCILE, high=True)
    low_reach = _threshold(reaches, _TERCILE, high=False)
    low_engagement = _threshold(engagements, _TERCILE, high=False)

    by_score = sorted(candidates, key=lambda c: c.composite, reverse=True)

    winners = [c.post_id for c in by_score[:n_winners]]
    taken = set(winners)

    # REACH-ONLY: travelled far, did not stick. Ranked by reach because that is
    # the axis they succeeded on.
    reach_only: List[str] = []
    for c in sorted(candidates, key=lambda c: c.reach, reverse=True):
        if len(reach_only) >= n_reach_only:
            break
        if (c.post_id not in taken
                and c.reach >= high_reach
                and c.engagement <= low_engagement):
            reach_only.append(c.post_id)
            taken.add(c.post_id)

    # ANOMALY: unusual engagement shape, i.e. saves out of proportion to likes.
    anomalies: List[str] = []
    for c in sorted(candidates, key=lambda c: c.anomaly, reverse=True):
        if len(anomalies) >= n_anomalies:
            break
        if c.post_id not in taken:
            anomalies.append(c.post_id)
            taken.add(c.post_id)

    # LOSER: must fail on BOTH axes. A post that succeeded on either is not a
    # valid member of the control group.
    losers: List[str] = []
    for c in reversed(by_score):
        if len(losers) >= n_losers:
            break
        if (c.post_id not in taken
                and c.reach <= low_reach
                and c.engagement <= low_engagement):
            losers.append(c.post_id)
            taken.add(c.post_id)

    notes = []
    if not rankable:
        notes.append(
            f"cohort of {len(candidates)} is below the minimum for reliable "
            "ranking; widen the account list or lengthen the scrape window "
            "before trusting these results"
        )
    if len(losers) < n_losers:
        notes.append(
            f"only {len(losers)} posts failed on both axes, so the control "
            f"group is smaller than the requested {n_losers}; winner/loser "
            "contrast will be correspondingly weaker"
        )

    return Selection(
        winners=winners,
        losers=losers,
        anomalies=anomalies,
        reach_only=reach_only,
        rankable=rankable,
        note="; ".join(notes),
    )
