"""
HTML email report generator.

Produces the same style report I generate manually — ranked top 10 table,
confidence bars, tiered accumulators, and scorecard when results are available.
"""
from datetime import date
from typing import Optional

from services.scoring.ranking import RankedPick, build_accumulators


_PREDICTION_LABELS = {
    "home_win": "Home Win",
    "away_win": "Away Win",
    "draw": "Draw",
    "over_2.5": "Over 2.5 Goals",
    "under_2.5": "Under 2.5 Goals",
    "over_1.5": "Over 1.5 Goals",
    "btts_yes": "BTTS Yes",
    "btts_no": "BTTS No",
}

_RISK_COLORS = {
    "Low": "#27ae60",
    "Medium": "#f39c12",
    "High": "#e74c3c",
}

_TIER_COLORS = {
    "Banker": "#1abc9c",
    "Extended": "#3498db",
    "Value": "#9b59b6",
}


def _confidence_bar(pct: float) -> str:
    color = "#27ae60" if pct >= 65 else "#f39c12" if pct >= 50 else "#e74c3c"
    return (
        f'<div style="background:#eee;border-radius:4px;height:8px;width:100%;">'
        f'<div style="background:{color};width:{pct}%;height:8px;border-radius:4px;"></div>'
        f'</div>'
    )


def _pick_row(pick: RankedPick) -> str:
    pred_label = _PREDICTION_LABELS.get(pick.prediction_type, pick.prediction_type)
    risk_color = _RISK_COLORS.get(pick.risk_label, "#888")
    tier_color = _TIER_COLORS.get(pick.accumulator_tier, "#888")
    odds_str = f"{pick.best_odds:.2f}" if pick.best_odds else "N/A"
    tier_badge = (
        f'<span style="background:{tier_color};color:#fff;padding:2px 8px;'
        f'border-radius:12px;font-size:11px;">{pick.accumulator_tier}</span>'
        if pick.accumulator_tier else ""
    )
    return f"""
    <tr style="border-bottom:1px solid #eee;">
      <td style="padding:12px 8px;font-weight:700;font-size:18px;color:#2c3e50;">#{pick.rank}</td>
      <td style="padding:12px 8px;">
        <div style="font-weight:600;color:#2c3e50;">{pick.home_team} vs {pick.away_team}</div>
        <div style="color:#888;font-size:12px;">{pick.league} &bull; {pick.kickoff_uk} (UK)</div>
      </td>
      <td style="padding:12px 8px;">
        <span style="background:#2c3e50;color:#fff;padding:3px 10px;border-radius:12px;font-size:12px;">
          {pred_label}
        </span>
      </td>
      <td style="padding:12px 8px;">
        <div style="font-weight:700;color:#2c3e50;">{pick.confidence_pct}%</div>
        {_confidence_bar(pick.confidence_pct)}
        <div style="font-size:11px;color:#888;margin-top:2px;">{pick.sources_agreeing}/3 sources agree</div>
      </td>
      <td style="padding:12px 8px;">
        <span style="color:{risk_color};font-weight:600;">{pick.risk_label} Risk</span>
        &nbsp;{tier_badge}
      </td>
      <td style="padding:12px 8px;font-weight:600;">{odds_str}</td>
      <td style="padding:12px 8px;color:#555;font-size:12px;max-width:250px;">{pick.reasoning}</td>
    </tr>"""


def _accum_section(accumulators: dict) -> str:
    sections = []
    for key in ("banker", "extended", "value"):
        acc = accumulators[key]
        legs = acc["legs"]
        if not legs:
            continue
        tier_color = _TIER_COLORS.get(legs[0].accumulator_tier if legs else "", "#888")
        leg_lines = "".join(
            f'<li style="margin-bottom:4px;">'
            f'<strong>{l.home_team} vs {l.away_team}</strong> — '
            f'{_PREDICTION_LABELS.get(l.prediction_type, l.prediction_type)} '
            f'({l.confidence_pct}%) '
            f'{"@ " + str(l.best_odds) if l.best_odds else ""}'
            f'</li>'
            for l in legs
        )
        sections.append(f"""
        <div style="background:#f8f9fa;border-left:4px solid {tier_color};padding:16px;margin-bottom:16px;border-radius:4px;">
          <div style="font-weight:700;color:{tier_color};margin-bottom:8px;">{acc['label']}</div>
          <ul style="margin:0;padding-left:20px;">{leg_lines}</ul>
          <div style="margin-top:12px;font-size:14px;">
            Combined odds: <strong>{acc['combined_odds']}</strong> &nbsp;|&nbsp;
            Avg confidence: <strong>{acc['avg_confidence_pct']}%</strong>
          </div>
        </div>""")
    return "".join(sections)


def _scorecard_section(scorecard: Optional[list[dict]]) -> str:
    if not scorecard:
        return ""
    correct = sum(1 for s in scorecard if s.get("correct"))
    total = len(scorecard)
    rows = "".join(
        f'<tr>'
        f'<td style="padding:8px;">{s["match"]}</td>'
        f'<td style="padding:8px;">{_PREDICTION_LABELS.get(s["prediction"], s["prediction"])}</td>'
        f'<td style="padding:8px;">{s.get("result", "—")}</td>'
        f'<td style="padding:8px;color:{"#27ae60" if s.get("correct") else "#e74c3c"};font-weight:700;">'
        f'{"✓ Correct" if s.get("correct") else "✗ Wrong"}</td>'
        f'</tr>'
        for s in scorecard
    )
    return f"""
    <h2 style="color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:8px;">
      Yesterday's Scorecard — {correct}/{total} correct
    </h2>
    <table style="width:100%;border-collapse:collapse;margin-bottom:32px;">
      <thead>
        <tr style="background:#2c3e50;color:#fff;">
          <th style="padding:10px;text-align:left;">Match</th>
          <th style="padding:10px;text-align:left;">Prediction</th>
          <th style="padding:10px;text-align:left;">Result</th>
          <th style="padding:10px;text-align:left;">Outcome</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def build_html_report(
    picks: list[RankedPick],
    run_date: date,
    scorecard: Optional[list[dict]] = None,
) -> tuple[str, str]:
    """
    Returns (subject, html_body) for the email report.
    """
    subject = f"Football Predictions — {run_date.strftime('%A %d %B %Y')} | Top {len(picks)} Picks"

    accumulators = build_accumulators(picks)
    top_conf = picks[0].confidence_pct if picks else 0

    rows = "".join(_pick_row(p) for p in picks)

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:900px;margin:0 auto;padding:20px;color:#333;">

  <div style="background:linear-gradient(135deg,#2c3e50,#3498db);color:#fff;padding:24px;border-radius:8px;margin-bottom:24px;">
    <h1 style="margin:0;font-size:24px;">Football Predictions</h1>
    <div style="margin-top:4px;opacity:0.85;">{run_date.strftime('%A, %d %B %Y')}</div>
    <div style="margin-top:8px;font-size:14px;">
      {len(picks)} picks &nbsp;|&nbsp; Top confidence: {top_conf}%
    </div>
  </div>

  {_scorecard_section(scorecard)}

  <h2 style="color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:8px;">
    Top {len(picks)} Predictions — Ranked by Confidence
  </h2>

  <table style="width:100%;border-collapse:collapse;margin-bottom:32px;">
    <thead>
      <tr style="background:#2c3e50;color:#fff;text-align:left;">
        <th style="padding:10px;">#</th>
        <th style="padding:10px;">Match</th>
        <th style="padding:10px;">Prediction</th>
        <th style="padding:10px;">Confidence</th>
        <th style="padding:10px;">Risk / Tier</th>
        <th style="padding:10px;">Best Odds</th>
        <th style="padding:10px;">Reasoning</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <h2 style="color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:8px;">
    Accumulator Suggestions
  </h2>
  {_accum_section(accumulators)}

  <div style="background:#fff9e6;border:1px solid #f39c12;padding:16px;border-radius:4px;font-size:12px;color:#555;margin-top:24px;">
    <strong>Disclaimer:</strong> Predictions are generated from live sports data APIs
    (API-Football, The Odds API, Football-Data.org) and statistical analysis.
    Football results are inherently unpredictable. No prediction is guaranteed.
    Please gamble responsibly.
  </div>

</body>
</html>"""

    return subject, html


def build_plain_text_report(picks: list[RankedPick], run_date: date) -> str:
    """Fallback plain-text version of the report."""
    lines = [
        f"FOOTBALL PREDICTIONS — {run_date.strftime('%A %d %B %Y').upper()}",
        "=" * 60,
        "",
    ]
    for p in picks:
        pred = _PREDICTION_LABELS.get(p.prediction_type, p.prediction_type)
        odds = f"@ {p.best_odds}" if p.best_odds else ""
        lines.append(
            f"#{p.rank}  {p.home_team} vs {p.away_team}"
            f"\n     {p.league} | {p.kickoff_uk} (UK)"
            f"\n     Bet: {pred} {odds}"
            f"\n     Confidence: {p.confidence_pct}% | {p.risk_label} Risk | {p.accumulator_tier}"
            f"\n     {p.reasoning}"
            f"\n"
        )
    lines.append("\nDisclaimer: Predictions are for informational purposes only. Gamble responsibly.")
    return "\n".join(lines)
