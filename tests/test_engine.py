"""Ablauf der Engine mit nachgebautem Telegram-Client und LLM."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from telethon.tl.types.auth import LoginTokenSuccess

from tarpit import engine as engine_mod
from tarpit.config import Config
from tarpit.db import Database
from tarpit.engine import Tarpit

from .fakes import ME, FakeClient, FakeLLM, tg_user
from tarpit.referrals import extract_candidates

CHAT = 7


@pytest.fixture
def setup(tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(engine_mod.asyncio, "sleep", lambda s: real_sleep(0))
    config = Config(1, "h", "", "http://x", "a", "b", tmp_path, "127.0.0.1", 8080)
    db = Database(tmp_path / "t.db")
    t = Tarpit(config, db)
    t.client = FakeClient()
    t.llm = FakeLLM()
    db.upsert_chat(CHAT, "Scam", None)
    db.update_chat(CHAT, enabled=True)
    return t, db


def run(coro_fn):
    """Führt eine Aktion aus und wartet, bis alle Hintergrund-Aufgaben fertig sind."""

    async def go():
        result = coro_fn()
        if asyncio.iscoroutine(result):
            await result
        for _ in range(50):
            await asyncio.sleep(0)
            tasks = [x for d in (t_ref.timers, t_ref.drafting, t_ref.analyzing) for x in d.values() if not x.done()]
            if not tasks:
                break
            await asyncio.wait(tasks)

    asyncio.run(go())


t_ref = None


@pytest.fixture(autouse=True)
def _remember(request):
    global t_ref
    if "setup" in request.fixturenames:
        t_ref = request.getfixturevalue("setup")[0]
    yield


def scammer_writes(t, db, text="hallo, willst du reich werden?", tg_id=None):
    db.add_message(CHAT, "them", text, tg_msg_id=tg_id)
    t.on_scammer_message(CHAT)


def sent_texts(t):
    return [text for _, text in t.client.sent]


def test_auto_mode_drafts_schedules_and_sends(setup):
    t, db = setup
    run(lambda: scammer_writes(t, db))
    assert sent_texts(t) == ["ach herrje, wie geht das denn?"]
    chat = db.chat(CHAT)
    assert chat["due_at"] is None and chat["draft_text"] is None
    assert db.last_sender(CHAT) == "ai"
    assert t.client.typing >= 1 and t.client.read == [CHAT]
    assert len(t.client.status_updates) == 2  # online beim Tippen, danach offline


def test_due_at_is_planned_and_kept(setup, monkeypatch):
    t, db = setup
    # Timer nicht feuern lassen: nur Planung prüfen
    monkeypatch.setattr(t, "_start_timer", lambda *a, **k: None)
    db.set_setting("min_delay", 3600)
    db.set_setting("max_delay", 7200)
    db.set_setting("quiet_start", 0)
    db.set_setting("quiet_end", 0)

    async def go():
        scammer_writes(t, db)
        due = db.chat(CHAT)["due_at"]
        assert due and due > time.time()
        t.timers[CHAT] = SimpleNamespace(cancel=lambda: None, done=lambda: False)
        db.add_message(CHAT, "them", "hallo??")
        t.on_scammer_message(CHAT)  # weitere Nachricht verschiebt den Termin nicht
        assert db.chat(CHAT)["due_at"] == due
        # Im Automatikmodus entsteht der Entwurf erst kurz vor dem Senden
        assert t._draft_delay(CHAT) > 60
        for task in t.drafting.values():
            task.cancel()
        t.timers.clear()

    asyncio.run(go())


def test_split_reply(setup):
    t, db = setup
    t.llm = FakeLLM(["oh ja gerne\n---\nwie geht das denn"])
    run(lambda: scammer_writes(t, db))
    assert sent_texts(t) == ["oh ja gerne", "wie geht das denn"]
    assert db.ai_sent_today(db.chat(CHAT)) == 2


def test_blocked_reply_is_retried_then_dropped(setup):
    t, db = setup
    t.llm = FakeLLM(["meine nummer ist 0171 2345678 90", "als KI darf ich das nicht"])
    run(lambda: scammer_writes(t, db))
    assert t.client.sent == []
    notes = [m["text"] for m in db.messages(CHAT) if m["sender"] == "note"]
    assert len(notes) == 2 and all(n.startswith("Blockiert") for n in notes)
    assert db.chat(CHAT)["due_at"] is None
    assert len(t.llm.reply_calls) == 2  # kein weiterer automatischer Versuch
    run(lambda: t.reply_now(CHAT))  # per Knopf: neuer Versuch
    assert sent_texts(t) == ["ach herrje, wie geht das denn?"]


def test_skip(setup):
    t, db = setup
    t.llm = FakeLLM(["[SKIP]"])
    run(lambda: scammer_writes(t, db))
    assert t.client.sent == []
    assert db.last_sender(CHAT) == "them"
    assert db.chat(CHAT)["draft_text"] is None


def test_daily_limit_blocks_automatic_but_not_instant(setup):
    t, db = setup
    db.set_setting("daily_limit", 0)
    run(lambda: scammer_writes(t, db))
    assert t.client.sent == []
    run(lambda: t.reply_now(CHAT))  # bewusst per Knopf: geht trotzdem
    assert len(t.client.sent) == 1


def test_review_mode_only_drafts(setup):
    t, db = setup
    t.set_mode(CHAT, "review")
    run(lambda: scammer_writes(t, db))
    chat = db.chat(CHAT)
    assert t.client.sent == [] and chat["due_at"] is None
    assert chat["draft_text"] == "ach herrje, wie geht das denn?"
    run(lambda: t.reply_now(CHAT))
    assert sent_texts(t) == ["ach herrje, wie geht das denn?"]
    assert len(t.llm.reply_calls) == 1  # Entwurf wurde wiederverwendet


def test_manual_mode_does_nothing(setup):
    t, db = setup
    t.set_mode(CHAT, "manual")
    run(lambda: scammer_writes(t, db))
    assert t.client.sent == [] and t.llm.reply_calls == []
    assert db.chat(CHAT)["draft_text"] is None


def test_edited_draft_is_sent_as_is(setup):
    t, db = setup
    t.set_mode(CHAT, "review")
    run(lambda: scammer_writes(t, db))
    t.save_draft(CHAT, "moment, mein enkel kommt gleich")
    db.add_message(CHAT, "them", "hallo???")  # neue Nachricht nach der Bearbeitung
    run(lambda: t.on_scammer_message(CHAT))
    assert db.chat(CHAT)["draft_text"] == "moment, mein enkel kommt gleich"  # nicht überschrieben
    run(lambda: t.reply_now(CHAT))
    assert sent_texts(t) == ["moment, mein enkel kommt gleich"]
    assert db.messages(CHAT)[-1]["edited"] == 1


def test_stale_draft_is_regenerated_before_sending(setup):
    t, db = setup
    t.set_mode(CHAT, "review")
    t.llm = FakeLLM(["alter entwurf", "neuer entwurf"])
    run(lambda: scammer_writes(t, db))
    db.add_message(CHAT, "them", "und?")
    run(lambda: t.reply_now(CHAT))
    assert sent_texts(t) == ["neuer entwurf"]


def test_instruction_goes_into_prompt_and_is_cleared(setup):
    t, db = setup
    t.set_mode(CHAT, "review")
    db.add_message(CHAT, "them", "hallo")
    run(lambda: t.regenerate_draft(CHAT, "frag nach seinem Hund"))
    last_call = t.llm.reply_calls[-1]
    assert "frag nach seinem Hund" in last_call[-1]["content"]
    assert "frag nach seinem Hund" not in last_call[0]["content"]  # System-Prompt bleibt gleich (Cache)
    assert db.chat(CHAT)["instruction"] == "frag nach seinem Hund"
    run(lambda: t.reply_now(CHAT))
    assert db.chat(CHAT)["instruction"] is None


def test_send_manual_discards_draft_and_schedule(setup):
    t, db = setup
    t.set_mode(CHAT, "review")
    run(lambda: scammer_writes(t, db))
    run(lambda: t.send_manual(CHAT, "bin gleich wieder da"))
    chat = db.chat(CHAT)
    assert chat["draft_text"] is None and chat["due_at"] is None
    assert db.last_sender(CHAT) == "me"


def test_disable_chat_cancels_everything(setup):
    t, db = setup
    t.set_mode(CHAT, "review")
    run(lambda: scammer_writes(t, db))
    t.set_enabled(CHAT, False)
    assert db.chat(CHAT)["draft_text"] is None


def test_analysis_runs_after_enough_messages(setup):
    t, db = setup
    t.set_mode(CHAT, "manual")
    db.set_setting("analyze_every", 3)
    for i in range(3):
        db.add_message(CHAT, "them", f"nachricht {i}")
    db.update_chat(CHAT, mode="review")
    run(lambda: t.on_scammer_message(CHAT))
    analysis = db.analysis(db.chat(CHAT))
    assert analysis["scam_type"] == "Krypto-Investment" and analysis["stage"] == 3
    assert analysis["best_of"][0]["sender"] == "them"


def test_chat_status_and_health(setup):
    t, db = setup
    t.set_mode(CHAT, "review")
    run(lambda: scammer_writes(t, db))
    status = t.chat_status(CHAT)
    assert status["mode"] == "review" and status["draft_text"] and not status["draft_stale"]
    health = t.health()
    assert health["llm_healthy"] is True and health["llm"].cached_tokens_today == 300


def test_describe_code_type():
    from telethon.tl.types.auth import SentCodeTypeApp, SentCodeTypeEmailCode, SentCodeTypeSms

    assert "Telegram-App" in engine_mod.describe_code_type(SentCodeTypeApp(length=5))
    assert engine_mod.describe_code_type(SentCodeTypeSms(length=5)) == "per SMS"
    assert "m***@x.de" in engine_mod.describe_code_type(
        SentCodeTypeEmailCode(email_pattern="m***@x.de", length=6)
    )


# --- QR-Login -------------------------------------------------------------------

class FakeQR:
    def __init__(self, outcomes, accept_on_recreate=False):
        self.outcomes = list(outcomes)
        self.recreated = 0
        self.url = "tg://login?token=x"
        self.accept_on_recreate = accept_on_recreate
        self._resp = None

    async def wait(self, timeout=None):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def recreate(self):
        self.recreated += 1
        if self.accept_on_recreate:
            self._resp = LoginTokenSuccess(authorization=SimpleNamespace(user=ME))


@pytest.fixture
def qr_setup(setup, monkeypatch):
    t, db = setup
    logins = []

    async def fake_after_login(user=None):
        logins.append(user)
        t.me = user

    monkeypatch.setattr(t, "_after_login", fake_after_login)
    return t, logins


def test_qr_loop_renews_token_and_logs_in(qr_setup):
    t, logins = qr_setup
    t._qr = FakeQR([asyncio.TimeoutError(), asyncio.TimeoutError(), ME])
    asyncio.run(t._qr_loop())
    assert t._qr.recreated == 2 and t.qr_state == "done" and logins == [ME]


def test_qr_scanned_while_token_renewed(qr_setup):
    """Regression: Scan während der Erneuerung -> recreate() liefert LoginTokenSuccess."""
    t, logins = qr_setup
    t._qr = FakeQR([asyncio.TimeoutError()], accept_on_recreate=True)
    asyncio.run(t._qr_loop())
    assert t.qr_state == "done" and logins == [ME]
    assert t.client.on_login_calls == 1


def test_qr_loop_needs_password(qr_setup):
    from telethon.errors import SessionPasswordNeededError

    t, logins = qr_setup
    t._qr = FakeQR([SessionPasswordNeededError(request=None)])
    asyncio.run(t._qr_loop())
    assert t.qr_state == "password" and logins == []


def test_qr_error_but_session_authorized(qr_setup):
    t, logins = qr_setup
    t.client.logged_in_user = ME
    t._qr = FakeQR([AttributeError("kaputt")])
    asyncio.run(t._qr_loop())
    assert t.qr_state == "done" and logins == [ME]


def test_qr_error_not_authorized(qr_setup):
    t, logins = qr_setup
    t._qr = FakeQR([AttributeError("kaputt")])
    asyncio.run(t._qr_loop())
    assert t.qr_state == "error" and "kaputt" in t.qr_error and logins == []


def test_refresh_login_picks_up_existing_session(qr_setup):
    t, logins = qr_setup
    asyncio.run(t.refresh_login())
    assert logins == []
    t.client.logged_in_user = ME
    asyncio.run(t.refresh_login())
    assert logins == [ME] and t.authorized


def test_reply_now_does_not_wait_for_later_draft(tmp_path):
    """Regression: Sofort-Antwort wartete auf einen Entwurf, der erst Stunden später starten sollte."""
    config = Config(1, "h", "", "http://x", "a", "b", tmp_path, "127.0.0.1", 8080)
    db = Database(tmp_path / "t.db")
    t = Tarpit(config, db)
    t.client, t.llm = FakeClient(), FakeLLM()
    db.upsert_chat(CHAT, "Scam", None)
    db.update_chat(CHAT, enabled=True)
    db.set_setting("min_delay", 7200)
    db.set_setting("max_delay", 7200)
    db.set_setting("quiet_start", 0)
    db.set_setting("quiet_end", 0)
    engine_mod_typing = engine_mod.typing_plan
    engine_mod.typing_plan = lambda text, rng=None, instant=False: [("typing", 0.01)]
    try:
        async def go():
            db.add_message(CHAT, "them", "hallo")
            t.on_scammer_message(CHAT)
            assert t._draft_start[CHAT] > time.time() + 3600  # Entwurf erst kurz vor dem Termin
            t.reply_now(CHAT)
            await asyncio.wait_for(t.timers[CHAT], timeout=5)

        asyncio.run(go())
    finally:
        engine_mod.typing_plan = engine_mod_typing
    assert sent_texts(t) == ["ach herrje, wie geht das denn?"]



# --- Weiterleitungen ----------------------------------------------------------------

BOSS = 555


def scammer_refers(t, db, text):
    db.add_message(CHAT, "them", text)
    t.on_referral_candidates(CHAT, extract_candidates(text))
    t.on_scammer_message(CHAT)


@pytest.fixture
def ref_setup(setup):
    t, db = setup
    t.client.entities["crypto_boss"] = tg_user(BOSS, "Mr. Boss", "crypto_boss")
    db.set_setting("referral_min_delay", 60)
    db.set_setting("referral_max_delay", 120)
    return t, db


def test_referral_pauses_source_creates_chat_and_notifies(ref_setup, monkeypatch):
    t, db = ref_setup
    started = []
    monkeypatch.setattr(t, "_start_timer", lambda chat_id, due, instant=False: started.append((chat_id, due)))
    run(lambda: scammer_refers(t, db, "Bitte schreib meinem Manager @crypto_boss, er hilft dir"))

    assert db.chat(CHAT)["mode"] == "review"  # alter Chat wartet auf Freigabe
    assert not any(cid == CHAT for cid, _ in started)  # dort keine automatische Antwort
    new = db.chat(BOSS)
    assert new["enabled"] == 1 and new["mode"] == "auto" and new["referred_from"] == CHAT
    assert "Scam" in new["background"]
    assert new["draft_text"] == "ach herrje, wie geht das denn?"
    assert 60 <= new["due_at"] - time.time() <= 121 and started[-1][0] == BOSS
    ref = db.referrals(source_chat_id=CHAT)[0]
    assert ref["status"] == "scheduled" and ref["target_chat_id"] == BOSS
    opening_prompt = next(c for c in t.llm.reply_calls if "@crypto_boss" in c[-1]["content"])
    assert "Hintergrund" in opening_prompt[0]["content"] and "Scam" in opening_prompt[-1]["content"]

    notes = [m["text"] for m in db.messages(CHAT) if m["sender"] == "note"]
    assert any("Weiterleitung erkannt" in n for n in notes) and any("Neuer Chat" in n for n in notes)
    me_msgs = [text for chat, text in t.client.sent if chat == "me"]
    assert len(me_msgs) == 1 and "wartet jetzt auf deine Freigabe" in me_msgs[0] and "@crypto_boss" in me_msgs[0]


def test_referral_opening_is_sent_and_marked(ref_setup):
    t, db = ref_setup
    run(lambda: scammer_refers(t, db, "adde ihn: t.me/crypto_boss"))
    assert (BOSS, "ach herrje, wie geht das denn?") in t.client.sent
    assert db.referrals(source_chat_id=CHAT)[0]["status"] == "contacted"
    assert db.last_sender(BOSS) == "ai"
    # die gleiche Weiterleitung nochmal: kein zweiter Kontakt
    before = len(t.client.sent)
    run(lambda: scammer_refers(t, db, "hast du @crypto_boss schon geschrieben?"))
    assert len(db.referrals(source_chat_id=CHAT)) == 1
    assert len(t.client.sent) == before  # alter Chat steht auf Freigabe: nichts automatisch gesendet
    assert db.chat(CHAT)["draft_text"]  # aber ein Entwurf liegt bereit


@pytest.mark.parametrize("entity,reason", [
    (tg_user(BOSS, contact=True), "Kontakten"),
    (tg_user(BOSS, bot=True), "kein normaler Nutzer"),
    (tg_user(CHAT), "Scammer selbst"),
])
def test_referral_safety_rules(ref_setup, entity, reason):
    t, db = ref_setup
    t.client.entities["crypto_boss"] = entity
    run(lambda: scammer_refers(t, db, "schreib @crypto_boss"))
    ref = db.referrals(source_chat_id=CHAT)[0]
    assert ref["status"] == "skipped" and reason in ref["reason"]
    assert not [c for c, _ in t.client.sent if c not in ("me", CHAT)]


def test_referral_never_contacts_existing_chat(ref_setup):
    t, db = ref_setup
    db.upsert_chat(BOSS, "Mama", None)
    db.add_message(BOSS, "them", "Kommst du Sonntag zum Essen?")
    run(lambda: scammer_refers(t, db, "schreib @crypto_boss"))
    assert db.referrals(source_chat_id=CHAT)[0]["status"] == "skipped"
    assert db.chat(BOSS)["enabled"] == 0


def test_referral_unknown_user_and_daily_limit(ref_setup):
    t, db = ref_setup
    run(lambda: scammer_refers(t, db, "schreib @gibtsnicht_123"))
    assert db.referrals(source_chat_id=CHAT)[0]["status"] == "failed"
    db.set_setting("referral_daily_limit", 0)
    run(lambda: scammer_refers(t, db, "dann eben @crypto_boss"))
    ref = [r for r in db.referrals(source_chat_id=CHAT) if r["target"] == "crypto_boss"][0]
    assert ref["status"] == "skipped" and "Tageslimit" in ref["reason"]


def test_referral_by_phone_imports_contact(ref_setup):
    t, db = ref_setup
    t.client.phone_users["+447911123456"] = tg_user(777, "Phone Guy")
    run(lambda: scammer_refers(t, db, "Kontaktiere +44 7911 123456 auf Telegram"))
    assert t.client.imported == ["+447911123456"]
    assert db.chat(777)["referred_from"] == CHAT


def test_referral_suggest_mode_and_accept(ref_setup):
    t, db = ref_setup
    db.set_setting("referral_mode", "suggest")
    run(lambda: scammer_refers(t, db, "schreib @crypto_boss"))
    ref = db.referrals(source_chat_id=CHAT)[0]
    assert ref["status"] == "proposed" and db.chat(BOSS) is None
    assert "wartet auf deine Entscheidung" in [x for c, x in t.client.sent if c == "me"][0]
    run(lambda: t.accept_referral(ref["id"]))
    assert (BOSS, "ach herrje, wie geht das denn?") in t.client.sent


def test_referral_off(ref_setup):
    t, db = ref_setup
    db.set_setting("referral_mode", "off")
    run(lambda: scammer_refers(t, db, "schreib @crypto_boss"))
    assert db.referrals() == [] and db.chat(CHAT)["mode"] == "auto"


def test_analysis_history_is_kept(setup):
    t, db = setup
    t.set_mode(CHAT, "manual")
    db.add_message(CHAT, "them", "hallo")
    run(lambda: t.maybe_analyze(CHAT, force=True))
    db.add_message(CHAT, "them", "und?")
    run(lambda: t.maybe_analyze(CHAT, force=True))
    history = db.analysis_history(CHAT)
    assert len(history) == 2 and history[0]["messages"] == 2 and history[1]["messages"] == 1



# --- Bilder, Bild-/Spracherkennung, Token-Limit ---------------------------------------

def add_image(t, db, description="Katze Mausi auf dem Sofa"):
    persona = db.persona_for_chat(db.chat(CHAT))
    filename = f"img{len(db.persona_images(persona['id']))}.jpg"
    (t.config.media_dir / filename).write_bytes(b"jpeg")
    return db.add_persona_image(persona["id"], filename, description)


def test_persona_image_is_offered_and_sent_once(setup):
    t, db = setup
    image_id = add_image(t, db)
    t.llm = FakeLLM([f"[BILD:{image_id}]\nda ist mausi", f"[BILD:{image_id}]\nnochmal mausi"])
    run(lambda: scammer_writes(t, db, "schick mir ein foto von dir"))
    prompt = t.llm.reply_calls[0]
    assert f"{image_id}: Katze Mausi" in prompt[0]["content"]  # im festen System-Prompt (Cache)
    assert "fragt nach einem Foto" in prompt[-1]["content"]
    assert t.client.files == [(CHAT, str(t.config.media_dir / "img0.jpg"), "da ist mausi")]
    stored = db.messages(CHAT)[-1]
    assert stored["image_id"] == image_id and stored["text"].startswith("[Bild: Katze Mausi")

    run(lambda: scammer_writes(t, db, "noch ein bild bitte"))
    assert len(t.client.files) == 1  # jedes Bild nur einmal
    assert sent_texts(t)[-1] == "nochmal mausi"
    assert f"Bereits geschickte Fotos (nicht nochmal): {image_id}" in t.llm.reply_calls[-1][-1]["content"]
    assert t.chat_status(CHAT)["draft_images"] == []


def test_draft_images_in_status(setup):
    t, db = setup
    image_id = add_image(t, db)
    db.update_chat(CHAT, draft_text=f"[BILD:{image_id}] hier\n[BILD:999]")
    images = t.chat_status(CHAT)["draft_images"]
    assert images[0]["ok"] is True and images[0]["url"] == f"/media/images/{image_id}"
    assert images[1]["ok"] is False


def test_usage_is_recorded_with_purpose_and_estimated_cost(setup):
    t, db = setup
    t.llm = FakeLLM(cost=0.0)
    db.set_setting("price_input_per_m", 1.0)
    db.set_setting("price_output_per_m", 10.0)
    run(lambda: scammer_writes(t, db))
    rows = db.conn.execute("SELECT * FROM usage").fetchall()
    assert rows[0]["purpose"] == "reply" and rows[0]["chat_id"] == CHAT
    assert rows[0]["cost"] == pytest.approx((500 * 1 + 20 * 10) / 1_000_000)
    assert t.tokens_today() == 520


def test_daily_token_limit_stops_everything_and_notifies_once(setup):
    t, db = setup
    db.set_setting("daily_token_limit", 1000)
    db.add_usage("x", "reply", 900, 0, 200, 0.0)
    run(lambda: scammer_writes(t, db))
    assert [c for c, _ in t.client.sent if c == CHAT] == []
    assert t.llm.calls == []
    assert any("Token-Tageslimit" in m["text"] for m in db.messages(CHAT) if m["sender"] == "note")
    run(lambda: t.maybe_analyze(CHAT, force=True))
    notes_to_me = [x for c, x in t.client.sent if c == "me"]
    assert len(notes_to_me) == 1 and "Token-Tageslimit" in notes_to_me[0]


def media_msg(**kind):
    base = dict(photo=None, document=None, voice=None, audio=None, video_note=None, sticker=None,
                message="guck mal", file=SimpleNamespace(mime_type=None))
    base.update(kind)
    return SimpleNamespace(**base)


def test_vision_and_speech_recognition(setup):
    t, db = setup
    assert run_value(t._understand_media(CHAT, media_msg(photo=True))) is None  # aus = nichts
    db.set_setting("vision_model", "vision/model")
    db.set_setting("stt_model", "audio/model")
    t.llm = FakeLLM(["Screenshot einer Krypto-App mit 50.000 USDT", "hallo gerda"])
    text = run_value(t._understand_media(CHAT, media_msg(photo=True)))
    assert text == "[Foto: Screenshot einer Krypto-App mit 50.000 USDT] guck mal"
    vision_call = t.llm.calls[0][0]["content"]
    assert vision_call[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    text = run_value(t._understand_media(CHAT, media_msg(voice=True, message="")))
    assert text == "[Sprachnachricht: „hallo gerda“]"
    assert t.llm.calls[1][0]["content"][1]["type"] == "input_audio"
    db.set_setting("stt_backend", "whisper")
    assert "hier ist anna" in run_value(t._understand_media(CHAT, media_msg(voice=True)))
    purposes = [r["purpose"] for r in db.conn.execute("SELECT purpose FROM usage")]
    assert purposes == ["vision", "stt", "stt"]
    db.update_chat(CHAT, enabled=False)  # nur in KI-Chats (Kosten, Datenschutz)
    assert run_value(t._understand_media(CHAT, media_msg(photo=True))) is None


def run_value(coro):
    return asyncio.run(coro)
