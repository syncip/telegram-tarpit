"""SQLite-Speicher für Chats, Nachrichten, Personas, Einstellungen und Log.

Alle Zugriffe passieren aus dem asyncio-Event-Loop-Thread; die Abfragen sind
winzig, deshalb reicht das synchrone sqlite3-Modul. Nur das Log kann auch aus
anderen Threads (Logging-Handler) beschrieben werden und ist per Lock geschützt.
"""

from __future__ import annotations

import json
import sqlite3
import threading
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

-- Ereignis-Log für die Statusseite
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    level    TEXT NOT NULL,
    source   TEXT NOT NULL,
    chat_id  INTEGER,
    message  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);

-- Jede Analyse wird aufbewahrt: "Wie war der Stand zu diesem Zeitpunkt?"
CREATE TABLE IF NOT EXISTS analysis_history (
    id        INTEGER PRIMARY KEY,
    chat_id   INTEGER NOT NULL,
    ts        REAL NOT NULL,
    basis     INTEGER,
    messages  INTEGER NOT NULL DEFAULT 0,
    data      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_history_chat ON analysis_history (chat_id, ts);

-- KI-Anbieter (OpenAI-kompatible APIs: OpenRouter, OpenAI, Google, Ollama, ...)
CREATE TABLE IF NOT EXISTS providers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    api_key     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

-- Bilder, die eine Persona verschicken kann
CREATE TABLE IF NOT EXISTS persona_images (
    id          INTEGER PRIMARY KEY,
    persona_id  INTEGER NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

-- Jeder KI-Aufruf, für Tageslimit und Verbrauchsstatistik
CREATE TABLE IF NOT EXISTS usage (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    day         TEXT NOT NULL,
    model       TEXT NOT NULL,
    purpose     TEXT NOT NULL,   -- reply | analysis | vision | stt | other
    chat_id     INTEGER,
    prompt      INTEGER NOT NULL DEFAULT 0,
    cached      INTEGER NOT NULL DEFAULT 0,
    completion  INTEGER NOT NULL DEFAULT 0,
    cost        REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage (day);

-- Weiterleitungen: Scammer will, dass du jemand anderen anschreibst
CREATE TABLE IF NOT EXISTS referrals (
    id              INTEGER PRIMARY KEY,
    source_chat_id  INTEGER NOT NULL,
    kind            TEXT NOT NULL,      -- username | phone | user_id
    target          TEXT NOT NULL,
    target_chat_id  INTEGER,
    status          TEXT NOT NULL,      -- proposed | pending | contacted | skipped | failed
    reason          TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE (source_chat_id, kind, target)
);
"""

# Spalten, die nach der ersten Version dazugekommen sind (werden per ALTER TABLE ergänzt)
MIGRATIONS: dict[str, dict[str, str]] = {
    "chats": {
        # auto = KI antwortet selbst, review = KI schlägt vor, du gibst frei, manual = nur du
        "mode": "TEXT NOT NULL DEFAULT 'auto'",
        "due_at": "REAL",
        "draft_text": "TEXT",
        "draft_edited": "INTEGER NOT NULL DEFAULT 0",
        "draft_basis": "INTEGER",
        "draft_at": "REAL",
        "instruction": "TEXT",
        "analysis": "TEXT",
        "analysis_at": "REAL",
        "analysis_basis": "INTEGER",
        "referred_from": "INTEGER",
        "background": "TEXT",
    },
    "messages": {
        "edited": "INTEGER NOT NULL DEFAULT 0",
        "image_id": "INTEGER",
    },
}

MODES = ("auto", "review", "manual")

DEFAULT_SETTINGS: dict[str, str] = {
    "global_enabled": "1",
    "model": "openai/gpt-4o-mini",
    "analysis_model": "",     # leer = gleiches Modell wie für die Antworten
    "temperature": "0.9",
    "min_delay": "45",        # Sekunden
    "max_delay": "10800",     # Sekunden (3 h)
    "daily_limit": "40",      # KI-Nachrichten pro Chat und Tag
    "history_limit": "30",    # Nachrichten Kontext für die KI
    "quiet_start": "23",      # Stunde, ab der "geschlafen" wird
    "quiet_end": "7",         # Stunde, ab der wieder geantwortet wird
    "max_reply_tokens": "300",  # Obergrenze für die Länge einer KI-Antwort
    "auto_analyze": "1",
    "referral_mode": "auto",        # auto | suggest | off
    "referral_pause_source": "1",   # alten Chat bei Weiterleitung auf Freigabe stellen
    "referral_daily_limit": "3",
    "referral_min_delay": "120",
    "referral_max_delay": "900",
    "notify_enabled": "1",
    # Anbieter je Rolle: ID aus der Tabelle providers, leer = Standard aus der .env
    "model_provider": "",
    "analysis_provider": "",
    "vision_provider": "",
    "stt_provider": "",
    "vision_model": "",           # leer = aus; z. B. ein bildfähiges Modell
    "stt_model": "",              # leer = aus; Spracherkennung
    "stt_backend": "chat",        # chat (Audio über die Chat-API) | whisper (/audio/transcriptions)
    "daily_token_limit": "0",     # 0 = unbegrenzt; gilt für alle KI-Aufrufe zusammen
    "price_input_per_m": "0",     # $ pro 1 Mio. Eingabe-Token, nur falls die API keine Kosten meldet
    "price_output_per_m": "0",
    "analyze_every": "10",     # neue Nachrichten bis zur nächsten automatischen Analyse
}

OLD_DEFAULTS_V1 = {"history_limit": "40", "analyze_every": "6"}

INT_SETTINGS = {
    "min_delay", "max_delay", "daily_limit", "history_limit", "quiet_start", "quiet_end",
    "analyze_every", "max_reply_tokens", "referral_daily_limit", "referral_min_delay",
    "referral_max_delay", "daily_token_limit",
}
FLOAT_SETTINGS = {"temperature", "price_input_per_m", "price_output_per_m"}
STT_BACKENDS = ("chat", "whisper")
BOOL_SETTINGS = {"global_enabled", "auto_analyze", "referral_pause_source", "notify_enabled"}
REFERRAL_MODES = ("auto", "suggest", "off")

CHAT_FIELDS = {
    "enabled", "mode", "persona_id", "due_at", "draft_text", "draft_edited", "draft_basis",
    "draft_at", "instruction", "analysis", "analysis_at", "analysis_basis", "referred_from",
    "background",
}

MAX_EVENTS = 5000


class Database:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._migrate()
        self._seed()

    def _migrate(self) -> None:
        with self.conn:
            for table, columns in MIGRATIONS.items():
                existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, ddl in columns.items():
                    if name not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                        if (table, name) == ("chats", "mode"):
                            # früheres "pausiert" entspricht jetzt "nur ich"
                            self.conn.execute("UPDATE chats SET mode = 'manual' WHERE paused = 1")

    def _seed(self) -> None:
        with self.conn:
            for key, value in DEFAULT_SETTINGS.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
                )
            version = self.conn.execute(
                "SELECT value FROM settings WHERE key = 'settings_version'"
            ).fetchone()
            if version is None:
                # Version 2: sparsamere Standardwerte, nur wenn noch der alte Standard gesetzt ist
                for key, old_default in OLD_DEFAULTS_V1.items():
                    self.conn.execute(
                        "UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                        (DEFAULT_SETTINGS[key], key, old_default),
                    )
                self.conn.execute("INSERT INTO settings (key, value) VALUES ('settings_version', '2')")
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
        for key in BOOL_SETTINGS:
            result[key] = result[key] == "1"
        for key in FLOAT_SETTINGS:
            result[key] = float(result[key])
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

    def persona_images(self, persona_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM persona_images WHERE persona_id = ? ORDER BY id", (persona_id,)
        ).fetchall()

    def persona_image(self, image_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM persona_images WHERE id = ?", (image_id,)).fetchone()

    def add_persona_image(self, persona_id: int, filename: str, description: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO persona_images (persona_id, filename, description, created_at) VALUES (?, ?, ?, ?)",
                (persona_id, filename, description, time.time()),
            )
        return int(cur.lastrowid)

    def update_persona_image(self, image_id: int, description: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE persona_images SET description = ? WHERE id = ?", (description, image_id))

    def delete_persona_image(self, image_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM persona_images WHERE id = ?", (image_id,))

    # --- Anbieter ------------------------------------------------------------

    def providers(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM providers ORDER BY id").fetchall()

    def provider(self, provider_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()

    def save_provider(
        self, provider_id: int | None, name: str, kind: str, base_url: str, api_key: str | None
    ) -> int:
        """Legt einen Anbieter an oder ändert ihn. api_key=None behält den gespeicherten Schlüssel."""
        with self.conn:
            if provider_id is None:
                cur = self.conn.execute(
                    "INSERT INTO providers (name, kind, base_url, api_key, created_at) VALUES (?, ?, ?, ?, ?)",
                    (name, kind, base_url, api_key or "", time.time()),
                )
                return int(cur.lastrowid)
            if api_key is None:
                self.conn.execute(
                    "UPDATE providers SET name = ?, kind = ?, base_url = ? WHERE id = ?",
                    (name, kind, base_url, provider_id),
                )
            else:
                self.conn.execute(
                    "UPDATE providers SET name = ?, kind = ?, base_url = ?, api_key = ? WHERE id = ?",
                    (name, kind, base_url, api_key, provider_id),
                )
            return provider_id

    def delete_provider(self, provider_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            # Rollen, die diesen Anbieter nutzen, fallen auf den Standard zurück
            self.conn.execute(
                "UPDATE settings SET value = '' WHERE key LIKE '%_provider' AND value = ?", (str(provider_id),)
            )

    def sent_image_ids(self, chat_id: int) -> set[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT image_id FROM messages WHERE chat_id = ? AND image_id IS NOT NULL", (chat_id,)
        ).fetchall()
        return {r[0] for r in rows}

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
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.chat_id = c.chat_id AND m.sender = 'them') AS n_them,
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

    def update_chat(self, chat_id: int, **fields: Any) -> None:
        unknown = set(fields) - CHAT_FIELDS
        if unknown:
            raise ValueError(f"Unbekannte Felder: {unknown}")
        if "mode" in fields and fields["mode"] not in MODES:
            raise ValueError(f"Ungültiger Modus: {fields['mode']}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        with self.conn:
            self.conn.execute(
                f"UPDATE chats SET {assignments} WHERE chat_id = ?", (*values, chat_id)
            )

    def set_chat_flag(self, chat_id: int, field: str, value: bool) -> None:
        if field != "enabled":
            raise ValueError(field)
        self.update_chat(chat_id, enabled=value)

    def set_chat_persona(self, chat_id: int, persona_id: int | None) -> None:
        self.update_chat(chat_id, persona_id=persona_id)

    def clear_draft(self, chat_id: int) -> None:
        self.update_chat(
            chat_id, draft_text=None, draft_edited=0, draft_basis=None, draft_at=None
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

    def analysis(self, chat: sqlite3.Row) -> dict | None:
        if not chat["analysis"]:
            return None
        try:
            return json.loads(chat["analysis"])
        except ValueError:
            return None

    def analyses(self) -> list[tuple[sqlite3.Row, dict]]:
        rows = self.conn.execute(
            "SELECT * FROM chats WHERE analysis IS NOT NULL ORDER BY analysis_at DESC"
        ).fetchall()
        return [(row, a) for row in rows if (a := self.analysis(row)) is not None]

    def add_analysis_snapshot(self, chat_id: int, data: dict, basis: int, ts: float | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO analysis_history (chat_id, ts, basis, messages, data) VALUES (?, ?, ?, ?, ?)",
                (chat_id, ts or time.time(), basis, self.message_count(chat_id),
                 json.dumps(data, ensure_ascii=False)),
            )

    def analysis_history(self, chat_id: int, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM analysis_history WHERE chat_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        result = []
        for row in rows:
            try:
                result.append({**json.loads(row["data"]), "ts": row["ts"], "messages": row["messages"]})
            except ValueError:
                continue
        return result

    # --- Weiterleitungen ---------------------------------------------------

    def add_referral(self, source_chat_id: int, kind: str, target: str, status: str) -> int | None:
        """Legt eine Weiterleitung an. None, wenn es sie für diesen Chat schon gibt."""
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO referrals (source_chat_id, kind, target, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source_chat_id, kind, target, status, now, now),
            )
        return int(cur.lastrowid) if cur.rowcount else None

    def update_referral(self, referral_id: int, **fields: Any) -> None:
        unknown = set(fields) - {"status", "reason", "target_chat_id"}
        if unknown:
            raise ValueError(f"Unbekannte Felder: {unknown}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.conn:
            self.conn.execute(
                f"UPDATE referrals SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), time.time(), referral_id),
            )

    def referral(self, referral_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM referrals WHERE id = ?", (referral_id,)).fetchone()

    def referrals(
        self, source_chat_id: int | None = None, target_chat_id: int | None = None, limit: int = 100
    ) -> list[sqlite3.Row]:
        conditions, params = [], []
        if source_chat_id is not None:
            conditions.append("r.source_chat_id = ?")
            params.append(source_chat_id)
        if target_chat_id is not None:
            conditions.append("r.target_chat_id = ?")
            params.append(target_chat_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return self.conn.execute(
            f"""SELECT r.*, s.title AS source_title, t.title AS target_title
                FROM referrals r
                LEFT JOIN chats s ON s.chat_id = r.source_chat_id
                LEFT JOIN chats t ON t.chat_id = r.target_chat_id
                {where} ORDER BY r.created_at DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()

    def referrals_contacted_since(self, since: float, exclude_id: int | None = None) -> int:
        """Wie viele neue Kontakte wurden seit ``since`` angelegt (für das Tageslimit)?"""
        return self.conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE status IN ('scheduled', 'drafted', 'contacted') "
            "AND created_at >= ? AND id != ?",
            (since, exclude_id or -1),
        ).fetchone()[0]

    # --- Nachrichten -------------------------------------------------------

    def add_message(
        self, chat_id: int, sender: str, text: str, ts: float | None = None,
        tg_msg_id: int | None = None, edited: bool = False, image_id: int | None = None,
    ) -> bool:
        """Speichert eine Nachricht. Gibt False zurück, wenn sie schon existiert."""
        ts = time.time() if ts is None else ts
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages (chat_id, tg_msg_id, sender, text, ts, edited, image_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, tg_msg_id, sender, text, ts, int(edited), image_id),
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

    def messages_window(self, chat_id: int, limit: int, step: int = 10) -> list[sqlite3.Row]:
        """Die letzten ``limit`` bis ``limit + step - 1`` Nachrichten (ohne Vermerke).

        Der Beginn des Fensters springt nur in ``step``-Schritten weiter statt bei
        jeder neuen Nachricht. So bleibt der Anfang der KI-Anfrage öfter gleich,
        und Anbieter mit Prompt-Caching berechnen ihn günstiger.
        """
        total = self.message_count(chat_id)
        start = max(0, total - limit)
        start -= start % step
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND sender != 'note' "
            "ORDER BY ts, id LIMIT -1 OFFSET ?",
            (chat_id, start),
        ).fetchall()
        return list(rows)

    def message_count(self, chat_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND sender != 'note'", (chat_id,)
        ).fetchone()[0]

    def last_message(self, chat_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND sender != 'note' "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()

    def last_sender(self, chat_id: int) -> str | None:
        row = self.last_message(chat_id)
        return row["sender"] if row else None

    def last_message_id(self, chat_id: int, include_notes: bool = False) -> int:
        where = "" if include_notes else "AND sender != 'note'"
        row = self.conn.execute(
            f"SELECT MAX(id) FROM messages WHERE chat_id = ? {where}", (chat_id,)
        ).fetchone()
        return row[0] or 0

    def messages_since(self, chat_id: int, message_id: int | None) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND sender != 'note' AND id > ?",
            (chat_id, message_id or 0),
        ).fetchone()[0]

    def message_counts_by_day(self, since: float, chat_id: int | None = None) -> list[sqlite3.Row]:
        where = "AND chat_id = ?" if chat_id is not None else ""
        params: tuple = (since, chat_id) if chat_id is not None else (since,)
        return self.conn.execute(
            f"""
            SELECT date(ts, 'unixepoch', 'localtime') AS day, sender, COUNT(*) AS n
            FROM messages WHERE ts >= ? AND sender IN ('them', 'ai', 'me') {where}
            GROUP BY day, sender
            """,
            params,
        ).fetchall()

    def message_counts_by_hour(self, since: float, chat_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime') AS hour, sender, COUNT(*) AS n
            FROM messages WHERE ts >= ? AND chat_id = ? AND sender IN ('them', 'ai', 'me')
            GROUP BY hour, sender
            """,
            (since, chat_id),
        ).fetchall()

    def scammer_texts(self, limit: int = 20000) -> list[str]:
        rows = self.conn.execute(
            "SELECT m.text FROM messages m JOIN chats c ON c.chat_id = m.chat_id "
            "WHERE m.sender = 'them' AND (c.enabled = 1 OR c.analysis IS NOT NULL) "
            "ORDER BY m.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["text"] for r in rows]

    # --- Verbrauch ---------------------------------------------------------

    def add_usage(
        self, model: str, purpose: str, prompt: int, cached: int, completion: int, cost: float,
        chat_id: int | None = None, ts: float | None = None,
    ) -> None:
        ts = ts or time.time()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO usage (ts, day, model, purpose, chat_id, prompt, cached, completion, cost) "
                "VALUES (?, date(?, 'unixepoch', 'localtime'), ?, ?, ?, ?, ?, ?, ?)",
                (ts, ts, model, purpose, chat_id, prompt, cached, completion, cost),
            )

    def tokens_on(self, day: str) -> int:
        return self.conn.execute(
            "SELECT COALESCE(SUM(prompt + completion), 0) FROM usage WHERE day = ?", (day,)
        ).fetchone()[0]

    def usage_by_day(self, since_day: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT day, purpose, COUNT(*) AS calls, SUM(prompt) AS prompt, SUM(cached) AS cached,
                      SUM(completion) AS completion, SUM(cost) AS cost
               FROM usage WHERE day >= ? GROUP BY day, purpose ORDER BY day""",
            (since_day,),
        ).fetchall()

    def usage_by_model(self, since_day: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT model, COUNT(*) AS calls, SUM(prompt) AS prompt, SUM(cached) AS cached,
                      SUM(completion) AS completion, SUM(cost) AS cost
               FROM usage WHERE day >= ? GROUP BY model ORDER BY SUM(prompt + completion) DESC""",
            (since_day,),
        ).fetchall()

    # --- Ereignis-Log ------------------------------------------------------

    def add_event(
        self, level: str, source: str, message: str, chat_id: int | None = None,
        ts: float | None = None,
    ) -> None:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO events (ts, level, source, chat_id, message) VALUES (?, ?, ?, ?, ?)",
                (ts or time.time(), level, source, chat_id, message[:4000]),
            )
            if cur.lastrowid % 200 == 0:
                self.conn.execute(
                    "DELETE FROM events WHERE id <= ?", (cur.lastrowid - MAX_EVENTS,)
                )

    def events(
        self, limit: int = 300, level: str | None = None, source: str | None = None,
        chat_id: int | None = None,
    ) -> list[sqlite3.Row]:
        conditions, params = [], []
        if level == "problems":
            conditions.append("level IN ('WARNING', 'ERROR', 'CRITICAL')")
        elif level:
            conditions.append("level = ?")
            params.append(level)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if chat_id is not None:
            conditions.append("chat_id = ?")
            params.append(chat_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()

    def problem_count(self, since: float) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE ts >= ? AND level IN ('WARNING', 'ERROR', 'CRITICAL')",
                (since,),
            ).fetchone()[0]
