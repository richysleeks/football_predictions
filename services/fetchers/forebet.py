"""
Forebet scraper — mathematical football predictions.
Covers 850+ leagues. No API key required.
URL: https://www.forebet.com/en/football-tips-and-predictions-for-today
"""
import logging
from datetime import date
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.forebet.com/en/football-tips-and-predictions-for-today"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def get_predictions(for_date: date) -> list[dict]:
    """
    Scrapes Forebet's today page and returns a list of prediction dicts:
    {
        home_team: str,
        away_team: str,
        home_prob: float,   # 0.0–1.0
        draw_prob: float,
        away_prob: float,
        match_date: str,    # YYYY-MM-DD
    }
    Returns [] on any error so the pipeline degrades gracefully.
    """
    date_str = for_date.strftime("%Y-%m-%d")
    try:
        resp = requests.get(BASE_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Forebet fetch failed: %s", exc)
        return []

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select(".rcnt")
        if not rows:
            logger.warning("Forebet: no .rcnt rows found — page layout may have changed")
            return []

        results: list[dict] = []
        for row in rows:
            try:
                home_el = row.select_one(".homeTeam [itemprop=name]")
                away_el = row.select_one(".awayTeam [itemprop=name]")
                time_el = row.select_one("time[datetime]")
                prob_spans = row.select(".fprc span")

                if not home_el or not away_el or len(prob_spans) < 3:
                    continue

                match_date = time_el["datetime"] if time_el else date_str
                if match_date != date_str:
                    continue  # skip matches not on the target date

                home_pct = int(prob_spans[0].text.strip())
                draw_pct = int(prob_spans[1].text.strip())
                away_pct = int(prob_spans[2].text.strip())
                total = home_pct + draw_pct + away_pct
                if total == 0:
                    continue

                results.append({
                    "home_team": home_el.text.strip(),
                    "away_team": away_el.text.strip(),
                    "home_prob": home_pct / total,
                    "draw_prob": draw_pct / total,
                    "away_prob": away_pct / total,
                    "match_date": match_date,
                })
            except (ValueError, KeyError, TypeError):
                continue

        logger.info("Forebet: %d predictions scraped", len(results))
        return results

    except Exception as exc:
        logger.warning("Forebet parse error: %s", exc)
        return []


def _normalize(name: str) -> str:
    noise = {"fc", "afc", "cf", "sc", "sv", "ac", "as", "fk", "sk", "bfc"}
    return " ".join(w for w in name.lower().split() if w not in noise)


def match_to_fixture(fb_pred: dict, home_team: str, away_team: str) -> bool:
    """Fuzzy match a Forebet prediction to an API-Football fixture."""
    fh = _normalize(fb_pred["home_team"])
    fa = _normalize(fb_pred["away_team"])
    h = _normalize(home_team)
    a = _normalize(away_team)

    def similar(s1: str, s2: str) -> bool:
        if not s1 or not s2:
            return False
        if SequenceMatcher(None, s1, s2).ratio() >= 0.75:
            return True
        words1 = {w for w in s1.split() if len(w) >= 4}
        words2 = {w for w in s2.split() if len(w) >= 4}
        return bool(words1 & words2)

    return similar(h, fh) and similar(a, fa)
