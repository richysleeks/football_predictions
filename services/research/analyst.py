"""
Rule-based match analyst — no API key required.

Reads GNews articles for both teams and scores them against a curated
keyword dictionary to produce a structured confidence adjustment.

Signal categories and their weights:
  - Key player injury / suspension  → -0.08 to -0.12
  - Minor injury mention             → -0.03 to -0.05
  - Opponent key player out          → +0.04 to +0.06
  - Positive form signals            → +0.03 to +0.05
  - Negative form signals            → -0.04 to -0.07
  - Motivation (title / relegation)  → ±0.03
"""
import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword tables
# ---------------------------------------------------------------------------

# Signals that HURT the predicted winner
_INJURY_SEVERE = re.compile(
    r"\b(ruled out|season[- ]ending|long[- ]term injury|surgery|will miss|out for weeks|suspended for)\b",
    re.IGNORECASE,
)
_INJURY_MODERATE = re.compile(
    r"\b(injured|injury concern|doubt(ful)?|missing|unavailable|not training|on the sideline|fitness doubt)\b",
    re.IGNORECASE,
)
_FORM_BAD = re.compile(
    r"\b(winless|without a win|losing streak|poor form|struggling|no win in|hasn't won|failed to score"
    r"|kept off the score|conceded \d+ goals|hammered|thrashed)\b",
    re.IGNORECASE,
)
_MOTIVATION_LOW = re.compile(
    r"\b(nothing to play for|mid[- ]table|already safe|already relegated|secured safety|no pressure)\b",
    re.IGNORECASE,
)

# Signals that HELP the predicted winner
_FORM_GOOD = re.compile(
    r"\b(unbeaten|winning streak|in form|back to form|clean sheet|scored \d+|impressed|dominant"
    r"|top of|leading|excellent run|consecutive win)\b",
    re.IGNORECASE,
)
_MOTIVATION_HIGH = re.compile(
    r"\b(must win|title race|champions league|relegation battle|survival|cup final|need a win"
    r"|desperate for|promotion push|fighting for)\b",
    re.IGNORECASE,
)
# Key-player qualifiers amplify the injury signal
_KEY_PLAYER = re.compile(
    r"\b(captain|star|key player|top scorer|leading scorer|talisman|playmaker|striker|goalkeeper)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Core scoring function
# ---------------------------------------------------------------------------

def _score_articles(articles: list[dict], is_predicted_winner: bool) -> tuple[float, list[str]]:
    """
    Score a list of articles for one team.
    Returns (raw_adjustment, signals_found).
    is_predicted_winner=True  → negative signals hurt, positive signals help.
    is_predicted_winner=False → negative signals help, positive signals hurt.
    """
    adj = 0.0
    signals: list[str] = []

    for article in articles:
        text = f"{article.get('title', '')} {article.get('description', '')}".strip()
        if not text:
            continue

        has_key_player = bool(_KEY_PLAYER.search(text))

        if _INJURY_SEVERE.search(text):
            delta = -0.10 if has_key_player else -0.06
            label = "Key player ruled out" if has_key_player else "Player ruled out"
            adj += delta if is_predicted_winner else -delta
            signals.append(label)

        elif _INJURY_MODERATE.search(text):
            delta = -0.05 if has_key_player else -0.03
            label = "Key player injury doubt" if has_key_player else "Player injury doubt"
            adj += delta if is_predicted_winner else -delta
            signals.append(label)

        if _FORM_BAD.search(text):
            delta = -0.05
            adj += delta if is_predicted_winner else -delta
            signals.append("Poor recent form")

        if _FORM_GOOD.search(text):
            delta = +0.04
            adj += delta if is_predicted_winner else -delta
            signals.append("Strong recent form")

        if _MOTIVATION_HIGH.search(text):
            delta = +0.03
            adj += delta if is_predicted_winner else -delta
            signals.append("High motivation")

        if _MOTIVATION_LOW.search(text):
            delta = -0.03
            adj += delta if is_predicted_winner else -delta
            signals.append("Low motivation")

    return adj, signals


def analyse(
    *,
    home_team: str,
    away_team: str,
    league: str,
    kickoff_str: str,
    prediction_type: str,
    confidence: float,
    bm_implied: float,
    sources_agreeing: int,
    existing_reasoning: str,
    news_text: str,          # kept for interface compatibility, not used here
    home_articles: list[dict] = None,
    away_articles: list[dict] = None,
) -> dict:
    """
    Analyse team news articles and return a structured confidence assessment.
    """
    home_articles = home_articles or []
    away_articles = away_articles or []

    if not home_articles and not away_articles:
        return _neutral("No news articles available")

    # Who is the predicted winner?
    if prediction_type == "home_win":
        winner_articles, loser_articles = home_articles, away_articles
        winner_name, loser_name = home_team, away_team
    elif prediction_type == "away_win":
        winner_articles, loser_articles = away_articles, home_articles
        winner_name, loser_name = away_team, home_team
    else:
        # For draw predictions, treat both teams symmetrically
        winner_articles = home_articles + away_articles
        loser_articles = []
        winner_name, loser_name = home_team, away_team

    winner_adj, winner_signals = _score_articles(winner_articles, is_predicted_winner=True)
    loser_adj, loser_signals = _score_articles(loser_articles, is_predicted_winner=False)

    raw_adj = winner_adj + loser_adj
    # Clamp to safe range
    adj = round(max(-0.15, min(0.10, raw_adj)), 4)

    # Classify verdict
    if adj >= 0.06:
        verdict = "Backs"
    elif adj >= 0.02:
        verdict = "Mildly Backs"
    elif adj <= -0.08:
        verdict = "Contradicts"
    elif adj <= -0.03:
        verdict = "Weakens"
    else:
        verdict = "Neutral"

    # Separate into supporting / risk
    supporting = [s for s in winner_signals if "form" in s.lower() or "motivation" in s.lower()]
    supporting += [f"Opponent: {s}" for s in loser_signals if "injury" in s.lower() or "poor" in s.lower()]
    risk_flags = [s for s in winner_signals if "injury" in s.lower() or "poor" in s.lower() or "low motiv" in s.lower()]
    risk_flags += [f"Opponent: {s}" for s in loser_signals if "strong" in s.lower() or "high motiv" in s.lower()]

    # Build summary
    all_signals = winner_signals + [f"Opponent {s.lower()}" for s in loser_signals]
    unique_signals = list(dict.fromkeys(all_signals))  # deduplicate preserving order
    if unique_signals:
        summary = f"News analysis for {winner_name}: {', '.join(unique_signals[:4])}. Confidence adjustment: {adj:+.1%}."
    else:
        summary = f"No significant injury or form signals found in recent news for this match."

    return {
        "confidence_adjustment": adj,
        "verdict": verdict,
        "supporting_factors": list(dict.fromkeys(supporting))[:4],
        "risk_flags": list(dict.fromkeys(risk_flags))[:4],
        "research_summary": summary,
    }


def _neutral(reason: str = "") -> dict:
    return {
        "confidence_adjustment": 0.0,
        "verdict": "Neutral",
        "supporting_factors": [],
        "risk_flags": [],
        "research_summary": reason or "No significant signals found.",
    }
