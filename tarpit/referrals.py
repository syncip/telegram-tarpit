"""Erkennt, wenn ein Scammer dich an jemand anderen weiterleiten will.

Typisch: "Schreib meinem Manager @xyz", "Adde sie: t.me/xyz", eine Telefonnummer
oder ein geteilter Kontakt. Die Engine schreibt diese Person dann (mit Limits und
Schutzregeln) selbst an.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

USERNAME_RE = re.compile(r"(?<![\w.@])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
# t.me/name, telegram.me/name; Einladungslinks (+..., joinchat) und Kanäle (c/, s/) sind keine Personen
TME_RE = re.compile(
    r"\b(?:https?://)?(?:t|telegram)\.me/(?!joinchat\b|addstickers\b|share\b|c/|s/|\+)([A-Za-z][A-Za-z0-9_]{3,31})\b",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"(?<![\w+])(?:\+|00)\d[\d\s\-/().]{6,18}\d")

# Namen, die fast immer Bots, Kanäle oder Telegram selbst sind
IGNORED_USERNAMES = {"telegram", "botfather", "gif", "vid", "pic", "bing", "wiki", "imdb", "stickers"}


@dataclass(frozen=True)
class Candidate:
    kind: str   # username | phone | user_id
    value: str  # normalisiert: Benutzername ohne @ (klein), Telefonnummer +49..., User-ID

    @property
    def label(self) -> str:
        if self.kind == "username":
            return f"@{self.value}"
        if self.kind == "phone":
            return self.value
        return f"Kontakt #{self.value}"


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("00"):
        digits = digits[2:]
    if not 8 <= len(digits) <= 15:
        return None
    return "+" + digits


def extract_candidates(text: str) -> list[Candidate]:
    """Findet @Namen, t.me-Links und internationale Telefonnummern in einem Text."""
    found: list[Candidate] = []

    def add(candidate: Candidate) -> None:
        if candidate not in found:
            found.append(candidate)

    for match in USERNAME_RE.finditer(text):
        name = match.group(1).lower()
        if name not in IGNORED_USERNAMES and not name.endswith("bot"):
            add(Candidate("username", name))
    for match in TME_RE.finditer(text):
        name = match.group(1).lower()
        if name not in IGNORED_USERNAMES and not name.endswith("bot"):
            add(Candidate("username", name))
    for match in PHONE_RE.finditer(text):
        phone = normalize_phone(match.group(0))
        if phone:
            add(Candidate("phone", phone))
    return found
