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
from services.fetchers import odds_api as oa
from services.scoring.confidence import ConfidenceInput, compute_confidence
from services.scoring.consensus import compute_consensus, is_strong_enough
from services.scoring.ranking import rank_picks, build_accumulators
from services.emailing.report import build_html_report, build_plain_text_report
from services.emailing.smtp import send_report

logger = logging.getLogger(__name__)
UK_TZ = pytz.timezone("Europe/London")


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
        raw_fixtures = af.get_fixtures_for_date(date_str, run)

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

        if not in_window:
            self.stdout.write(self.style.WARNING("  No fixtures in the 2–24 hour window. Exiting."))
            run.completed = True
            run.save(update_fields=["completed"])
            return

        # ------------------------------------------------------------------ #
        # STEP 2 — Persist fixture records
        # ------------------------------------------------------------------ #
        self.stdout.write("  [2/6] Saving fixture records...")
        matches: list[Match] = []
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
        # STEP 3 — Fetch AI predictions from API-Football
        # ------------------------------------------------------------------ #
        self.stdout.write(f"  [3/6] Fetching predictions for {len(matches)} fixtures...")
        api_source, _ = DataSource.objects.get_or_create(
            name="API-Football",
            defaults={"api_endpoint": "https://v3.football.api-sports.io", "weight": 0.35},
        )
        predictions_by_fixture: dict[int, dict] = {}
        for match in matches:
            pred = af.get_prediction(match.fixture_id, run)
            if not pred:
                logger.warning("No prediction for fixture %s", match.fixture_id)
                continue
            predictions_by_fixture[match.fixture_id] = pred

            # Determine prediction type from winner
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
                    over_under_signal=pred.get("over_under_signal", ""),
                    advice=pred.get("advice", ""),
                    home_form_score=pred.get("home_form_score"),
                    away_form_score=pred.get("away_form_score"),
                ),
            )

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
        # STEP 5 — Score, rank, save
        # ------------------------------------------------------------------ #
        self.stdout.write("  [5/6] Scoring and ranking fixtures...")
        candidates: list[dict] = []

        for match in matches:
            api_pred = predictions_by_fixture.get(match.fixture_id, {})
            bm_data = odds_by_fixture.get(match.fixture_id, {})

            if not api_pred and not bm_data:
                continue   # no data at all — skip

            api_home = api_pred.get("home_win_prob", 0.0)
            api_draw = api_pred.get("draw_prob", 0.0)
            api_away = api_pred.get("away_win_prob", 0.0)
            bm_home = bm_data.get("home_implied", 0.0)
            bm_draw = bm_data.get("draw_implied", 0.0)
            bm_away = bm_data.get("away_implied", 0.0)

            consensus = compute_consensus(
                api_home_prob=api_home,
                api_draw_prob=api_draw,
                api_away_prob=api_away,
                bm_home_implied=bm_home,
                bm_draw_implied=bm_draw,
                bm_away_implied=bm_away,
                fd_form_home=api_pred.get("home_form_score", 0.0),
                fd_form_away=api_pred.get("away_form_score", 0.0),
            )

            if not is_strong_enough(consensus):
                continue

            conf_input = ConfidenceInput(
                home_win_prob=api_home,
                draw_prob=api_draw,
                away_win_prob=api_away,
                home_form_score=api_pred.get("home_form_score", 0.5),
                away_form_score=api_pred.get("away_form_score", 0.5),
                over_under_signal=api_pred.get("over_under_signal", ""),
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

            # Persist combined prediction
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
                "form_score": api_pred.get("home_form_score", 0.5),
            })

        picks = rank_picks(candidates)

        # Persist recommendations
        Recommendation.objects.filter(run_date=target_date).delete()
        for pick in picks:
            match = Match.objects.get(fixture_id=pick.fixture_id)
            Recommendation.objects.create(
                run_date=target_date,
                rank=pick.rank,
                match=match,
                prediction=match.prediction,
                risk_label=pick.risk_label,
            )
            match.prediction.accumulator_tier = pick.accumulator_tier
            match.prediction.save(update_fields=["accumulator_tier"])

        run.predictions_generated = len(picks)
        run.save(update_fields=["predictions_generated"])

        # Print to console
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS(f"  TOP {len(picks)} PREDICTIONS — {target_date}"))
        self.stdout.write("=" * 60)
        for p in picks:
            odds_str = f"@ {p.best_odds}" if p.best_odds else ""
            self.stdout.write(
                f"\n  #{p.rank} {p.home_team} vs {p.away_team}"
                f"\n     {p.league} | {p.kickoff_uk} (UK)"
                f"\n     Bet: {p.prediction_type.replace('_', ' ').title()} {odds_str}"
                f"\n     Confidence: {p.confidence_pct}% | {p.risk_label} Risk | {p.accumulator_tier}"
                f"\n     {p.reasoning}"
            )
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
