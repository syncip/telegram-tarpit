from tarpit.db import Database


def test_db_roundtrip(tmp_path):
    db = Database(tmp_path / "t.db")
    assert len(db.personas()) == 2
    s = db.settings()
    assert s["global_enabled"] is True and s["min_delay"] == 45

    db.upsert_chat(42, "Scammer", "scam", 100.0)
    chat = db.chat(42)
    assert chat["enabled"] == 0
    assert db.persona_for_chat(chat)["id"] == db.personas()[0]["id"]

    assert db.add_message(42, "them", "hi", 101.0, tg_msg_id=1)
    assert not db.add_message(42, "them", "hi", 101.0, tg_msg_id=1)  # Duplikat
    db.add_message(42, "note", "intern")
    assert db.last_sender(42) == "them"
    assert db.has_tg_message(42, 1)
    assert [m["sender"] for m in db.messages(42, include_notes=False)] == ["them"]

    db.set_chat_flag(42, "enabled", True)
    db.bump_ai_count(42)
    db.bump_ai_count(42)
    assert db.ai_sent_today(db.chat(42)) == 2

    pid = db.save_persona(None, "Test", "prompt")
    db.set_chat_persona(42, pid)
    db.delete_persona(pid)
    assert db.chat(42)["persona_id"] is None  # ON DELETE SET NULL

    stats = db.chats_with_stats()[0]
    assert stats["n_ai"] == 0 and stats["n_them_baited"] == 0


def test_migration_from_first_version(tmp_path):
    """Eine Datenbank der ersten Version wird ergänzt, ohne Daten zu verlieren."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '', username TEXT,
            enabled INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0, persona_id INTEGER,
            ai_day TEXT, ai_today INTEGER NOT NULL DEFAULT 0, last_message_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, tg_msg_id INTEGER,
            sender TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO chats (chat_id, title, enabled, paused) VALUES (1, 'Alt', 1, 1), (2, 'Aktiv', 1, 0);
        INSERT INTO messages (chat_id, sender, text, ts) VALUES (1, 'them', 'hallo', 1);
        INSERT INTO settings VALUES ('history_limit', '40'), ('analyze_every', '6'), ('daily_limit', '99');
    """)
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.chat(1)["mode"] == "manual"  # früher "pausiert"
    assert db.chat(2)["mode"] == "auto"
    assert db.messages(1)[0]["edited"] == 0
    s = db.settings()
    assert s["history_limit"] == 30 and s["analyze_every"] == 10  # alte Standardwerte ersetzt
    assert s["daily_limit"] == 99  # eigene Werte bleiben
    db.set_setting("history_limit", 40)
    db.close()
    assert Database(path).settings()["history_limit"] == 40  # Migration läuft nur einmal


def test_messages_window_is_aligned(tmp_path):
    db = Database(tmp_path / "t.db")
    db.upsert_chat(1, "x", None)
    for i in range(37):
        db.add_message(1, "them", f"m{i}", ts=float(i))
    window = db.messages_window(1, limit=20, step=10)
    assert window[0]["text"] == "m10" and len(window) == 27  # Start springt nur in 10er-Schritten
    db.add_message(1, "them", "m37", ts=37.0)
    assert db.messages_window(1, limit=20, step=10)[0]["text"] == "m10"  # Anfang bleibt gleich


def test_events(tmp_path):
    db = Database(tmp_path / "t.db")
    db.add_event("INFO", "engine", "alles gut", chat_id=5)
    db.add_event("ERROR", "llm", "kaputt")
    assert [e["message"] for e in db.events(level="problems")] == ["kaputt"]
    assert [e["message"] for e in db.events(chat_id=5)] == ["alles gut"]
    assert db.problem_count(0) == 1
