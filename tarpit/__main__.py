"""Startet Webinterface und Telegram-Client: ``python -m tarpit``."""

import logging
import sys

import uvicorn

from .config import ConfigError, load_config
from .web import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        config = load_config()
    except ConfigError as exc:
        sys.exit(f"Konfigurationsfehler: {exc}")
    app = create_app(config)
    log = logging.getLogger("tarpit")
    log.info("Webinterface: http://%s:%s", config.host, config.port)
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")


if __name__ == "__main__":
    main()
