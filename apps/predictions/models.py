from django.db import models


class DataSource(models.Model):
    name = models.CharField(max_length=100)
    api_endpoint = models.CharField(max_length=500)
    weight = models.FloatField(default=0.33)
    correct_predictions = models.IntegerField(default=0)
    total_predictions = models.IntegerField(default=0)
    accuracy_rate = models.FloatField(default=0.0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def update_accuracy(self) -> None:
        if self.total_predictions > 0:
            self.accuracy_rate = self.correct_predictions / self.total_predictions
            self.save(update_fields=["accuracy_rate"])

    def __str__(self) -> str:
        return self.name


class Match(models.Model):
    fixture_id = models.IntegerField(unique=True)
    home_team = models.CharField(max_length=200)
    away_team = models.CharField(max_length=200)
    league_name = models.CharField(max_length=200)
    league_country = models.CharField(max_length=100, blank=True)
    kickoff_utc = models.DateTimeField()
    kickoff_uk = models.DateTimeField()
    status = models.CharField(max_length=10, default="NS")
    home_team_id = models.IntegerField(null=True, blank=True)
    away_team_id = models.IntegerField(null=True, blank=True)
    league_id = models.IntegerField(null=True, blank=True)
    season = models.IntegerField(null=True, blank=True)
    home_position = models.IntegerField(null=True, blank=True)
    away_position = models.IntegerField(null=True, blank=True)
    home_points = models.IntegerField(null=True, blank=True)
    away_points = models.IntegerField(null=True, blank=True)
    total_teams_in_league = models.IntegerField(null=True, blank=True)
    context_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.home_team} vs {self.away_team} ({self.kickoff_uk:%Y-%m-%d %H:%M} UK)"


class SourcePrediction(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="source_predictions")
    source = models.ForeignKey(DataSource, on_delete=models.CASCADE)
    prediction_type = models.CharField(max_length=50)
    home_win_prob = models.FloatField(null=True, blank=True)
    draw_prob = models.FloatField(null=True, blank=True)
    away_win_prob = models.FloatField(null=True, blank=True)
    predicted_home_goals = models.FloatField(null=True, blank=True)
    predicted_away_goals = models.FloatField(null=True, blank=True)
    over_under_signal = models.CharField(max_length=10, blank=True)
    advice = models.TextField(blank=True)
    home_form_score = models.FloatField(null=True, blank=True)
    away_form_score = models.FloatField(null=True, blank=True)
    bookmaker_count = models.IntegerField(default=0)
    implied_probability = models.FloatField(null=True, blank=True)
    raw_odds = models.JSONField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.source.name} | {self.match} | {self.prediction_type}"


class MatchPrediction(models.Model):
    match = models.OneToOneField(Match, on_delete=models.CASCADE, related_name="prediction")
    prediction_type = models.CharField(max_length=50)
    confidence = models.FloatField()
    sources_agreeing = models.IntegerField(default=0)
    consensus_label = models.CharField(max_length=20)
    reasoning = models.TextField(blank=True)
    best_odds = models.FloatField(null=True, blank=True)
    accumulator_tier = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def confidence_pct(self) -> float:
        return round(self.confidence * 100, 1)

    def __str__(self) -> str:
        return f"{self.match} → {self.prediction_type} ({self.confidence_pct()}%)"


class Recommendation(models.Model):
    run_date = models.DateField()
    rank = models.IntegerField()
    match = models.ForeignKey(Match, on_delete=models.CASCADE)
    prediction = models.ForeignKey(MatchPrediction, on_delete=models.CASCADE)
    risk_label = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["run_date", "rank"]

    def __str__(self) -> str:
        return f"#{self.rank} {self.match}"


class MatchResult(models.Model):
    match = models.OneToOneField(Match, on_delete=models.CASCADE, related_name="result")
    home_goals = models.IntegerField()
    away_goals = models.IntegerField()
    outcome = models.CharField(max_length=20)  # home_win | away_win | draw
    prediction_correct = models.BooleanField(null=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.match} → {self.home_goals}-{self.away_goals}"


class FetchRun(models.Model):
    run_date = models.DateField(auto_now_add=True)
    run_time = models.DateTimeField(auto_now_add=True)
    api_football_calls = models.IntegerField(default=0)
    odds_api_calls = models.IntegerField(default=0)
    football_data_calls = models.IntegerField(default=0)
    fixtures_found = models.IntegerField(default=0)
    predictions_generated = models.IntegerField(default=0)
    email_sent = models.BooleanField(default=False)
    errors = models.TextField(blank=True)
    completed = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"Run {self.run_date} {self.run_time:%H:%M}"


class EmailReport(models.Model):
    run = models.ForeignKey(FetchRun, on_delete=models.CASCADE)
    recipient = models.EmailField()
    subject = models.CharField(max_length=300)
    sent_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"Email to {self.recipient} ({'OK' if self.success else 'FAILED'})"
