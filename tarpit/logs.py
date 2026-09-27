"""Schreibt Log-Einträge in die Datenbank, damit das Webinterface sie anzeigen kann."""

from __future__ import annotations

import logging

from .db import Database

SOURCES = {
    "telegram": "Telegram",
    "llm": "KI-Modell",
    "safety": "Filter",
    "engine": "Steuerung",
    "web": "Webinterface",
    "system": "System",
}


def _source_for(logger_name: str) -> str:
    if logger_name.startswith("telethon"):
        return "telegram"
    if logger_name.startswith(("httpx", "httpcore")):
        return "llm"
    if logger_name.startswith(("uvicorn", "tarpit.web")):
        return "web"
    if logger_name.startswith("tarpit"):
        return "engine"
    return "system"


class DatabaseLogHandler(logging.Handler):
    """Übernimmt alles ab WARNING sowie INFO-Meldungen der App selbst."""

    def __init__(self, db: Database):
        super().__init__(logging.INFO)
        self.db = db

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING and not record.name.startswith("tarpit"):
            return
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                message += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
            self.db.add_event(
                record.levelname,
                getattr(record, "source", None) or _source_for(record.name),
                message,
                getattr(record, "chat_id", None),
                record.created,
            )
        except Exception:
            self.handleError(record)
