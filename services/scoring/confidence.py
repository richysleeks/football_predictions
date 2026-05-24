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
    advice: str = ""

    # The Odds API
    home_implied: float = 0.0           # average bookmaker implied prob
    draw_implied: float = 0.0
    away_implied: float = 0.0
    bookmaker_count: int = 0

    # League position context (for motivation scoring)
    home_position: int | None = None
    away_position: int | None = None
    total_teams: int = 20               # default to 20-team league
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


def compute_confidence(inp: ConfidenceInput, predicted_outcome: str) -> tuple[float, str]:
    """
    Compute the combined confidence score for a specific predicted outcome.

    predicted_outcome: "home_win" | "away_win" | "draw" | "over_2.5" | "under_2.5" | "btts_yes" | "btts_no"

    Returns (confidence: float 0.0–1.0, reasoning: str)
    """
    reasons: list[str] = []

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
    elif predicted_outcome == "draw":
        api_prob = inp.draw_prob
        form = (inp.home_form_score + inp.away_form_score) / 2
        opp_form = form
        predicted_pos = None
        opp_pos = None
    elif predicted_outcome in ("over_2.5", "over_1.5"):
        api_prob = min(inp.home_win_prob + inp.away_win_prob, 0.95)
        form = (inp.home_form_score + inp.away_form_score) / 2
        opp_form = form
        predicted_pos = None
        opp_pos = None
    else:
        api_prob = inp.draw_prob
        form = 0.5
        opp_form = 0.5
        predicted_pos = None
        opp_pos = None

    if api_prob > 0:
        reasons.append(f"API-Football: {api_prob * 100:.0f}%")

    # --- 2. Bookmaker implied probability (40%) ---
    if predicted_outcome == "home_win":
        bm_prob = inp.home_implied
    elif predicted_outcome == "away_win":
        bm_prob = inp.away_implied
    elif predicted_outcome == "draw":
        bm_prob = inp.draw_implied
    else:
        bm_prob = (inp.home_implied + inp.away_implied) / 2

    if inp.bookmaker_count > 0:
        reasons.append(f"Bookmakers ({inp.bookmaker_count}): {bm_prob * 100:.0f}% implied")
    else:
        bm_prob = api_prob   # fall back to API prob if no odds data

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
