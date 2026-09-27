"""Dauerhafte Speicherung: Datenverzeichnis in Docker, Warnung ohne Volume, Neustart, Abmelden."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from telethon import TelegramClient

from tarpit import config as config_mod
from tarpit import web
from tarpit.config import Config, load_config, storage_status
from tarpit.db import Database
from tarpit.engine import Tarpit

from .test_web import WebTarpit


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    monkeypatch.setenv("WEB_PASSWORD", "geheim")
    monkeypatch.setattr(config_mod, "load_dotenv", lambda: None)
    return monkeypatch


def test_docker_ignores_relative_data_dir_and_localhost(env, tmp_path):
    docker_data = tmp_path / "volume"
    env.setattr(config_mod, "IN_DOCKER", True)
    env.setattr(config_mod, "DOCKER_DATA_DIR", str(docker_data))
    env.setenv("DATA_DIR", "./data")
    env.setenv("HOST", "127.0.0.1")
    cfg = load_config()
    assert cfg.data_dir == docker_data.resolve() and cfg.in_docker
    assert cfg.host == "0.0.0.0"
    assert any("DATA_DIR=./data wird in Docker ignoriert" in w for w in cfg.warnings)
    assert cfg.session_path.parent == cfg.db_path.parent == cfg.data_dir  # alles im Volume


def test_without_docker_data_dir_is_respected(env, tmp_path):
    env.setattr(config_mod, "IN_DOCKER", False)
    env.setenv("DATA_DIR", str(tmp_path / "eigen"))
    env.delenv("HOST", raising=False)
    cfg = load_config()
    assert cfg.data_dir == (tmp_path / "eigen").resolve() and cfg.host == "127.0.0.1" and not cfg.warnings


def test_storage_warning_when_not_mounted(tmp_path, monkeypatch):
    cfg = Config(1, "h", "", "http://x", "admin", "geheim", tmp_path, "0.0.0.0", 8080, in_docker=True)
    status = storage_status(cfg)
    assert status["persistent"] is False and status["ok"] is False  # tmp_path ist kein Mount

    monkeypatch.setattr(web, "Tarpit", WebTarpit)
    with TestClient(web.create_app(cfg)) as client:
        page = client.get("/", auth=("admin", "geheim")).text
        assert "Daten werden nicht dauerhaft gespeichert" in page
        logs = client.get("/logs?level=problems", auth=("admin", "geheim")).text
        assert "kein eingebundenes Volume" in logs

    ok = Config(1, "h", "", "http://x", "admin", "geheim", tmp_path, "0.0.0.0", 8080, in_docker=False)
    assert storage_status(ok)["ok"] is True


def test_everything_survives_restart(tmp_path):
    db = Database(tmp_path / "tarpit.db")
    pid = db.save_persona(None, "Oma Erna", "Du bist Erna.")
    image_id = db.add_persona_image(pid, "erna.jpg", "Erna mit Hut")
    db.set_setting("model", "gemma3:4b")
    db.upsert_chat(5, "Scammer", None)
    db.update_chat(5, enabled=True, instruction="frag nach dem Hund", mode="review")
    db.close()

    db = Database(tmp_path / "tarpit.db")  # "Neustart"
    assert db.persona(pid)["name"] == "Oma Erna"
    assert db.persona_image(image_id)["description"] == "Erna mit Hut"
    assert db.settings()["model"] == "gemma3:4b"
    chat = db.chat(5)
    assert chat["enabled"] == 1 and chat["instruction"] == "frag nach dem Hund" and chat["mode"] == "review"


def test_logout_creates_fresh_client(tmp_path, monkeypatch):
    connects = []

    async def fake_connect(self):
        connects.append(self)  # kein echter Netzwerkzugriff im Test

    monkeypatch.setattr(TelegramClient, "connect", fake_connect)
    cfg = Config(1, "h", "", "http://x", "a", "b", tmp_path, "127.0.0.1", 8080)
    t = Tarpit(cfg, Database(tmp_path / "t.db"))

    class LoggingOutClient:
        async def log_out(self):
            self.session = None  # wie Telethon: Session gelöscht, Client unbrauchbar

    old = LoggingOutClient()
    t.client = old
    t.me = object()
    asyncio.run(t.logout())
    assert t.me is None and t.client is not old and isinstance(t.client, TelegramClient)
    assert t.client.session is not None
    assert len(t.client.list_event_handlers()) == 2  # Nachrichten-Handler wieder registriert
    assert connects == [t.client]  # bereit für den nächsten Login
