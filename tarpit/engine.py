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
from datetime import date, datetime
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl import functions, types
from telethon.tl.custom import Message
from telethon.tl.types import User

from .analysis import build_analysis_messages, parse_analysis
from .config import Config
from .db import Database
from .llm import ChatResult, LLMClient, LLMError, Usage
from .media import (
    PERSONA_IMAGE_PROMPT, VISION_PROMPT as VISION_PROMPT_FOR_CHAT, asks_for_photo, build_stt_messages, build_vision_messages, image_marker_ids,
    strip_image_markers,
)
from .notify import Notifier
from .prompts import SKIP_TOKEN, build_messages, build_opening_messages
from .referrals import Candidate, extract_candidates, normalize_phone
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
    elif msg.contact:
        c = msg.contact
        name = " ".join(filter(None, [c.first_name, c.last_name]))
        kind = f"[Kontakt: {name} {c.phone_number or ''}]".replace("  ", " ")
    elif msg.document:
        kind = "[Datei]"
    parts = [p for p in (kind, msg.message) if p]
    return " ".join(parts) or "[Nachricht ohne Text]"


def message_entity_candidates(msg: Message) -> list[Candidate]:
    """Weiterleitungen, die nicht im Text stehen: Erwähnung mit Nutzer-ID, Link hinter Text, geteilter Kontakt."""
    found: list[Candidate] = []
    for entity in msg.entities or []:
        if isinstance(entity, types.MessageEntityMentionName):
            found.append(Candidate("user_id", str(entity.user_id)))
        elif isinstance(entity, types.MessageEntityTextUrl):
            found.extend(extract_candidates(entity.url))
    if msg.contact:
        if msg.contact.user_id:
            found.append(Candidate("user_id", str(msg.contact.user_id)))
        elif msg.contact.phone_number and (phone := normalize_phone("+" + msg.contact.phone_number.lstrip("+"))):
            found.append(Candidate("phone", phone))
    return found


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
        self.notifier = Notifier(config.notify_bot_token, config.notify_chat_id, config.public_url)
        self.me: User | None = None
        self.rng = random.Random()
        # laufende Hintergrund-Aufgaben pro Chat
        self.timers: dict[int, asyncio.Task] = {}      # wartet bis due_at, sendet dann
        self.drafting: dict[int, asyncio.Task] = {}    # erzeugt (bald) einen Entwurf
        self._draft_start: dict[int, float] = {}       # wann der geplante Entwurf startet
        self.analyzing: dict[int, asyncio.Task] = {}   # erstellt gerade eine Analyse
        self.sending: set[int] = set()                 # tippt/sendet gerade
        self.draft_failed: dict[int, int] = {}         # Chat -> Nachrichten-ID, für die der Entwurf scheiterte
        self._limit_notified: str | None = None        # Tag, an dem über das Token-Limit informiert wurde
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
        await self.notifier.aclose()
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

    def _is_own_chat(self, chat_id: int) -> bool:
        return self.me is not None and chat_id == self.me.id

    async def _on_incoming(self, event: events.NewMessage.Event) -> None:
        if self._is_own_chat(event.chat_id):
            return
        sender = await event.get_sender()
        if not isinstance(sender, User) or sender.bot:
            return
        msg: Message = event.message
        self.db.upsert_chat(event.chat_id, display_name(sender), sender.username, msg.date.timestamp())
        text = describe_message(msg)
        understood = await self._understand_media(event.chat_id, msg)
        if understood:
            text = understood
        self.db.add_message(event.chat_id, "them", text, msg.date.timestamp(), msg.id)
        candidates = extract_candidates(msg.message or "") + message_entity_candidates(msg)
        if candidates:
            # vor der normalen Antwortplanung, damit der Chat ggf. zuerst auf Freigabe gestellt wird
            self.on_referral_candidates(event.chat_id, candidates)
        self.on_scammer_message(event.chat_id)

    async def _on_outgoing(self, event: events.NewMessage.Event) -> None:
        # Von der KI gesendete Nachrichten landen auch hier. Kurz warten, bis
        # _send_reply sie gespeichert hat, und nur echte manuelle Nachrichten
        # (z. B. vom Handy) als 'me' übernehmen.
        if self._is_own_chat(event.chat_id):
            return  # z. B. Benachrichtigungen in "Gespeicherte Nachrichten"
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
        last = self.db.last_sender(chat_id)
        if last is None and chat["draft_text"]:
            # neuer Kontakt aus einer Weiterleitung: erste Nachricht steht bereit
            if chat["mode"] == "auto":
                self.ensure_due(chat_id)
            return
        if last != "them":
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
            if type(exc).__name__ == "PeerFloodError":
                message = ("Telegram hat das Anschreiben neuer Kontakte vorübergehend gesperrt (Spam-Schutz). "
                           "Bitte ein paar Stunden warten.")
            else:
                message = f"Fehler beim Senden: {exc}"
            _log(logging.ERROR, "engine", "%s", message, chat_id=chat_id, exc_info=True)
            self.db.add_message(chat_id, "note", message)
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
        text = await self._generate(
            chat_id, persona["prompt"], history, settings, chat["instruction"], chat["background"]
        )
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
            last = self.db.last_sender(chat_id)
            opening = last is None and bool(chat["draft_text"])
            if not self.is_active(chat) or chat["mode"] != "auto" or (last != "them" and not opening):
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
            allowed = self.sendable_images(chat_id)
            for i, part in enumerate(parts):
                if i:
                    await asyncio.sleep(self.rng.uniform(1, 3) if instant else self.rng.uniform(3, 20))
                caption = strip_image_markers(part)
                image_ids = [n for n in image_marker_ids(part) if n in allowed]
                for n in image_marker_ids(part):
                    if n not in allowed:
                        _log(logging.WARNING, "engine", "Bild %d nicht verfügbar oder schon geschickt, übersprungen",
                             n, chat_id=chat_id)
                if image_ids:
                    for k, image_id in enumerate(image_ids):
                        image = allowed.pop(image_id)
                        text_for_image = caption if k == 0 else ""
                        async with self.client.action(chat_id, "photo"):
                            await asyncio.sleep(self.rng.uniform(1, 2) if instant else self.rng.uniform(3, 12))
                        sent = await self.client.send_file(
                            chat_id, str(self.image_path(image)), caption=text_for_image or None
                        )
                        stored = f"[Bild: {image['description'] or 'Foto'}]" + (f" {text_for_image}" if text_for_image else "")
                        self.db.add_message(chat_id, "ai", stored, time.time(), sent.id, edited=edited,
                                            image_id=image_id)
                        self.db.bump_ai_count(chat_id)
                        sent_any = True
                    continue
                if not caption:
                    continue
                await self._type(chat_id, caption, instant)
                sent = await self.client.send_message(chat_id, caption)
                self.db.add_message(chat_id, "ai", caption, time.time(), sent.id, edited=edited)
                self.db.bump_ai_count(chat_id)
                sent_any = True
            _log(logging.INFO, "telegram", "KI-Antwort gesendet (%d Nachricht%s%s)", len(parts),
                 "en" if len(parts) > 1 else "", ", von dir bearbeitet" if edited else "", chat_id=chat_id)
            self.db.update_chat(
                chat_id, due_at=None, draft_text=None, draft_edited=0, draft_basis=None,
                draft_at=None, instruction=None,
            )
            for ref in self.db.referrals(target_chat_id=chat_id):
                if ref["status"] in ("scheduled", "drafted"):
                    self.db.update_referral(ref["id"], status="contacted", reason="erste Nachricht gesendet")
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

    # --- KI-Aufrufe (zentral: Tageslimit und Verbrauch) -----------------------

    def tokens_today(self) -> int:
        return self.db.tokens_on(date.today().isoformat())

    async def _check_token_limit(self, purpose: str) -> None:
        limit = self.db.settings()["daily_token_limit"]
        if limit and self.tokens_today() >= limit:
            today = date.today().isoformat()
            if self._limit_notified != today:
                self._limit_notified = today
                _log(logging.WARNING, "llm", "Token-Tageslimit von %s erreicht, KI pausiert bis morgen", limit)
                if self.db.settings()["notify_enabled"]:
                    await self.notifier.send(
                        self.client, self.me.id if self.me else None,
                        f"⛽ Token-Tageslimit von {limit:,} erreicht. Die KI pausiert bis Mitternacht."
                        .replace(",", ".") + self.notifier.link("/verbrauch"),
                    )
            raise LLMError(f"Token-Tageslimit von {limit} erreicht ({purpose} nicht ausgeführt)")

    def _record_usage(self, model: str, purpose: str, usage: Usage, chat_id: int | None) -> None:
        cost = usage.cost
        settings = self.db.settings()
        if not cost and (settings["price_input_per_m"] or settings["price_output_per_m"]):
            cost = (usage.prompt * settings["price_input_per_m"]
                    + usage.completion * settings["price_output_per_m"]) / 1_000_000
        self.db.add_usage(model, purpose, usage.prompt, usage.cached, usage.completion, cost, chat_id)

    async def _llm(
        self, purpose: str, model: str, messages: list[dict], temperature: float,
        max_tokens: int | None = None, chat_id: int | None = None,
    ) -> ChatResult:
        await self._check_token_limit(purpose)
        result = await self.llm.complete(model, messages, temperature, max_tokens)
        self._record_usage(model, purpose, result.usage, chat_id)
        return result

    async def _generate(
        self, chat_id: int, persona_prompt: str, history, settings, instruction: str | None = None,
        background: str | None = None,
    ) -> str | None:
        chat = self.db.chat(chat_id)
        persona = self.db.persona_for_chat(chat) if chat is not None else None
        images = [(i["id"], i["description"]) for i in self.db.persona_images(persona["id"])] if persona else []
        last = history[-1] if history else None
        messages = build_messages(
            persona_prompt, history, instruction=instruction, background=background, images=images,
            sent_images=self.db.sent_image_ids(chat_id) & {i for i, _ in images},
            photo_request=bool(images and last is not None and last["sender"] == "them" and asks_for_photo(last["text"])),
        )
        return await self._generate_from(chat_id, messages, settings)

    async def _generate_from(self, chat_id: int, messages: list[dict[str, str]], settings) -> str | None:
        for attempt in range(1, 3):
            try:
                result = await self._llm(
                    "reply", settings["model"], messages, settings["temperature"],
                    max_tokens=settings["max_reply_tokens"] or None, chat_id=chat_id,
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
            reason = check_reply(strip_image_markers(text) or "…")
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
            result = await self._llm("analysis", model, build_analysis_messages(history), 0.3,
                                     max_tokens=700, chat_id=chat_id)
            data = parse_analysis(result.text)
            now = time.time()
            self.db.update_chat(
                chat_id, analysis=json.dumps(data, ensure_ascii=False), analysis_at=now,
                analysis_basis=basis,
            )
            self.db.add_analysis_snapshot(chat_id, data, basis, now)
            _log(logging.INFO, "llm", "Analyse aktualisiert: %s, Phase %d (%s)", data["scam_type"],
                 data["stage"], result.usage.describe(), chat_id=chat_id)
        except LLMError as exc:
            _log(logging.WARNING, "llm", "Analyse fehlgeschlagen: %s", exc, chat_id=chat_id)
        except ValueError as exc:
            _log(logging.WARNING, "llm", "Analyse nicht lesbar: %s", exc, chat_id=chat_id)
        finally:
            if self.analyzing.get(chat_id) is asyncio.current_task():
                del self.analyzing[chat_id]

    # --- Bilder & Sprache --------------------------------------------------------

    def image_path(self, image) -> Path:
        return self.config.media_dir / image["filename"]

    def sendable_images(self, chat_id: int) -> dict[int, object]:
        """Fotos der Persona dieses Chats, die dort noch nicht geschickt wurden."""
        chat = self.db.chat(chat_id)
        persona = self.db.persona_for_chat(chat) if chat is not None else None
        if persona is None:
            return {}
        sent = self.db.sent_image_ids(chat_id)
        return {
            i["id"]: i for i in self.db.persona_images(persona["id"])
            if i["id"] not in sent and self.image_path(i).exists()
        }

    async def describe_image(self, image: bytes, prompt: str, chat_id: int | None = None) -> str | None:
        """Bilderkennung; None, wenn kein Bildmodell eingestellt ist."""
        model = self.db.settings()["vision_model"]
        if not model:
            return None
        result = await self._llm("vision", model, build_vision_messages(image, prompt), 0.2,
                                 max_tokens=300, chat_id=chat_id)
        _log(logging.INFO, "llm", "Bild erkannt: %s", result.usage.describe(), chat_id=chat_id)
        return " ".join(result.text.split())

    async def transcribe(self, audio: bytes, chat_id: int | None = None) -> str | None:
        """Spracherkennung; None, wenn kein Sprachmodell eingestellt ist."""
        settings = self.db.settings()
        model = settings["stt_model"]
        if not model:
            return None
        if settings["stt_backend"] == "whisper":
            await self._check_token_limit("stt")
            text = await self.llm.transcribe(
                model, audio, base_url=self.config.stt_base_url or None,
                api_key=self.config.stt_api_key or self.config.llm_api_key or None,
            )
            self.db.add_usage(model, "stt", 0, 0, 0, 0.0, chat_id)
        else:
            result = await self._llm("stt", model, build_stt_messages(audio, "ogg"), 0.0,
                                     max_tokens=800, chat_id=chat_id)
            text = result.text
        _log(logging.INFO, "llm", "Sprachnachricht transkribiert (%d Zeichen)", len(text), chat_id=chat_id)
        return " ".join(text.split())

    async def _understand_media(self, chat_id: int, msg: Message) -> str | None:
        """Wandelt Fotos und Sprachnachrichten in Text um, damit die KI darauf eingehen kann.

        Nur in Chats, die die KI übernommen hat (kostet Tokens).
        """
        chat = self.db.chat(chat_id)
        if not self.is_active(chat):
            return None
        settings = self.db.settings()
        is_image = bool(msg.photo) or bool(
            msg.document and (msg.file.mime_type or "").startswith("image/") and not msg.sticker
        )
        is_voice = bool(msg.voice or msg.audio or msg.video_note)
        if not ((is_image and settings["vision_model"]) or (is_voice and settings["stt_model"])):
            return None
        caption = f" {msg.message}" if msg.message else ""
        try:
            data = await asyncio.wait_for(self.client.download_media(msg, file=bytes), timeout=60)
            if is_image:
                description = await asyncio.wait_for(self.describe_image(data, VISION_PROMPT_FOR_CHAT, chat_id), 120)
                return f"[Foto: {description}]{caption}" if description else None
            transcript = await asyncio.wait_for(self.transcribe(data, chat_id), 180)
            return f"[Sprachnachricht: „{transcript}“]{caption}" if transcript else None
        except Exception as exc:
            _log(logging.WARNING, "llm", "%s konnte nicht erkannt werden: %s",
                 "Bild" if is_image else "Sprachnachricht", exc, chat_id=chat_id)
            return None

    async def describe_persona_image(self, image: bytes) -> str | None:
        try:
            return await self.describe_image(image, PERSONA_IMAGE_PROMPT)
        except LLMError as exc:
            _log(logging.WARNING, "llm", "Bildbeschreibung fehlgeschlagen: %s", exc)
            return None

    # --- Weiterleitungen ("Adde sie", "Schreib meinem Manager") ---------------

    def on_referral_candidates(self, chat_id: int, candidates: list[Candidate]) -> list[int]:
        """Scammer will, dass du jemand anderen anschreibst. Gibt die neuen Weiterleitungs-IDs zurück."""
        chat = self.db.chat(chat_id)
        settings = self.db.settings()
        if not self.is_active(chat) or settings["referral_mode"] == "off":
            return []
        auto = settings["referral_mode"] == "auto" and chat["mode"] != "manual"
        new_ids = []
        for candidate in candidates:
            ref_id = self.db.add_referral(chat_id, candidate.kind, candidate.value,
                                          "pending" if auto else "proposed")
            if ref_id is not None:
                new_ids.append(ref_id)
        if not new_ids:
            return []
        labels = ", ".join(c.label for c in candidates)
        _log(logging.WARNING, "engine", "Weiterleitung erkannt: soll %s anschreiben", labels, chat_id=chat_id)
        self.db.add_message(chat_id, "note", f"↪ Weiterleitung erkannt: {labels}")
        paused = False
        if settings["referral_pause_source"] and chat["mode"] == "auto":
            self.set_mode(chat_id, "review")
            paused = True
        asyncio.create_task(self._process_referrals(chat_id, new_ids, auto, paused))
        return new_ids

    async def _process_referrals(self, chat_id: int, ref_ids: list[int], auto: bool, paused: bool) -> None:
        chat = self.db.chat(chat_id)
        lines = [f"„{chat['title']}“ möchte, dass du jemand Neues anschreibst."]
        if paused:
            lines.append("⏸ Der Chat wartet jetzt auf deine Freigabe (Modus „KI schlägt vor“).")
        for ref_id in ref_ids:
            ref = self.db.referral(ref_id)
            label = Candidate(ref["kind"], ref["target"]).label
            if auto:
                try:
                    lines.append(await self.contact_referral(ref_id))
                except Exception as exc:
                    _log(logging.ERROR, "engine", "Weiterleitung an %s fehlgeschlagen: %s", label, exc,
                         chat_id=chat_id, exc_info=True)
                    self.db.update_referral(ref_id, status="failed", reason=str(exc))
                    lines.append(f"❌ {label}: {exc}")
            else:
                lines.append(f"❓ {label}: wartet auf deine Entscheidung (anschreiben oder ignorieren)")
        if self.db.settings()["notify_enabled"]:
            text = "\n".join(lines) + self.notifier.link(f"/chats/{chat_id}")
            await self.notifier.send(self.client, self.me.id if self.me else None, text)

    async def accept_referral(self, ref_id: int) -> str:
        """Du hast eine vorgeschlagene Weiterleitung bestätigt."""
        return await self.contact_referral(ref_id, approved=True)

    def ignore_referral(self, ref_id: int) -> None:
        self.db.update_referral(ref_id, status="skipped", reason="von dir ignoriert")

    async def contact_referral(self, ref_id: int, approved: bool = False) -> str:
        """Legt den neuen Kontakt an und bereitet die erste Nachricht vor. Gibt eine Statuszeile zurück."""
        ref = self.db.referral(ref_id)
        source = self.db.chat(ref["source_chat_id"])
        settings = self.db.settings()
        label = Candidate(ref["kind"], ref["target"]).label

        def skip(reason: str, status: str = "skipped", target_chat_id: int | None = None) -> str:
            fields = {"status": status, "reason": reason}
            if target_chat_id is not None:
                fields["target_chat_id"] = target_chat_id
            self.db.update_referral(ref_id, **fields)
            _log(logging.WARNING, "engine", "Weiterleitung an %s nicht ausgeführt: %s", label, reason,
                 chat_id=source["chat_id"])
            return f"⏭ {label}: {reason}"

        recent = self.db.referrals_contacted_since(time.time() - 86400, exclude_id=ref_id)
        if not approved and recent >= settings["referral_daily_limit"]:
            return skip(f"Tageslimit von {settings['referral_daily_limit']} neuen Kontakten erreicht")

        entity, imported = await self._resolve_referral(ref)
        if entity is None:
            return skip("nicht bei Telegram gefunden", status="failed")
        if not isinstance(entity, User) or entity.bot or entity.deleted or entity.is_self:
            return skip("kein normaler Nutzer (Bot, Kanal oder Gruppe)")
        if entity.id == source["chat_id"]:
            return skip("das ist der Scammer selbst")
        existing = self.db.chat(entity.id)
        if existing is not None and existing["enabled"]:
            return skip("wird bereits von der KI bearbeitet", target_chat_id=entity.id)
        if existing is not None and self.db.message_count(entity.id) > 0:
            return skip("bestehender Chat, wird zum Schutz nicht angeschrieben")
        if entity.contact and not imported:
            return skip("steht in deinen Kontakten, wird zum Schutz nicht angeschrieben")

        send_auto = approved or settings["referral_mode"] == "auto"
        self.db.upsert_chat(entity.id, display_name(entity), entity.username, time.time())
        self.db.update_chat(
            entity.id, enabled=True, mode="auto" if send_auto else "review",
            persona_id=source["persona_id"], referred_from=source["chat_id"],
            background=self._referral_background(source),
        )
        self.db.update_referral(ref_id, target_chat_id=entity.id, status="pending")
        text = await self._make_opening(entity.id, source["title"], label)
        if text is None:
            self.db.update_referral(ref_id, status="failed", reason="erste Nachricht konnte nicht erzeugt werden")
            return f"❌ {label}: erste Nachricht konnte nicht erzeugt werden"
        self.db.add_message(source["chat_id"], "note", f"↪ Neuer Chat mit {display_name(entity)} ({label}) angelegt")
        if send_auto:
            due = time.time() + self.rng.uniform(settings["referral_min_delay"], settings["referral_max_delay"])
            self.db.update_chat(entity.id, due_at=due)
            self.ensure_due(entity.id)
            self.db.update_referral(ref_id, status="scheduled", reason="erste Nachricht geplant")
            when = datetime.fromtimestamp(due).strftime("%H:%M:%S")
            _log(logging.INFO, "engine", "Weiterleitung an %s: erste Nachricht geplant um %s", label, when,
                 chat_id=entity.id)
            return f"✅ {label}: neuer Chat angelegt, erste Nachricht um {when}"
        self.db.update_referral(ref_id, status="drafted", reason="erste Nachricht wartet auf Freigabe")
        return f"👀 {label}: neuer Chat angelegt, erste Nachricht wartet auf deine Freigabe"

    async def _resolve_referral(self, ref) -> tuple[object | None, bool]:
        """Findet den Telegram-Nutzer. Telefonnummern werden dafür als Kontakt importiert."""
        kind, target = ref["kind"], ref["target"]
        if kind == "phone":
            source = self.db.chat(ref["source_chat_id"])
            result = await self.client(functions.contacts.ImportContactsRequest([
                types.InputPhoneContact(
                    client_id=self.rng.randrange(1, 2**31), phone=target,
                    first_name="🕸 Tarpit", last_name=f"via {source['title']}"[:60],
                )
            ]))
            users = getattr(result, "users", None) or []
            return (users[0] if users else None), True
        try:
            return await self.client.get_entity(int(target) if kind == "user_id" else target), False
        except (ValueError, TypeError):
            return None, False

    def _referral_background(self, source) -> str:
        analysis = self.db.analysis(source)
        if analysis and analysis.get("summary"):
            story = f"{analysis['scam_type']}: {analysis['summary']}"
        else:
            lines = [
                f"{'Scammer' if m['sender'] == 'them' else 'Du'}: {m['text']}"
                for m in self.db.messages(source["chat_id"], limit=8, include_notes=False)
            ]
            story = " / ".join(lines)
        return (f"„{source['title']}“ hat dich an diese Person verwiesen. "
                f"Bisheriger Stand mit „{source['title']}“: {story}")[:900]

    async def _make_opening(self, chat_id: int, source_title: str, target_label: str) -> str | None:
        chat = self.db.chat(chat_id)
        persona = self.db.persona_for_chat(chat)
        if persona is None:
            return None
        messages = build_opening_messages(persona["prompt"], chat["background"], source_title, target_label)
        text = await self._generate_from(chat_id, messages, self.db.settings())
        if text is None or text.strip() == SKIP_TOKEN:
            return None
        self.db.update_chat(chat_id, draft_text=text, draft_edited=0, draft_basis=0, draft_at=time.time())
        _log(logging.INFO, "llm", "Erste Nachricht für neuen Kontakt erstellt", chat_id=chat_id)
        return text

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
            "referred_from": chat["referred_from"],
            "draft_images": self._draft_images(chat_id, chat["draft_text"]),
        }

    def _draft_images(self, chat_id: int, draft: str | None) -> list[dict]:
        if not draft:
            return []
        allowed = self.sendable_images(chat_id)
        result = []
        for image_id in image_marker_ids(draft):
            image = self.db.persona_image(image_id)
            result.append({
                "id": image_id,
                "url": f"/media/images/{image_id}" if image else None,
                "description": image["description"] if image else "unbekanntes Bild",
                "ok": image_id in allowed,
            })
        return result

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
            "notify_channel": self.notifier.channel,
            "tokens_today": self.tokens_today(),
            "token_limit": self.db.settings()["daily_token_limit"],
            "notify_error": self.notifier.last_error,
        }

    async def test_model(self) -> tuple[bool, str]:
        settings = self.db.settings()
        started = time.monotonic()
        try:
            reply = (await self._llm(
                "other", settings["model"], [{"role": "user", "content": "Antworte nur mit dem Wort: OK"}], 0.0,
                max_tokens=5,
            )).text
        except LLMError as exc:
            _log(logging.ERROR, "llm", "Modelltest fehlgeschlagen: %s", exc)
            return False, str(exc)
        ms = int((time.monotonic() - started) * 1000)
        message = f"Modell {settings['model']} antwortet in {ms} ms: {reply.strip()[:50]!r}"
        _log(logging.INFO, "llm", "Modelltest erfolgreich: %s", message)
        return True, message
