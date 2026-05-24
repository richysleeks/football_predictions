"""
Usage:
    python manage.py check_results
    python manage.py check_results --date 2026-05-24

What it does:
  1. Finds yesterday's (or --date) recommendations
  2. Fetches actual results from API-Football
  3. Compares to predictions
  4. Prints a scorecard
  5. Updates source accuracy rates (improvement loop)
"""
import logging
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from apps.predictions.models import (
    DataSource, FetchRun, Match, MatchResult, Recommendation,
)
from services.fetchers import api_football as af

logger = logging.getLogger(__name__)

_OUTCOME_MAP = {"home_win": "H", "away_win": "A", "draw": "D"}
_REVERSE_OUTCOME = {"H": "home_win", "A": "away_win", "D": "draw"}


class Command(BaseCommand):
    help = "Check yesterday's predictions against actual results and update accuracy"

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help="Date of predictions to check (YYYY-MM-DD). Defaults to yesterday.",
        )

    def handle(self, *args, **options):
        check_date = (
            date.fromisoformat(options["date"]) if options["date"]
            else date.today() - timedelta(days=1)
        )
        self.stdout.write(self.style.SUCCESS(f"\n=== Results Check: {check_date} ===\n"))

        recs = Recommendation.objects.filter(run_date=check_date).select_related(
            "match", "prediction"
        ).order_by("rank")

        if not recs.exists():
            self.stdout.write(self.style.WARNING("No recommendations found for this date."))
            return

        run = FetchRun.objects.create()
        correct = 0
        total = 0

        self.stdout.write(f"  Checking {recs.count()} predictions...\n")
        self.stdout.write(f"  {'#':<4} {'Match':<40} {'Predicted':<15} {'Result':<12} {'Outcome'}")
        self.stdout.write("  " + "-" * 85)

        sources_to_update: list[DataSource] = list(DataSource.objects.all())

        for rec in recs:
            match: Match = rec.match
            pred = rec.prediction
            fixture_id = match.fixture_id

            # Fetch live result
            fix_data = af.get_fixture_result(fixture_id, run)
            if not fix_data:
                self.stdout.write(f"  #{rec.rank:<3} {match.home_team} vs {match.away_team} — no result yet")
                continue

            goals = fix_data.get("goals", {})
            home_goals = goals.get("home")
            away_goals = goals.get("away")

            if home_goals is None or away_goals is None:
                self.stdout.write(f"  #{rec.rank:<3} {match.home_team} vs {match.away_team} — not finished")
                continue

            if home_goals > away_goals:
                actual_outcome = "home_win"
            elif away_goals > home_goals:
                actual_outcome = "away_win"
            else:
                actual_outcome = "draw"

            correct_flag = pred.prediction_type == actual_outcome

            # Persist result
            MatchResult.objects.update_or_create(
                match=match,
                defaults=dict(
                    home_goals=home_goals,
                    away_goals=away_goals,
                    outcome=actual_outcome,
                    prediction_correct=correct_flag,
                ),
            )
            match.status = "FT"
            match.save(update_fields=["status"])

            total += 1
            if correct_flag:
                correct += 1
            symbol = "✓" if correct_flag else "✗"
            match_str = f"{match.home_team} vs {match.away_team}"[:38]
            result_str = f"{home_goals}-{away_goals}"
            pred_str = pred.prediction_type.replace("_", " ").title()

            self.stdout.write(
                f"  #{rec.rank:<3} {match_str:<40} {pred_str:<15} {result_str:<12} {symbol}"
            )

        # ------------------------------------------------------------------ #
        # Improvement loop — update source accuracy
        # ------------------------------------------------------------------ #
        for source in sources_to_update:
            source.total_predictions += total
            source.correct_predictions += correct
            source.update_accuracy()

        run.completed = True
        run.save(update_fields=["completed"])

        accuracy = round(correct / total * 100, 1) if total else 0
        grade = "Excellent" if accuracy >= 70 else "Good" if accuracy >= 60 else "Average" if accuracy >= 50 else "Poor"

        self.stdout.write("\n  " + "=" * 50)
        self.stdout.write(self.style.SUCCESS(
            f"  SCORECARD: {correct}/{total} correct ({accuracy}%) — {grade}"
        ))
        self.stdout.write("  " + "=" * 50 + "\n")
