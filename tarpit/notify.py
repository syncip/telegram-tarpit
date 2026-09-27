"""Benachrichtigt dich per Telegram, wenn etwas deine Aufmerksamkeit braucht.

Zwei Wege:
- Mit NOTIFY_BOT_TOKEN (eigener Bot von @BotFather): Der Bot schreibt dir, mit
  echter Push-Benachrichtigung. Einmalig den Bot öffnen und "Start" drücken.
- Ohne: Nachricht in deine "Gespeicherten Nachrichten". Kommt zuverlässig an,
  aber ohne Push, weil sie von dir selbst stammt.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot_token: str = "", chat_id: str = "", public_url: str = ""):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.public_url = public_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=20) if bot_token else None
        self.last_error: str | None = None

    @property
    def channel(self) -> str:
        return "Telegram-Bot (mit Push)" if self.bot_token else "Gespeicherte Nachrichten (ohne Push)"

    def link(self, path: str) -> str:
        return f"\n{self.public_url}{path}" if self.public_url else ""

    async def send(self, client, me_id: int | None, text: str) -> bool:
        text = "🕸 Telegram Tarpit\n" + text
        try:
            if self._http is not None:
                target = self.chat_id or (str(me_id) if me_id else "")
                if not target:
                    raise RuntimeError("Unbekannt, an wen die Benachrichtigung gehen soll")
                response = await self._http.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={"chat_id": target, "text": text, "disable_web_page_preview": True},
                )
                if response.status_code == 403 or (
                    response.status_code == 400 and "chat not found" in response.text
                ):
                    raise RuntimeError(
                        "Der Benachrichtigungs-Bot darf dir noch nicht schreiben: "
                        "öffne ihn in Telegram und drücke einmal auf Start"
                    )
                response.raise_for_status()
            else:
                await client.send_message("me", text, link_preview=False)
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Benachrichtigung fehlgeschlagen: %s", exc, extra={"source": "system"})
            return False
        self.last_error = None
        log.info("Benachrichtigung gesendet (%s)", self.channel, extra={"source": "system"})
        return True

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
