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
    for url in ["/", "/chats/1", "/chats/1/messages", "/chats/1/verlauf", "/chats/1/verlauf/body",
                "/chats/1/lage", "/personas", "/personas?edit=1", "/settings", "/verbrauch",
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



def test_report_page_with_cloud_and_summary_history(client):
    d = engine(client).db
    for i in range(3):
        d.add_message(CHAT, "them", f"Bitcoin Rendite garantiert Nummer {i}", time.time() + i)
        d.add_message(CHAT, "ai", "bitcoin? ist das mit münzen", time.time() + i + 0.5)
    page = client.get("/chats/1/verlauf").text
    assert "Wortwolke" in page and "bitcoin" in page and "w-them" in page
    assert "Gesamter Verlauf (7 Nachrichten)" in page and "Noch keine. Klick" in page

    client.post("/chats/1/enabled", data={"value": "1"})
    client.post("/chats/1/mode", data={"mode": "manual"})
    r = client.post("/chats/1/summary", data={"next": "/chats/1/verlauf"}, follow_redirects=False)
    assert r.headers["location"] == "/chats/1/verlauf"
    wait_idle(client)
    page = client.get("/chats/1/verlauf").text
    assert "Frühere Zusammenfassungen" in page and "nach 7 Nachrichten" in page
    assert "Der Scammer bietet Bitcoin-Rendite an" in page
    assert "Verlauf &amp; Auswertung" in client.get("/chats/1").text  # Link auf der Chat-Seite
    assert "Wortwolke aller KI-Chats" in client.get("/").text


def test_referral_buttons(client):
    t = engine(client)
    d = t.db
    d.update_chat(CHAT, enabled=True, mode="manual")  # im Modus "nur ich": nur vorschlagen
    t.client.entities["crypto_boss"] = __import__("tests.fakes", fromlist=["tg_user"]).tg_user(555, "Boss", "crypto_boss")
    d.add_message(CHAT, "them", "schreib @crypto_boss")
    from tarpit.referrals import extract_candidates

    async def detect():  # läuft in der Event-Loop der App, wie im echten Betrieb
        return t.on_referral_candidates(CHAT, extract_candidates("schreib @crypto_boss"))

    ids = client.portal.call(detect)
    wait_idle(client)
    assert d.referral(ids[0])["status"] == "proposed"
    page = client.get("/chats/1").text
    assert "@crypto_boss" in page and "Anschreiben" in page
    assert "Weiterleitungen" in client.get("/").text
    client.post(f"/referrals/{ids[0]}/accept", data={"next": "/chats/1"})
    assert d.referral(ids[0])["status"] == "scheduled"
    assert "vermittelt" in client.get("/chats/555").text
    assert client.post("/referrals/9999/ignore").status_code == 404


def test_referral_settings_and_test_notification(client):
    client.post("/settings", data={"referral_mode": "suggest", "referral_daily_limit": "5",
                                   "referral_pause_source": "1", "notify_enabled": "1"})
    s = engine(client).db.settings()
    assert s["referral_mode"] == "suggest" and s["referral_daily_limit"] == 5
    assert s["referral_pause_source"] is True and s["max_reply_tokens"] == 300
    client.post("/logs/test-notify")
    assert any(chat == "me" and "Test" in text for chat, text in engine(client).client.sent)



def jpeg_bytes(color="orange"):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "PNG")
    return buf.getvalue()


def test_persona_image_upload_serve_and_delete(client):
    t = engine(client)
    r = client.post("/personas/1/images", files={"file": ("mausi.png", jpeg_bytes(), "image/png")},
                    data={"description": "Katze Mausi"}, follow_redirects=False)
    assert r.status_code == 303
    image = t.db.persona_images(1)[0]
    assert image["description"] == "Katze Mausi" and image["filename"].endswith(".jpg")
    served = client.get(f"/media/images/{image['id']}")
    assert served.status_code == 200 and served.headers["content-type"] == "image/jpeg"
    page = client.get("/personas").text
    assert "Katze Mausi" in page and f"/media/images/{image['id']}" in page

    # Bild erscheint im Chat als einfügbar
    t.db.update_chat(CHAT, enabled=True)
    assert f'data-insert-image="{image["id"]}"' in client.get("/chats/1").text

    # kaputte Datei wird abgelehnt
    r = client.post("/personas/1/images", files={"file": ("x.png", b"kaputt", "image/png")})
    assert "Keine gültige Bilddatei" in r.text
    assert client.get("/media/images/999").status_code == 404

    client.post(f"/personas/images/{image['id']}", data={"description": "Mausi schläft"})
    assert t.db.persona_image(image["id"])["description"] == "Mausi schläft"
    client.post(f"/personas/images/{image['id']}/delete")
    assert t.db.persona_images(1) == []
    assert not (t.config.media_dir / image["filename"]).exists()


def test_persona_image_auto_description(client):
    t = engine(client)
    t.db.set_setting("vision_model", "vision/model")
    t.llm = FakeLLM(["Eine orange Fläche"])
    client.post("/personas/1/images", files={"file": ("a.png", jpeg_bytes(), "image/png")})
    assert t.db.persona_images(1)[0]["description"] == "Eine orange Fläche"


def test_usage_page_and_limit_settings(client):
    t = engine(client)
    client.post("/settings", data={"daily_token_limit": "50000", "price_input_per_m": "0,15",
                                   "price_output_per_m": "0.6", "vision_model": "v/m", "stt_model": "s/m",
                                   "stt_backend": "whisper"})
    s = t.db.settings()
    assert s["daily_token_limit"] == 50000 and s["price_input_per_m"] == 0.15 and s["price_output_per_m"] == 0.6
    assert s["vision_model"] == "v/m" and s["stt_backend"] == "whisper"
    t.db.add_usage("openai/gpt-4o-mini", "reply", 1200, 800, 50, 0.0002)
    page = client.get("/verbrauch").text
    assert "Hochrechnung Monat" in page and "openai/gpt-4o-mini" in page and "1.250" in page
    assert "50.000" in page  # Limit
    assert "1.250 Token heute" in client.get("/").text



def test_providers_page_add_edit_test_and_settings(client, monkeypatch):
    from tarpit.llm import LLMClient

    async def fake_list_models(self):
        return ["gemma3:4b", "llama3.2:3b"]

    monkeypatch.setattr(LLMClient, "list_models", fake_list_models)
    t = engine(client)
    assert "Standard (.env)" in client.get("/anbieter").text

    client.post("/anbieter", data={"name": "Mein Ollama", "kind": "ollama",
                                   "base_url": "http://host.docker.internal:11434/v1/", "api_key": ""})
    client.post("/anbieter", data={"name": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1",
                                   "api_key": "sk-supergeheim1234"})
    ollama, openai = t.db.providers()
    assert ollama["base_url"] == "http://host.docker.internal:11434/v1"  # Schrägstrich am Ende entfernt
    page = client.get("/anbieter").text
    assert "Mein Ollama" in page and "••••1234" in page and "sk-supergeheim" not in page  # Schlüssel maskiert

    client.post("/anbieter", data={"provider_id": str(openai["id"]), "name": "OpenAI", "kind": "openai",
                                   "base_url": "https://api.openai.com/v1", "api_key": ""})
    assert t.db.provider(openai["id"])["api_key"] == "sk-supergeheim1234"  # leer = behalten
    assert "http:// oder https://" in client.post("/anbieter", data={
        "name": "x", "kind": "custom", "base_url": "ftp://x"}).text

    client.post(f"/anbieter/{ollama['id']}/test")
    assert "2 Modelle gefunden" in client.get("/anbieter").text

    client.post("/settings", data={"model_provider": str(ollama["id"]), "model": "gemma3:4b",
                                   "vision_provider": "999"})  # unbekannte ID wird ignoriert
    s = t.db.settings()
    assert s["model_provider"] == str(ollama["id"]) and s["model"] == "gemma3:4b" and s["vision_provider"] == ""
    settings_page = client.get("/settings").text
    assert f'<datalist id="models-{ollama["id"]}">' in settings_page and 'value="llama3.2:3b"' in settings_page
    assert t.route("reply")[0].base_url == "http://host.docker.internal:11434/v1"

    client.post(f"/anbieter/{ollama['id']}/delete")
    assert t.db.settings()["model_provider"] == ""


def test_approval_card_on_chat_and_overview(client):
    t = engine(client)
    t.db.update_chat(CHAT, enabled=True, mode="review", draft_text="moment ich such meine brille", draft_basis=10**9)
    page = client.get("/chats/1").text
    card = page[page.index('id="approval"'):page.index('id="approval"') + 400]
    assert "hidden" not in card.split(">")[0]
    assert "✅ Freigeben &amp; senden" in page and "❌ Ablehnen" in page and "🔄 Neu generieren" in page
    index = client.get("/").text
    assert "Wartet auf deine Freigabe (1)" in index and "moment ich such meine brille" in index
    r = client.post("/chats/1/draft/discard", data={"next": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/" and t.db.chat(CHAT)["draft_text"] is None
    assert "Wartet auf deine Freigabe" not in client.get("/").text
