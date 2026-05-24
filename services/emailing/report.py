"""HTML email report generator."""
import re
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
    "btts_yes": "Both Teams to Score",
    "btts_no": "Under / Clean Sheet",
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


# ---------------------------------------------------------------------------
# Plain-English reasoning
# ---------------------------------------------------------------------------

def _parse_reasoning(reasoning: str) -> dict:
    """Extract structured data from the pipe-delimited reasoning string."""
    data = {}
    for part in reasoning.split("|"):
        part = part.strip()
        m = re.search(r"Bookmakers \((\d+)\).*?(\d+)%", part)
        if m:
            data["bm_count"] = int(m.group(1))
            data["bm_pct"] = int(m.group(2))
            continue
        m = re.search(r"API-Football:\s*(\d+)%", part)
        if m:
            data["api_pct"] = int(m.group(1))
            continue
        m = re.search(r"Form:\s*(\d+)%", part)
        if m:
            data["form_pct"] = int(m.group(1))
            continue
        m = re.search(r"ELO edge:\s*(\S+)\s+\+(\d+)", part)
        if m:
            data["elo_team"] = m.group(1)
            data["elo_pts"] = int(m.group(2))
            continue
        m = re.search(r"H2H.*?leads\s+(\d+)-(\d+)-(\d+)\s+over last\s+(\d+)", part)
        if m:
            data["h2h_wins"] = int(m.group(1))
            data["h2h_draws"] = int(m.group(2))
            data["h2h_losses"] = int(m.group(3))
            data["h2h_n"] = int(m.group(4))
            continue
        m = re.search(r"H2H avg goals:\s*([\d.]+)", part)
        if m:
            data["h2h_avg_goals"] = float(m.group(1))
            continue
        if "Weather:" in part:
            data["weather"] = part.replace("Weather:", "").strip()
            continue
        if any(kw in part for kw in ["Must-win", "survival", "title urgency", "CL race", "Cup final"]):
            data["motivation"] = part.strip()
            continue
        if "dead-rubber" in part.lower() or "nothing to play for" in part.lower():
            data["dead_rubber"] = True
            continue
        if "Tip:" in part:
            m2 = re.search(r'Tip: "(.+?)"', part)
            if m2:
                data["api_tip"] = m2.group(1)
    return data


def plain_english_reason(pick: RankedPick) -> str:
    """Convert the technical reasoning string into simple, readable sentences."""
    d = _parse_reasoning(pick.reasoning)
    pred_label = _PREDICTION_LABELS.get(pick.prediction_type, pick.prediction_type)
    home = pick.home_team
    away = pick.away_team
    sentences = []

    # --- Who agrees and how strongly ---
    if pick.consensus_label == "Strong":
        sentences.append(
            f"All {pick.sources_agreeing} data sources agree — the data strongly points to <strong>{pred_label}</strong>."
        )
    elif pick.consensus_label == "Moderate":
        sentences.append(
            f"{pick.sources_agreeing} out of 3 sources lean toward <strong>{pred_label}</strong>."
        )
    else:
        sentences.append(
            f"One clear signal pointing to <strong>{pred_label}</strong>, but treat with some caution."
        )

    # --- Bookmaker backing ---
    bm_pct = d.get("bm_pct")
    bm_count = d.get("bm_count")
    if bm_pct and bm_count:
        if bm_pct >= 72:
            sentences.append(
                f"The bookmakers are very confident — {bm_count} of them price this at {bm_pct}% probability."
            )
        elif bm_pct >= 58:
            sentences.append(
                f"{bm_count} bookmakers back this at {bm_pct}% probability."
            )
        else:
            sentences.append(
                f"Bookmakers give this a {bm_pct}% chance — a decent signal, not overwhelming."
            )

    # --- Form ---
    form_pct = d.get("form_pct")
    if form_pct:
        if pick.prediction_type == "home_win":
            team = home
        elif pick.prediction_type == "away_win":
            team = away
        else:
            team = None

        if form_pct >= 70 and team:
            sentences.append(f"{team} are in excellent form right now.")
        elif form_pct >= 55 and team:
            sentences.append(f"{team} have been solid in recent games.")
        elif form_pct <= 35 and team:
            sentences.append(f"Worth noting — {team}'s recent form is patchy, but the overall data still points their way.")

    # --- ELO quality edge ---
    if d.get("elo_team") and d.get("elo_pts"):
        pts = d["elo_pts"]
        team = d["elo_team"]
        if pts >= 80:
            sentences.append(
                f"The long-term quality gap is clear — {team} are the significantly stronger side "
                f"(+{pts} ELO points). That matters."
            )
        elif pts >= 35:
            sentences.append(
                f"Over a full season, {team} have proved to be the better side (+{pts} ELO points)."
            )

    # --- H2H ---
    if d.get("h2h_wins") is not None:
        n = d["h2h_n"]
        w = d["h2h_wins"]
        dr = d["h2h_draws"]
        l = d["h2h_losses"]
        if pick.prediction_type == "home_win":
            sentences.append(
                f"Head-to-head history backs this up too — {home} have won {w} of the last {n} meetings "
                f"({dr} draws, {l} losses)."
            )
        elif pick.prediction_type == "away_win":
            sentences.append(
                f"Head-to-head history backs this up — {away} have won {w} of the last {n} meetings "
                f"({dr} draws, {l} losses)."
            )
        elif pick.prediction_type == "draw":
            sentences.append(
                f"These two sides draw a lot — {dr} draws in their last {n} meetings."
            )

    if d.get("h2h_avg_goals") and pick.prediction_type in ("over_2.5", "under_2.5", "btts_yes", "btts_no"):
        avg = d["h2h_avg_goals"]
        if pick.prediction_type in ("over_2.5", "btts_yes") and avg >= 2.5:
            sentences.append(
                f"These teams have averaged {avg:.1f} goals per game when they meet — goals are expected."
            )
        elif pick.prediction_type in ("under_2.5", "btts_no") and avg < 2.5:
            sentences.append(
                f"When these two meet, goals are rare — they average just {avg:.1f} per game."
            )

    # --- Motivation ---
    if d.get("motivation"):
        mot = d["motivation"]
        if "Must-win" in mot or "survival" in mot:
            sentences.append("This is a must-win game for them — expect full commitment.")
        elif "title" in mot:
            sentences.append("They're fighting for the title here — maximum motivation.")
        elif "CL race" in mot:
            sentences.append("They're pushing hard for a Champions League spot — big stakes.")
        elif "Cup final" in mot:
            sentences.append("It's a cup final — both teams will go all out.")

    if d.get("dead_rubber"):
        sentences.append("The opponent has nothing left to play for this season, which helps.")

    # --- API tip (plain version) ---
    tip = d.get("api_tip", "")
    if tip and len(tip) < 80:
        sentences.append(f"The data model's tip: &ldquo;{tip}&rdquo;")

    # --- Goals markets — extra context ---
    if pick.prediction_type == "over_2.5":
        if not any("goals" in s.lower() for s in sentences):
            sentences.append("Both attacks have been active recently — expect goals in this one.")
    elif pick.prediction_type == "under_2.5":
        if not any("goals" in s.lower() for s in sentences):
            sentences.append("Both teams have been tight defensively — this looks like a low-scoring game.")
    elif pick.prediction_type == "btts_yes":
        if not any("teams" in s.lower() or "goals" in s.lower() for s in sentences):
            sentences.append("Both sides have the attacking quality to get on the scoresheet.")
    elif pick.prediction_type == "btts_no":
        if not any("goals" in s.lower() for s in sentences):
            sentences.append("At least one of these defences looks set to keep a clean sheet.")

    # --- Weather warning ---
    if d.get("weather"):
        sentences.append(f"&#9888; Weather note: {d['weather']} — this could tighten the game up.")

    return " ".join(sentences) if sentences else pred_label


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------

def _confidence_bar(pct: float) -> str:
    color = "#27ae60" if pct >= 65 else "#f39c12" if pct >= 50 else "#e74c3c"
    return (
        f'<div style="background:#eee;border-radius:4px;height:7px;width:120px;">'
        f'<div style="background:{color};width:{min(pct,100):.0f}%;height:7px;border-radius:4px;"></div>'
        f'</div>'
    )


def _pick_card(pick: RankedPick) -> str:
    pred_label = _PREDICTION_LABELS.get(pick.prediction_type, pick.prediction_type)
    risk_color = _RISK_COLORS.get(pick.risk_label, "#888")
    tier_color = _TIER_COLORS.get(pick.accumulator_tier, "#aaa")
    odds_str = f"@ {pick.best_odds:.2f}" if pick.best_odds else ""
    tier_badge = (
        f'<span style="background:{tier_color};color:#fff;padding:2px 9px;'
        f'border-radius:10px;font-size:11px;font-weight:700;">{pick.accumulator_tier}</span>'
        if pick.accumulator_tier else ""
    )
    reason_html = plain_english_reason(pick)

    return f"""
<div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;margin-bottom:14px;overflow:hidden;">

  <!-- Card header -->
  <div style="background:#2c3e50;color:#fff;padding:10px 16px;display:flex;justify-content:space-between;align-items:center;">
    <span style="font-size:13px;font-weight:700;">#{pick.rank} &nbsp; {pick.home_team} vs {pick.away_team}</span>
    <span style="font-size:11px;opacity:0.75;">{pick.league} &bull; {pick.kickoff_uk} UK</span>
  </div>

  <!-- Bet + stats row -->
  <div style="padding:12px 16px;border-bottom:1px solid #f0f0f0;display:flex;flex-wrap:wrap;gap:16px;align-items:center;">
    <div>
      <span style="background:#3498db;color:#fff;padding:4px 12px;border-radius:12px;font-size:13px;font-weight:700;">
        {pred_label}
      </span>
      &nbsp;
      <span style="color:#555;font-size:13px;font-weight:600;">{odds_str}</span>
    </div>
    <div style="text-align:center;">
      <div style="font-weight:700;font-size:16px;color:#2c3e50;">{pick.confidence_pct}%</div>
      {_confidence_bar(pick.confidence_pct)}
      <div style="font-size:10px;color:#aaa;margin-top:2px;">confidence</div>
    </div>
    <div>
      <span style="color:{risk_color};font-weight:700;font-size:13px;">{pick.risk_label} Risk</span>
      &nbsp;&nbsp;{tier_badge}
    </div>
    <div style="font-size:12px;color:#888;">{pick.sources_agreeing}/3 sources agree</div>
  </div>

  <!-- Plain-English reasoning -->
  <div style="padding:12px 16px;font-size:13px;color:#444;line-height:1.6;background:#fafafa;">
    {reason_html}
  </div>

</div>"""


def _my_accumulator_section(picks: list[RankedPick]) -> str:
    """Show the 3 best picks as a clean accumulator card."""
    # Prefer Banker tier, fill with Extended, then best confidence — deduplicated
    seen: set[int] = set()
    pool: list[RankedPick] = []
    for tier_key in ("Banker", "Extended", None):
        for p in picks:
            if p.fixture_id in seen:
                continue
            if tier_key is None or p.accumulator_tier == tier_key:
                pool.append(p)
                seen.add(p.fixture_id)
            if len(pool) == 3:
                break
        if len(pool) == 3:
            break
    acca_legs = pool[:3]

    if not acca_legs:
        return ""

    combined_odds = 1.0
    for leg in acca_legs:
        if leg.best_odds and leg.best_odds > 1.0:
            combined_odds *= leg.best_odds
        else:
            prob = leg.confidence if leg.confidence > 0 else 0.5
            combined_odds *= round(1.0 / prob, 2)
    combined_odds = round(combined_odds, 2)

    avg_conf = round(sum(l.confidence_pct for l in acca_legs) / len(acca_legs), 1)

    leg_rows = "".join(
        f"""
        <tr style="border-bottom:1px solid #e8f5e9;">
          <td style="padding:10px 14px;">
            <div style="font-weight:700;color:#1a1a2e;">{leg.home_team} vs {leg.away_team}</div>
            <div style="font-size:11px;color:#888;">{leg.league} &bull; {leg.kickoff_uk} UK</div>
          </td>
          <td style="padding:10px 14px;">
            <span style="background:#27ae60;color:#fff;padding:3px 10px;border-radius:10px;font-size:12px;font-weight:700;">
              {_PREDICTION_LABELS.get(leg.prediction_type, leg.prediction_type)}
            </span>
          </td>
          <td style="padding:10px 14px;font-weight:700;color:#27ae60;font-size:15px;">
            {f"@ {leg.best_odds:.2f}" if leg.best_odds else f"{leg.confidence_pct}% conf"}
          </td>
        </tr>"""
        for leg in acca_legs
    )

    return f"""
<div style="background:linear-gradient(135deg,#1a472a,#27ae60);color:#fff;padding:20px 24px;border-radius:10px;margin-bottom:28px;">
  <div style="font-size:20px;font-weight:700;margin-bottom:4px;">&#127951; My Accumulator</div>
  <div style="font-size:13px;opacity:0.85;margin-bottom:16px;">
    My 3 strongest picks of the day — combined odds: <strong>{combined_odds}</strong> &nbsp;|&nbsp;
    Avg confidence: <strong>{avg_conf}%</strong>
  </div>
  <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;">
    <thead>
      <tr style="background:#145a32;color:#fff;font-size:12px;">
        <th style="padding:8px 14px;text-align:left;">Match</th>
        <th style="padding:8px 14px;text-align:left;">Bet</th>
        <th style="padding:8px 14px;text-align:left;">Odds</th>
      </tr>
    </thead>
    <tbody>{leg_rows}</tbody>
  </table>
  <div style="margin-top:14px;font-size:12px;opacity:0.80;">
    Stake these together as a treble. Only use an amount you are comfortable losing.
  </div>
</div>"""


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
        f'{"&#10003; Correct" if s.get("correct") else "&#10007; Wrong"}</td>'
        f'</tr>'
        for s in scorecard
    )
    return f"""
    <h2 style="color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:8px;">
      Yesterday&#39;s Scorecard &#8212; {correct}/{total} correct
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


# ---------------------------------------------------------------------------
# Main builders
# ---------------------------------------------------------------------------

def build_html_report(
    picks: list[RankedPick],
    run_date: date,
    scorecard: Optional[list[dict]] = None,
) -> tuple[str, str]:
    subject = f"Football Predictions &#8212; {run_date.strftime('%A %d %B %Y')} | Top {len(picks)} Picks"

    top_conf = picks[0].confidence_pct if picks else 0
    pick_cards = "".join(_pick_card(p) for p in picks)

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;padding:16px;color:#333;background:#f4f4f4;">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:24px;border-radius:10px;margin-bottom:20px;text-align:center;">
    <div style="font-size:28px;font-weight:700;letter-spacing:1px;">&#9917; Football Predictions</div>
    <div style="margin-top:6px;opacity:0.80;font-size:14px;">{run_date.strftime('%A, %d %B %Y')}</div>
    <div style="margin-top:8px;font-size:13px;background:rgba(255,255,255,0.1);display:inline-block;padding:4px 16px;border-radius:20px;">
      {len(picks)} picks today &nbsp;&bull;&nbsp; Top confidence: {top_conf}%
    </div>
  </div>

  {_scorecard_section(scorecard)}

  {_my_accumulator_section(picks)}

  <!-- All picks -->
  <h2 style="color:#2c3e50;margin-bottom:12px;">Today&#39;s Full Predictions</h2>
  {pick_cards}

  <!-- Disclaimer -->
  <div style="background:#fff9e6;border:1px solid #f39c12;padding:14px 16px;border-radius:6px;font-size:11px;color:#777;margin-top:8px;line-height:1.6;">
    <strong>Disclaimer:</strong> Predictions are generated from live sports data APIs
    (API-Football, The Odds API, Football-Data.org) and statistical analysis.
    Football results are inherently unpredictable. No prediction is guaranteed.
    Please gamble responsibly.
  </div>

</body>
</html>"""

    return subject, html


def build_plain_text_report(picks: list[RankedPick], run_date: date) -> str:
    lines = [
        f"FOOTBALL PREDICTIONS — {run_date.strftime('%A %d %B %Y').upper()}",
        "=" * 60,
        "",
        "MY ACCUMULATOR (top 3 treble)",
        "-" * 40,
    ]
    seen_ids: set[int] = set()
    acca: list[RankedPick] = []
    for tier_key in ("Banker", "Extended", None):
        for p in picks:
            if p.fixture_id in seen_ids:
                continue
            if tier_key is None or p.accumulator_tier == tier_key:
                acca.append(p)
                seen_ids.add(p.fixture_id)
            if len(acca) == 3:
                break
        if len(acca) == 3:
            break
    for leg in acca:
        pred = _PREDICTION_LABELS.get(leg.prediction_type, leg.prediction_type)
        odds = f"@ {leg.best_odds}" if leg.best_odds else ""
        lines.append(f"  • {leg.home_team} vs {leg.away_team} — {pred} {odds}")
    lines.append("")

    lines += ["ALL PICKS", "-" * 40, ""]
    for p in picks:
        pred = _PREDICTION_LABELS.get(p.prediction_type, p.prediction_type)
        odds = f"@ {p.best_odds}" if p.best_odds else ""
        # Strip HTML tags from plain_english_reason output
        reason_raw = plain_english_reason(p)
        reason_clean = re.sub(r"<[^>]+>", "", reason_raw).replace("&ldquo;", '"').replace("&rdquo;", '"').replace("&#9888;", "⚠")
        lines.append(
            f"#{p.rank}  {p.home_team} vs {p.away_team}"
            f"\n     {p.league} | {p.kickoff_uk} UK"
            f"\n     Bet: {pred} {odds}"
            f"\n     Confidence: {p.confidence_pct}% | {p.risk_label} Risk | {p.accumulator_tier}"
            f"\n     {reason_clean}"
            f"\n"
        )
    lines.append("Disclaimer: Predictions are for informational purposes only. Gamble responsibly.")
    return "\n".join(lines)
