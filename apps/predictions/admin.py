from django.contrib import admin
from .models import (
    DataSource, Match, SourcePrediction, MatchPrediction,
    Recommendation, MatchResult, FetchRun, EmailReport,
)

admin.site.register(DataSource)
admin.site.register(Match)
admin.site.register(SourcePrediction)
admin.site.register(MatchPrediction)
admin.site.register(Recommendation)
admin.site.register(MatchResult)
admin.site.register(FetchRun)
admin.site.register(EmailReport)
