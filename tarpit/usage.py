"""Auswertung des Token-Verbrauchs mit Hochrechnung für den Monat."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from .charts import Series, stacked_bars
from .db import Database

# Zweck -> (Anzeigename, CSS-Klasse). Reihenfolge = feste Farbreihenfolge der Palette.
CATEGORIES = {
    "reply": ("Antworten", "s-c1"),
    "analysis": ("Analyse", "s-c2"),
    "vision": ("Bilderkennung", "s-c3"),
    "stt": ("Spracherkennung", "s-c4"),
    "other": ("Sonstiges", "s-c5"),
}


def _category(purpose: str) -> str:
    return purpose if purpose in CATEGORIES else "other"


def _money(value: float) -> str:
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.01:
        return f"${value:.3f}"
    return f"${value:.5f}"


def usage_report(db: Database, settings: dict, today: date | None = None, days: int = 30) -> dict:
    today = today or date.today()
    first = today - timedelta(days=days - 1)
    rows = db.usage_by_day(first.isoformat())

    day_keys = [(first + timedelta(days=i)).isoformat() for i in range(days)]
    tokens = {cat: {d: 0 for d in day_keys} for cat in CATEGORIES}
    cost_by_day = {d: 0.0 for d in day_keys}
    calls_by_cat = {cat: 0 for cat in CATEGORIES}
    prompt = cached = completion = 0
    for r in rows:
        cat = _category(r["purpose"])
        if r["day"] in cost_by_day:
            tokens[cat][r["day"]] += (r["prompt"] or 0) + (r["completion"] or 0)
            cost_by_day[r["day"]] += r["cost"] or 0.0
    tokens_by_day = {d: sum(tokens[c][d] for c in CATEGORIES) for d in day_keys}

    month_start = today.replace(day=1)
    month_rows = db.usage_by_day(month_start.isoformat())
    mtd_tokens = sum((r["prompt"] or 0) + (r["completion"] or 0) for r in month_rows)
    mtd_cost = sum(r["cost"] or 0.0 for r in month_rows)
    for r in month_rows:
        calls_by_cat[_category(r["purpose"])] += r["calls"]
        prompt += r["prompt"] or 0
        cached += r["cached"] or 0
        completion += r["completion"] or 0

    # Durchschnitt der letzten 7 vollen Tage; ohne Daten: heute als Schätzung
    last7 = [(today - timedelta(days=i)).isoformat() for i in range(1, 8)]
    active7 = [d for d in last7 if d in tokens_by_day and tokens_by_day[d] > 0]
    if active7:
        avg_tokens = sum(tokens_by_day[d] for d in last7 if d in tokens_by_day) / 7
        avg_cost = sum(cost_by_day[d] for d in last7 if d in cost_by_day) / 7
        basis = "Durchschnitt der letzten 7 Tage"
    else:
        avg_tokens = tokens_by_day[today.isoformat()]
        avg_cost = cost_by_day[today.isoformat()]
        basis = "nur heute (noch keine Vortage)"
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    remaining = days_in_month - today.day
    # heute ist schon in mtd enthalten, der Rest des Tages wird mit dem Durchschnitt geschätzt
    today_left = max(0.0, avg_tokens - tokens_by_day[today.isoformat()])
    today_cost_left = max(0.0, avg_cost - cost_by_day[today.isoformat()])
    projection_tokens = mtd_tokens + today_left + avg_tokens * remaining
    projection_cost = mtd_cost + today_cost_left + avg_cost * remaining

    labels = [date.fromisoformat(d).strftime("%d.%m.") for d in day_keys]
    series = [Series(name, css, [tokens[cat][d] for d in day_keys]) for cat, (name, css) in CATEGORIES.items()]
    cost_series = [Series("Kosten", "s-c1", [round(cost_by_day[d] * 100000) for d in day_keys])]

    limit = settings["daily_token_limit"]
    today_tokens = tokens_by_day[today.isoformat()]
    replies = calls_by_cat["reply"]
    return {
        "today_tokens": today_tokens,
        "today_cost": _money(cost_by_day[today.isoformat()]),
        "limit": limit,
        "limit_pct": min(100, round(100 * today_tokens / limit)) if limit else None,
        "mtd_tokens": mtd_tokens,
        "mtd_cost": _money(mtd_cost),
        "avg_tokens": round(avg_tokens),
        "avg_cost": _money(avg_cost),
        "projection_tokens": round(projection_tokens),
        "projection_cost": _money(projection_cost),
        "projection_basis": basis,
        "month_name": today.strftime("%m/%Y"),
        "days_in_month": days_in_month,
        "cache_rate": round(100 * cached / prompt) if prompt else None,
        "prompt": prompt,
        "cached": cached,
        "completion": completion,
        "per_reply": round((prompt + completion) / replies) if replies else None,
        "calls": calls_by_cat,
        "categories": CATEGORIES,
        "models": db.usage_by_model(month_start.isoformat()),
        "money": _money,
        "has_cost": mtd_cost > 0,
        "prices_set": bool(settings["price_input_per_m"] or settings["price_output_per_m"]),
        "chart_tokens": stacked_bars(labels, series, f"Tokens pro Tag ({days} Tage)"),
        "chart_cost": stacked_bars(
            labels, cost_series, f"Kosten pro Tag ({days} Tage)", height=170,
            fmt=lambda v: _money(v / 100000), axis_fmt=lambda v: _money(v / 100000),
        ),
    }
