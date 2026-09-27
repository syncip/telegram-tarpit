"""Webinterface gegen die echte Engine, mit nachgebautem Telegram und LLM."""

import time

import pytest
from fastapi.testclient import TestClient
from telethon.errors import PhoneCodeInvalidError, PhoneNumberInvalidError

from tarpit import engine as engine_mod
from tarpit import web
from tarpit.config import Config
from tarpit.engine import Tarpit

from .fakes import ME, FakeClient, FakeLLM

CHAT = 1


class WebTarpit(Tarpit):
    """Echte Engine; nur Telegram-Verbindung und Login sind nachgebaut."""

    def __init__(self, config, db):
        super().__init__(config, db)
        self.client = FakeClient()
        self.llm = FakeLLM()
        self._fake_qr_url = None

    async def start(self):
        self.me = ME
        self.db.upsert_chat(CHAT, "Herr Scam", "scam", time.time())
        self.db.add_message(CHAT, "them", "Hallo, Investment? Bitcoin garantiert!", time.time(), 10)

    async def stop(self):
        for tasks in (self.timers, self.drafting, self.analyzing):
            for task in tasks.values():
                task.cancel()

    async def sync_dialogs(self, limit=300):
        return 1

    async def import_history(self, chat_id, limit=50):
        return 0

    # --- Login-Nachbau ---
    async def refresh_login(self):
        pass

    async def request_login_code(self, phone):
        if phone != "+491234":
            raise PhoneNumberInvalidError(request=None)
        self._login_phone = phone
        self.login_code_hint = "per SMS"
        self.login_resend_hint = "per Anruf"

    async def submit_login_code(self, code):
        if code != "12345":
            raise PhoneCodeInvalidError(request=None)
        return False  # 2FA nötig

    async def submit_login_password(self, password):
        self.me = ME

    async def start_qr_login(self):
        self._fake_qr_url = "tg://login?token=abc"
        self.qr_state = "waiting"

    def cancel_qr_login(self):
        self._fake_qr_url = None
        self.qr_state = None

    @property
    def qr_url(self):
        return self._fake_qr_url

    async def logout(self):
        self.stop_all()
        self.me = None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "Tarpit", WebTarpit)
    monkeypatch.setattr(engine_mod, "DRAFT_DEBOUNCE", 0.05)
    monkeypatch.setattr(engine_mod, "typing_plan", lambda text, rng=None, instant=False: [("typing", 0.01)])
    config = Config(1, "h", "", "http://x", "admin", "geheim", tmp_path, "127.0.0.1", 8080)
    with TestClient(web.create_app(config)) as c:
        c.auth = ("admin", "geheim")
        yield c


def engine(client) -> WebTarpit:
    return client.app.state.tarpit


def wait_idle(client, timeout=3.0):
    """Wartet, bis Entwurf/Senden/Analyse im Hintergrund fertig sind."""
    t = engine(client)
    deadline = time.time() + timeout
    while time.time() < deadline:
        busy = [x for d in (t.drafting, t.analyzing) for x in d.values() if not x.done()]
        busy += [x for x in t.timers.values() if not x.done() and t.db.chat(CHAT)["due_at"] and t.db.chat(CHAT)["due_at"] <= time.time() + 1]
        if not busy and not t.sending:
            return
        client.get("/chats/1/status")  # gibt der Event-Loop Zeit
        time.sleep(0.05)


def test_requires_auth(client):
    assert client.get("/", auth=("admin", "falsch")).status_code == 401


def test_pages_render(client):
    for url in ["/", "/chats/1", "/chats/1/messages", "/personas", "/personas?edit=1", "/settings",
                "/logs", "/logs?level=problems", "/logs?source=llm&chat=1", "/logs/rows"]:
        r = client.get(url)
        assert r.status_code == 200, url
    page = client.get("/").text
    assert "Herr Scam" in page and "Nachrichten pro Tag" in page and "Best-of" in page
    assert "Bitcoin / Krypto" not in page  # Chat noch nicht unter KI-Kontrolle


def test_status_json(client):
    s = client.get("/chats/1/status").json()
    assert s["mode"] == "auto" and s["enabled"] is False and "now" in s
    assert "chats" in client.get("/api/status").json()


def test_review_flow_edit_and_send(client):
    client.post("/chats/1/enabled", data={"value": "1", "next": "/chats/1"})
    client.post("/chats/1/mode", data={"mode": "review"})
    client.get("/chats/1")  # Seite öffnen erzeugt den Entwurf sofort
    wait_idle(client)
    s = client.get("/chats/1/status").json()
    assert s["draft_text"] == "ach herrje, wie geht das denn?" and s["due_at"] is None
    assert "ach herrje" in client.get("/chats/1").text

    client.post("/chats/1/draft", data={"text": "moment, ich hol meine brille", "action": "save"})
    s = client.get("/chats/1/status").json()
    assert s["draft_edited"] is True

    client.post("/chats/1/draft", data={"text": "moment, ich hol meine brille", "action": "send"})
    wait_idle(client)
    assert engine(client).client.sent[-1] == (CHAT, "moment, ich hol meine brille")
    assert "✏️ von dir bearbeitet" in client.get("/chats/1/messages").text


def test_regenerate_with_instruction_and_discard(client):
    client.post("/chats/1/enabled", data={"value": "1"})
    client.post("/chats/1/mode", data={"mode": "review"})
    client.post("/chats/1/draft/regenerate", data={"instruction": "frag nach dem Hund"})
    wait_idle(client)
    s = client.get("/chats/1/status").json()
    assert s["instruction"] == "frag nach dem Hund" and s["draft_text"]
    client.post("/chats/1/draft/discard")
    assert client.get("/chats/1/status").json()["draft_text"] is None


def test_auto_mode_countdown_and_reply_now(client):
    client.post("/chats/1/enabled", data={"value": "1"})
    s = client.get("/chats/1/status").json()
    assert s["due_at"] and s["due_at"] > time.time()
    assert client.get("/api/status").json()["chats"]["1"]["due_at"] == s["due_at"]
    client.post("/chats/1/reply-now", data={"next": "/"})
    wait_idle(client)
    assert engine(client).client.sent, "Sofort-Antwort wurde nicht gesendet"
    assert client.get("/chats/1/status").json()["due_at"] is None


def test_manual_mode_and_own_message(client):
    client.post("/chats/1/enabled", data={"value": "1"})
    client.post("/chats/1/mode", data={"mode": "manual"})
    s = client.get("/chats/1/status").json()
    assert s["due_at"] is None and s["draft_text"] is None
    client.post("/chats/1/send", data={"text": "moin, hier ist der echte mensch"})
    assert "moin, hier ist der echte mensch" in client.get("/chats/1/messages").text
    assert client.post("/chats/1/mode", data={"mode": "quatsch"}).status_code == 400


def test_analysis_and_best_of(client):
    client.post("/chats/1/enabled", data={"value": "1"})
    client.post("/chats/1/mode", data={"mode": "manual"})
    client.post("/chats/1/analyze")
    wait_idle(client)
    page = client.get("/chats/1").text
    assert "Krypto-Investment" in page and "Köder / Angebot" in page and "7/10" in page
    index = client.get("/").text
    assert "Bitcoin" in index and "willst du reich werden" in index  # Wolke + Hall of Fame
    assert "Bitcoin / Krypto" in index  # Vokabular-Diagramm


def test_logs_and_model_test(client):
    client.post("/chats/1/enabled", data={"value": "1"})
    assert "KI übernimmt den Chat" in client.get("/logs").text
    client.post("/logs/test-model")
    page = client.get("/logs").text
    assert "✅" in page and "antwortet in" in page
    assert "KI übernimmt" not in client.get("/logs?level=problems").text


def test_personas_and_settings(client):
    client.post("/personas", data={"name": "Neu", "prompt": "Du bist neu."})
    assert "Du bist neu." in client.get("/personas").text
    r = client.post("/settings", data={
        "model": "x/y", "analysis_model": "billig/modell", "temperature": "0,5", "min_delay": "500",
        "max_delay": "100", "daily_limit": "10", "history_limit": "20", "quiet_start": "22",
        "quiet_end": "6", "analyze_every": "8", "max_reply_tokens": "200",
    })
    assert r.status_code == 200
    page = client.get("/settings").text
    assert 'value="x/y"' in page and 'value="100"' in page and 'value="500"' in page
    assert 'value="billig/modell"' in page
    assert 'name="auto_analyze" value="1" >' in page or 'name="auto_analyze" value="1" \n' in page or "checked" not in page.split('name="auto_analyze"')[1][:30]


def test_cross_origin_post_blocked(client):
    r = client.post("/global", data={"enabled": "0"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_global_stop(client):
    client.post("/chats/1/enabled", data={"value": "1"})
    assert client.get("/chats/1/status").json()["due_at"]
    client.post("/global", data={"enabled": "0"})
    assert client.get("/chats/1/status").json()["due_at"] is None
    client.post("/global", data={"enabled": "1"})
    assert client.get("/chats/1/status").json()["due_at"]


def test_telegram_login_flow(client):
    client.post("/logout")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Telefonnummer" in client.get("/login").text

    r = client.post("/login/phone", data={"phone": "+49 999"})
    assert r.status_code == 400 and "Ungültige Telefonnummer" in r.text
    r = client.post("/login/phone", data={"phone": "+49 1234"})
    assert "Login-Code" in r.text and "per SMS" in r.text and "erneut senden (per Anruf)" in r.text
    assert "per SMS" in client.post("/login/resend").text

    r = client.post("/login/code", data={"code": "000"})
    assert r.status_code == 400 and "falsch" in r.text
    assert "Zwei-Schritt-Passwort" in client.post("/login/code", data={"code": "12345"}).text

    r = client.post("/login/password", data={"password": "pw"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_qr_login_flow(client):
    client.post("/logout")
    assert client.get("/login/qr", follow_redirects=False).headers["location"] == "/login"
    r = client.post("/login/qr")
    assert r.status_code == 200 and "<svg" in r.text and "Desktop-Gerät verbinden" in r.text
    engine(client).qr_state = "password"
    assert "Zwei-Schritt-Passwort" in client.get("/login/qr").text
    client.post("/login/password", data={"password": "pw"})
    assert client.get("/login/qr", follow_redirects=False).headers["location"] == "/"


def test_qr_page_while_login_completes(client):
    """Regression: Neuladen während des Login-Abschlusses darf nicht abbrechen."""
    client.post("/logout")
    client.post("/login/qr")
    t = engine(client)
    t.qr_state, t._fake_qr_url = "done", None
    r = client.get("/login/qr", follow_redirects=False)
    assert r.status_code == 200 and "Anmeldung wird abgeschlossen" in r.text
    client.get("/login")  # darf den laufenden Login nicht abbrechen
    assert t.qr_state == "done"
