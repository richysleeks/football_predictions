"""TheSportsDB fetcher — free historical H2H data.

URL: https://www.thesportsdb.com/api
Free tier (key=3): team search, last 15 events per team.
No API key needed for free tier.
"""
from __future__ import annotations

import logging
from difflib import get_close_matches
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE = "https://www.thesportsdb.com/api/v1/json/3"


def search_team(team_name: str) -> Optional[dict]:
    try:
        resp = requests.get(
            f"{BASE}/searchteams.php",
            params={"t": team_name},
            timeout=8,
        )
        resp.raise_for_status()
        teams = resp.json().get("teams") or []
        if not teams:
            return None
        names = [t.get("strTeam", "") for t in teams]
        matches = get_close_matches(team_name, names, n=1, cutoff=0.6)
        if matches:
            return next(t for t in teams if t.get("strTeam") == matches[0])
        return teams[0]
    except Exception as exc:
        logger.debug("TheSportsDB team search failed for '%s': %s", team_name, exc)
    return None


def fetch_last_events(team_id: str) -> list[dict]:
    try:
        resp = requests.get(
            f"{BASE}/eventslast.php",
            params={"id": team_id},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json().get("results") or []
    except Exception as exc:
        logger.debug("TheSportsDB last events failed for id=%s: %s", team_id, exc)
    return []


def _score(event: dict) -> tuple[int, int] | None:
    try:
        hg = int(event.get("intHomeScore") or -1)
        ag = int(event.get("intAwayScore") or -1)
        if hg < 0 or ag < 0:
            return None
        return hg, ag
    except (ValueError, TypeError):
        return None


def get_h2h(home_team: str, away_team: str) -> dict:
    """Return H2H stats between two teams, or {} if unavailable."""
    home_data = search_team(home_team)
    if not home_data:
        return {}

    home_id = home_data.get("idTeam")
    if not home_id:
        return {}

    events = fetch_last_events(home_id)
    away_lower = away_team.lower()

    h2h_events = [
        e for e in events
        if away_lower in (e.get("strHomeTeam") or "").lower()
        or away_lower in (e.get("strAwayTeam") or "").lower()
    ]

    if not h2h_events:
        return {}

    home_wins = draws = away_wins = goals_home_total = goals_away_total = 0

    for ev in h2h_events:
        s = _score(ev)
        if s is None:
            continue
        hg, ag = s
        ev_home = (ev.get("strHomeTeam") or "").lower()
        is_home = home_team.lower() in ev_home
        goals_home_total += hg if is_home else ag
        goals_away_total += ag if is_home else hg

        if hg == ag:
            draws += 1
        elif (hg > ag and is_home) or (ag > hg and not is_home):
            home_wins += 1
        else:
            away_wins += 1

    n = len(h2h_events)
    avg_goals = (goals_home_total + goals_away_total) / n if n else 0

    return {
        "h2h_count": n,
        "h2h_home_wins": home_wins,
        "h2h_away_wins": away_wins,
        "h2h_draws": draws,
        "h2h_avg_goals": round(avg_goals, 2),
    }
