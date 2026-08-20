"""Parsen und Formatieren von Zeitdauern."""

from __future__ import annotations

import re

_UNITS = {
    "s": 1,
    "sec": 1,
    "sek": 1,
    "m": 60,
    "min": 60,
    "h": 3600,
    "std": 3600,
    "d": 86400,
    "t": 86400,
    "w": 604800,
}

_TOKEN = re.compile(r"(\d+)\s*([a-zA-Zä]+)")
_DISABLED = {"", "0", "off", "aus", "never", "nie", "none"}


class DurationError(ValueError):
    """Ungueltige Zeitdauer-Angabe."""


def parse_duration(value: object, *, context: str = "") -> int:
    """Wandelt Angaben wie ``"2h"``, ``"1d12h"``, ``"90min"`` in Sekunden um.

    Ganze Zahlen werden als Sekunden interpretiert. ``0`` / ``"off"`` / ``"nie"``
    ergeben 0 (= deaktiviert).
    """
    where = f" ({context})" if context else ""

    if isinstance(value, bool):
        raise DurationError(f"Zeitdauer darf kein Wahrheitswert sein{where}")
    if isinstance(value, int):
        if value < 0:
            raise DurationError(f"Zeitdauer darf nicht negativ sein{where}")
        return value
    if isinstance(value, float):
        if value < 0:
            raise DurationError(f"Zeitdauer darf nicht negativ sein{where}")
        return int(value)
    if not isinstance(value, str):
        raise DurationError(f"Zeitdauer muss Text oder Zahl sein, nicht {type(value).__name__}{where}")

    text = value.strip().lower().replace(" ", "")
    if text in _DISABLED:
        return 0

    if text.isdigit():
        return int(text)

    pos = 0
    total = 0
    found = False
    for match in _TOKEN.finditer(text):
        if match.start() != pos:
            raise DurationError(f"Zeitdauer '{value}' konnte nicht gelesen werden{where}")
        pos = match.end()
        amount, unit = match.groups()
        unit = unit.replace("ä", "a")
        factor = _UNITS.get(unit)
        if factor is None:
            raise DurationError(
                f"Unbekannte Zeiteinheit '{unit}' in '{value}'{where}. "
                f"Erlaubt: s, m/min, h, d, w"
            )
        total += int(amount) * factor
        found = True

    if not found or pos != len(text):
        raise DurationError(
            f"Zeitdauer '{value}' konnte nicht gelesen werden{where}. "
            f"Beispiele: '30m', '4h', '1d12h', '2w'"
        )
    return total


def format_duration(seconds: float) -> str:
    """Formatiert Sekunden als deutsche Klartext-Dauer, z. B. ``2 Tage 3 Std.``."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} Sek."

    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60

    parts: list[str] = []
    if days:
        parts.append(f"{days} Tag" if days == 1 else f"{days} Tage")
    if hours:
        parts.append(f"{hours} Std.")
    if minutes and not days:
        parts.append(f"{minutes} Min.")
    return " ".join(parts) if parts else "0 Min."
