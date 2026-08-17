"""Pick which posts are worth paying a multimodal model to look at.

This is the cost-control step. The scrape returns ~1,000 posts; the AI analysis
batch is ~50. Running vision analysis over everything instead costs roughly 20x
more for signal you would have to filter afterwards anyway.

    1000 scraped
        │
        ├── winners   (top by composite)      ── what is working now
        ├── losers    (bottom by composite)   ── the CONTROL GROUP
        └── anomalies (odd engagement shape)  ── what is *newly* working
        │
        ▼
    ~50 analysed

The losers are not optional. Analysing only winners means every attribute they
share looks predictive, including the ones every loser shares too. Without a
control group "has a hook" reads as a success factor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

WINNER = "winner"
LOSER = "loser"
ANOMALY = "anomaly"

DEFAULT_N_WINNERS = 30
DEFAULT_N_LOSERS = 10
DEFAULT_N_ANOMALIES = 10


@dataclass(frozen=True)
class Candidate:
    """A scored post awaiting selection."""

    post_id: str
    composite: float
    anomaly: float


@dataclass(frozen=True)
class Selection:
    """The analysis batch, plus why it is the size it is."""

    winners: List[str]
    losers: List[str]
    anomalies: List[str]
    rankable: bool
    note: str = ""

    @property
    def all_ids(self) -> List[str]:
        return list(self.winners) + list(self.losers) + list(self.anomalies)

    @property
    def total(self) -> int:
        return len(self.all_ids)

    def label_for(self, post_id: str) -> str:
        if post_id in self.winners:
            return WINNER
        if post_id in self.losers:
            return LOSER
        if post_id in self.anomalies:
            return ANOMALY
        raise KeyError(f"{post_id} is not in this selection")


def select(
    candidates: Sequence[Candidate],
    rankable: bool,
    n_winners: int = DEFAULT_N_WINNERS,
    n_losers: int = DEFAULT_N_LOSERS,
    n_anomalies: int = DEFAULT_N_ANOMALIES,
) -> Selection:
    """Split a scored cohort into winners, losers, and anomalies.

    The three groups never overlap. Winners are taken first, then losers from
    the opposite end, then anomalies from whatever is left. A post that is both
    a top performer and structurally odd is analysed as a winner, since that is
    the more actionable label.

    `rankable` comes from `scoring.cohort_is_rankable`. When it is False the
    selection still returns posts, because looking at them is better than
    looking at nothing, but `note` carries the warning so the caller can surface
    it rather than presenting thin data as authoritative.
    """
    if not candidates:
        return Selection([], [], [], rankable=False, note="no candidates in cohort")

    by_score = sorted(candidates, key=lambda c: c.composite, reverse=True)

    winners = [c.post_id for c in by_score[:n_winners]]
    taken = set(winners)

    # Walk from the bottom up, skipping anything already claimed. In a cohort
    # smaller than n_winners + n_losers this correctly yields fewer losers
    # rather than double-counting posts into both groups.
    losers: List[str] = []
    for c in reversed(by_score):
        if len(losers) >= n_losers:
            break
        if c.post_id not in taken:
            losers.append(c.post_id)
            taken.add(c.post_id)

    by_anomaly = sorted(candidates, key=lambda c: c.anomaly, reverse=True)
    anomalies: List[str] = []
    for c in by_anomaly:
        if len(anomalies) >= n_anomalies:
            break
        if c.post_id not in taken:
            anomalies.append(c.post_id)
            taken.add(c.post_id)

    note = ""
    if not rankable:
        note = (
            f"cohort of {len(candidates)} is below the minimum for reliable "
            "ranking; widen the account list or lengthen the scrape window "
            "before trusting these results"
        )

    return Selection(
        winners=winners,
        losers=losers,
        anomalies=anomalies,
        rankable=rankable,
        note=note,
    )
