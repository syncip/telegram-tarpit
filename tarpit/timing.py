"""Zeitverhalten: Antwortverzögerungen, Nachtruhe, Tippdauer.

Hier sitzt der eigentliche Tarpit-Effekt: Jede Antwort kostet den Scammer
Wartezeit, uns aber praktisch nichts.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta


def sample_delay(min_s: float, max_s: float, rng: random.Random | None = None) -> float:
    """Log-uniform verteilte Verzögerung.

    Das ergibt viele Antworten nach ein paar Minuten und gelegentlich sehr lange
    Pausen, ähnlich wie bei einem echten Menschen, der nebenbei noch lebt.
    """
    rng = rng or random.Random()
    min_s = max(1.0, float(min_s))
    max_s = float(max_s)
    if max_s <= min_s:
        return min_s
    return math.exp(rng.uniform(math.log(min_s), math.log(max_s)))


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def postpone_quiet_hours(
    due: datetime, start: int, end: int, rng: random.Random | None = None
) -> datetime:
    """Verschiebt einen Zeitpunkt, der in die Nachtruhe fällt, auf den Morgen danach."""
    if not in_quiet_hours(due.hour, start, end):
        return due
    rng = rng or random.Random()
    wake = due.replace(hour=end, minute=0, second=0, microsecond=0)
    if wake <= due:
        wake += timedelta(days=1)
    # nicht exakt um 7:00, sondern irgendwann in den 90 Minuten danach
    return wake + timedelta(seconds=rng.uniform(0, 90 * 60))


def typing_duration(text: str, rng: random.Random | None = None) -> float:
    """Wie lange 'tippt' die Persona? Langsam, aber gedeckelt."""
    rng = rng or random.Random()
    chars_per_second = rng.uniform(3.0, 6.0)
    return min(2.0 + len(text) / chars_per_second, 30.0)


def typing_plan(
    text: str, rng: random.Random | None = None, instant: bool = False
) -> list[tuple[str, float]]:
    """Ablauf aus ("typing", Sekunden) und ("pause", Sekunden) für eine Nachricht.

    Echte Menschen tippen nicht am Stück: Sie fangen an, hören auf, überlegen,
    tippen weiter. Genau das sieht der Scammer als "schreibt ..." an- und
    ausgehen.
    """
    rng = rng or random.Random()
    total = typing_duration(text, rng)
    if instant:
        return [("typing", min(total, 3.0))]
    plan: list[tuple[str, float]] = []
    if rng.random() < 0.15:
        # Fehlstart: kurz tippen, abbrechen, später richtig schreiben
        plan += [("typing", rng.uniform(2, 5)), ("pause", rng.uniform(8, 30))]
    if total > 6 and rng.random() < 0.4:
        # zwischendurch zögern
        first = total * rng.uniform(0.3, 0.7)
        plan += [("typing", first), ("pause", rng.uniform(2, 8)), ("typing", total - first)]
    else:
        plan.append(("typing", total))
    return plan
