"""Einmalige Anmeldung bei Telegram (fragt Telefonnummer, Code und ggf. 2FA-Passwort ab)."""

import asyncio

from telethon import TelegramClient

from .config import load_config


async def main() -> None:
    config = load_config()
    client = TelegramClient(str(config.session_path), config.api_id, config.api_hash)
    await client.start()
    me = await client.get_me()
    print(f"Angemeldet als {me.first_name} (@{me.username or '-'}, id {me.id}).")
    print(f"Session gespeichert unter {config.session_path}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
