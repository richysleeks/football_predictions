"""
Usage:
    python manage.py run_predictions
    python manage.py run_predictions --date 2026-05-24
    python manage.py run_predictions --no-email

What it does:
  1. Fetches today's fixtures from API-Football (status=NS, 2–24 hrs ahead)
  2. Fetches AI predictions for each fixture
  3. Fetches bookmaker odds from The Odds API
  4. Scores each fixture using the confidence formula
  5. Computes cross-source consensus
  6. Ranks top 10, assigns accumulator tiers
  7. Saves everything to the database
  8. Sends the HTML prediction report by email
"""
import json
import logging
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pytz
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.predictions.models import (
    DataSource, FetchRun, Match, MatchPrediction, Recommendation, SourcePrediction,
)
from services.fetchers import api_football as af
from services.fetchers.api_football import QuotaExceeded
from services.fetchers import odds_api as oa
from services.fetchers import football_data as fd
from services.fetchers import forebet as fb_scraper
from services.scoring.confidence import ConfidenceInput, compute_confidence
from services.scoring.consensus import (
    compute_consensus, compute_over_under_consensus, compute_btts_consensus, is_strong_enough,
)
from services.scoring.ranking import rank_picks, build_accumulators
from services.emailing.report import build_html_report, build_plain_text_report
from services.emailing.smtp import send_report
from services.research import gnews as gn
from services.research import analyst as news_analyst

logger = logging.getLogger(__name__)
UK_TZ = pytz.timezone("Europe/London")

# Top-tier leagues to prioritise for the limited API-Football quota.
# Matches these keywords (case-insensitive) get predictions first.
_PRIORITY_KEYWORDS = [
    "premier league", "la liga", "serie a", "bundesliga", "ligue 1",
    "eredivisie", "primeira liga", "champions league", "europa league",
    "conference league", "championship", "2. bundesliga",
]

# Football-Data.org competition codes for the 12 supported leagues,
# keyed by the lowercased league name fragment we expect from API-Football.
_FD_COMPETITION_MAP: dict[str, str] = {
    "premier league": "PL",
    "bundesliga": "BL1",
    "serie a": "SA",
    "ligue 1": "FL1",
    "la liga": "PD",
    "eredivisie": "DED",
    "primeira liga": "PPL",
    "champions league": "CL",
    "europa league": "EL",
    "conference league": "ECL",
    "championship": "ELC",
    "2. bundesliga": "BL2",
}

API_PREDICTION_BUDGET = 90   # stay comfortably inside 100 req/day


def _is_priority(league_name: str) -> bool:
    name = league_name.lower()
    return any(kw in name for kw in _PRIORITY_KEYWORDS)


def _fd_code_for_league(league_name: str) -> str | None:
    name = league_name.lower()
    for fragment, code in _FD_COMPETITION_MAP.items():
        if fragment in name:
            return code
    return None


def _form_lookup(fd_form: dict[str, float], team_name: str) -> float:
    """Exact match first, then partial. Returns 0.0 when no FD data found."""
    if team_name in fd_form:
        return fd_form[team_name]
    tl = team_name.lower()
    for key, score in fd_form.items():
        kl = key.lower()
        if tl in kl or kl in tl:
            return score
    return 0.0


class Command(BaseCommand):
    help = "Fetch football predictions and email the top 10 picks"

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help="Date to fetch predictions for (YYYY-MM-DD). Defaults to today.",
        )
        parser.add_argument(
            "--no-email",
            action="store_true",
            help="Skip sending the email report.",
        )
        parser.add_argument(
            "--no-research",
            action="store_true",
            help="Skip GNews + Claude research enrichment for top 4.",
        )
        parser.add_argument(
            "--save-json",
            action="store_true",
            help="Save the ranked picks to data/final/ as JSON.",
        )

    def handle(self, *args, **options):
        target_date = (
            date.fromisoformat(options["date"]) if options["date"] else date.today()
        )
        self.stdout.write(self.style.SUCCESS(f"\n=== Football Predictions: {target_date} ===\n"))

        run = FetchRun.objects.create()

        try:
            self._run(run, target_date, options)
        except Exception as exc:
            run.errors += f"\nFATAL: {exc}"
            run.save(update_fields=["errors"])
            logger.exception("Run failed")
            self.stderr.write(self.style.ERROR(f"Run failed: {exc}"))
            sys.exit(1)

    def _run(self, run: FetchRun, target_date: date, options: dict):
        now_utc = datetime.now(timezone.utc)
        window_start = now_utc + timedelta(hours=2)
        window_end = now_utc + timedelta(hours=24)

        # ------------------------------------------------------------------ #
        # STEP 1 — Fetch fixtures from API-Football
        # ------------------------------------------------------------------ #
        self.stdout.write("  [1/6] Fetching fixtures from API-Football...")
        date_str = target_date.strftime("%Y-%m-%d")
        api_quota_exhausted = False
        try:
            raw_fixtures = af.get_fixtures_for_date(date_str, run)
        except QuotaExceeded:
            self.stdout.write(self.style.WARNING(
                "  API-Football quota exhausted on fixture fetch — using DB matches for today."
            ))
            raw_fixtures = []
            api_quota_exhausted = True

        # Filter to selection window
        in_window = []
        for fix in raw_fixtures:
            ko_str = fix.get("fixture", {}).get("date")
            if not ko_str:
                continue
            try:
                ko_utc = datetime.fromisoformat(ko_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if window_start <= ko_utc <= window_end:
                in_window.append((fix, ko_utc))

        self.stdout.write(f"     {len(raw_fixtures)} fixtures found, {len(in_window)} in selection window")
        run.fixtures_found = len(in_window)
        run.save(update_fields=["fixtures_found"])

        # ------------------------------------------------------------------ #
        # STEP 2 — Persist fixture records
        # ------------------------------------------------------------------ #
        self.stdout.write("  [2/6] Saving fixture records...")
        matches: list[Match] = []

        if not in_window:
            # No fixtures fetched (quota hit or window closed) — fall back to matches already in DB
            existing = list(Match.objects.filter(kickoff_utc__date=target_date))
            if existing:
                self.stdout.write(f"     Using {len(existing)} cached DB matches for {target_date}.")
                matches = existing
                api_quota_exhausted = True  # skip fresh API prediction calls
            else:
                self.stdout.write(self.style.WARNING("  No fixtures in the 2–24 hour window. Exiting."))
                run.completed = True
                run.save(update_fields=["completed"])
                return

        for fix, ko_utc in in_window:
            fixture_data = fix.get("fixture", {})
            league_data = fix.get("league", {})
            teams = fix.get("teams", {})
            fixture_id = fixture_data.get("id")
            if not fixture_id:
                continue

            ko_uk = ko_utc.astimezone(UK_TZ)
            match, _ = Match.objects.update_or_create(
                fixture_id=fixture_id,
                defaults=dict(
                    home_team=teams.get("home", {}).get("name", ""),
                    away_team=teams.get("away", {}).get("name", ""),
                    league_name=league_data.get("name", ""),
                    league_country=league_data.get("country", ""),
                    kickoff_utc=ko_utc,
                    kickoff_uk=ko_uk,
                    status=fixture_data.get("status", {}).get("short", "NS"),
                    home_team_id=teams.get("home", {}).get("id"),
                    away_team_id=teams.get("away", {}).get("id"),
                    league_id=league_data.get("id"),
                    season=league_data.get("season"),
                ),
            )
            matches.append(match)

        # ------------------------------------------------------------------ #
        # STEP 3 — Fetch AI predictions from API-Football (priority leagues first)
        # ------------------------------------------------------------------ #
        priority = [m for m in matches if _is_priority(m.league_name)]
        other   = [m for m in matches if not _is_priority(m.league_name)]
        ordered = priority + other

        self.stdout.write(
            f"  [3/6] Fetching predictions — {len(priority)} priority + "
            f"{len(other)} other fixtures (budget: {API_PREDICTION_BUDGET})..."
        )
        api_source, _ = DataSource.objects.get_or_create(
            name="API-Football",
            defaults={"api_endpoint": "https://v3.football.api-sports.io", "weight": 0.35},
        )
        predictions_by_fixture: dict[int, dict] = {}
        if api_quota_exhausted:
            self.stdout.write("  Skipping API-Football predictions (quota already exhausted).")
        try:
            for match in ([] if api_quota_exhausted else ordered[:API_PREDICTION_BUDGET]):
                pred = af.get_prediction(match.fixture_id, run)
                if not pred:
                    logger.warning("No prediction for fixture %s", match.fixture_id)
                    continue
                predictions_by_fixture[match.fixture_id] = pred

                if pred.get("winner_name") == match.home_team:
                    pred_type = "home_win"
                elif pred.get("winner_name") == match.away_team:
                    pred_type = "away_win"
                elif pred.get("winner_name") is None:
                    pred_type = "draw"
                else:
                    pred_type = "home_win"

                SourcePrediction.objects.update_or_create(
                    match=match,
                    source=api_source,
                    defaults=dict(
                        prediction_type=pred_type,
                        home_win_prob=pred.get("home_win_prob", 0),
                        draw_prob=pred.get("draw_prob", 0),
                        away_win_prob=pred.get("away_win_prob", 0),
                        predicted_home_goals=pred.get("predicted_home_goals"),
                        predicted_away_goals=pred.get("predicted_away_goals"),
                        over_under_signal=pred.get("over_under_signal") or "",
                        advice=pred.get("advice", ""),
                        home_form_score=pred.get("home_form_score"),
                        away_form_score=pred.get("away_form_score"),
                    ),
                )
        except QuotaExceeded:
            self.stdout.write(self.style.WARNING(
                f"  API-Football quota exhausted — {len(predictions_by_fixture)} predictions saved."
            ))

        # ------------------------------------------------------------------ #
        # STEP 3.5 — Fetch Football-Data.org form for supported competitions
        # ------------------------------------------------------------------ #
        self.stdout.write("  [3.5/6] Enriching with Football-Data.org form...")
        fd_form_by_team: dict[str, float] = {}
        needed_codes: dict[str, str] = {}   # code → friendly name
        for match in matches:
            code = _fd_code_for_league(match.league_name)
            if code and code not in needed_codes:
                needed_codes[code] = match.league_name

        for code, league_label in needed_codes.items():
            standings = fd.get_standings(code, run)
            total = len(standings)
            for row in standings:
                team_name = row.get("team_name", "")
                team_short = row.get("team_short", "")
                form_score = fd.derive_form_score(row.get("form", ""), row.get("position"), total)
                if team_name:
                    fd_form_by_team[team_name] = form_score
                if team_short:
                    fd_form_by_team[team_short] = form_score
            if standings:
                logger.info("FD form loaded: %s (%d teams)", league_label, len(standings))
            else:
                logger.warning("FD standings unavailable for %s (%s)", league_label, code)

        self.stdout.write(f"     {len(fd_form_by_team)} team form entries loaded from Football-Data.org")

        # ------------------------------------------------------------------ #
        # STEP 4 — Fetch bookmaker odds from The Odds API
        # ------------------------------------------------------------------ #
        self.stdout.write("  [4/6] Fetching bookmaker odds...")
        odds_source, _ = DataSource.objects.get_or_create(
            name="The Odds API",
            defaults={"api_endpoint": "https://api.the-odds-api.com", "weight": 0.40},
        )
        odds_events = oa.get_soccer_odds(target_date, run)
        odds_by_fixture: dict[int, dict] = {}
        for match in matches:
            for event in odds_events:
                if oa.match_event_to_fixture(event, match.home_team, match.away_team):
                    implied = oa.extract_implied_probability(event)
                    odds_by_fixture[match.fixture_id] = implied
                    SourcePrediction.objects.update_or_create(
                        match=match,
                        source=odds_source,
                        defaults=dict(
                            prediction_type=(
                                "home_win" if implied["home_implied"] > implied["away_implied"]
                                else "away_win"
                            ),
                            home_win_prob=implied["home_implied"],
                            draw_prob=implied["draw_implied"],
                            away_win_prob=implied["away_implied"],
                            bookmaker_count=implied["bookmaker_count"],
                            implied_probability=max(
                                implied["home_implied"],
                                implied["draw_implied"],
                                implied["away_implied"],
                            ),
                            raw_odds=implied,
                        ),
                    )
                    break

        # ------------------------------------------------------------------ #
        # STEP 4.5 — Scrape Forebet mathematical predictions
        # ------------------------------------------------------------------ #
        self.stdout.write("  [4.5/6] Scraping Forebet predictions...")
        forebet_preds = fb_scraper.get_predictions(target_date)
        forebet_by_fixture: dict[int, dict] = {}
        for match in matches:
            for fb_pred in forebet_preds:
                if fb_scraper.match_to_fixture(fb_pred, match.home_team, match.away_team):
                    forebet_by_fixture[match.fixture_id] = fb_pred
                    break
        self.stdout.write(f"     {len(forebet_by_fixture)} Forebet predictions matched to fixtures")

        # ------------------------------------------------------------------ #
        # STEP 5 — Score, rank, save
        # ------------------------------------------------------------------ #
        self.stdout.write("  [5/6] Scoring and ranking fixtures...")
        candidates: list[dict] = []

        for match in matches:
            api_pred = predictions_by_fixture.get(match.fixture_id, {})
            # If API quota was exhausted, fall back to stored SourcePrediction for goal/signal data
            if not api_pred:
                stored_sp = SourcePrediction.objects.filter(
                    match=match, source__name="API-Football"
                ).first()
                if stored_sp:
                    def _decode_stored(v):
                        """Decode stored goal thresholds (may be old negative values)."""
                        if v is None:
                            return None
                        try:
                            f = float(v)
                        except (TypeError, ValueError):
                            return None
                        if f < 0:
                            return abs(f) / 2
                        if f > 0:
                            return f + 0.5
                        return 0.0

                    api_pred = {
                        "home_win_prob": stored_sp.home_win_prob or 0.0,
                        "draw_prob": stored_sp.draw_prob or 0.0,
                        "away_win_prob": stored_sp.away_win_prob or 0.0,
                        "home_form_score": stored_sp.home_form_score,
                        "away_form_score": stored_sp.away_form_score,
                        "over_under_signal": stored_sp.over_under_signal or "",
                        "advice": stored_sp.advice or "",
                        "predicted_home_goals": _decode_stored(stored_sp.predicted_home_goals),
                        "predicted_away_goals": _decode_stored(stored_sp.predicted_away_goals),
                    }
            bm_data = odds_by_fixture.get(match.fixture_id, {})
            fb_pred = forebet_by_fixture.get(match.fixture_id, {})

            if not api_pred and not bm_data and not fb_pred:
                continue   # no data at all — skip

            api_home = api_pred.get("home_win_prob", 0.0)
            api_draw = api_pred.get("draw_prob", 0.0)
            api_away = api_pred.get("away_win_prob", 0.0)
            bm_home = bm_data.get("home_implied", 0.0)
            bm_draw = bm_data.get("draw_implied", 0.0)
            bm_away = bm_data.get("away_implied", 0.0)
            fb_home = fb_pred.get("home_prob", 0.0)
            fb_draw = fb_pred.get("draw_prob", 0.0)
            fb_away = fb_pred.get("away_prob", 0.0)

            # Use API-Football form when available; fall back to Football-Data.org
            fd_home = api_pred.get("home_form_score") or _form_lookup(fd_form_by_team, match.home_team)
            fd_away = api_pred.get("away_form_score") or _form_lookup(fd_form_by_team, match.away_team)

            consensus = compute_consensus(
                api_home_prob=api_home,
                api_draw_prob=api_draw,
                api_away_prob=api_away,
                bm_home_implied=bm_home,
                bm_draw_implied=bm_draw,
                bm_away_implied=bm_away,
                fd_form_home=fd_home,
                fd_form_away=fd_away,
                fb_home_prob=fb_home,
                fb_draw_prob=fb_draw,
                fb_away_prob=fb_away,
            )

            bm_implied_max = max(bm_home, bm_draw, bm_away) if bm_data else 0.0

            # ── 1X2 candidate (requires strong enough signal) ─────────── #
            if is_strong_enough(consensus, bm_implied_max):
                conf_input = ConfidenceInput(
                    home_win_prob=api_home,
                    draw_prob=api_draw,
                    away_win_prob=api_away,
                    home_form_score=fd_home or 0.5,
                    away_form_score=fd_away or 0.5,
                    over_under_signal=api_pred.get("over_under_signal") or "",
                    advice=api_pred.get("advice", ""),
                    home_implied=bm_home,
                    draw_implied=bm_draw,
                    away_implied=bm_away,
                    bookmaker_count=bm_data.get("bookmaker_count", 0),
                    home_position=match.home_position,
                    away_position=match.away_position,
                    total_teams=match.total_teams_in_league or 20,
                )
                confidence, reasoning = compute_confidence(conf_input, consensus.prediction_type)
                match_pred, _ = MatchPrediction.objects.update_or_create(
                    match=match,
                    defaults=dict(
                        prediction_type=consensus.prediction_type,
                        confidence=confidence,
                        sources_agreeing=consensus.sources_agreeing,
                        consensus_label=consensus.consensus_label,
                        reasoning=reasoning,
                        best_odds=bm_data.get("best_home_odds") if consensus.prediction_type == "home_win"
                                  else bm_data.get("best_away_odds"),
                    ),
                )
                candidates.append({
                    "fixture_id": match.fixture_id,
                    "home_team": match.home_team,
                    "away_team": match.away_team,
                    "league": match.league_name,
                    "kickoff_uk": match.kickoff_uk.strftime("%d %b %H:%M"),
                    "prediction_type": consensus.prediction_type,
                    "confidence": confidence,
                    "sources_agreeing": consensus.sources_agreeing,
                    "consensus_label": consensus.consensus_label,
                    "risk_label": consensus.risk_label,
                    "reasoning": reasoning,
                    "best_odds": match_pred.best_odds,
                    "bookmaker_count": bm_data.get("bookmaker_count", 0),
                    "api_prob": max(api_home, api_draw, api_away),
                    "bm_implied": max(bm_home, bm_draw, bm_away),
                    "form_score": fd_home or 0.5,
                })

            # ── Over / Under 2.5 (independent — always attempted) ─────── #
            ou_consensus = compute_over_under_consensus(
                api_signal=api_pred.get("over_under_signal") or "",
                api_home_goals=api_pred.get("predicted_home_goals"),
                api_away_goals=api_pred.get("predicted_away_goals"),
                bm_over25_implied=bm_data.get("over25_implied", 0.0),
                bm_under25_implied=bm_data.get("under25_implied", 0.0),
            )
            if ou_consensus:
                ou_input = ConfidenceInput(
                    over_under_signal=api_pred.get("over_under_signal") or "",
                    predicted_home_goals=api_pred.get("predicted_home_goals"),
                    predicted_away_goals=api_pred.get("predicted_away_goals"),
                    home_form_score=fd_home or 0.5,
                    away_form_score=fd_away or 0.5,
                    over25_implied=bm_data.get("over25_implied", 0.0),
                    under25_implied=bm_data.get("under25_implied", 0.0),
                    bookmaker_count=bm_data.get("bookmaker_count", 0),
                )
                ou_conf, ou_reason = compute_confidence(ou_input, ou_consensus.prediction_type)
                best_ou_odds = (
                    bm_data.get("best_over25_odds") if ou_consensus.prediction_type == "over_2.5"
                    else bm_data.get("best_under25_odds")
                )
                candidates.append({
                    "fixture_id": match.fixture_id,
                    "home_team": match.home_team,
                    "away_team": match.away_team,
                    "league": match.league_name,
                    "kickoff_uk": match.kickoff_uk.strftime("%d %b %H:%M"),
                    "prediction_type": ou_consensus.prediction_type,
                    "confidence": ou_conf,
                    "sources_agreeing": ou_consensus.sources_agreeing,
                    "consensus_label": ou_consensus.consensus_label,
                    "risk_label": ou_consensus.risk_label,
                    "reasoning": ou_reason,
                    "best_odds": best_ou_odds,
                    "bookmaker_count": bm_data.get("bookmaker_count", 0),
                    "api_prob": 0.0,
                    "bm_implied": bm_data.get("over25_implied", 0.0) if ou_consensus.prediction_type == "over_2.5"
                                  else bm_data.get("under25_implied", 0.0),
                    "form_score": (fd_home or 0.5 + fd_away or 0.5) / 2,
                })

            # ── BTTS Yes / No ─────────────────────────────────────────── #
            btts_consensus = compute_btts_consensus(
                api_home_goals=api_pred.get("predicted_home_goals"),
                api_away_goals=api_pred.get("predicted_away_goals"),
                home_form_score=fd_home or 0.5,
                away_form_score=fd_away or 0.5,
            )
            if btts_consensus:
                btts_input = ConfidenceInput(
                    predicted_home_goals=api_pred.get("predicted_home_goals"),
                    predicted_away_goals=api_pred.get("predicted_away_goals"),
                    home_form_score=fd_home or 0.5,
                    away_form_score=fd_away or 0.5,
                    bookmaker_count=bm_data.get("bookmaker_count", 0),
                )
                btts_conf, btts_reason = compute_confidence(btts_input, btts_consensus.prediction_type)
                candidates.append({
                    "fixture_id": match.fixture_id,
                    "home_team": match.home_team,
                    "away_team": match.away_team,
                    "league": match.league_name,
                    "kickoff_uk": match.kickoff_uk.strftime("%d %b %H:%M"),
                    "prediction_type": btts_consensus.prediction_type,
                    "confidence": btts_conf,
                    "sources_agreeing": btts_consensus.sources_agreeing,
                    "consensus_label": btts_consensus.consensus_label,
                    "risk_label": btts_consensus.risk_label,
                    "reasoning": btts_reason,
                    "best_odds": None,
                    "bookmaker_count": bm_data.get("bookmaker_count", 0),
                    "api_prob": 0.0,
                    "bm_implied": 0.0,
                    "form_score": (fd_home or 0.5 + fd_away or 0.5) / 2,
                })

        # ------------------------------------------------------------------ #
        # STEP 5.5 — Enrich top candidates with ELO, H2H, and weather
        # ------------------------------------------------------------------ #
        self.stdout.write("  [5.5/6] Enriching top candidates with ELO, H2H, weather...")
        self._enrich_candidates(candidates, matches)

        picks = rank_picks(candidates)

        # ------------------------------------------------------------------ #
        # STEP 5.75 — Research-enrich the top 4 with GNews keyword analysis
        # ------------------------------------------------------------------ #
        research_by_fixture: dict[int, dict] = {}
        if picks and not options.get("no_research"):
            self.stdout.write("  [5.75/6] Researching top 4 predictions with GNews...")
            for pick in picks[:4]:
                self.stdout.write(f"     → {pick.home_team} vs {pick.away_team}...")
                news = gn.fetch_match_news(pick.home_team, pick.away_team)
                assessment = news_analyst.analyse(
                    home_team=pick.home_team,
                    away_team=pick.away_team,
                    league=pick.league,
                    kickoff_str=pick.kickoff_uk,
                    prediction_type=pick.prediction_type,
                    confidence=pick.confidence,
                    bm_implied=pick.confidence,
                    sources_agreeing=pick.sources_agreeing,
                    existing_reasoning=pick.reasoning,
                    news_text="",
                    home_articles=news.get("home", []),
                    away_articles=news.get("away", []),
                )
                research_by_fixture[pick.fixture_id] = assessment

                # Apply confidence adjustment to the persisted prediction
                adj = assessment["confidence_adjustment"]
                if adj != 0.0:
                    try:
                        mp = Match.objects.get(fixture_id=pick.fixture_id).prediction
                        mp.confidence = round(min(max(mp.confidence + adj, 0.0), 1.0), 4)
                        mp.reasoning = mp.reasoning + f" | Research ({assessment['verdict']}): {assessment['research_summary']}"
                        mp.save(update_fields=["confidence", "reasoning"])
                    except Exception as exc:
                        logger.warning("Could not apply research adjustment for %s: %s", pick.fixture_id, exc)

        # Persist recommendations
        Recommendation.objects.filter(run_date=target_date).delete()
        for pick in picks:
            match = Match.objects.get(fixture_id=pick.fixture_id)
            # Ensure MatchPrediction exists — may not if this is an OU/BTTS-only pick
            mp, _ = MatchPrediction.objects.update_or_create(
                match=match,
                defaults=dict(
                    prediction_type=pick.prediction_type,
                    confidence=pick.confidence,
                    sources_agreeing=pick.sources_agreeing,
                    consensus_label=pick.consensus_label,
                    reasoning=pick.reasoning,
                    best_odds=pick.best_odds,
                ),
            )
            Recommendation.objects.create(
                run_date=target_date,
                rank=pick.rank,
                match=match,
                prediction=mp,
                risk_label=pick.risk_label,
            )
            mp.accumulator_tier = pick.accumulator_tier
            mp.save(update_fields=["accumulator_tier"])

        run.predictions_generated = len(picks)
        run.save(update_fields=["predictions_generated"])

        # Print to console
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS(f"  TOP {len(picks)} PREDICTIONS — {target_date}"))
        self.stdout.write("=" * 60)
        for p in picks:
            odds_str = f"@ {p.best_odds}" if p.best_odds else ""
            research = research_by_fixture.get(p.fixture_id)
            self.stdout.write(
                f"\n  #{p.rank} {p.home_team} vs {p.away_team}"
                f"\n     {p.league} | {p.kickoff_uk} (UK)"
                f"\n     Bet: {p.prediction_type.replace('_', ' ').title()} {odds_str}"
                f"\n     Confidence: {p.confidence_pct}% | {p.risk_label} Risk | {p.accumulator_tier}"
                f"\n     {p.reasoning}"
            )
            if research:
                adj = research["confidence_adjustment"]
                adj_str = f"+{adj*100:.1f}%" if adj >= 0 else f"{adj*100:.1f}%"
                self.stdout.write(
                    f"\n     ── Research ({research['verdict']}, {adj_str}) ──"
                    f"\n     {research['research_summary']}"
                )
                if research["risk_flags"]:
                    self.stdout.write(f"\n     Risk flags: {' | '.join(research['risk_flags'])}")
                if research["supporting_factors"]:
                    self.stdout.write(f"\n     Supporting: {' | '.join(research['supporting_factors'])}")
        self.stdout.write("\n" + "=" * 60)

        # ------------------------------------------------------------------ #
        # STEP 6 — Optionally save JSON
        # ------------------------------------------------------------------ #
        if options.get("save_json"):
            out_dir = Path(__file__).resolve().parents[5] / "data" / "final"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / f"predictions_{target_date}.json"
            with open(out_file, "w") as f:
                json.dump(
                    [p.__dict__ for p in picks],
                    f, indent=2, default=str
                )
            self.stdout.write(f"\n  Saved JSON → {out_file}")

        # ------------------------------------------------------------------ #
        # STEP 7 — Send email
        # ------------------------------------------------------------------ #
        if not options.get("no_email"):
            self.stdout.write("\n  [6/6] Sending email report...")
            subject, html = build_html_report(picks, target_date)
            plain = build_plain_text_report(picks, target_date)
            sent = send_report(subject, html, plain, run)
            if sent:
                self.stdout.write(self.style.SUCCESS("  Email sent successfully!"))
            else:
                self.stdout.write(self.style.WARNING("  Email failed — check logs."))
        else:
            self.stdout.write("  (Email skipped — --no-email flag set)")

        run.completed = True
        run.save(update_fields=["completed"])
        self.stdout.write(self.style.SUCCESS("\nDone.\n"))

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _enrich_candidates(self, candidates: list[dict], matches: list) -> None:
        """Fetch ELO, H2H, and weather for each unique match in the candidate list.

        Results are stored in Match.stats_json (persisted) and injected into each
        candidate dict as 'stats_json' so the ranker can use them immediately.
        All enrichment is best-effort — failures are silently skipped.
        """
        from services.fetchers import club_elo, the_sports_db, open_meteo

        match_by_fixture: dict[int, object] = {m.fixture_id: m for m in matches}
        enriched_cache: dict[int, dict] = {}

        for candidate in candidates:
            fixture_id = candidate.get("fixture_id")
            if fixture_id in enriched_cache:
                candidate["stats_json"] = enriched_cache[fixture_id]
                continue

            match = match_by_fixture.get(fixture_id)
            if not match:
                continue

            stats = dict(match.stats_json or {})
            updated = False

            if not stats.get("elo_home"):
                elo = club_elo.fetch_match_elo(match.home_team, match.away_team)
                if elo:
                    stats.update(elo)
                    updated = True

            if not stats.get("h2h_count"):
                h2h = the_sports_db.get_h2h(match.home_team, match.away_team)
                if h2h:
                    stats.update(h2h)
                    updated = True

            if not stats.get("weather_precip_pct") and match.league_country and match.kickoff_utc:
                weather = open_meteo.fetch_match_weather(match.league_country, match.kickoff_utc)
                if weather:
                    stats.update(weather)
                    updated = True

            if updated:
                match.stats_json = stats
                match.save(update_fields=["stats_json"])

            enriched_cache[fixture_id] = stats
            candidate["stats_json"] = stats
