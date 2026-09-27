"""Telegram-Anbindung und Tarpit-Logik."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl import functions, types
from telethon.tl.custom import Message
from telethon.tl.types import User

from .config import Config
from .db import Database
from .llm import LLMClient, LLMError
from .prompts import SKIP_TOKEN, build_messages
from .safety import check_reply, clean_reply, split_reply
from .timing import postpone_quiet_hours, sample_delay, typing_duration

log = logging.getLogger(__name__)

HISTORY_IMPORT_LIMIT = 50
QR_WAIT_SECONDS = 20  # QR-Tokens gelten ca. 30 Sekunden


@dataclass
class PendingReply:
    task: asyncio.Task
    due: float


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
        self.pending: dict[int, PendingReply] = {}
        self.me: User | None = None
        self.rng = random.Random()
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
        await self.client.connect()
        if await self.client.is_user_authorized():
            await self._after_login()
        else:
            log.warning("Telegram-Session ist nicht angemeldet. Login über das Webinterface.")

    async def _after_login(self, user: User | None = None) -> None:
        me = user or await self.client.get_me()
        if me is None:
            raise RuntimeError("Telegram meldet die Session als nicht angemeldet")
        self.me = me
        log.info("Angemeldet als %s (id %s)", display_name(me), me.id)
        try:
            await self.sync_dialogs()
        except Exception:
            log.exception("Chatliste konnte nicht geladen werden")
        # Was während der Downtime passiert ist, nachholen und ggf. antworten
        for chat in self.db.enabled_chats():
            try:
                await self.import_history(chat["chat_id"], limit=20)
            except Exception:
                log.exception("Konnte Verlauf von %s nicht nachladen", chat["chat_id"])
            self.maybe_schedule(chat["chat_id"])

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
        self.cancel_all()
        await self.client.log_out()
        self.me = None
        # log_out() trennt die Verbindung; für einen neuen Login wieder verbinden
        await self.client.connect()

    async def stop(self) -> None:
        self.cancel_qr_login()
        for pending in list(self.pending.values()):
            pending.task.cancel()
        self.pending.clear()
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
        self.maybe_schedule(event.chat_id)

    async def _on_outgoing(self, event: events.NewMessage.Event) -> None:
        # Von der KI gesendete Nachrichten landen auch hier. Kurz warten, bis
        # _send_reply sie gespeichert hat, und nur echte manuelle Nachrichten
        # (z. B. vom Handy) als 'me' übernehmen.
        await asyncio.sleep(2)
        msg: Message = event.message
        if self.db.has_tg_message(event.chat_id, msg.id):
            return
        if self.db.chat(event.chat_id) is None:
            chat = await event.get_chat()
            if isinstance(chat, User):
                self.db.upsert_chat(event.chat_id, display_name(chat), chat.username)
        self.db.add_message(event.chat_id, "me", describe_message(msg), msg.date.timestamp(), msg.id)

    # --- Steuerung ---------------------------------------------------------

    def can_reply(self, chat_id: int) -> bool:
        chat = self.db.chat(chat_id)
        return bool(
            chat is not None
            and chat["enabled"]
            and not chat["paused"]
            and self.db.settings()["global_enabled"]
        )

    def maybe_schedule(self, chat_id: int) -> None:
        """Plant eine Antwort, falls die KI zuständig ist und das Gegenüber zuletzt geschrieben hat."""
        if chat_id in self.pending or not self.can_reply(chat_id):
            return
        if self.db.last_sender(chat_id) != "them":
            return
        settings = self.db.settings()
        delay = sample_delay(settings["min_delay"], settings["max_delay"], self.rng)
        due = datetime.fromtimestamp(time.time() + delay)
        due = postpone_quiet_hours(due, settings["quiet_start"], settings["quiet_end"], self.rng)
        self._schedule(chat_id, due.timestamp())

    def reply_now(self, chat_id: int) -> None:
        self.cancel(chat_id)
        self._schedule(chat_id, time.time(), force=True)

    def cancel(self, chat_id: int) -> None:
        pending = self.pending.pop(chat_id, None)
        if pending:
            pending.task.cancel()

    def cancel_all(self) -> None:
        for chat_id in list(self.pending):
            self.cancel(chat_id)

    def reschedule_all(self) -> None:
        for chat in self.db.enabled_chats():
            self.maybe_schedule(chat["chat_id"])

    def _schedule(self, chat_id: int, due: float, force: bool = False) -> None:
        task = asyncio.create_task(self._reply_after(chat_id, due, force))
        self.pending[chat_id] = PendingReply(task=task, due=due)
        log.info("Antwort für %s geplant um %s", chat_id, datetime.fromtimestamp(due).strftime("%d.%m. %H:%M:%S"))

    async def _reply_after(self, chat_id: int, due: float, force: bool) -> None:
        # Bei cancel() wurde der Eintrag in self.pending schon entfernt bzw. ersetzt.
        await asyncio.sleep(max(0.0, due - time.time()))
        sent = False
        try:
            sent = await self._reply(chat_id, force)
        except Exception as exc:
            log.exception("Antwort für %s fehlgeschlagen", chat_id)
            self.db.add_message(chat_id, "note", f"Fehler: {exc}")
        finally:
            current = self.pending.get(chat_id)
            if current is not None and current.task is asyncio.current_task():
                del self.pending[chat_id]
        if sent:
            # Während wir "getippt" haben, kam evtl. schon die nächste Nachricht
            self.maybe_schedule(chat_id)

    # --- Antworten ---------------------------------------------------------

    async def _reply(self, chat_id: int, force: bool = False) -> bool:
        """Erzeugt und sendet eine Antwort. True, wenn etwas gesendet wurde."""
        chat = self.db.chat(chat_id)
        if chat is None:
            return False
        if not force and not self.can_reply(chat_id):
            return False
        if not force and self.db.last_sender(chat_id) != "them":
            return False  # du hast in der Zwischenzeit selbst geantwortet

        settings = self.db.settings()
        if self.db.ai_sent_today(chat) >= settings["daily_limit"]:
            self.db.add_message(chat_id, "note", "Tageslimit erreicht, keine Antwort.")
            return False

        persona = self.db.persona_for_chat(chat)
        if persona is None:
            self.db.add_message(chat_id, "note", "Keine Persona vorhanden.")
            return False

        history = self.db.messages(chat_id, limit=settings["history_limit"], include_notes=False)
        if not history:
            return False

        # "Nachricht lesen", kurz nachdenken
        await asyncio.sleep(self.rng.uniform(2, 15))
        await self.client.send_read_acknowledge(chat_id)

        reply = await self._generate(persona["prompt"], history, settings)
        if reply is None:
            return False
        if reply == SKIP_TOKEN:
            self.db.add_message(chat_id, "note", "KI lässt das Gegenüber bewusst zappeln ([SKIP]).")
            return False

        parts = split_reply(reply)
        for i, part in enumerate(parts):
            if i:
                await asyncio.sleep(self.rng.uniform(3, 20))
            async with self.client.action(chat_id, "typing"):
                await asyncio.sleep(typing_duration(part, self.rng))
            sent = await self.client.send_message(chat_id, part)
            self.db.add_message(chat_id, "ai", part, time.time(), sent.id)
            self.db.bump_ai_count(chat_id)
        return True

    async def _generate(self, persona_prompt: str, history, settings) -> str | None:
        messages = build_messages(persona_prompt, history)
        chat_id = history[-1]["chat_id"]
        for attempt in range(1, 3):
            try:
                raw = await self.llm.chat(settings["model"], messages, settings["temperature"])
            except LLMError as exc:
                self.db.add_message(chat_id, "note", str(exc))
                return None
            text = clean_reply(raw)
            if text == SKIP_TOKEN:
                return SKIP_TOKEN
            reason = check_reply(text)
            if reason is None:
                return text
            log.warning("Antwort blockiert (%s), Versuch %d: %r", reason, attempt, text)
            self.db.add_message(chat_id, "note", f"Blockiert ({reason}): {text}")
        return None

    async def send_manual(self, chat_id: int, text: str) -> None:
        sent = await self.client.send_message(chat_id, text)
        self.db.add_message(chat_id, "me", text, time.time(), sent.id)
        self.cancel(chat_id)
