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
    fb_home_prob: float = 0.0,
    fb_draw_prob: float = 0.0,
    fb_away_prob: float = 0.0,
) -> ConsensusResult:
    """
    Determines the consensus prediction across up to 4 sources:
      1. API-Football win-probability model
      2. Bookmaker market direction (implied probability)
      3. Football-Data.org form signal (home wins if form meaningfully stronger)
      4. Forebet mathematical prediction

    Returns the dominant outcome and how many sources agree.
    """
    # Only vote if API-Football actually returned probabilities
    has_api_data = api_home_prob > 0 or api_draw_prob > 0 or api_away_prob > 0
    api_dir = _api_football_direction(api_home_prob, api_draw_prob, api_away_prob) if has_api_data else "unknown"

    bm_dir = _bookmaker_direction(bm_home_implied, bm_draw_implied, bm_away_implied)

    # Football-Data.org form: gap of ≥0.20 is a meaningful signal
    if fd_form_home > 0 or fd_form_away > 0:
        gap = fd_form_home - fd_form_away
        if gap >= 0.20:
            form_dir = "home_win"
        elif gap <= -0.20:
            form_dir = "away_win"
        else:
            form_dir = "draw"
    else:
        form_dir = None

    # Forebet mathematical prediction
    has_fb_data = fb_home_prob > 0 or fb_draw_prob > 0 or fb_away_prob > 0
    fb_dir = _api_football_direction(fb_home_prob, fb_draw_prob, fb_away_prob) if has_fb_data else "unknown"

    votes: dict[str, int] = {}
    for direction in [api_dir, bm_dir, fb_dir]:
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


def compute_over_under_consensus(
    *,
    api_signal: str,
    api_home_goals: float | None,
    api_away_goals: float | None,
    bm_over25_implied: float,
    bm_under25_implied: float,
) -> ConsensusResult | None:
    """
    Returns a ConsensusResult for over/under 2.5 goals, or None if insufficient data.
    Three possible votes:
      1. API-Football over_under_signal ("+2.5" → over, "-2.5" → under)
      2. API-Football predicted goals (total > 2.5 → over)
      3. Bookmaker totals market (higher implied → winning direction)
    Requires at least 2 agreeing votes.
    """
    votes: dict[str, int] = {}

    if api_signal:
        try:
            sig_val = float(api_signal)
            # +2.5 or higher → over 2.5; only -2.5 exactly → under 2.5
            # (-3.5 is ambiguous: under 3.5 could still be 3 goals = over 2.5)
            if sig_val >= 2.5:
                votes["over_2.5"] = votes.get("over_2.5", 0) + 1
            elif sig_val == -2.5:
                votes["under_2.5"] = votes.get("under_2.5", 0) + 1
        except (ValueError, TypeError):
            pass

    if api_home_goals is not None and api_away_goals is not None:
        total = float(api_home_goals) + float(api_away_goals)
        direction = "over_2.5" if total > 2.5 else "under_2.5"
        votes[direction] = votes.get(direction, 0) + 1

    if bm_over25_implied > 0 or bm_under25_implied > 0:
        direction = "over_2.5" if bm_over25_implied >= bm_under25_implied else "under_2.5"
        votes[direction] = votes.get(direction, 0) + 1

    if not votes:
        return None

    top_outcome = max(votes, key=lambda k: votes[k])
    top_count = votes[top_outcome]

    if top_count < 2:
        # Allow single-source when the prediction is unambiguous:
        #  - signal is directly on the 2.5 line (±2.5), OR
        #  - predicted total goals is far from the 2.5 line (< 1.5 or > 4.0)
        allow_single = False
        if api_signal:
            try:
                if abs(float(api_signal)) == 2.5:
                    allow_single = True
            except (ValueError, TypeError):
                pass
        if not allow_single and api_home_goals is not None and api_away_goals is not None:
            total = float(api_home_goals) + float(api_away_goals)
            # Decoded totals: ≥3.0 is clearly over 2.5; ≤1.5 is clearly under 2.5
            if total >= 3.0 or total <= 1.5:
                allow_single = True
        if not allow_single:
            return None

    label, risk = _label(top_count)
    return ConsensusResult(prediction_type=top_outcome, sources_agreeing=top_count,
                           consensus_label=label, risk_label=risk)


def compute_btts_consensus(
    *,
    api_home_goals: float | None,
    api_away_goals: float | None,
    home_form_score: float = 0.5,
    away_form_score: float = 0.5,
) -> ConsensusResult | None:
    """
    Returns a ConsensusResult for BTTS (Both Teams To Score), or None if insufficient data.
    Two votes:
      1. API-Football predicted goals (both > 0 → btts_yes)
      2. Form-based attack signal (both teams with form > 0.55 → btts_yes)
    Requires both votes to agree.
    """
    if api_home_goals is None or api_away_goals is None:
        return None

    votes: dict[str, int] = {}

    home_g = float(api_home_goals)
    away_g = float(api_away_goals)
    # > 0.5 threshold: abs(-0.5)=0.5 means "predicted under 0.5 goals" = doesn't score
    votes["btts_yes" if (home_g > 0.5 and away_g > 0.5) else "btts_no"] = 1

    if home_form_score > 0.55 and away_form_score > 0.55:
        votes["btts_yes"] = votes.get("btts_yes", 0) + 1
    elif home_form_score < 0.45 or away_form_score < 0.45:
        votes["btts_no"] = votes.get("btts_no", 0) + 1

    if not votes:
        return None

    top_outcome = max(votes, key=lambda k: votes[k])
    top_count = votes[top_outcome]

    if top_count < 2:
        # Allow single-source btts_yes when both teams have clear scoring expectation (> 1.5 goals each)
        # or btts_no when at least one team has near-zero expectation (≤ 0.5 goals)
        if api_home_goals is not None and api_away_goals is not None:
            h, a = float(api_home_goals), float(api_away_goals)
            if top_outcome == "btts_yes" and h > 1.0 and a > 1.0:
                pass  # allow (both teams decoded > 1.0 = original threshold ≥ -2.5)
            elif top_outcome == "btts_no" and (h <= 0.5 or a <= 0.5):
                pass  # allow
            else:
                return None
        else:
            return None

    label, risk = _label(top_count)
    return ConsensusResult(prediction_type=top_outcome, sources_agreeing=top_count,
                           consensus_label=label, risk_label=risk)


def is_strong_enough(result: ConsensusResult, bm_implied_max: float = 0.0) -> bool:
    """
    Pass if at least 2 sources agree, OR if a single very-strong bookmaker
    signal exists (implied prob ≥ 0.62 = clear market favourite).
    """
    if result.sources_agreeing >= 2:
        return True
    return result.sources_agreeing == 1 and bm_implied_max >= 0.62
