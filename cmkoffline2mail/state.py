"""Persistenter Zustand, damit nicht bei jedem Lauf erneut gemailt wird."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

LOG = logging.getLogger(__name__)

STATE_VERSION = 1

#: Toleranz beim Vergleich des Offline-Zeitpunkts (Livestatus-Werte koennen leicht schwanken).
OFFLINE_SINCE_TOLERANCE = 120


@dataclass
class HostState:
    offline_since: int
    last_notified: int = 0
    notify_count: int = 0
    folder: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "offline_since": self.offline_since,
            "last_notified": self.last_notified,
            "notify_count": self.notify_count,
            "folder": self.folder,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HostState":
        return cls(
            offline_since=int(data.get("offline_since", 0)),
            last_notified=int(data.get("last_notified", 0)),
            notify_count=int(data.get("notify_count", 0)),
            folder=str(data.get("folder", "")),
        )


@dataclass
class NotificationState:
    """Merkt sich pro Host, wann zuletzt benachrichtigt wurde."""

    path: Path
    hosts: dict[str, HostState] = field(default_factory=dict)
    last_run: int = 0

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "NotificationState":
        state_path = Path(path)
        state = cls(path=state_path)
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            LOG.info("Keine Statusdatei gefunden - erster Lauf (%s)", state_path)
            return state
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning(
                "Statusdatei '%s' konnte nicht gelesen werden (%s) - es wird neu begonnen. "
                "Dadurch kann es einmalig zu wiederholten Benachrichtigungen kommen.",
                state_path,
                exc,
            )
            return state

        if not isinstance(raw, dict):
            LOG.warning("Statusdatei '%s' hat ein unerwartetes Format - es wird neu begonnen", state_path)
            return state

        state.last_run = int(raw.get("last_run", 0) or 0)
        hosts = raw.get("hosts")
        if isinstance(hosts, dict):
            for name, entry in hosts.items():
                if isinstance(entry, dict):
                    try:
                        state.hosts[str(name)] = HostState.from_dict(entry)
                    except (TypeError, ValueError):
                        LOG.debug("Ungueltiger Statuseintrag fuer '%s' wird verworfen", name)
        LOG.debug("Status fuer %s Host(s) geladen", len(state.hosts))
        return state

    def save(self, *, now: int | None = None) -> None:
        """Schreibt die Statusdatei atomar (temporaere Datei + os.replace)."""
        self.last_run = int(now if now is not None else time.time())
        payload = {
            "version": STATE_VERSION,
            "last_run": self.last_run,
            "hosts": {name: entry.to_dict() for name, entry in sorted(self.hosts.items())},
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(self.path.parent),
            prefix=self.path.name + ".",
            suffix=".tmp",
        )
        try:
            with handle as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, self.path)
        except OSError as exc:
            _remove_quietly(handle.name)
            raise OSError(f"Statusdatei '{self.path}' konnte nicht geschrieben werden: {exc}") from exc
        LOG.debug("Status mit %s Eintrag/Eintraegen gespeichert (%s)", len(self.hosts), self.path)

    # -- Auswertung ---------------------------------------------------------

    def should_notify(
        self, host_name: str, offline_since: int, repeat_after: int, now: int
    ) -> tuple[bool, str]:
        """Entscheidet, ob fuer den Host jetzt eine Mail faellig ist."""
        entry = self.hosts.get(host_name)
        if entry is None:
            return True, "neu"
        if abs(entry.offline_since - offline_since) > OFFLINE_SINCE_TOLERANCE:
            return True, "erneut offline"
        if not entry.last_notified:
            return True, "noch nicht benachrichtigt"
        if repeat_after > 0 and now - entry.last_notified >= repeat_after:
            return True, "Wiederholung"
        return False, "bereits benachrichtigt"

    def record_notification(self, host_name: str, offline_since: int, folder: str, now: int) -> None:
        entry = self.hosts.get(host_name)
        if entry is None or abs(entry.offline_since - offline_since) > OFFLINE_SINCE_TOLERANCE:
            entry = HostState(offline_since=offline_since, folder=folder)
            self.hosts[host_name] = entry
        entry.last_notified = now
        entry.notify_count += 1
        entry.folder = folder

    def touch(self, host_name: str, offline_since: int, folder: str) -> None:
        """Haelt einen bekannten Offline-Host im Status, ohne zu benachrichtigen."""
        entry = self.hosts.get(host_name)
        if entry is None:
            self.hosts[host_name] = HostState(offline_since=offline_since, folder=folder)
        else:
            entry.folder = folder
            if abs(entry.offline_since - offline_since) > OFFLINE_SINCE_TOLERANCE:
                entry.offline_since = offline_since
                entry.last_notified = 0
                entry.notify_count = 0

    def forget_recovered(self, still_offline: Iterable[str]) -> list[str]:
        """Entfernt Hosts, die wieder online sind, und meldet deren Namen."""
        current = set(still_offline)
        recovered = sorted(set(self.hosts) - current)
        for name in recovered:
            del self.hosts[name]
        if recovered:
            LOG.info("%s Host(s) wieder online - Status bereinigt: %s", len(recovered), ", ".join(recovered))
        return recovered


def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:  # pragma: no cover - defensiv
        pass
