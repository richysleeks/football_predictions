"""Open-Meteo + Nominatim weather fetcher.

Both services are completely free with no API key required.
Nominatim (OpenStreetMap) geocodes a city/country to lat/lon.
Open-Meteo provides hourly weather forecasts.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from functools import lru_cache
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

RAIN_HEAVY_PCT = 70
WIND_STRONG_KMH = 35


@lru_cache(maxsize=64)
def geocode(location: str) -> Optional[Tuple[float, float]]:
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": location, "format": "json", "limit": 1},
            headers={"User-Agent": "FootballPredictor/1.0 (prediction-research)"},
            timeout=8,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as exc:
        logger.debug("Nominatim geocode failed for '%s': %s", location, exc)
    return None


def fetch_kickoff_weather(lat: float, lon: float, kickoff_utc: datetime) -> dict:
    date_str = kickoff_utc.strftime("%Y-%m-%d")
    ko_hour_str = kickoff_utc.strftime("%Y-%m-%dT%H:00")
    try:
        resp = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "precipitation_probability,windspeed_10m,rain",
                "timezone": "UTC",
                "start_date": date_str,
                "end_date": date_str,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])

        idx = times.index(ko_hour_str) if ko_hour_str in times else 12

        precip_pct = hourly.get("precipitation_probability", [0])[idx] or 0
        wind_kmh = hourly.get("windspeed_10m", [0])[idx] or 0
        rain_mm = hourly.get("rain", [0.0])[idx] or 0.0

        risk = []
        if precip_pct >= RAIN_HEAVY_PCT:
            risk.append(f"Heavy rain expected ({precip_pct:.0f}% chance)")
        if wind_kmh >= WIND_STRONG_KMH:
            risk.append(f"Strong wind ({wind_kmh:.0f} km/h)")

        return {
            "weather_precip_pct": int(precip_pct),
            "weather_wind_kmh": round(float(wind_kmh), 1),
            "weather_rain_mm": round(float(rain_mm), 2),
            "weather_risk": " | ".join(risk),
        }
    except Exception as exc:
        logger.debug("Open-Meteo fetch failed: %s", exc)
    return {}


def fetch_match_weather(country: str, kickoff_utc: datetime) -> dict:
    if not country or not kickoff_utc:
        return {}
    coords = geocode(country)
    if not coords:
        return {}
    time.sleep(0.5)  # Nominatim usage policy: max 1 req/s
    return fetch_kickoff_weather(coords[0], coords[1], kickoff_utc)
