"""
Confidence scoring engine.

Implements the formula from CLAUDE.md:

    confidence = (
        api_football_probability * 0.35
        + bookmaker_implied_probability * 0.40
        + form_score * 0.15
        + context_bonus * 0.10
    )

Also encodes the motivation analysis I apply manually:
  - Is the predicted winner fighting for something critical?
  - Does the opponent have nothing to play for?
"""
from dataclasses import dataclass


@dataclass
class ConfidenceInput:
    # API-Football
    home_win_prob: float = 0.0          # 0.0–1.0
    draw_prob: float = 0.0
    away_win_prob: float = 0.0
    home_form_score: float = 0.5        # 0.0–1.0 from last 5
    away_form_score: float = 0.5
    over_under_signal: str = ""         # "+2.5" or "-2.5"
    predicted_home_goals: float | None = None
    predicted_away_goals: float | None = None
    advice: str = ""

    # The Odds API — 1X2
    home_implied: float = 0.0
    draw_implied: float = 0.0
    away_implied: float = 0.0
    bookmaker_count: int = 0

    # The Odds API — totals (over/under 2.5)
    over25_implied: float = 0.0
    under25_implied: float = 0.0

    # League position context (for motivation scoring)
    home_position: int | None = None
    away_position: int | None = None
    total_teams: int = 20
    is_final_matchday: bool = False
    is_cup_final: bool = False


def motivation_bonus(position: int | None, total_teams: int, is_final_matchday: bool, is_cup_final: bool) -> float:
    """
    Returns a 0.0–1.0 motivation score for a team given its league position.

    Logic:
      - Cup final → maximum motivation (1.0)
      - Title race (top 2) → very high (0.90)
      - Champions League race (3rd–4th) → high (0.80)
      - Europa / Conference race (5th–6th) → moderate-high (0.70)
      - Safe mid-table (nothing at stake) → neutral (0.50)
      - Near relegation (bottom 4, late season) → very high (0.85)
      - Already relegated → low (0.30) — nothing matters
    """
    if is_cup_final:
        return 1.0
    if position is None:
        return 0.5

    bottom_zone = total_teams - 2       # bottom 3 = relegation
    near_bottom = total_teams - 4       # danger zone on final days

    if position <= 2:
        return 0.90
    if position <= 4:
        return 0.80
    if position <= 6:
        return 0.70
    if position >= bottom_zone:
        return 0.90                     # fighting for survival = max urgency
    if position >= near_bottom and is_final_matchday:
        return 0.80
    return 0.50                         # safe, nothing at stake


def opponent_dead_rubber_discount(opp_position: int | None, total_teams: int, is_final_matchday: bool) -> float:
    """
    Returns a small bonus (0.0–0.10) to confidence when the opponent
    has nothing to play for — the 'dead rubber' effect I use in my predictions.
    """
    if opp_position is None:
        return 0.0
    safe_floor = total_teams - 4
    safe_ceiling = 7
    if safe_ceiling < opp_position < safe_floor:
        if is_final_matchday:
            return 0.08   # clearly safe, end of season
        return 0.03
    return 0.0


def _goals_to_over_prob(home_goals: float | None, away_goals: float | None) -> float:
    """Derives an over-2.5 probability from API-Football predicted goals."""
    if home_goals is None or away_goals is None:
        return 0.0
    total = float(home_goals) + float(away_goals)
    if total >= 3.5:
        return 0.75
    if total >= 2.5:
        return 0.60
    if total >= 1.5:
        return 0.35
    return 0.20


def compute_confidence(inp: ConfidenceInput, predicted_outcome: str) -> tuple[float, str]:
    """
    Compute the combined confidence score for a specific predicted outcome.

    predicted_outcome: "home_win" | "away_win" | "draw"
                     | "over_2.5" | "under_2.5"
                     | "btts_yes" | "btts_no"

    Returns (confidence: float 0.0–1.0, reasoning: str)
    """
    reasons: list[str] = []

    # ------------------------------------------------------------------ #
    # Over / Under 2.5
    # ------------------------------------------------------------------ #
    if predicted_outcome in ("over_2.5", "under_2.5"):
        is_over = predicted_outcome == "over_2.5"

        # Signal 1 �� bookmaker totals (primary, 50%)
        bm_prob = inp.over25_implied if is_over else inp.under25_implied
        if bm_prob > 0:
            reasons.append(f"Bookmakers ({inp.bookmaker_count}): {bm_prob * 100:.0f}% implied ({'over' if is_over else 'under'} 2.5)")
        else:
            bm_prob = 0.5   # neutral fallback

        # Signal 2 �� API-Football predicted goals (35%)
        goals_prob = _goals_to_over_prob(inp.predicted_home_goals, inp.predicted_away_goals)
        if not is_over:
            goals_prob = 1.0 - goals_prob
        if inp.predicted_home_goals is not None:
            total = float(inp.predicted_home_goals) + float(inp.predicted_away_goals or 0)
            reasons.append(f"Predicted goals: {total:.1f} total")

        # Signal 3 — over_under_signal from API-Football (15%)
        signal_bonus = 0.0
        if inp.over_under_signal:
            signal_is_over = "+" in inp.over_under_signal
            if signal_is_over == is_over:
                signal_bonus = 0.07
                reasons.append(f'API tip: "{inp.over_under_signal}"')
            else:
                signal_bonus = -0.05
                reasons.append(f'API tip contradicts: "{inp.over_under_signal}"')

        raw = bm_prob * 0.50 + goals_prob * 0.35 + 0.50 * 0.15 + signal_bonus
        confidence = round(min(max(raw, 0.0), 1.0), 4)
        return confidence, " | ".join(reasons)

    # ------------------------------------------------------------------ #
    # BTTS Yes / No
    # ------------------------------------------------------------------ #
    if predicted_outcome in ("btts_yes", "btts_no"):
        is_yes = predicted_outcome == "btts_yes"

        # Signal 1 — API-Football predicted goals (50%)
        if inp.predicted_home_goals is not None and inp.predicted_away_goals is not None:
            home_scores = float(inp.predicted_home_goals) > 0.5
            away_scores = float(inp.predicted_away_goals) > 0.5
            btts_likely = home_scores and away_scores
            goals_prob = 0.72 if btts_likely else 0.28
            if not is_yes:
                goals_prob = 1.0 - goals_prob
            pred_h = float(inp.predicted_home_goals)
            pred_a = float(inp.predicted_away_goals)
            reasons.append(f"Predicted goals: {pred_h:.1f}–{pred_a:.1f}")
        else:
            goals_prob = 0.5

        # Signal 2 — form-based attack score (50%)
        # Both teams with high form → both likely to score
        combined_attack = (inp.home_form_score + inp.away_form_score) / 2
        if is_yes:
            form_prob = min(combined_attack + 0.1, 0.9)
        else:
            form_prob = 1.0 - min(combined_attack + 0.1, 0.9)
        reasons.append(f"Combined attack form: {combined_attack * 100:.0f}%")

        raw = goals_prob * 0.50 + form_prob * 0.50
        confidence = round(min(max(raw, 0.0), 1.0), 4)
        return confidence, " | ".join(reasons)

    # ------------------------------------------------------------------ #
    # 1X2 (home_win / away_win / draw)  — original logic
    # ------------------------------------------------------------------ #

    # --- 1. API-Football probability (35%) ---
    if predicted_outcome == "home_win":
        api_prob = inp.home_win_prob
        form = inp.home_form_score
        opp_form = inp.away_form_score
        predicted_pos = inp.home_position
        opp_pos = inp.away_position
    elif predicted_outcome == "away_win":
        api_prob = inp.away_win_prob
        form = inp.away_form_score
        opp_form = inp.home_form_score
        predicted_pos = inp.away_position
        opp_pos = inp.home_position
    else:  # draw
        api_prob = inp.draw_prob
        form = (inp.home_form_score + inp.away_form_score) / 2
        opp_form = form
        predicted_pos = None
        opp_pos = None

    if api_prob > 0:
        reasons.append(f"API-Football: {api_prob * 100:.0f}%")

    # --- 2. Bookmaker implied probability (40%) ---
    if predicted_outcome == "home_win":
        bm_prob = inp.home_implied
    elif predicted_outcome == "away_win":
        bm_prob = inp.away_implied
    else:
        bm_prob = inp.draw_implied

    if inp.bookmaker_count > 0:
        reasons.append(f"Bookmakers ({inp.bookmaker_count}): {bm_prob * 100:.0f}% implied")
    else:
        bm_prob = api_prob

    # --- 3. Form score (15%) ---
    reasons.append(f"Form: {form * 100:.0f}%")

    # --- 4. Context bonus — motivation + dead rubber (10%) ---
    mot = motivation_bonus(predicted_pos, inp.total_teams, inp.is_final_matchday, inp.is_cup_final)
    dead_rubber_bonus = opponent_dead_rubber_discount(opp_pos, inp.total_teams, inp.is_final_matchday)
    context = min(mot + dead_rubber_bonus, 1.0)

    if inp.is_cup_final:
        reasons.append("Cup final — maximum stakes")
    elif mot >= 0.85:
        reasons.append("Must-win situation — survival/title urgency")
    elif mot >= 0.75:
        reasons.append("CL race — high motivation")
    elif mot <= 0.35:
        reasons.append("Already relegated — low motivation")
    if dead_rubber_bonus >= 0.06:
        reasons.append("Opponent in dead-rubber mode")

    if inp.advice:
        reasons.append(f'Tip: "{inp.advice}"')

    # --- Final weighted formula ---
    raw = (
        api_prob * 0.35
        + bm_prob * 0.40
        + form * 0.15
        + context * 0.10
    )

    confidence = round(min(max(raw, 0.0), 1.0), 4)
    return confidence, " | ".join(reasons)
