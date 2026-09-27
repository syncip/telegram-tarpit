"""Antwort-Ablauf der Engine mit gefälschtem Telegram-Client und LLM."""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from tarpit import engine as engine_mod
from tarpit.config import Config
from tarpit.db import Database
from tarpit.engine import Tarpit


class FakeClient:
    def __init__(self):
        self.sent = []
        self.read = []

    async def send_read_acknowledge(self, chat_id):
        self.read.append(chat_id)

    @contextlib.asynccontextmanager
    async def action(self, chat_id, kind):
        yield

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=1000 + len(self.sent))


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def chat(self, model, messages, temperature):
        self.calls += 1
        return self.replies.pop(0)

    async def aclose(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(engine_mod.asyncio, "sleep", lambda s: real_sleep(0))
    config = Config(1, "h", "", "http://x", "a", "b", tmp_path, "127.0.0.1", 8080)
    db = Database(tmp_path / "t.db")
    t = Tarpit(config, db)
    t.client = FakeClient()
    db.upsert_chat(7, "Scam", None)
    db.set_chat_flag(7, "enabled", True)
    db.add_message(7, "them", "hallo, willst du reich werden?", tg_msg_id=1)
    return t, db


def run(t, replies):
    t.llm = FakeLLM(replies)

    async def go():
        t.maybe_schedule(7)
        while t.pending:
            await asyncio.gather(*(p.task for p in list(t.pending.values())))

    asyncio.run(go())


def test_sends_split_reply(setup):
    t, db = setup
    run(t, ["oh ja gerne\n---\nwie geht das denn"])
    assert [text for _, text in t.client.sent] == ["oh ja gerne", "wie geht das denn"]
    assert db.last_sender(7) == "ai"
    assert db.ai_sent_today(db.chat(7)) == 2


def test_blocked_reply_is_retried_then_dropped(setup):
    t, db = setup
    run(t, ["meine nummer ist 0171 2345678 90", "als KI darf ich das nicht"])
    assert t.client.sent == []
    notes = [m["text"] for m in db.messages(7) if m["sender"] == "note"]
    assert len(notes) == 2 and all(n.startswith("Blockiert") for n in notes)


def test_skip(setup):
    t, db = setup
    run(t, ["[SKIP]"])
    assert t.client.sent == []
    assert db.last_sender(7) == "them"


def test_daily_limit(setup):
    t, db = setup
    db.set_setting("daily_limit", 0)
    run(t, ["egal"])
    assert t.client.sent == [] and t.llm.calls == 0


def test_no_reply_when_disabled_or_last_message_is_ours(setup):
    t, db = setup
    db.set_chat_flag(7, "paused", True)
    run(t, ["x"])
    assert t.llm.calls == 0
    db.set_chat_flag(7, "paused", False)
    db.add_message(7, "me", "hab selbst geantwortet")
    run(t, ["x"])
    assert t.llm.calls == 0


def test_describe_code_type():
    from telethon.tl.types.auth import SentCodeTypeApp, SentCodeTypeEmailCode, SentCodeTypeSms

    assert "Telegram-App" in engine_mod.describe_code_type(SentCodeTypeApp(length=5))
    assert engine_mod.describe_code_type(SentCodeTypeSms(length=5)) == "per SMS"
    assert "m***@x.de" in engine_mod.describe_code_type(
        SentCodeTypeEmailCode(email_pattern="m***@x.de", length=6)
    )


class FakeQR:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.recreated = 0
        self.url = "tg://login?token=x"

    async def wait(self):
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome

    async def recreate(self):
        self.recreated += 1


def test_qr_loop_renews_token_and_logs_in(setup, monkeypatch):
    t, _ = setup
    logged_in = []

    async def fake_after_login():
        logged_in.append(True)

    monkeypatch.setattr(t, "_after_login", fake_after_login)
    t._qr = FakeQR([asyncio.TimeoutError(), asyncio.TimeoutError(), None])
    asyncio.run(t._qr_loop())
    assert t._qr.recreated == 2 and t.qr_state == "done" and logged_in


def test_qr_loop_needs_password(setup):
    from telethon.errors import SessionPasswordNeededError

    t, _ = setup
    t._qr = FakeQR([SessionPasswordNeededError(request=None)])
    asyncio.run(t._qr_loop())
    assert t.qr_state == "password"
