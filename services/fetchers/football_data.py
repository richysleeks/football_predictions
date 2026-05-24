"""
Football-Data.org fetcher — top 12 European competitions, unlimited (10 req/min).
Docs: https://docs.football-data.org/general/v4/index.html
"""
import logging
import time
from datetime import date

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"

# Competition codes available on the free tier
COMPETITIONS = {
    "PL": "Premier League",
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "PD": "La Liga",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "CL": "Champions League",
    "EL": "Europa League",
    "ECL": "Conference League",
    "ELC": "Championship",
    "BL2": "2. Bundesliga",
}


def _headers() -> dict[str, str]:
    return {"X-Auth-Token": settings.FOOTBALL_DATA_KEY}


def _get(path: str, params: dict = None, run=None) -> dict:
    try:
        resp = requests.get(f"{BASE_URL}/{path}", headers=_headers(), params=params or {}, timeout=10)
        resp.raise_for_status()
        if run:
            run.football_data_calls += 1
            run.save(update_fields=["football_data_calls"])
        time.sleep(6.5)  # stay within 10 req/min limit
        return resp.json()
    except requests.RequestException as exc:
        logger.error("Football-Data.org %s failed: %s", path, exc)
        return {}


def get_matches_for_date(for_date: date, run=None) -> list[dict]:
    """Returns all matches from Football-Data.org for a given date."""
    date_str = for_date.strftime("%Y-%m-%d")
    data = _get("matches", {"dateFrom": date_str, "dateTo": date_str}, run)
    return data.get("matches", [])


def get_standings(competition_code: str, run=None) -> list[dict]:
    """
    Returns flat standings list for a competition.
    Each entry: {team_name, position, points, goalDifference, playedGames, form}
    """
    data = _get(f"competitions/{competition_code}/standings", run=run)
    standings = data.get("standings", [])
    if not standings:
        return []
    total_standings = next((s for s in standings if s.get("type") == "TOTAL"), standings[0])
    return [
        {
            "team_name": entry.get("team", {}).get("name", ""),
            "team_short": entry.get("team", {}).get("shortName", ""),
            "position": entry.get("position"),
            "points": entry.get("points"),
            "goal_difference": entry.get("goalDifference"),
            "played": entry.get("playedGames"),
            "form": entry.get("form", ""),
        }
        for entry in total_standings.get("table", [])
    ]


def derive_form_score(form_str: str) -> float:
    """
    Convert a form string like 'WWDLW' to a 0.0–1.0 score.
    W=1.0, D=0.5, L=0.0
    """
    if not form_str:
        return 0.5
    scores = {"W": 1.0, "D": 0.5, "L": 0.0}
    values = [scores.get(c, 0.5) for c in form_str.upper() if c in scores]
    return sum(values) / len(values) if values else 0.5
