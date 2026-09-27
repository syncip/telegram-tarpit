"""Konfiguration aus Umgebungsvariablen bzw. .env-Datei."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    llm_api_key: str
    llm_base_url: str
    web_user: str
    web_password: str
    data_dir: Path
    host: str
    port: int
    notify_bot_token: str = ""
    notify_chat_id: str = ""
    public_url: str = ""

    @property
    def session_path(self) -> Path:
        return self.data_dir / "telegram"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tarpit.db"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Umgebungsvariable {name} fehlt (siehe .env.example)")
    return value


def load_config() -> Config:
    load_dotenv()
    try:
        api_id = int(_required("TG_API_ID"))
    except ValueError as exc:
        raise ConfigError("TG_API_ID muss eine Zahl sein") from exc

    web_password = _required("WEB_PASSWORD")
    if web_password == "bitte-aendern":
        raise ConfigError("Bitte WEB_PASSWORD in der .env ändern")

    data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        api_id=api_id,
        api_hash=_required("TG_API_HASH"),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        web_user=os.environ.get("WEB_USER", "admin"),
        web_password=web_password,
        data_dir=data_dir,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
        notify_bot_token=os.environ.get("NOTIFY_BOT_TOKEN", "").strip(),
        notify_chat_id=os.environ.get("NOTIFY_CHAT_ID", "").strip(),
        public_url=os.environ.get("PUBLIC_URL", "").strip(),
    )
