"""
Ranking engine — selects and sorts the top 10 recommendations.

Priority order:
  1. Consensus label (Strong > Moderate > Weak)
  2. Combined confidence score (higher = better)
  3. Bookmaker count (more bookmakers = more liquid = more reliable signal)
  4. Data completeness (all fields populated)

Also assigns accumulator tiers:
  Banker  — top picks, very high confidence, combined odds ~3
  Extended — next tier, combined odds ~5–8
  Value    — broader selection, combined odds 10–15+
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class RankedPick:
    rank: int
    fixture_id: int
    home_team: str
    away_team: str
    league: str
    kickoff_uk: str             # formatted string
    prediction_type: str
    confidence: float           # 0.0–1.0
    confidence_pct: float       # 0–100
    sources_agreeing: int
    consensus_label: str
    risk_label: str
    reasoning: str
    best_odds: Optional[float]
    accumulator_tier: str       # "Banker" | "Extended" | "Value" | ""


_CONSENSUS_ORDER = {"Strong": 3, "Moderate": 2, "Weak": 1}


def _completeness_score(pick: dict) -> int:
    """Returns 0–4 based on how many key fields are populated."""
    score = 0
    if pick.get("api_prob", 0) > 0:
        score += 1
    if pick.get("bm_implied", 0) > 0:
        score += 1
    if pick.get("bookmaker_count", 0) >= 3:
        score += 1
    if pick.get("form_score", 0) > 0:
        score += 1
    return score


def rank_picks(candidates: list[dict]) -> list[RankedPick]:
    """
    candidates: list of dicts with keys:
      fixture_id, home_team, away_team, league, kickoff_uk,
      prediction_type, confidence, sources_agreeing, consensus_label,
      risk_label, reasoning, best_odds, bookmaker_count,
      api_prob, bm_implied, form_score

    Returns top 10 ranked RankedPick objects with accumulator tiers assigned.
    """
    # Filter: only picks with at least Moderate consensus
    eligible = [c for c in candidates if c.get("sources_agreeing", 0) >= 2]

    # Sort: consensus → confidence → bookmaker count → completeness
    eligible.sort(key=lambda c: (
        _CONSENSUS_ORDER.get(c.get("consensus_label", "Weak"), 0),
        c.get("confidence", 0),
        c.get("bookmaker_count", 0),
        _completeness_score(c),
    ), reverse=True)

    top10 = eligible[:10]

    # Assign accumulator tiers
    result: list[RankedPick] = []
    for i, pick in enumerate(top10):
        if i < 3 and pick.get("confidence", 0) >= 0.65:
            tier = "Banker"
        elif i < 6 and pick.get("confidence", 0) >= 0.58:
            tier = "Extended"
        elif i < 10:
            tier = "Value"
        else:
            tier = ""

        result.append(RankedPick(
            rank=i + 1,
            fixture_id=pick.get("fixture_id", 0),
            home_team=pick.get("home_team", ""),
            away_team=pick.get("away_team", ""),
            league=pick.get("league", ""),
            kickoff_uk=pick.get("kickoff_uk", ""),
            prediction_type=pick.get("prediction_type", ""),
            confidence=pick.get("confidence", 0),
            confidence_pct=round(pick.get("confidence", 0) * 100, 1),
            sources_agreeing=pick.get("sources_agreeing", 0),
            consensus_label=pick.get("consensus_label", ""),
            risk_label=pick.get("risk_label", ""),
            reasoning=pick.get("reasoning", ""),
            best_odds=pick.get("best_odds"),
            accumulator_tier=tier,
        ))

    return result


def build_accumulators(picks: list[RankedPick]) -> dict[str, dict]:
    """
    Build three tiered accumulators from the ranked picks.

    Returns a dict with keys: "banker", "extended", "value"
    Each value: {"legs": [...], "combined_odds": float, "confidence_pct": float}
    """
    banker = [p for p in picks if p.accumulator_tier == "Banker"]
    extended = [p for p in picks if p.accumulator_tier in ("Banker", "Extended")]
    value = [p for p in picks if p.accumulator_tier in ("Banker", "Extended", "Value")]

    def combined(legs: list[RankedPick]) -> float:
        result = 1.0
        for leg in legs:
            if leg.best_odds and leg.best_odds > 1.0:
                result *= leg.best_odds
            else:
                # Estimate odds from confidence if no odds data
                prob = leg.confidence if leg.confidence > 0 else 0.5
                result *= round(1.0 / prob, 2)
        return round(result, 2)

    def avg_conf(legs: list[RankedPick]) -> float:
        if not legs:
            return 0.0
        return round(sum(l.confidence for l in legs) / len(legs) * 100, 1)

    return {
        "banker": {
            "legs": banker,
            "combined_odds": combined(banker),
            "avg_confidence_pct": avg_conf(banker),
            "label": "Banker (~3 odds, ultra-safe)",
        },
        "extended": {
            "legs": extended,
            "combined_odds": combined(extended),
            "avg_confidence_pct": avg_conf(extended),
            "label": "Extended (~5–8 odds)",
        },
        "value": {
            "legs": value,
            "combined_odds": combined(value),
            "avg_confidence_pct": avg_conf(value),
            "label": "Value (~10–15+ odds)",
        },
    }
