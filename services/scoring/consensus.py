"""
Cross-source consensus engine.

Checks whether API-Football and The Odds API agree on the same predicted outcome.
Returns a consensus label and sources-agreeing count.
"""
from dataclasses import dataclass


PREDICTION_TYPES = [
    "home_win",
    "away_win",
    "draw",
    "over_2.5",
    "under_2.5",
    "over_1.5",
    "btts_yes",
    "btts_no",
]


@dataclass
class ConsensusResult:
    prediction_type: str
    sources_agreeing: int       # 1, 2, or 3
    consensus_label: str        # "Strong" | "Moderate" | "Weak"
    risk_label: str             # "Low" | "Medium" | "High"


def _bookmaker_direction(home_implied: float, draw_implied: float, away_implied: float) -> str:
    """
    The bookmaker market's favoured outcome is whichever implied probability is highest.
    Implied probability = 1 / decimal_odds, so the highest = shortest odds = market favourite.
    """
    if home_implied <= 0 and away_implied <= 0:
        return "unknown"
    best = max(home_implied, draw_implied, away_implied)
    if best == home_implied:
        return "home_win"
    if best == away_implied:
        return "away_win"
    return "draw"


def _api_football_direction(home_win_prob: float, draw_prob: float, away_win_prob: float) -> str:
    best = max(home_win_prob, draw_prob, away_win_prob)
    if best == home_win_prob:
        return "home_win"
    if best == away_win_prob:
        return "away_win"
    return "draw"


def _label(count: int) -> tuple[str, str]:
    if count >= 3:
        return "Strong", "Low"
    if count == 2:
        return "Moderate", "Medium"
    return "Weak", "High"


def compute_consensus(
    *,
    api_home_prob: float,
    api_draw_prob: float,
    api_away_prob: float,
    bm_home_implied: float,
    bm_draw_implied: float,
    bm_away_implied: float,
    fd_form_home: float = 0.0,
    fd_form_away: float = 0.0,
) -> ConsensusResult:
    """
    Determines the consensus prediction across up to 3 sources:
      1. API-Football win-probability model
      2. Bookmaker market direction (implied probability)
      3. Football-Data.org form signal (home wins if form meaningfully stronger)

    Returns the dominant outcome and how many sources agree.
    """
    api_dir = _api_football_direction(api_home_prob, api_draw_prob, api_away_prob)

    bm_dir = _bookmaker_direction(bm_home_implied, bm_draw_implied, bm_away_implied)

    # Form signal: if one team's form score is at least 0.20 higher, that's a signal
    if fd_form_home > 0 or fd_form_away > 0:
        gap = fd_form_home - fd_form_away
        if gap >= 0.20:
            form_dir = "home_win"
        elif gap <= -0.20:
            form_dir = "away_win"
        else:
            form_dir = "draw"
    else:
        form_dir = None   # no Football-Data.org data available

    votes: dict[str, int] = {}
    for direction in [api_dir, bm_dir]:
        if direction != "unknown":
            votes[direction] = votes.get(direction, 0) + 1

    if form_dir and form_dir != "unknown":
        votes[form_dir] = votes.get(form_dir, 0) + 1

    if not votes:
        return ConsensusResult("home_win", 0, "Weak", "High")

    top_outcome = max(votes, key=lambda k: votes[k])
    top_count = votes[top_outcome]
    label, risk = _label(top_count)

    return ConsensusResult(
        prediction_type=top_outcome,
        sources_agreeing=top_count,
        consensus_label=label,
        risk_label=risk,
    )


def is_strong_enough(result: ConsensusResult) -> bool:
    """Only include fixtures where at least 2/3 sources agree."""
    return result.sources_agreeing >= 2
