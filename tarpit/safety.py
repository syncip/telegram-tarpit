"""Filter für ausgehende KI-Nachrichten.

Der Prompt verbietet diese Dinge bereits, aber ein Sprachmodell lässt sich
überreden. Deshalb prüfen wir jede Antwort zusätzlich im Code, bevor sie
rausgeht. Lieber einmal zu viel blockieren als eine echte Nummer verschicken.
"""

from __future__ import annotations

import re

from .prompts import MESSAGE_SEPARATOR

MAX_REPLY_CHARS = 1200
MAX_PARTS = 3

_CHECKS: list[tuple[str, re.Pattern[str]]] = [
    ("E-Mail-Adresse", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    (
        "Link",
        re.compile(
            r"(https?://|www\.|t\.me/|\b[\w-]+\.(com|net|org|de|ru|io|me|ly|xyz|info|biz|link|app|to|cc)\b)",
            re.IGNORECASE,
        ),
    ),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}", re.IGNORECASE)),
    # Telefon-, Karten-, Konto- oder Ausweisnummern: 9+ Ziffern, ggf. mit Trennzeichen
    ("lange Ziffernfolge", re.compile(r"(?:\d[\s\-/.]?){9,}")),
    (
        "KI-Enttarnung",
        re.compile(
            r"\b(als (eine )?ki|künstliche intelligenz|sprachmodell|language model|as an ai|"
            r"i am an ai|i'm an ai|ich bin (eine )?ki|chatbot|openai|chatgpt|anthropic|llm|"
            r"system ?prompt)\b",
            re.IGNORECASE,
        ),
    ),
]


def check_reply(text: str) -> str | None:
    """Gibt den Grund zurück, warum die Antwort nicht gesendet werden darf, sonst None."""
    if not text.strip():
        return "leere Antwort"
    if len(text) > MAX_REPLY_CHARS:
        return "Antwort zu lang"
    for reason, pattern in _CHECKS:
        if pattern.search(text):
            return reason
    return None


_QUOTE_PAIRS = {('"', '"'), ("'", "'"), ("“", "”"), ("„", "“"), ("»", "«"), ("«", "»")}


def clean_reply(text: str) -> str:
    """Entfernt typische LLM-Artefakte (Anführungszeichen, Markdown, Rollen-Präfixe)."""
    text = text.strip()
    if len(text) >= 2 and (text[0], text[-1]) in _QUOTE_PAIRS:
        text = text[1:-1].strip()
    text = re.sub(r"^(assistant|antwort)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("**", "").replace("__", "")
    return text.strip()


def split_reply(text: str) -> list[str]:
    """Zerlegt eine Antwort an ``---``-Zeilen in einzelne Messenger-Nachrichten."""
    parts = re.split(rf"^\s*{re.escape(MESSAGE_SEPARATOR)}\s*$", text, flags=re.MULTILINE)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > MAX_PARTS:
        parts = parts[: MAX_PARTS - 1] + ["\n".join(parts[MAX_PARTS - 1 :])]
    return parts
