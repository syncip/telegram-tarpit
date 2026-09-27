"""KI-Analyse eines Chats (Zusammenfassung, Phase, Schlagworte, Best-of)
und lokale Auswertungen für die Grafiken."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Iterable, Mapping

# Phasen eines typischen Scams, von der KI pro Chat eingeordnet
STAGES = [
    "Erstkontakt",
    "Vertrauensaufbau",
    "Köder / Angebot",
    "Geldforderung",
    "Druck & Drohung",
    "Aufgegeben",
]
STAGES_SHORT = ["Kontakt", "Vertrauen", "Köder", "Geld", "Druck", "Aufgabe"]

ANALYSIS_PROMPT = """\
Du analysierst einen Telegram-Chat. Die Person „Scammer“ ist sehr wahrscheinlich ein Betrüger.
„Köder“ ist eine KI-Persona, die den Scammer absichtlich hinhält, um seine Zeit zu verschwenden.

Antworte ausschließlich mit einem JSON-Objekt (ohne Markdown) mit genau diesen Feldern:
{{
  "scam_type": "kurze Bezeichnung der Masche, z. B. Krypto-Investment, Romance, Fake-Paket",
  "stage": Zahl von 1 bis 6 ({stages}),
  "summary": "2 bis 3 Sätze auf Deutsch: Was ist bisher passiert, wo stehen beide gerade?",
  "scammer_goal": "Was will der Scammer im Moment konkret erreichen? (1 Satz)",
  "bait_tactic": "Womit hält der Köder ihn gerade hin? (1 Satz)",
  "frustration": Zahl von 0 bis 10, wie genervt/ungeduldig der Scammer wirkt,
  "keywords": ["bis zu 8 typische Schlagworte/Buzzwords des Scammers, wörtlich oder sinngemäß"],
  "best_of": [
    {{"sender": "scammer" oder "köder", "text": "wörtliches Zitat, das besonders lustig, dreist oder typisch ist"}}
  ]
}}
Höchstens 5 Einträge in best_of. Zitate wörtlich aus dem Chat übernehmen.

Chat:
{transcript}
"""

SENDER_LABELS = {"them": "Scammer", "ai": "Köder", "me": "Köder"}


def build_analysis_messages(history: Iterable[Mapping]) -> list[dict[str, str]]:
    lines = [
        f"{SENDER_LABELS[m['sender']]}: {m['text']}"
        for m in history
        if m["sender"] in SENDER_LABELS
    ]
    stages = ", ".join(f"{i} = {name}" for i, name in enumerate(STAGES, 1))
    prompt = ANALYSIS_PROMPT.format(stages=stages, transcript="\n".join(lines))
    return [{"role": "user", "content": prompt}]


def _clamp_int(value, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def parse_analysis(raw: str) -> dict:
    """Liest das JSON aus der Modellantwort, robust gegen Markdown-Zäune und Geschwätz."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("Keine JSON-Antwort erhalten")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("JSON-Antwort ist kein Objekt")

    best_of = []
    for item in data.get("best_of") or []:
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        sender = "them" if str(item.get("sender", "")).lower().startswith("scam") else "ai"
        best_of.append({"sender": sender, "text": str(item["text"]).strip()[:400]})

    keywords = []
    for kw in data.get("keywords") or []:
        kw = str(kw).strip().strip("#")
        if kw and kw.lower() not in {k.lower() for k in keywords}:
            keywords.append(kw[:40])

    return {
        "scam_type": str(data.get("scam_type") or "unbekannt")[:80],
        "stage": _clamp_int(data.get("stage"), 1, len(STAGES), 1),
        "summary": str(data.get("summary") or "")[:1000],
        "scammer_goal": str(data.get("scammer_goal") or "")[:300],
        "bait_tactic": str(data.get("bait_tactic") or "")[:300],
        "frustration": _clamp_int(data.get("frustration"), 0, 10, 0),
        "keywords": keywords[:8],
        "best_of": best_of[:5],
    }


# --- Lokale Auswertung (ohne KI) ---------------------------------------------

# Typisches Scam-Vokabular: Anzeigename -> Regex
SCAM_LEXICON: dict[str, str] = {
    "Bitcoin / Krypto": r"\b(bitcoin|btc|krypto\w*|crypto\w*|usdt|ethereum|eth|binance|wallet)\b",
    "Investment": r"\b(invest\w*|anlage\w*|portfolio|trading|trader|broker|forex)\b",
    "Rendite / Gewinn": r"\b(rendite|gewinn\w*|profit\w*|return\w*|verdien\w*|earn\w*)\b",
    "Überweisung": r"\b(überweis\w*|transfer\w*|einzahl\w*|deposit\w*|zahlung\w*|payment)\b",
    "Bank / Konto": r"\b(bank\w*|konto\w*|account|iban|sparkasse|volksbank)\b",
    "Gebühr / Steuer": r"\b(gebühr\w*|fee|fees|steuer\w*|tax|zoll\w*|freischalt\w*)\b",
    "Gutschein / Geschenkkarte": r"\b(gutschein\w*|gift ?card\w*|google ?play|itunes|steam|paysafe\w*|amazon[- ]?karte)\b",
    "PayPal / Western Union": r"\b(paypal|western ?union|moneygram|wise|revolut)\b",
    "Code / Verifizierung": r"\b(code|verifi\w*|bestätig\w*|tan|passwort|password|pin)\b",
    "Dringend / Sofort": r"\b(dringend\w*|sofort|schnell|urgent\w*|asap|heute noch|letzte chance)\b",
    "Paket / Lieferung": r"\b(paket\w*|lieferung\w*|sendung\w*|dhl|hermes|dpd|zustell\w*)\b",
    "Liebe / Schatz": r"\b(liebe\w*|schatz\w*|darling|honey|baby|herz\w*|vermiss\w*)\b",
    "Job / Nebenverdienst": r"\b(job\w*|nebenjob|home ?office|aufgabe\w*|task\w*|provision\w*)\b",
    "Vertrauen / Seriös": r"\b(vertrau\w*|seriös\w*|garantiert|garantie|trust\w*|legit\w*|sicher\w*)\b",
}
_LEXICON_RE = {name: re.compile(rx, re.IGNORECASE) for name, rx in SCAM_LEXICON.items()}


def lexicon_counts(texts: Iterable[str]) -> list[tuple[str, int]]:
    """Wie viele Scammer-Nachrichten enthalten welches Schlagwort-Thema?"""
    counts: Counter[str] = Counter()
    for text in texts:
        for name, rx in _LEXICON_RE.items():
            if rx.search(text):
                counts[name] += 1
    return counts.most_common()


def keyword_cloud(analyses: Iterable[dict], limit: int = 30) -> list[tuple[str, int]]:
    """Schlagworte aus allen Analysen, gezählt nach Anzahl der Chats."""
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for analysis in analyses:
        for kw in {k.lower(): k for k in analysis.get("keywords", [])}.items():
            counts[kw[0]] += 1
            display.setdefault(kw[0], kw[1])
    return [(display[k], n) for k, n in counts.most_common(limit)]


def response_times(messages: Iterable[Mapping]) -> dict[str, float | None]:
    """Durchschnittliche Antwortzeit des Scammers auf den Köder und umgekehrt (Sekunden)."""
    scammer, bait = [], []
    prev = None
    for m in messages:
        if m["sender"] not in ("them", "ai", "me"):
            continue
        if prev is not None and (prev["sender"] == "them") != (m["sender"] == "them"):
            delta = m["ts"] - prev["ts"]
            if delta >= 0:
                (scammer if m["sender"] == "them" else bait).append(delta)
        prev = m
    avg = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    return {"scammer": avg(scammer), "bait": avg(bait)}
