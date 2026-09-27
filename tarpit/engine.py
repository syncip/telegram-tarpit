"""Telegram-Anbindung und Tarpit-Logik.

Ablauf pro Chat:
1. Der Scammer schreibt. Nach kurzer Pause (Debounce) erzeugt die KI einen
   **Entwurf**, den du im Webinterface siehst und bearbeiten kannst.
2. Im Modus ``auto`` wird ein **Sendezeitpunkt** (``due_at``) geplant. Er
   bleibt stehen, auch wenn weitere Nachrichten kommen (Tarpit!), und
   überlebt Neustarts.
3. Zum Sendezeitpunkt, oder sofort per Knopf, wird der Entwurf gesendet. Ist
   er veraltet (neue Nachricht seitdem) und nicht von dir bearbeitet, wird
   vorher neu generiert.

Modi: ``auto`` (KI sendet selbst), ``review`` (KI schlägt nur vor, du gibst
frei), ``manual`` (nur du, keine KI).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl import functions, types
from telethon.tl.custom import Message
from telethon.tl.types import User

from .analysis import build_analysis_messages, parse_analysis
from .config import Config
from .db import Database
from .llm import LLMClient, LLMError
from .prompts import SKIP_TOKEN, build_messages
from .safety import check_reply, clean_reply, split_reply
from .timing import postpone_quiet_hours, sample_delay, typing_plan

log = logging.getLogger(__name__)

HISTORY_IMPORT_LIMIT = 50
QR_WAIT_SECONDS = 20  # QR-Tokens gelten ca. 30 Sekunden
DRAFT_DEBOUNCE = 6.0  # Sekunden warten, falls der Scammer mehrere Nachrichten am Stück schickt
# Im Automatikmodus entsteht der Entwurf erst kurz vor dem Senden. Schreibt der
# Scammer bis dahin weiter, muss nicht jedes Mal neu generiert werden (spart Tokens).
DRAFT_LEAD = 300.0
ANALYSIS_HISTORY = 80
WATCHDOG_INTERVAL = 30.0


def _log(level: int, source: str, message: str, *args, chat_id: int | None = None, **kwargs) -> None:
    """Loggt mit Quelle und Chat, damit die Statusseite filtern kann."""
    log.log(level, message, *args, extra={"source": source, "chat_id": chat_id}, **kwargs)



def display_name(user: User) -> str:
    name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    return name or (f"@{user.username}" if user.username else str(user.id))


def describe_message(msg: Message) -> str:
    """Textdarstellung einer Nachricht, inkl. Platzhalter für Medien."""
    kind = None
    if msg.sticker:
        kind = "[Sticker]"
    elif msg.voice:
        kind = "[Sprachnachricht]"
    elif msg.video_note or msg.video or msg.gif:
        kind = "[Video]"
    elif msg.photo:
        kind = "[Foto]"
    elif msg.document:
        kind = "[Datei]"
    parts = [p for p in (kind, msg.message) if p]
    return " ".join(parts) or "[Nachricht ohne Text]"


_CODE_TYPES = {
    "SentCodeTypeApp": "als Nachricht vom Konto „Telegram“ in deine Telegram-App, und zwar auf einem "
    "anderen Gerät, auf dem du schon angemeldet bist (nicht per SMS!)",
    "SentCodeTypeSms": "per SMS",
    "SentCodeTypeFirebaseSms": "per SMS",
    "SentCodeTypeSmsWord": "per SMS (als Wort)",
    "SentCodeTypeSmsPhrase": "per SMS (als Satz)",
    "SentCodeTypeCall": "per Anruf",
    "SentCodeTypeFlashCall": "per verpasstem Anruf (der Code steckt in der anrufenden Nummer)",
    "SentCodeTypeMissedCall": "per verpasstem Anruf (der Code sind die letzten Ziffern der anrufenden Nummer)",
    "SentCodeTypeFragmentSms": "über Fragment (fragment.com)",
    "CodeTypeSms": "per SMS",
    "CodeTypeCall": "per Anruf",
    "CodeTypeFlashCall": "per verpasstem Anruf",
    "CodeTypeMissedCall": "per verpasstem Anruf",
    "CodeTypeFragmentSms": "über Fragment (fragment.com)",
}


def describe_code_type(code_type) -> str:
    name = type(code_type).__name__
    if name == "SentCodeTypeEmailCode":
        return f"per E-Mail an {code_type.email_pattern}"
    if name == "SentCodeTypeSetUpEmailRequired":
        return ("gar nicht: Telegram verlangt für diese Anmeldung eine Login-E-Mail. "
                "Bitte stattdessen den QR-Code-Login verwenden")
    return _CODE_TYPES.get(name, f"auf unbekanntem Weg ({name})")




class Tarpit:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.client = TelegramClient(str(config.session_path), config.api_id, config.api_hash)
        self.llm = LLMClient(config.llm_base_url, config.llm_api_key)
        self.me: User | None = None
        self.rng = random.Random()
        # laufende Hintergrund-Aufgaben pro Chat
        self.timers: dict[int, asyncio.Task] = {}      # wartet bis due_at, sendet dann
        self.drafting: dict[int, asyncio.Task] = {}    # erzeugt (bald) einen Entwurf
        self._draft_start: dict[int, float] = {}       # wann der geplante Entwurf startet
        self.analyzing: dict[int, asyncio.Task] = {}   # erstellt gerade eine Analyse
        self.sending: set[int] = set()                 # tippt/sendet gerade
        self.draft_failed: dict[int, int] = {}         # Chat -> Nachrichten-ID, für die der Entwurf scheiterte
        self._watchdog: asyncio.Task | None = None
        self._was_connected: bool | None = None
        self._login_phone = ""
        self._login_hash = ""
        self.login_code_hint: str | None = None
        self.login_resend_hint: str | None = None
        self._qr = None
        self._qr_task: asyncio.Task | None = None
        self.qr_state: str | None = None  # waiting | password | error | done
        self.qr_error: str | None = None

    # --- Lebenszyklus ------------------------------------------------------

    @property
    def authorized(self) -> bool:
        return self.me is not None

    async def start(self) -> None:
        """Verbindet mit Telegram. Ohne gültige Session läuft die App trotzdem,
        das Webinterface zeigt dann die Login-Seite."""
        self.client.add_event_handler(
            self._on_incoming, events.NewMessage(incoming=True, func=lambda e: e.is_private)
        )
        self.client.add_event_handler(
            self._on_outgoing, events.NewMessage(outgoing=True, func=lambda e: e.is_private)
        )
        _log(logging.INFO, "telegram", "Verbinde mit Telegram …")
        await self.client.connect()
        self._watchdog = asyncio.create_task(self._watch_connection())
        if await self.client.is_user_authorized():
            await self._after_login()
        else:
            _log(logging.WARNING, "telegram", "Telegram-Session ist nicht angemeldet. Login über das Webinterface.")

    async def _after_login(self, user: User | None = None) -> None:
        me = user or await self.client.get_me()
        if me is None:
            raise RuntimeError("Telegram meldet die Session als nicht angemeldet")
        self.me = me
        _log(logging.INFO, "telegram", "Angemeldet als %s (id %s)", display_name(me), me.id)
        try:
            await self.sync_dialogs()
        except Exception:
            _log(logging.ERROR, "telegram", "Chatliste konnte nicht geladen werden", exc_info=True)
        # Was während der Downtime passiert ist, nachholen; geplante Antworten wieder aufnehmen
        for chat in self.db.enabled_chats():
            try:
                await self.import_history(chat["chat_id"], limit=20)
            except Exception:
                _log(logging.WARNING, "telegram", "Verlauf konnte nicht nachgeladen werden",
                     chat_id=chat["chat_id"], exc_info=True)
            self.activate(chat["chat_id"])

    async def _watch_connection(self) -> None:
        """Meldet Verbindungsabbrüche zu Telegram im Log."""
        while True:
            connected = self.client.is_connected()
            if connected != self._was_connected:
                if connected:
                    _log(logging.INFO, "telegram", "Verbindung zu Telegram steht")
                elif self._was_connected is not None:
                    _log(logging.ERROR, "telegram", "Verbindung zu Telegram verloren")
                self._was_connected = connected
            await asyncio.sleep(WATCHDOG_INTERVAL)

    # --- Login über das Webinterface ---------------------------------------

    async def request_login_code(self, phone: str) -> None:
        """Fordert einen Login-Code an. Mit derselben Nummer erneut aufgerufen,
        schickt Telegram den Code nochmal, oft auf anderem Weg (z. B. SMS)."""
        sent = await self.client.send_code_request(phone)
        self._login_phone = phone
        self._login_hash = sent.phone_code_hash or self._login_hash
        self.login_code_hint = describe_code_type(sent.type)
        next_type = getattr(sent, "next_type", None)
        self.login_resend_hint = describe_code_type(next_type) if next_type else None

    @property
    def login_phone(self) -> str:
        return self._login_phone

    async def submit_login_code(self, code: str) -> bool:
        """Gibt False zurück, wenn zusätzlich das 2FA-Passwort nötig ist."""
        try:
            await self.client.sign_in(
                self._login_phone, code.strip().replace(" ", ""), phone_code_hash=self._login_hash
            )
        except SessionPasswordNeededError:
            return False
        await self._after_login()
        return True

    async def submit_login_password(self, password: str) -> None:
        await self.client.sign_in(password=password)
        await self._after_login()

    # --- Login per QR-Code ---------------------------------------------------

    async def start_qr_login(self) -> None:
        self.cancel_qr_login()
        self._qr = await self.client.qr_login()
        self.qr_state = "waiting"
        self.qr_error = None
        self._qr_task = asyncio.create_task(self._qr_loop())

    def cancel_qr_login(self) -> None:
        if self._qr_task is not None:
            self._qr_task.cancel()
        self._qr_task = None
        self._qr = None
        self.qr_state = None

    @property
    def qr_url(self) -> str | None:
        try:
            return self._qr.url if self._qr is not None else None
        except AttributeError:  # Token schon eingelöst, es gibt keinen neuen QR-Code mehr
            return None

    async def _qr_loop(self) -> None:
        user = None
        try:
            while True:
                try:
                    # Eigenes, festes Timeout statt Telethons Berechnung aus der
                    # Ablaufzeit: die geht schief, wenn die Uhr des Hosts abweicht.
                    user = await self._qr.wait(QR_WAIT_SECONDS)
                    break
                except asyncio.TimeoutError:
                    await self._qr.recreate()
                    user = await self._qr_token_accepted()
                    if user is not None:
                        break
        except SessionPasswordNeededError:
            log.info("QR-Code bestätigt, Zwei-Schritt-Passwort nötig")
            self.qr_state = "password"
            return
        except Exception as exc:
            user = await self._fetch_me()
            if user is None:
                log.exception("QR-Login fehlgeschlagen")
                self.qr_state = "error"
                self.qr_error = str(exc)
                return
            await self.client._on_login(user)
        log.info("QR-Code bestätigt")
        self.qr_state = "done"
        try:
            await self._after_login(user)
        except Exception as exc:
            log.exception("Login konnte nicht abgeschlossen werden")
            self.qr_state = "error"
            self.qr_error = str(exc)

    async def _qr_token_accepted(self) -> User | None:
        """Wurde der QR-Code gescannt, während wir ihn erneuert haben, liefert
        Telegram statt eines neuen Tokens direkt das Login-Ergebnis zurück."""
        resp = getattr(self._qr, "_resp", None)
        if isinstance(resp, types.auth.LoginTokenMigrateTo):
            # Account liegt in einem anderen Rechenzentrum
            await self.client._switch_dc(resp.dc_id)
            resp = await self.client(functions.auth.ImportLoginTokenRequest(resp.token))
        if not isinstance(resp, types.auth.LoginTokenSuccess):
            return None
        user = resp.authorization.user
        # Das macht Telethon sonst selbst in QRLogin.wait(): Login-Status und
        # Update-Zustand setzen, damit neue Nachrichten ankommen.
        await self.client._on_login(user)
        return user

    async def _fetch_me(self) -> User | None:
        """Fragt Telegram direkt, ob die Session angemeldet ist.

        Nicht is_user_authorized() verwenden: Telethon merkt sich dort das
        Ergebnis vom Start ("nein") und fragt danach nie wieder nach.
        """
        try:
            return await self.client.get_me()
        except Exception:
            return None

    async def refresh_login(self) -> None:
        """Übernimmt einen Login, der auf Telegram-Seite schon erfolgt ist,
        den die App aber (noch) nicht mitbekommen hat."""
        if self.authorized or (self._qr_task is not None and not self._qr_task.done()):
            return
        me = await self._fetch_me()
        if me is not None:
            await self.client._on_login(me)
            await self._after_login(me)

    async def logout(self) -> None:
        self.stop_all()
        await self.client.log_out()
        self.me = None
        _log(logging.WARNING, "telegram", "Telegram-Session abgemeldet")
        # log_out() trennt die Verbindung; für einen neuen Login wieder verbinden
        await self.client.connect()

    async def stop(self) -> None:
        self.cancel_qr_login()
        if self._watchdog is not None:
            self._watchdog.cancel()
        for tasks in (self.timers, self.drafting, self.analyzing):
            for task in tasks.values():
                task.cancel()
            tasks.clear()
        await self.llm.aclose()
        await self.client.disconnect()

    # --- Synchronisation ---------------------------------------------------

    async def sync_dialogs(self, limit: int = 300) -> int:
        """Liest die Chatliste aus Telegram (nur Privatchats mit echten Nutzern)."""
        count = 0
        async for dialog in self.client.iter_dialogs(limit=limit):
            entity = dialog.entity
            if not isinstance(entity, User) or entity.bot or entity.is_self or entity.deleted:
                continue
            ts = dialog.date.timestamp() if dialog.date else None
            self.db.upsert_chat(dialog.id, display_name(entity), entity.username, ts)
            count += 1
        return count

    async def import_history(self, chat_id: int, limit: int = HISTORY_IMPORT_LIMIT) -> int:
        """Lädt die letzten Nachrichten eines Chats in die Datenbank (für den KI-Kontext)."""
        added = 0
        async for msg in self.client.iter_messages(chat_id, limit=limit):
            if msg.action is not None:  # Servicenachrichten
                continue
            sender = "me" if msg.out else "them"
            if self.db.add_message(chat_id, sender, describe_message(msg), msg.date.timestamp(), msg.id):
                added += 1
        return added

    # --- Telegram-Events ---------------------------------------------------

    async def _on_incoming(self, event: events.NewMessage.Event) -> None:
        sender = await event.get_sender()
        if not isinstance(sender, User) or sender.bot:
            return
        msg: Message = event.message
        self.db.upsert_chat(event.chat_id, display_name(sender), sender.username, msg.date.timestamp())
        self.db.add_message(event.chat_id, "them", describe_message(msg), msg.date.timestamp(), msg.id)
        self.on_scammer_message(event.chat_id)

    async def _on_outgoing(self, event: events.NewMessage.Event) -> None:
        # Von der KI gesendete Nachrichten landen auch hier. Kurz warten, bis
        # _send_reply sie gespeichert hat, und nur echte manuelle Nachrichten
        # (z. B. vom Handy) als 'me' übernehmen.
        await asyncio.sleep(2)
        msg: Message = event.message
        if self.db.has_tg_message(event.chat_id, msg.id):
            return
        chat = self.db.chat(event.chat_id)
        if chat is None:
            peer = await event.get_chat()
            if isinstance(peer, User):
                self.db.upsert_chat(event.chat_id, display_name(peer), peer.username)
        self.db.add_message(event.chat_id, "me", describe_message(msg), msg.date.timestamp(), msg.id)
        if chat is not None and chat["enabled"]:
            # Du hast selbst geantwortet, die KI muss das nicht mehr tun
            self.cancel_reply(event.chat_id, clear_draft=True)
            _log(logging.INFO, "engine", "Manuelle Antwort vom Handy erkannt, KI-Antwort verworfen",
                 chat_id=event.chat_id)

    # --- Steuerung ---------------------------------------------------------

    def is_active(self, chat) -> bool:
        return bool(chat is not None and chat["enabled"] and self.db.settings()["global_enabled"])

    def on_scammer_message(self, chat_id: int) -> None:
        chat = self.db.chat(chat_id)
        if not self.is_active(chat) or chat["mode"] == "manual":
            return
        if chat["mode"] == "auto":
            self.ensure_due(chat_id)
        self.request_draft(chat_id, debounce=self._draft_delay(chat_id))
        self.maybe_analyze(chat_id)

    def activate(self, chat_id: int) -> None:
        """Nimmt die Arbeit an einem Chat (wieder) auf, z. B. nach Start oder Moduswechsel."""
        chat = self.db.chat(chat_id)
        if not self.is_active(chat) or chat["mode"] == "manual":
            return
        if self.db.last_sender(chat_id) != "them":
            if chat["due_at"]:
                self.db.update_chat(chat_id, due_at=None)
            return
        if chat["mode"] == "auto":
            self.ensure_due(chat_id)
        if not chat["draft_text"]:
            self.request_draft(chat_id, debounce=self._draft_delay(chat_id))

    def ensure_due(self, chat_id: int) -> None:
        """Sorgt für einen geplanten Sendezeitpunkt. Ein bestehender bleibt erhalten."""
        if chat_id in self.timers:
            return
        chat = self.db.chat(chat_id)
        now = time.time()
        due = chat["due_at"]
        if due is None:
            settings = self.db.settings()
            delay = sample_delay(settings["min_delay"], settings["max_delay"], self.rng)
            due_dt = postpone_quiet_hours(
                datetime.fromtimestamp(now + delay), settings["quiet_start"], settings["quiet_end"], self.rng
            )
            due = due_dt.timestamp()
            _log(logging.INFO, "engine", "Nächste KI-Antwort geplant für %s",
                 due_dt.strftime("%d.%m. %H:%M:%S"), chat_id=chat_id)
        elif due < now:
            # nach einem Neustart verpasst: bald nachholen
            due = now + self.rng.uniform(10, 60)
        self.db.update_chat(chat_id, due_at=due)
        self._start_timer(chat_id, due)

    def _start_timer(self, chat_id: int, due: float, instant: bool = False) -> None:
        old = self.timers.pop(chat_id, None)
        if old is not None:
            old.cancel()
        self.timers[chat_id] = asyncio.create_task(self._timer(chat_id, due, instant))

    async def _timer(self, chat_id: int, due: float, instant: bool) -> None:
        await asyncio.sleep(max(0.0, due - time.time()))
        if self.timers.get(chat_id) is asyncio.current_task():
            del self.timers[chat_id]
        try:
            await self._send_reply(chat_id, instant)
        except Exception as exc:
            _log(logging.ERROR, "engine", "Antwort fehlgeschlagen: %s", exc, chat_id=chat_id, exc_info=True)
            self.db.add_message(chat_id, "note", f"Fehler beim Senden: {exc}")
            self.db.update_chat(chat_id, due_at=None)

    def reply_now(self, chat_id: int) -> None:
        """Sofort antworten: Entwurf (ggf. neu erzeugt) ohne Wartezeit senden."""
        now = time.time()
        self.db.update_chat(chat_id, due_at=now)
        self._start_timer(chat_id, now, instant=True)
        _log(logging.INFO, "engine", "Sofort-Antwort ausgelöst", chat_id=chat_id)

    def cancel_reply(self, chat_id: int, clear_draft: bool = False) -> None:
        timer = self.timers.pop(chat_id, None)
        if timer is not None:
            timer.cancel()
        fields = {"due_at": None}
        if clear_draft:
            task = self.drafting.pop(chat_id, None)
            if task is not None:
                task.cancel()
            fields.update(draft_text=None, draft_edited=0, draft_basis=None, draft_at=None, instruction=None)
        if self.db.chat(chat_id) is not None:
            self.db.update_chat(chat_id, **fields)

    def stop_all(self) -> None:
        for chat_id in set(self.timers) | set(self.drafting):
            timer = self.timers.pop(chat_id, None)
            if timer is not None:
                timer.cancel()
            task = self.drafting.pop(chat_id, None)
            if task is not None:
                task.cancel()
        for chat in self.db.enabled_chats():
            if chat["due_at"]:
                self.db.update_chat(chat["chat_id"], due_at=None)

    def resume_all(self) -> None:
        for chat in self.db.enabled_chats():
            self.activate(chat["chat_id"])

    def set_enabled(self, chat_id: int, enabled: bool) -> None:
        self.db.update_chat(chat_id, enabled=enabled)
        if enabled:
            _log(logging.INFO, "engine", "KI übernimmt den Chat", chat_id=chat_id)
            self.activate(chat_id)
            self.maybe_analyze(chat_id)
        else:
            _log(logging.INFO, "engine", "KI für den Chat deaktiviert", chat_id=chat_id)
            self.cancel_reply(chat_id, clear_draft=True)

    def set_mode(self, chat_id: int, mode: str) -> None:
        self.db.update_chat(chat_id, mode=mode)
        _log(logging.INFO, "engine", "Modus: %s", {"auto": "KI automatisch", "review": "KI schlägt vor",
             "manual": "nur ich"}[mode], chat_id=chat_id)
        if mode == "manual":
            self.cancel_reply(chat_id, clear_draft=True)
        elif mode == "review":
            self.cancel_reply(chat_id)
            self.activate(chat_id)
        else:
            self.activate(chat_id)

    # --- Entwürfe ------------------------------------------------------------

    def _draft_delay(self, chat_id: int) -> float:
        chat = self.db.chat(chat_id)
        if chat["mode"] == "auto" and chat["due_at"]:
            return max(DRAFT_DEBOUNCE, chat["due_at"] - time.time() - DRAFT_LEAD)
        return DRAFT_DEBOUNCE

    def ensure_preview(self, chat_id: int) -> None:
        """Du schaust dir den Chat an: Entwurf jetzt erzeugen statt erst kurz vor dem Senden."""
        chat = self.db.chat(chat_id)
        if (
            self.is_active(chat) and chat["mode"] != "manual" and not chat["draft_text"]
            and self.db.last_sender(chat_id) == "them" and chat_id not in self.sending
        ):
            task = self.drafting.get(chat_id)
            starts = self._draft_start.get(chat_id, 0.0)
            if task is None or task.done() or starts > time.time() + 1:
                self.request_draft(chat_id, debounce=0.5)

    def request_draft(self, chat_id: int, debounce: float = 0.0, force: bool = False) -> None:
        """Erzeugt (nach kurzer Wartezeit) einen neuen Entwurf. Von dir bearbeitete
        Entwürfe werden nur mit ``force`` überschrieben."""
        chat = self.db.chat(chat_id)
        if chat is None or (chat["draft_edited"] and not force):
            return
        old = self.drafting.pop(chat_id, None)
        if old is not None:
            old.cancel()
        self._draft_start[chat_id] = time.time() + debounce
        self.drafting[chat_id] = asyncio.create_task(self._draft_after(chat_id, debounce))

    async def _draft_after(self, chat_id: int, debounce: float) -> None:
        try:
            await asyncio.sleep(debounce)
            await self._make_draft(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(logging.ERROR, "engine", "Entwurf fehlgeschlagen: %s", exc, chat_id=chat_id, exc_info=True)
        finally:
            if self.drafting.get(chat_id) is asyncio.current_task():
                del self.drafting[chat_id]

    async def _make_draft(self, chat_id: int) -> str | None:
        chat = self.db.chat(chat_id)
        settings = self.db.settings()
        persona = self.db.persona_for_chat(chat)
        if persona is None:
            self.db.add_message(chat_id, "note", "Keine Persona vorhanden.")
            return None
        history = self.db.messages_window(chat_id, settings["history_limit"])
        if not history:
            return None
        basis = self.db.last_message_id(chat_id)
        text = await self._generate(chat_id, persona["prompt"], history, settings, chat["instruction"])
        if text is None:
            self.draft_failed[chat_id] = basis
            return None
        self.draft_failed.pop(chat_id, None)
        self.db.update_chat(
            chat_id, draft_text=text, draft_edited=0, draft_basis=basis, draft_at=time.time()
        )
        _log(logging.INFO, "llm", "Entwurf erstellt (%d Zeichen)", len(text), chat_id=chat_id)
        return text

    def save_draft(self, chat_id: int, text: str) -> None:
        task = self.drafting.pop(chat_id, None)
        if task is not None:
            task.cancel()
        self.db.update_chat(
            chat_id, draft_text=text, draft_edited=1, draft_at=time.time(),
            draft_basis=self.db.last_message_id(chat_id),
        )
        _log(logging.INFO, "engine", "Entwurf von dir bearbeitet", chat_id=chat_id)

    def regenerate_draft(self, chat_id: int, instruction: str | None) -> None:
        self.db.update_chat(chat_id, instruction=(instruction or "").strip() or None, draft_edited=0)
        self.request_draft(chat_id, force=True)

    def discard_draft(self, chat_id: int) -> None:
        """Diesmal nicht antworten."""
        self.cancel_reply(chat_id, clear_draft=True)
        _log(logging.INFO, "engine", "Entwurf verworfen, keine Antwort", chat_id=chat_id)

    # --- Senden --------------------------------------------------------------

    async def _send_reply(self, chat_id: int, instant: bool = False) -> bool:
        """Sendet den Entwurf. True, wenn etwas gesendet wurde."""
        if chat_id in self.sending:
            return False
        chat = self.db.chat(chat_id)
        if chat is None or not chat["enabled"]:
            return False
        if not instant:
            # automatischer Versand nur, wenn weiterhin alles passt
            if not self.is_active(chat) or chat["mode"] != "auto" or self.db.last_sender(chat_id) != "them":
                self.db.update_chat(chat_id, due_at=None)
                return False
            settings = self.db.settings()
            if self.db.ai_sent_today(chat) >= settings["daily_limit"]:
                _log(logging.WARNING, "engine", "Tageslimit von %d KI-Nachrichten erreicht, keine Antwort",
                     settings["daily_limit"], chat_id=chat_id)
                self.db.add_message(chat_id, "note", "Tageslimit erreicht, keine automatische Antwort.")
                self.db.update_chat(chat_id, due_at=None)
                return False

        self.sending.add(chat_id)
        sent_any = False
        try:
            task = self.drafting.get(chat_id)
            if task is not None and not task.done():
                if self._draft_start.get(chat_id, 0.0) > time.time():
                    # Entwurf ist erst für später geplant: nicht warten, gleich selbst erzeugen
                    task.cancel()
                    self.drafting.pop(chat_id, None)
                else:
                    # Entwurf wird gerade erzeugt: darauf warten statt doppelt zu bezahlen
                    await asyncio.wait([task])
            chat = self.db.chat(chat_id)
            text, edited = chat["draft_text"], bool(chat["draft_edited"])
            last_id = self.db.last_message_id(chat_id)
            if not text or (not edited and (chat["draft_basis"] or 0) < last_id):
                if not text and not instant and self.draft_failed.get(chat_id) == last_id:
                    # Für genau diese Nachricht ist der Entwurf schon gescheitert (Filter/Fehler):
                    # nicht automatisch nochmal Tokens verbrennen. Per Knopf geht es weiterhin.
                    _log(logging.WARNING, "engine", "Kein gültiger Entwurf, automatische Antwort entfällt",
                         chat_id=chat_id)
                    self.db.update_chat(chat_id, due_at=None)
                    return False
                text, edited = await self._make_draft(chat_id), False
            if text is None:
                self.db.update_chat(chat_id, due_at=None)
                return False

            if not instant:
                await asyncio.sleep(self.rng.uniform(2, 15))  # "Nachricht lesen"
            await self.client.send_read_acknowledge(chat_id)

            if text.strip() == SKIP_TOKEN:
                self.db.add_message(chat_id, "note", "KI lässt das Gegenüber bewusst zappeln ([SKIP]).")
                _log(logging.INFO, "engine", "KI antwortet diesmal bewusst nicht ([SKIP])", chat_id=chat_id)
                self.cancel_reply(chat_id, clear_draft=True)
                return False

            await self._set_online(True)
            parts = split_reply(text)
            for i, part in enumerate(parts):
                if i:
                    await asyncio.sleep(self.rng.uniform(1, 3) if instant else self.rng.uniform(3, 20))
                await self._type(chat_id, part, instant)
                sent = await self.client.send_message(chat_id, part)
                self.db.add_message(chat_id, "ai", part, time.time(), sent.id, edited=edited)
                self.db.bump_ai_count(chat_id)
                sent_any = True
            _log(logging.INFO, "telegram", "KI-Antwort gesendet (%d Nachricht%s%s)", len(parts),
                 "en" if len(parts) > 1 else "", ", von dir bearbeitet" if edited else "", chat_id=chat_id)
            self.db.update_chat(
                chat_id, due_at=None, draft_text=None, draft_edited=0, draft_basis=None,
                draft_at=None, instruction=None,
            )
        finally:
            self.sending.discard(chat_id)
            if sent_any:
                await self._set_online(False)
        self.maybe_analyze(chat_id)
        # Während wir "getippt" haben, kam evtl. schon die nächste Nachricht
        if self.db.last_sender(chat_id) == "them":
            self.on_scammer_message(chat_id)
        return sent_any

    async def _type(self, chat_id: int, text: str, instant: bool) -> None:
        """Zeigt dem Gegenüber "schreibt ..." an, mit realistischen Pausen."""
        for action, seconds in typing_plan(text, self.rng, instant):
            if action == "typing":
                async with self.client.action(chat_id, "typing"):
                    await asyncio.sleep(seconds)
            else:
                await asyncio.sleep(seconds)

    async def _set_online(self, online: bool) -> None:
        """Beim Tippen "online" erscheinen, danach wieder "zuletzt online"."""
        try:
            await self.client(functions.account.UpdateStatusRequest(offline=not online))
        except Exception:
            pass

    async def _generate(
        self, chat_id: int, persona_prompt: str, history, settings, instruction: str | None = None
    ) -> str | None:
        messages = build_messages(persona_prompt, history, instruction=instruction)
        for attempt in range(1, 3):
            try:
                result = await self.llm.complete(
                    settings["model"], messages, settings["temperature"],
                    max_tokens=settings["max_reply_tokens"] or None,
                )
            except LLMError as exc:
                _log(logging.ERROR, "llm", "%s", exc, chat_id=chat_id)
                self.db.add_message(chat_id, "note", f"KI-Fehler: {exc}")
                return None
            _log(logging.INFO, "llm", "Antwort erzeugt in %d ms: %s", result.latency_ms,
                 result.usage.describe(), chat_id=chat_id)
            text = clean_reply(result.text)
            if text == SKIP_TOKEN:
                return SKIP_TOKEN
            reason = check_reply(text)
            if reason is None:
                return text
            _log(logging.WARNING, "safety", "Antwort blockiert (%s), Versuch %d: %s", reason, attempt, text,
                 chat_id=chat_id)
            self.db.add_message(chat_id, "note", f"Blockiert ({reason}): {text}")
        return None

    async def send_manual(self, chat_id: int, text: str) -> None:
        """Du antwortest selbst ("nur ich"): KI-Entwurf und geplante Antwort entfallen."""
        sent = await self.client.send_message(chat_id, text)
        self.db.add_message(chat_id, "me", text, time.time(), sent.id)
        self.cancel_reply(chat_id, clear_draft=True)
        _log(logging.INFO, "telegram", "Eigene Nachricht gesendet", chat_id=chat_id)

    # --- Analyse -------------------------------------------------------------

    def maybe_analyze(self, chat_id: int, force: bool = False) -> bool:
        task = self.analyzing.get(chat_id)
        if task is not None and not task.done():
            return False
        chat = self.db.chat(chat_id)
        if chat is None:
            return False
        if not force:
            settings = self.db.settings()
            if not settings["auto_analyze"] or not chat["enabled"]:
                return False
            if self.db.messages_since(chat_id, chat["analysis_basis"]) < settings["analyze_every"]:
                return False
        self.analyzing[chat_id] = asyncio.create_task(self._analyze(chat_id))
        return True

    async def _analyze(self, chat_id: int) -> None:
        try:
            history = self.db.messages(chat_id, limit=ANALYSIS_HISTORY, include_notes=False)
            if not history:
                return
            basis = self.db.last_message_id(chat_id)
            settings = self.db.settings()
            model = settings["analysis_model"] or settings["model"]
            result = await self.llm.complete(model, build_analysis_messages(history), 0.3, max_tokens=700)
            data = parse_analysis(result.text)
            self.db.update_chat(
                chat_id, analysis=json.dumps(data, ensure_ascii=False), analysis_at=time.time(),
                analysis_basis=basis,
            )
            _log(logging.INFO, "llm", "Analyse aktualisiert: %s, Phase %d (%s)", data["scam_type"],
                 data["stage"], result.usage.describe(), chat_id=chat_id)
        except LLMError as exc:
            _log(logging.WARNING, "llm", "Analyse fehlgeschlagen: %s", exc, chat_id=chat_id)
        except ValueError as exc:
            _log(logging.WARNING, "llm", "Analyse nicht lesbar: %s", exc, chat_id=chat_id)
        finally:
            if self.analyzing.get(chat_id) is asyncio.current_task():
                del self.analyzing[chat_id]

    # --- Status ----------------------------------------------------------------

    def chat_status(self, chat_id: int) -> dict:
        chat = self.db.chat(chat_id)
        settings = self.db.settings()
        last_id = self.db.last_message_id(chat_id)
        drafting = self.drafting.get(chat_id)
        analyzing = self.analyzing.get(chat_id)
        return {
            "now": time.time(),
            "enabled": bool(chat["enabled"]),
            "global_enabled": settings["global_enabled"],
            "mode": chat["mode"],
            "due_at": chat["due_at"],
            "sending": chat_id in self.sending,
            "generating": drafting is not None and not drafting.done(),
            "analyzing": analyzing is not None and not analyzing.done(),
            "draft_text": chat["draft_text"],
            "draft_edited": bool(chat["draft_edited"]),
            "draft_stale": bool(chat["draft_text"]) and (chat["draft_basis"] or 0) < last_id,
            "draft_at": chat["draft_at"],
            "instruction": chat["instruction"],
            "ai_today": self.db.ai_sent_today(chat),
            "daily_limit": settings["daily_limit"],
            "last_msg_id": self.db.last_message_id(chat_id, include_notes=True),
            "analysis_at": chat["analysis_at"],
        }

    def health(self) -> dict:
        stats = self.llm.stats
        return {
            "telegram_connected": self.client.is_connected(),
            "telegram_authorized": self.authorized,
            "me": display_name(self.me) if self.me else None,
            "llm_healthy": stats.healthy,
            "llm": stats,
            "llm_base_url": self.llm.base_url,
            "timers": len(self.timers),
            "drafting": sum(1 for t in self.drafting.values() if not t.done()),
            "sending": len(self.sending),
            "problems_24h": self.db.problem_count(time.time() - 86400),
        }

    async def test_model(self) -> tuple[bool, str]:
        settings = self.db.settings()
        started = time.monotonic()
        try:
            reply = await self.llm.chat(
                settings["model"], [{"role": "user", "content": "Antworte nur mit dem Wort: OK"}], 0.0,
                max_tokens=5,
            )
        except LLMError as exc:
            _log(logging.ERROR, "llm", "Modelltest fehlgeschlagen: %s", exc)
            return False, str(exc)
        ms = int((time.monotonic() - started) * 1000)
        message = f"Modell {settings['model']} antwortet in {ms} ms: {reply.strip()[:50]!r}"
        _log(logging.INFO, "llm", "Modelltest erfolgreich: %s", message)
        return True, message
