"""Logging fuer den unbeaufsichtigten Betrieb in der Aufgabenplanung."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import LoggingConfig

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def setup_logging(config: LoggingConfig, *, level_override: str | None = None) -> list[str]:
    """Richtet Datei- und Konsolen-Logging ein; liefert nicht-fatale Warnungen zurueck."""
    warnings: list[str] = []
    level = getattr(logging, (level_override or config.level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    if config.console:
        console = logging.StreamHandler(stream=sys.stdout)
        console.setFormatter(formatter)
        console.setLevel(level)
        root.addHandler(console)

    if config.file:
        try:
            config.file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                config.file,
                maxBytes=config.max_bytes,
                backupCount=config.backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            root.addHandler(file_handler)
        except OSError as exc:
            warnings.append(
                f"Protokolldatei '{config.file}' konnte nicht geoeffnet werden ({exc}) - "
                f"es wird nur auf die Konsole protokolliert"
            )
            if not config.console:
                console = logging.StreamHandler(stream=sys.stdout)
                console.setFormatter(formatter)
                root.addHandler(console)

    return warnings


def setup_basic_logging(level: str = "INFO") -> None:
    """Vorlaeufiges Logging, bevor die Konfiguration gelesen wurde."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
