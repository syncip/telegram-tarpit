"""Konfiguration aus Umgebungsvariablen bzw. .env-Datei."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# In Docker liegt diese Datei immer im Wurzelverzeichnis des Containers
IN_DOCKER = Path("/.dockerenv").exists()
DOCKER_DATA_DIR = "/data"


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
    stt_base_url: str = ""
    stt_api_key: str = ""
    in_docker: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def media_dir(self) -> Path:
        path = self.data_dir / "media"
        path.mkdir(parents=True, exist_ok=True)
        return path

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

    warnings: list[str] = []
    raw_data_dir = os.environ.get("DATA_DIR", "").strip() or ("/data" if IN_DOCKER else "./data")
    if IN_DOCKER and raw_data_dir != DOCKER_DATA_DIR:
        # Alles außerhalb von /data liegt im Container selbst und ist nach dem nächsten
        # Neubau weg (Login, Personas, Bilder). Deshalb in Docker immer /data.
        warnings.append(
            f"DATA_DIR={raw_data_dir} wird in Docker ignoriert, gespeichert wird in {DOCKER_DATA_DIR} "
            f"(bitte die Zeile DATA_DIR aus der .env entfernen)"
        )
        raw_data_dir = DOCKER_DATA_DIR
    data_dir = Path(raw_data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    host = os.environ.get("HOST", "").strip() or ("0.0.0.0" if IN_DOCKER else "127.0.0.1")
    if IN_DOCKER and host in ("127.0.0.1", "localhost"):
        warnings.append("HOST=127.0.0.1 ist in Docker nicht erreichbar, verwende 0.0.0.0 "
                        "(welche Adressen Zugriff haben, regelt die ports-Zeile in docker-compose.yml)")
        host = "0.0.0.0"

    return Config(
        api_id=api_id,
        api_hash=_required("TG_API_HASH"),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        web_user=os.environ.get("WEB_USER", "admin"),
        web_password=web_password,
        data_dir=data_dir,
        host=host,
        port=int(os.environ.get("PORT", "8080")),
        notify_bot_token=os.environ.get("NOTIFY_BOT_TOKEN", "").strip(),
        notify_chat_id=os.environ.get("NOTIFY_CHAT_ID", "").strip(),
        public_url=os.environ.get("PUBLIC_URL", "").strip(),
        stt_base_url=os.environ.get("STT_BASE_URL", "").strip().rstrip("/"),
        stt_api_key=os.environ.get("STT_API_KEY", "").strip(),
        in_docker=IN_DOCKER,
        warnings=tuple(warnings),
    )


def storage_status(config: Config) -> dict:
    """Wird dauerhaft gespeichert? Für Warnbanner und Statusseite."""
    data_dir = config.data_dir
    persistent = (not config.in_docker) or os.path.ismount(data_dir)
    writable = os.access(data_dir, os.W_OK)

    def size(path: Path) -> int:
        try:
            if path.is_file():
                return path.stat().st_size
            return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        except OSError:
            return 0

    session = config.session_path.with_suffix(".session")
    return {
        "path": str(data_dir),
        "persistent": persistent,
        "writable": writable,
        "ok": persistent and writable,
        "in_docker": config.in_docker,
        "session_exists": session.exists(),
        "db_size": size(config.db_path),
        "media_size": size(data_dir / "media"),
    }
