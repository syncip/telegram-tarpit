"""Kleine, abhängigkeitsfreie Diagramme als SVG bzw. HTML.

Farben kommen aus CSS-Variablen (siehe style.css): --series-ai (blau) und
--series-them (orange) sind Slot 1 und 2 der validierten Kategorie-Palette,
jeweils mit eigenem Hell- und Dunkel-Wert. Tooltips laufen über data-tip
(siehe app.js), eine Tabellenansicht steht jeweils darunter.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass
class Series:
    name: str
    css: str            # CSS-Klasse für die Farbe, z. B. "s-ai"
    values: list[int]


def _nice_max(value: float) -> int:
    if value <= 4:
        return 4
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        nice = step * magnitude
        if nice >= value:
            return int(math.ceil(nice))
    return int(value)


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """Balken mit abgerundeten Ecken nur oben (Datenende), unten bündig an der Achse."""
    r = min(r, w / 2, h)
    if h <= 0:
        return ""
    return (
        f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
        f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z"
    )


def grouped_bars(labels: list[str], series: list[Series], title: str, height: int = 200) -> str:
    """Gruppierte Balken (z. B. Nachrichten pro Tag: Scammer vs. KI)."""
    width = 640
    left, right, top, bottom = 34, 8, 10, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    n = max(1, len(labels))
    peak = max((v for s in series for v in s.values), default=0)
    ymax = _nice_max(peak)
    group_w = plot_w / n
    gap = 2  # Flächenabstand zwischen benachbarten Balken
    bar_w = max(2.0, min(22.0, (group_w * 0.72 - gap * (len(series) - 1)) / len(series)))
    inner_w = bar_w * len(series) + gap * (len(series) - 1)

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape(title)}" preserveAspectRatio="xMidYMid meet">'
    ]
    for i in range(5):
        value = ymax * i / 4
        y = top + plot_h - plot_h * i / 4
        parts.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="axis" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{value:g}</text>')

    label_every = max(1, math.ceil(n / 10))
    for gi, label in enumerate(labels):
        gx = left + gi * group_w + (group_w - inner_w) / 2
        tip_rows = []
        for si, s in enumerate(series):
            value = s.values[gi]
            h = plot_h * value / ymax if ymax else 0
            x = gx + si * (bar_w + gap)
            if h > 0:
                parts.append(f'<path class="bar {s.css}" d="{_bar_path(x, top + plot_h - h, bar_w, h)}"/>')
            tip_rows.append(f"{s.name}: {value}")
        tip = html.escape(f"{label} · " + " · ".join(tip_rows))
        # unsichtbare, breite Trefferfläche für den Tooltip
        parts.append(
            f'<rect class="hit" x="{left + gi * group_w:.1f}" y="{top}" width="{group_w:.1f}" '
            f'height="{plot_h}" data-tip="{tip}"/>'
        )
        if gi % label_every == 0:
            cx = left + gi * group_w + group_w / 2
            parts.append(
                f'<text class="axis" x="{cx:.1f}" y="{height - 8}" text-anchor="middle">{html.escape(label)}</text>'
            )
    parts.append(
        f'<line class="baseline" x1="{left}" x2="{width - right}" y1="{top + plot_h}" y2="{top + plot_h}"/>'
    )
    parts.append("</svg>")

    legend = "".join(
        f'<span class="legend-item"><span class="swatch {s.css}"></span>{html.escape(s.name)} '
        f'<span class="muted">({sum(s.values)})</span></span>'
        for s in series
    )
    table_head = "".join(f"<th>{html.escape(s.name)}</th>" for s in series)
    table_rows = "".join(
        f"<tr><td>{html.escape(label)}</td>"
        + "".join(f"<td>{s.values[i]}</td>" for s in series)
        + "</tr>"
        for i, label in enumerate(labels)
        if any(s.values[i] for s in series)
    )
    return (
        f'<figure class="viz"><figcaption>{html.escape(title)}</figcaption>'
        f'<div class="legend">{legend}</div>{"".join(parts)}'
        f'<details class="table-view"><summary>Als Tabelle</summary>'
        f'<table><thead><tr><th></th>{table_head}</tr></thead><tbody>{table_rows}</tbody></table>'
        f"</details></figure>"
    )


def hbars(items: list[tuple[str, float, str]], title: str, css: str, empty: str, link_prefix: str = "") -> str:
    """Horizontale Balken für eine Rangliste. items: (Label, Wert, formatierter Wert[, Link])."""
    if not items:
        return f'<figure class="viz"><figcaption>{html.escape(title)}</figcaption><p class="muted">{html.escape(empty)}</p></figure>'
    peak = max(v for _, v, *_ in items) or 1
    rows = []
    for item in items:
        label, value, formatted = item[0], item[1], item[2]
        link = item[3] if len(item) > 3 else None
        pct = max(1.5, 100 * value / peak)
        name = html.escape(label)
        if link:
            name = f'<a href="{html.escape(link)}">{name}</a>'
        rows.append(
            f'<div class="hbar-row" data-tip="{html.escape(label)}: {html.escape(formatted)}">'
            f'<div class="hbar-label">{name}</div>'
            f'<div class="hbar-track"><div class="hbar {css}" style="width:{pct:.1f}%"></div></div>'
            f'<div class="hbar-value">{html.escape(formatted)}</div></div>'
        )
    return f'<figure class="viz"><figcaption>{html.escape(title)}</figcaption><div class="hbars">{"".join(rows)}</div></figure>'


def tag_cloud(items: list[tuple[str, int]], empty: str) -> str:
    if not items:
        return f'<p class="muted">{html.escape(empty)}</p>'
    peak = max(n for _, n in items)
    tags = []
    for word, n in items:
        size = 0.85 + 0.9 * (n / peak if peak else 0)
        tags.append(
            f'<span class="tag" style="font-size:{size:.2f}rem" data-tip="{html.escape(word)}: in {n} Chat{"s" if n != 1 else ""}">'
            f"{html.escape(word)}</span>"
        )
    return f'<div class="cloud">{" ".join(tags)}</div>'


# --- Aufbereitung der Datenbank-Zählungen -------------------------------------

def daily_series(rows, days: int, today: date | None = None) -> tuple[list[str], list[Series]]:
    today = today or date.today()
    keys = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    counts = {("them", k): 0 for k in keys} | {("bait", k): 0 for k in keys}
    for r in rows:
        who = "them" if r["sender"] == "them" else "bait"
        if (who, r["day"]) in counts:
            counts[(who, r["day"])] += r["n"]
    labels = [datetime.fromisoformat(k).strftime("%d.%m.") for k in keys]
    return labels, [
        Series("Scammer", "s-them", [counts[("them", k)] for k in keys]),
        Series("Köder (KI/du)", "s-ai", [counts[("bait", k)] for k in keys]),
    ]


def hourly_series(rows, hours: int, now: datetime | None = None) -> tuple[list[str], list[Series]]:
    now = (now or datetime.now()).replace(minute=0, second=0, microsecond=0)
    keys = [(now - timedelta(hours=hours - 1 - i)) for i in range(hours)]
    fmt = "%Y-%m-%d %H"
    counts = {(w, k.strftime(fmt)): 0 for k in keys for w in ("them", "bait")}
    for r in rows:
        who = "them" if r["sender"] == "them" else "bait"
        if (who, r["hour"]) in counts:
            counts[(who, r["hour"])] += r["n"]
    labels = [k.strftime("%H:00") for k in keys]
    return labels, [
        Series("Scammer", "s-them", [counts[("them", k.strftime(fmt))] for k in keys]),
        Series("Köder (KI/du)", "s-ai", [counts[("bait", k.strftime(fmt))] for k in keys]),
    ]
