"""
GNews fetcher for match research.
Free tier: 100 requests/day.
We use 2 requests per match (one per team), so covers up to 50 matches/day.
Docs: https://gnews.io/docs/v4
"""
import logging
import time
from datetime import date

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://gnews.io/api/v4/search"
MAX_ARTICLES = 5


def _key() -> str:
    return settings.GNEWS_API_KEY


def fetch_team_news(team_name: str, max_articles: int = MAX_ARTICLES) -> list[dict]:
    """
    Fetches recent news articles for a team.
    Returns a list of {title, description, published_at, source} dicts.
    """
    if not _key():
        return []
    try:
        resp = requests.get(BASE_URL, params={
            "q": f'"{team_name}" football',
            "lang": "en",
            "max": max_articles,
            "apikey": _key(),
            "sortby": "publishedAt",
        }, timeout=10)
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        return [
            {
                "title": a.get("title", ""),
                "description": a.get("description", ""),
                "published_at": a.get("publishedAt", ""),
                "source": a.get("source", {}).get("name", ""),
            }
            for a in articles
        ]
    except requests.RequestException as exc:
        logger.warning("GNews fetch failed for %s: %s", team_name, exc)
        return []


def fetch_match_news(home_team: str, away_team: str) -> dict:
    """
    Fetches news for both teams. Uses 2 API requests.
    Returns {"home": [...], "away": [...]}
    """
    home_news = fetch_team_news(home_team)
    time.sleep(0.5)
    away_news = fetch_team_news(away_team)
    return {"home": home_news, "away": away_news}


def format_for_prompt(news: dict, home_team: str, away_team: str) -> str:
    """Formats news articles into a compact string for the Claude prompt."""
    lines = []
    for team, key in [(home_team, "home"), (away_team, "away")]:
        articles = news.get(key, [])
        if articles:
            lines.append(f"\n{team} recent news:")
            for a in articles[:3]:
                title = a["title"]
                desc = a["description"] or ""
                src = a["source"]
                pub = a["published_at"][:10] if a["published_at"] else ""
                lines.append(f"  [{pub}] {title} — {desc[:120]} ({src})")
        else:
            lines.append(f"\n{team} recent news: none found")
    return "\n".join(lines)
