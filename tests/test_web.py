"""Smoke-Test des Webinterfaces mit einem Fake statt echtem Telegram-Client."""

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tarpit import web
from tarpit.config import Config
from tarpit.engine import PendingReply


class FakeTarpit:
    def __init__(self, config, db):
        self.db = db
        self.pending = {}
        self.me = SimpleNamespace(first_name="Ich", username="ich")
        self.sent = []

    async def start(self):
        self.db.upsert_chat(1, "Herr Scam", "scam", time.time())
        self.db.add_message(1, "them", "Hallo, Investment?", time.time(), 10)

    async def stop(self):
        pass

    async def sync_dialogs(self):
        return 1

    async def import_history(self, chat_id, limit=50):
        return 0

    def maybe_schedule(self, chat_id):
        self.pending[chat_id] = PendingReply(task=None, due=time.time() + 60)

    def reply_now(self, chat_id):
        self.pending[chat_id] = PendingReply(task=None, due=time.time())

    def cancel(self, chat_id):
        self.pending.pop(chat_id, None)

    def cancel_all(self):
        self.pending.clear()

    def reschedule_all(self):
        pass

    async def send_manual(self, chat_id, text):
        self.sent.append((chat_id, text))
        self.db.add_message(chat_id, "me", text)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "Tarpit", FakeTarpit)
    config = Config(1, "h", "", "http://x", "admin", "geheim", tmp_path, "127.0.0.1", 8080)
    with TestClient(web.create_app(config)) as c:
        c.auth = ("admin", "geheim")
        yield c


def test_requires_auth(client):
    assert client.get("/", auth=("admin", "falsch")).status_code == 401


def test_pages_render(client):
    for url in ["/", "/chats/1", "/chats/1/messages", "/personas", "/personas?edit=1", "/settings"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Herr Scam" in client.get("/").text


def test_enable_chat_and_actions(client):
    r = client.post("/chats/1/toggle", data={"field": "enabled", "value": "1", "next": "/"})
    assert r.status_code == 200
    assert "Nächste KI-Antwort" in client.get("/chats/1/messages").text
    client.post("/chats/1/send", data={"text": "moin"})
    assert "moin" in client.get("/chats/1").text
    assert client.post("/chats/1/persona", data={"persona_id": "2"}).status_code == 200
    assert client.post("/chats/1/reply-now").status_code == 200
    assert client.post("/chats/1/toggle", data={"field": "bogus"}).status_code == 400


def test_personas_and_settings(client):
    client.post("/personas", data={"name": "Neu", "prompt": "Du bist neu."})
    assert "Du bist neu." in client.get("/personas").text
    r = client.post("/settings", data={
        "model": "x/y", "temperature": "0,5", "min_delay": "500", "max_delay": "100",
        "daily_limit": "10", "history_limit": "20", "quiet_start": "22", "quiet_end": "6",
    })
    assert r.status_code == 200
    page = client.get("/settings").text
    assert 'value="x/y"' in page and 'value="100"' in page and 'value="500"' in page


def test_cross_origin_post_blocked(client):
    r = client.post("/global", data={"enabled": "0"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
