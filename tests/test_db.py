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
