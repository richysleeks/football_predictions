"""ClubElo fetcher — free ELO ratings for football clubs.

URL: http://clubelo.com/API
No API key needed. Covers ~1,000 European clubs.
Returns CSV rows with Rank, Club, Country, Level, Elo, From, To.
"""
from __future__ import annotations

import csv
import io
import logging
from functools import lru_cache

import requests

logger = logging.getLogger(__name__)

BASE_URL = "http://api.clubelo.com"


def _format_name(team: str) -> str:
    return (
        team.strip()
        .replace(" FC", "").replace("FC ", "")
        .replace("AFC ", "").replace(" AFC", "")
        .replace(" ", "-")
        .replace("&", "and")
        .replace(".", "")
    )


@lru_cache(maxsize=128)
def fetch_elo(team_name: str) -> float | None:
    """Return the current ELO rating for a club, or None if not found."""
    formatted = _format_name(team_name)
    try:
        resp = requests.get(f"{BASE_URL}/{formatted}", timeout=8)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
        if rows:
            latest = rows[-1]
            val = latest.get("Elo", "").strip()
            return float(val) if val else None
    except Exception as exc:
        logger.debug("ClubElo fetch failed for '%s': %s", team_name, exc)
    return None


def elo_win_probabilities(home_elo: float, away_elo: float) -> dict:
    """Compute expected home/draw/away probabilities from ELO ratings.

    Uses 100-point home advantage and a simplified draw estimation.
    """
    diff = (home_elo + 100) - away_elo
    expected_home = 1 / (1 + 10 ** (-diff / 400))

    draw_prob = max(0.18, 0.28 - abs(diff) * 0.0004)
    draw_prob = min(draw_prob, 0.32)

    home_win = expected_home * (1 - draw_prob)
    away_win = (1 - expected_home) * (1 - draw_prob)

    return {
        "home_win_prob": round(home_win, 4),
        "draw_prob": round(draw_prob, 4),
        "away_win_prob": round(away_win, 4),
        "elo_home": round(home_elo, 1),
        "elo_away": round(away_elo, 1),
        "elo_diff": round(home_elo - away_elo, 1),
        "elo_home_win_prob": round(home_win, 4),
        "elo_away_win_prob": round(away_win, 4),
        "elo_draw_prob": round(draw_prob, 4),
    }


def fetch_match_elo(home_team: str, away_team: str) -> dict:
    """Return full ELO context dict for a match, or {} if either team missing."""
    home_elo = fetch_elo(home_team)
    away_elo = fetch_elo(away_team)
    if home_elo is None or away_elo is None:
        return {}
    result = elo_win_probabilities(home_elo, away_elo)
    logger.debug("ELO: %s %.0f vs %s %.0f (diff %+.0f)",
                 home_team, home_elo, away_team, away_elo, result["elo_diff"])
    return result
