"""
The Odds API fetcher.
Docs: https://the-odds-api.com/liveapi/guides/v4/
Free tier: 500 requests/month. Each sport key = 1 request.
We call ~10 specific league endpoints per run (~300 req/month).
"""
import logging
from datetime import date
from difflib import SequenceMatcher

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"

# Specific league sport keys — covers the most common European fixtures.
# Calling these individually returns far more events than the generic "soccer" key.
SOCCER_SPORT_KEYS = [
    "soccer_epl",                          # Premier League
    "soccer_spain_la_liga",                # La Liga
    "soccer_italy_serie_a",                # Serie A
    "soccer_germany_bundesliga",           # Bundesliga
    "soccer_france_ligue_one",             # Ligue 1
    "soccer_netherlands_eredivisie",       # Eredivisie
    "soccer_portugal_primeira_liga",       # Primeira Liga
    "soccer_uefa_champs_league",           # Champions League
    "soccer_uefa_europa_league",           # Europa League
    "soccer_england_championship",         # Championship
    "soccer_germany_bundesliga2",          # 2. Bundesliga
    "soccer_belgium_first_div",            # Belgian First Division
    "soccer_turkey_super_league",          # Süper Lig
    "soccer_scotland_premiership",         # Scottish Premiership
]


def _key() -> str:
    return settings.ODDS_API_KEY


def get_soccer_odds(for_date: date, run=None) -> list[dict]:
    """
    Fetches h2h odds for all soccer events on a given date across major leagues.
    Returns a deduplicated list of event dicts containing bookmaker odds.
    Each sport key is one API request.
    """
    date_str = for_date.strftime("%Y-%m-%d")
    params = {
        "apiKey": _key(),
        "regions": "eu,uk",
        "markets": "h2h,totals",   # h2h = 1X2, totals = over/under goals
        "dateFormat": "iso",
        "oddsFormat": "decimal",
        "commenceTimeFrom": f"{date_str}T00:00:00Z",
        "commenceTimeTo": f"{date_str}T23:59:59Z",
    }

    all_events: list[dict] = []
    seen_ids: set[str] = set()

    for sport_key in SOCCER_SPORT_KEYS:
        try:
            resp = requests.get(
                f"{BASE_URL}/sports/{sport_key}/odds",
                params=params,
                timeout=15,
            )
            if resp.status_code in (404, 422):
                # Sport not active or not found — skip silently
                continue
            resp.raise_for_status()
            if run:
                run.odds_api_calls += 1
                run.save(update_fields=["odds_api_calls"])
            for event in resp.json():
                event_id = event.get("id", "")
                if event_id and event_id not in seen_ids:
                    seen_ids.add(event_id)
                    all_events.append(event)
        except requests.RequestException as exc:
            logger.warning("Odds API %s failed: %s", sport_key, exc)

    logger.info("Odds API: %d events fetched across %d sport keys", len(all_events), len(SOCCER_SPORT_KEYS))
    return all_events


def extract_implied_probability(event: dict) -> dict:
    """
    For an event from The Odds API, compute average implied probability
    across all bookmakers for:
      - h2h (1X2): home / draw / away
      - totals: over 2.5 / under 2.5 goals
    """
    home_team = event.get("home_team", "")
    away_team = event.get("away_team", "")
    bookmakers = event.get("bookmakers", [])

    home_odds_list: list[float] = []
    draw_odds_list: list[float] = []
    away_odds_list: list[float] = []
    over25_odds_list: list[float] = []
    under25_odds_list: list[float] = []

    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            key = mkt.get("key", "")
            outcomes = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}

            if key == "h2h":
                if home_team in outcomes:
                    home_odds_list.append(outcomes[home_team])
                if away_team in outcomes:
                    away_odds_list.append(outcomes[away_team])
                if "Draw" in outcomes:
                    draw_odds_list.append(outcomes["Draw"])

            elif key == "totals":
                # Outcomes are named "Over 2.5" / "Under 2.5"
                for name, price in outcomes.items():
                    name_lower = name.lower()
                    if "over" in name_lower and "2.5" in name_lower:
                        over25_odds_list.append(price)
                    elif "under" in name_lower and "2.5" in name_lower:
                        under25_odds_list.append(price)

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
        "over25_implied": avg_implied(over25_odds_list),
        "under25_implied": avg_implied(under25_odds_list),
        "bookmaker_count": len(bookmakers),
        "best_home_odds": best_odds(home_odds_list),
        "best_away_odds": best_odds(away_odds_list),
        "best_over25_odds": best_odds(over25_odds_list),
        "best_under25_odds": best_odds(under25_odds_list),
    }


def _normalize(name: str) -> str:
    """Strip common club suffixes and lowercase for comparison."""
    noise = {"fc", "afc", "cf", "sc", "sv", "ac", "as", "fk", "sk", "bfc", "calcio", "united", "city"}
    return " ".join(w for w in name.lower().split() if w not in noise)


def match_event_to_fixture(event: dict, home_team: str, away_team: str) -> bool:
    """
    Fuzzy-match an Odds API event to an API-Football fixture by team name.
    Uses SequenceMatcher so 'Man City' matches 'Manchester City', etc.
    """
    ev_home = _normalize(event.get("home_team", ""))
    ev_away = _normalize(event.get("away_team", ""))
    h = _normalize(home_team)
    a = _normalize(away_team)

    def similar(s1: str, s2: str) -> bool:
        if not s1 or not s2:
            return False
        ratio = SequenceMatcher(None, s1, s2).ratio()
        if ratio >= 0.75:
            return True
        # Word-level overlap for cases like "Real Madrid" vs "Real Madrid CF"
        words1 = {w for w in s1.split() if len(w) >= 4}
        words2 = {w for w in s2.split() if len(w) >= 4}
        return bool(words1 & words2)

    return similar(h, ev_home) and similar(a, ev_away)
