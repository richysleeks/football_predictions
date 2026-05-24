"""
The Odds API fetcher.
Docs: https://the-odds-api.com/liveapi/guides/v4/
Free tier: 500 requests/month. Use 1 bulk call per day.
"""
import logging
from datetime import date

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"


def _key() -> str:
    return settings.ODDS_API_KEY


def get_soccer_odds(for_date: date, run=None) -> list[dict]:
    """
    Fetches h2h odds for all soccer events on a given date.
    Returns a list of event dicts, each containing bookmaker odds.
    """
    date_str = for_date.strftime("%Y-%m-%d")
    params = {
        "apiKey": _key(),
        "regions": "eu,uk",
        "markets": "h2h,totals",
        "dateFormat": "iso",
        "oddsFormat": "decimal",
        "commenceTimeFrom": f"{date_str}T00:00:00Z",
        "commenceTimeTo": f"{date_str}T23:59:59Z",
        "sport": "soccer",
    }
    try:
        resp = requests.get(f"{BASE_URL}/sports/soccer/odds", params=params, timeout=15)
        resp.raise_for_status()
        if run:
            run.odds_api_calls += 1
            run.save(update_fields=["odds_api_calls"])
        return resp.json()
    except requests.RequestException as exc:
        logger.error("Odds API failed: %s", exc)
        return []


def extract_implied_probability(event: dict, market: str = "h2h") -> dict:
    """
    For an event from The Odds API, compute average implied probability
    across all bookmakers for each outcome (home / draw / away).

    Returns:
      {
        "home_team": str,
        "away_team": str,
        "home_implied": float,   # 0.0 – 1.0
        "draw_implied": float,
        "away_implied": float,
        "bookmaker_count": int,
        "best_home_odds": float,
        "best_away_odds": float,
      }
    """
    home_team = event.get("home_team", "")
    away_team = event.get("away_team", "")
    bookmakers = event.get("bookmakers", [])

    home_odds_list: list[float] = []
    draw_odds_list: list[float] = []
    away_odds_list: list[float] = []

    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market:
                continue
            outcomes = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
            if home_team in outcomes:
                home_odds_list.append(outcomes[home_team])
            if away_team in outcomes:
                away_odds_list.append(outcomes[away_team])
            draw_price = outcomes.get("Draw")
            if draw_price:
                draw_odds_list.append(draw_price)

    def avg_implied(odds_list: list[float]) -> float:
        if not odds_list:
            return 0.0
        return round(sum(1.0 / o for o in odds_list) / len(odds_list), 4)

    def best_odds(odds_list: list[float]) -> float:
        return max(odds_list) if odds_list else 0.0

    return {
        "home_team": home_team,
        "away_team": away_team,
        "home_implied": avg_implied(home_odds_list),
        "draw_implied": avg_implied(draw_odds_list),
        "away_implied": avg_implied(away_odds_list),
        "bookmaker_count": len(bookmakers),
        "best_home_odds": best_odds(home_odds_list),
        "best_away_odds": best_odds(away_odds_list),
    }


def match_event_to_fixture(event: dict, home_team: str, away_team: str) -> bool:
    """Fuzzy match The Odds API event to an API-Football fixture by team name."""
    ev_home = event.get("home_team", "").lower()
    ev_away = event.get("away_team", "").lower()
    h = home_team.lower()
    a = away_team.lower()
    return (h[:6] in ev_home or ev_home[:6] in h) and (a[:6] in ev_away or ev_away[:6] in a)
