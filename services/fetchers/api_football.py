"""
API-Football fetcher (RapidAPI v3).
Docs: https://v3.football.api-sports.io
Free tier: 100 requests/day.
"""
import time
import logging
from datetime import datetime, timezone
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {
    "x-rapidapi-host": "v3.football.api-sports.io",
    "x-rapidapi-key": "",  # set at runtime from settings
}
CALL_DELAY = 0.5  # seconds between requests


def _headers() -> dict[str, str]:
    return {**HEADERS, "x-rapidapi-key": settings.API_FOOTBALL_KEY}


def _get(endpoint: str, params: dict, run: Any = None) -> dict:
    url = f"{BASE_URL}/{endpoint}"
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=10)
        resp.raise_for_status()
        if run:
            run.api_football_calls += 1
            run.save(update_fields=["api_football_calls"])
        time.sleep(CALL_DELAY)
        return resp.json()
    except requests.RequestException as exc:
        logger.error("API-Football %s failed: %s", endpoint, exc)
        return {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def get_fixtures_for_date(date_str: str, run: Any = None) -> list[dict]:
    """
    Returns list of raw fixture dicts for a given YYYY-MM-DD date.
    Only returns fixtures with status NS (Not Started).
    """
    data = _get("fixtures", {"date": date_str, "timezone": "UTC"}, run)
    fixtures = data.get("response", [])
    return [f for f in fixtures if f.get("fixture", {}).get("status", {}).get("short") == "NS"]


def get_fixture_result(fixture_id: int, run: Any = None) -> dict:
    """Returns a single fixture by ID, used to check results after the game."""
    data = _get("fixtures", {"id": fixture_id}, run)
    results = data.get("response", [])
    return results[0] if results else {}


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------

def get_prediction(fixture_id: int, run: Any = None) -> dict:
    """
    Returns API-Football's AI prediction for a fixture.

    Key fields extracted:
      winner_name, home_prob%, draw_prob%, away_prob%,
      advice, predicted_score, over_under,
      home_form_score, away_form_score,
      home_attack, home_defence, away_attack, away_defence,
      home_last5_wins, home_last5_draws, home_last5_losses,
      home_avg_goals_for, home_avg_goals_against,
      (same for away)
    """
    data = _get("predictions", {"fixture": fixture_id}, run)
    resp = data.get("response", [])
    if not resp:
        return {}
    raw = resp[0]
    pred = raw.get("predictions", {})
    teams = raw.get("teams", {})
    comp = raw.get("comparison", {})

    def pct(s: str) -> float:
        try:
            return float(str(s).replace("%", "")) / 100.0
        except (TypeError, ValueError):
            return 0.0

    def form_score(team_key: str) -> float:
        last5 = teams.get(team_key, {}).get("last_5", {})
        wins = last5.get("wins", 0) or 0
        draws = last5.get("draws", 0) or 0
        losses = last5.get("loses", 0) or 0
        total = wins + draws + losses
        if total == 0:
            return 0.5
        return (wins + 0.5 * draws) / total

    winner = pred.get("winner", {}) or {}
    pcts = pred.get("percent", {}) or {}
    goals = pred.get("goals", {}) or {}

    return {
        "fixture_id": fixture_id,
        "winner_name": winner.get("name"),
        "winner_comment": winner.get("comment", ""),
        "home_win_prob": pct(pcts.get("home")),
        "draw_prob": pct(pcts.get("draw")),
        "away_win_prob": pct(pcts.get("away")),
        "advice": pred.get("advice", ""),
        "predicted_home_goals": goals.get("home"),
        "predicted_away_goals": goals.get("away"),
        "over_under_signal": pred.get("under_over", ""),
        "home_form_score": form_score("home"),
        "away_form_score": form_score("away"),
        "home_attack": pct(comp.get("att", {}).get("home")),
        "away_attack": pct(comp.get("att", {}).get("away")),
        "home_defence": pct(comp.get("def", {}).get("home")),
        "away_defence": pct(comp.get("def", {}).get("away")),
        "home_avg_goals_for": teams.get("home", {}).get("last_5", {}).get("goals", {}).get("for", {}).get("average", 0),
        "away_avg_goals_for": teams.get("away", {}).get("last_5", {}).get("goals", {}).get("for", {}).get("average", 0),
    }


# ---------------------------------------------------------------------------
# Standings (for league position / motivation context)
# ---------------------------------------------------------------------------

def get_standings(league_id: int, season: int, run: Any = None) -> list[dict]:
    """
    Returns a flat list of team standing dicts:
      {team_id, team_name, rank, points, goalsDiff, form, played}
    """
    data = _get("standings", {"league": league_id, "season": season}, run)
    resp = data.get("response", [])
    if not resp:
        return []
    standings = resp[0].get("league", {}).get("standings", [])
    if not standings:
        return []
    flat = standings[0] if isinstance(standings[0], list) else standings
    result = []
    for entry in flat:
        result.append({
            "team_id": entry.get("team", {}).get("id"),
            "team_name": entry.get("team", {}).get("name"),
            "rank": entry.get("rank"),
            "points": entry.get("points"),
            "goals_diff": entry.get("goalsDiff"),
            "form": entry.get("form", ""),
            "played": entry.get("all", {}).get("played", 0),
        })
    return result


# ---------------------------------------------------------------------------
# H2H (optional, used when API budget allows)
# ---------------------------------------------------------------------------

def get_h2h(team_a_id: int, team_b_id: int, last: int = 10, run: Any = None) -> list[dict]:
    data = _get("fixtures/headtohead", {
        "h2h": f"{team_a_id}-{team_b_id}",
        "last": last,
    }, run)
    return data.get("response", [])
