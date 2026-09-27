"""SQLite-Speicher für Chats, Nachrichten, Personas und Einstellungen.

Alle Zugriffe passieren aus dem asyncio-Event-Loop-Thread; die Abfragen sind
winzig, deshalb reicht das synchrone sqlite3-Modul.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any

from .prompts import DEFAULT_PERSONAS

SCHEMA = """
CREATE TABLE IF NOT EXISTS personas (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id          INTEGER PRIMARY KEY,
    title            TEXT NOT NULL DEFAULT '',
    username         TEXT,
    enabled          INTEGER NOT NULL DEFAULT 0,
    paused           INTEGER NOT NULL DEFAULT 0,
    persona_id       INTEGER REFERENCES personas(id) ON DELETE SET NULL,
    ai_day           TEXT,
    ai_today         INTEGER NOT NULL DEFAULT 0,
    last_message_at  REAL
);

-- sender: them = Gegenüber, ai = KI, me = du selbst, note = interner Vermerk
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    tg_msg_id  INTEGER,
    sender     TEXT NOT NULL CHECK (sender IN ('them', 'ai', 'me', 'note')),
    text       TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages (chat_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_tg
    ON messages (chat_id, tg_msg_id) WHERE tg_msg_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

DEFAULT_SETTINGS: dict[str, str] = {
    "global_enabled": "1",
    "model": "openai/gpt-4o-mini",
    "temperature": "0.9",
    "min_delay": "45",        # Sekunden
    "max_delay": "10800",     # Sekunden (3 h)
    "daily_limit": "40",      # KI-Nachrichten pro Chat und Tag
    "history_limit": "40",    # Nachrichten Kontext für die KI
    "quiet_start": "23",      # Stunde, ab der "geschlafen" wird
    "quiet_end": "7",         # Stunde, ab der wieder geantwortet wird
}

INT_SETTINGS = {"min_delay", "max_delay", "daily_limit", "history_limit", "quiet_start", "quiet_end"}


class Database:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._seed()

    def _seed(self) -> None:
        with self.conn:
            for key, value in DEFAULT_SETTINGS.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
                )
            if self.conn.execute("SELECT COUNT(*) FROM personas").fetchone()[0] == 0:
                for name, prompt in DEFAULT_PERSONAS:
                    self.conn.execute(
                        "INSERT INTO personas (name, prompt, created_at) VALUES (?, ?, ?)",
                        (name, prompt, time.time()),
                    )

    def close(self) -> None:
        self.conn.close()

    # --- Einstellungen -----------------------------------------------------

    def settings(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        result: dict[str, Any] = dict(DEFAULT_SETTINGS)
        result.update({r["key"]: r["value"] for r in rows})
        for key in INT_SETTINGS:
            result[key] = int(result[key])
        result["temperature"] = float(result["temperature"])
        result["global_enabled"] = result["global_enabled"] == "1"
        return result

    def set_setting(self, key: str, value: Any) -> None:
        if isinstance(value, bool):
            value = "1" if value else "0"
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    # --- Personas ----------------------------------------------------------

    def personas(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM personas ORDER BY id").fetchall()

    def persona(self, persona_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone()

    def persona_for_chat(self, chat: sqlite3.Row) -> sqlite3.Row | None:
        if chat["persona_id"] is not None:
            persona = self.persona(chat["persona_id"])
            if persona is not None:
                return persona
        return self.conn.execute("SELECT * FROM personas ORDER BY id LIMIT 1").fetchone()

    def save_persona(self, persona_id: int | None, name: str, prompt: str) -> int:
        with self.conn:
            if persona_id is None:
                cur = self.conn.execute(
                    "INSERT INTO personas (name, prompt, created_at) VALUES (?, ?, ?)",
                    (name, prompt, time.time()),
                )
                return int(cur.lastrowid)
            self.conn.execute(
                "UPDATE personas SET name = ?, prompt = ? WHERE id = ?", (name, prompt, persona_id)
            )
            return persona_id

    def delete_persona(self, persona_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM personas WHERE id = ?", (persona_id,))

    # --- Chats -------------------------------------------------------------

    def upsert_chat(
        self, chat_id: int, title: str, username: str | None, last_message_at: float | None = None
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO chats (chat_id, title, username, last_message_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    username = excluded.username,
                    last_message_at = MAX(COALESCE(chats.last_message_at, 0),
                                          COALESCE(excluded.last_message_at, 0))
                """,
                (chat_id, title, username, last_message_at),
            )

    def chat(self, chat_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()

    def chats_with_stats(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT c.*,
                   p.name AS persona_name,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.chat_id = c.chat_id AND m.sender = 'ai') AS n_ai,
                   (SELECT MIN(ts) FROM messages m
                     WHERE m.chat_id = c.chat_id AND m.sender = 'ai') AS first_ai,
                   (SELECT MAX(ts) FROM messages m
                     WHERE m.chat_id = c.chat_id AND m.sender = 'them') AS last_them,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.chat_id = c.chat_id AND m.sender = 'them'
                       AND m.ts > (SELECT MIN(ts) FROM messages m2
                                    WHERE m2.chat_id = c.chat_id AND m2.sender = 'ai'))
                       AS n_them_baited
            FROM chats c
            LEFT JOIN personas p ON p.id = c.persona_id
            ORDER BY c.enabled DESC, COALESCE(c.last_message_at, 0) DESC
            """
        ).fetchall()

    def enabled_chats(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM chats WHERE enabled = 1").fetchall()

    def set_chat_flag(self, chat_id: int, field: str, value: bool) -> None:
        if field not in ("enabled", "paused"):
            raise ValueError(field)
        with self.conn:
            self.conn.execute(
                f"UPDATE chats SET {field} = ? WHERE chat_id = ?", (int(value), chat_id)
            )

    def set_chat_persona(self, chat_id: int, persona_id: int | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE chats SET persona_id = ? WHERE chat_id = ?", (persona_id, chat_id)
            )

    def ai_sent_today(self, chat: sqlite3.Row) -> int:
        return chat["ai_today"] if chat["ai_day"] == date.today().isoformat() else 0

    def bump_ai_count(self, chat_id: int) -> None:
        today = date.today().isoformat()
        with self.conn:
            self.conn.execute(
                """
                UPDATE chats SET
                    ai_today = CASE WHEN ai_day = ? THEN ai_today + 1 ELSE 1 END,
                    ai_day = ?
                WHERE chat_id = ?
                """,
                (today, today, chat_id),
            )

    # --- Nachrichten -------------------------------------------------------

    def add_message(
        self, chat_id: int, sender: str, text: str, ts: float | None = None,
        tg_msg_id: int | None = None,
    ) -> bool:
        """Speichert eine Nachricht. Gibt False zurück, wenn sie schon existiert."""
        ts = time.time() if ts is None else ts
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages (chat_id, tg_msg_id, sender, text, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, tg_msg_id, sender, text, ts),
            )
            if cur.rowcount and sender != "note":
                self.conn.execute(
                    "UPDATE chats SET last_message_at = MAX(COALESCE(last_message_at, 0), ?) "
                    "WHERE chat_id = ?",
                    (ts, chat_id),
                )
        return bool(cur.rowcount)

    def has_tg_message(self, chat_id: int, tg_msg_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM messages WHERE chat_id = ? AND tg_msg_id = ?", (chat_id, tg_msg_id)
        ).fetchone()
        return row is not None

    def messages(self, chat_id: int, limit: int = 200, include_notes: bool = True) -> list[sqlite3.Row]:
        where = "" if include_notes else "AND sender != 'note'"
        rows = self.conn.execute(
            f"SELECT * FROM messages WHERE chat_id = ? {where} ORDER BY ts DESC, id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return list(reversed(rows))

    def message_count(self, chat_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND sender != 'note'", (chat_id,)
        ).fetchone()[0]

    def last_sender(self, chat_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT sender FROM messages WHERE chat_id = ? AND sender != 'note' "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return row["sender"] if row else None
