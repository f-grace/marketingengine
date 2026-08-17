"""Pure scoring functions for scraped TikTok posts.

Nothing in this module touches the database, the network, or the clock.
Everything is a pure function of its arguments, which is what makes the whole
analytical half of the pipeline testable.

    raw counts ──> rates ──> baseline-relative ──> z-scores ──> composite
                                     │                 │
                                     │                 └─ guarded: a cohort with
                                     │                    no variance returns
                                     │                    neutral, never NaN
                                     └─ this is the step that stops the pipeline
                                        from just learning "big accounts get
                                        big numbers"

Review finding F4 is implemented here: z-scores over a zero-variance cohort
produce NaN, which then propagates silently into the format library. See
`zscores` and `MIN_COHORT_SIZE`.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Iterable, List, Optional, Sequence

# Below this many posts in a cohort, ranking is not meaningful. The caller is
# expected to surface this to the operator rather than quietly ranking anyway.
MIN_COHORT_SIZE = 12

# Standard deviations below this are treated as "no variance". Chosen well above
# float noise but far below any real spread in engagement rates.
_STDEV_EPSILON = 1e-9

# Rates are per-view, so the denominator is a view count that can legitimately
# be zero on a brand new post.
_MIN_DENOM = 1.0

# Floor for lift ratios. A post with literally zero saves would otherwise take
# log(0) and blow up the cohort.
_LIFT_FLOOR = 1e-6

# Strength of the account-median prior when smoothing per-view rates, expressed
# in imaginary views. 500 leaves a 30k-view post essentially untouched while
# pulling a 350-view post most of the way back to its account's norm.
PRIOR_WEIGHT_VIEWS = 500.0

# Engagement half-life. TikTok format cycles turn over in roughly three weeks,
# so a post older than that carries much less signal about what works *now*.
RECENCY_TAU_DAYS = 21.0

# Composite weights. Saves are weighted hardest because saves are the strongest
# quality signal the carousel algorithm reads.
W_SAVE = 0.45
W_ENGAGE = 0.25
W_REACH = 0.30


def save_rate(collect_count: int, play_count: int) -> float:
    """Fraction of viewers who saved the post.

    The single most predictive metric for carousel distribution.
    """
    return collect_count / max(float(play_count), _MIN_DENOM)


def engage_rate(
    digg_count: int,
    comment_count: int,
    share_count: int,
    collect_count: int,
    play_count: int,
) -> float:
    """Combined interaction rate per view."""
    total = digg_count + comment_count + share_count + collect_count
    return total / max(float(play_count), _MIN_DENOM)


def digg_rate(digg_count: int, play_count: int) -> float:
    """Like rate per view. Used as the denominator of the engagement-shape ratio."""
    return digg_count / max(float(play_count), _MIN_DENOM)


def smoothed_rate(
    numerator: int,
    play_count: int,
    prior_rate: Optional[float],
    prior_weight: float = PRIOR_WEIGHT_VIEWS,
) -> float:
    """A per-view rate pulled toward the account's own median on thin data.

    Raw rates are wildly unreliable at low view counts. A post with 8 saves on
    358 views reads as a 2.2% save rate, several times its account's median, but
    the gap between 8 saves and 2 is noise. Observed live: exactly that post
    bought a winner slot in the analysis batch, which costs money.

    Empirical-Bayes shrinkage fixes it without a hard cutoff. The account's
    median rate acts as a prior worth `prior_weight` imaginary views:

        (saves + weight * prior) / (views + weight)

    A 358-view post is dominated by the prior and barely moves off the account's
    median. A 32,000-view post is barely touched, since 500 imaginary views
    against 32,000 real ones changes almost nothing. High-volume outliers stay
    outliers; low-volume ones stop pretending to be.

    Falls back to the raw rate when no prior exists.
    """
    if prior_rate is None or prior_rate < 0:
        return numerator / max(float(play_count), _MIN_DENOM)
    return (numerator + prior_weight * prior_rate) / (play_count + prior_weight)


def lift(rate: float, account_median_rate: Optional[float]) -> float:
    """A post's rate divided by that account's own median rate.

    The generalisation of `reach_index` to every metric, and the fix for a bug
    the first live run exposed: scoring absolute `save_rate` alongside a
    normalised `reach_index` meant 70% of the composite weight sat on
    account-level traits rather than post-level performance. An account with a
    structurally high save rate swept every winner slot, including posts that
    underperformed its own baseline.

    Comparing a post only against its own account's median makes every term
    answer the same question: did THIS post beat what this creator normally does?

    Returns 1.0 ("exactly normal") when no baseline exists, so a missing
    baseline never manufactures signal in either direction.
    """
    if account_median_rate is None or account_median_rate <= 0:
        return 1.0
    return max(rate, _LIFT_FLOOR) / account_median_rate


def reach_index(play_count: int, account_median_plays: float) -> float:
    """Views relative to what this specific creator normally gets.

    This is the function that prevents the most common analytical error in
    competitor scraping. A post with 40k views on an account that averages 8k is
    a hit (5.0). A post with 200k views on an account averaging 400k is a flop
    (0.5). Absolute view counts get both backwards.

    Returns 1.0 (i.e. "exactly normal") when no baseline is available yet, so a
    missing baseline never manufactures a fake signal in either direction.
    """
    if account_median_plays is None or account_median_plays <= 0:
        return 1.0
    return play_count / float(account_median_plays)


def recency_weight(days_since_post: float, tau_days: float = RECENCY_TAU_DAYS) -> float:
    """Exponential decay so stale formats stop dominating the ranking.

    A post from today weighs 1.0, one from three weeks ago weighs ~0.37, one
    from three months ago weighs ~0.01.
    """
    if days_since_post < 0:
        # Clock skew or a bad timestamp. Treat as brand new rather than
        # amplifying it above 1.0.
        return 1.0
    return math.exp(-days_since_post / tau_days)


def engagement_shape(collect_count: int, digg_count: int, play_count: int) -> float:
    """Saves per like.

    High values mean people wanted to *keep* the post rather than merely enjoy
    it. Tracking the shape separately from the magnitude is what lets
    `anomaly_scores` find quietly high-value posts that raw reach misses.
    """
    sr = save_rate(collect_count, play_count)
    dr = digg_rate(digg_count, play_count)
    if dr <= 0:
        # No likes at all. Undefined shape rather than infinite.
        return 0.0
    return sr / dr


def anomaly_scores(shapes: Sequence[float]) -> List[float]:
    """How far each post's engagement *shape* departs from the cohort norm.

    Uses log-ratio against the median so that "twice the normal save:like ratio"
    and "half the normal ratio" are treated as equally unusual. Feed this the
    shapes for a single account to find that account's oddities; feed it a whole
    cohort to find oddities across the niche.
    """
    usable = [s for s in shapes if s > 0]
    if not usable:
        return [0.0] * len(shapes)
    norm = median(usable)
    if norm <= 0:
        return [0.0] * len(shapes)
    out = []
    for s in shapes:
        if s <= 0:
            out.append(0.0)
        else:
            out.append(abs(math.log(s / norm)))
    return out


def zscores(values: Sequence[float]) -> List[float]:
    """Standardise a cohort, returning neutral scores when there is no variance.

    Review finding F4. The textbook formula divides by the standard deviation,
    which is exactly zero when every post in the cohort performed identically,
    and near zero for tiny cohorts. That produces NaN or absurd magnitudes which
    then flow silently into the analysis batch and the format library.

    Here a degenerate cohort returns all-zeros, meaning "everything is average",
    which is both true and harmless. The separate `cohort_is_rankable` check is
    what tells the operator the data is too thin to trust at all.
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.0]

    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    stdev = math.sqrt(variance)

    if stdev < _STDEV_EPSILON:
        return [0.0] * n

    return [(v - mean) / stdev for v in values]


def cohort_is_rankable(cohort_size: int, minimum: int = MIN_COHORT_SIZE) -> bool:
    """Whether a cohort has enough posts for ranking to mean anything.

    Kept separate from `zscores` on purpose. `zscores` must never blow up;
    this function is what lets the caller refuse to act on thin data instead of
    presenting confident-looking nonsense.
    """
    return cohort_size >= minimum


def log_lifts(lifts: Sequence[float]) -> List[float]:
    """Log-transform lift ratios so they are symmetric around "normal".

    A post at 2x its account's median and one at 0.5x are equally far from
    typical, but on a raw ratio scale they sit 1.0 and 0.5 away from 1.0. Taking
    logs makes them +0.69 and -0.69, so z-scoring treats over- and
    under-performance evenhandedly instead of squashing every underperformer
    into a narrow band near zero.
    """
    return [math.log(max(l, _LIFT_FLOOR)) for l in lifts]


def composite_scores(
    save_lifts: Sequence[float],
    engage_lifts: Sequence[float],
    reach_indices: Sequence[float],
    recency_weights: Sequence[float],
) -> List[float]:
    """Weighted, standardised, recency-decayed score for a cohort.

    Takes LIFTS, not raw rates. Every input must already be normalised against
    the post's own account (see `lift` and `reach_index`), so that the composite
    ranks posts that beat their creator's usual performance rather than posts
    from creators with good usual performance.

    All four sequences must be the same length and in the same post order.
    """
    lengths = {
        len(save_lifts),
        len(engage_lifts),
        len(reach_indices),
        len(recency_weights),
    }
    if len(lengths) != 1:
        raise ValueError(
            "composite_scores requires equal-length sequences, got "
            f"{len(save_lifts)}, {len(engage_lifts)}, {len(reach_indices)}, "
            f"{len(recency_weights)}"
        )

    z_save = zscores(log_lifts(save_lifts))
    z_engage = zscores(log_lifts(engage_lifts))
    z_reach = zscores(log_lifts(reach_indices))

    return [
        (W_SAVE * zs + W_ENGAGE * ze + W_REACH * zr) * rw
        for zs, ze, zr, rw in zip(z_save, z_engage, z_reach, recency_weights)
    ]


def account_median_plays(play_counts: Iterable[int]) -> Optional[float]:
    """Median view count for one creator, or None if there is no history."""
    counts = [float(p) for p in play_counts if p is not None]
    if not counts:
        return None
    return median(counts)
