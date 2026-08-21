"""Verknuepft Checkmk-Daten mit den Ordnerregeln und entscheidet ueber Benachrichtigungen."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Sequence

from .checkmk import HostConfigInfo, MonitoredHost
from .config import AppConfig, FolderRule
from .duration import format_duration
from .state import NotificationState

LOG = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^a-z0-9_]+")


def attribute_placeholder(name: str) -> str:
    """Macht aus einem Attributnamen einen Platzhalternamen (``standort`` -> ``attr_standort``)."""
    cleaned = _UNSAFE.sub("_", name.strip().lower()).strip("_")
    return f"attr_{cleaned or 'unbekannt'}"


@dataclass
class OfflineHost:
    """Ein Host, der laenger offline ist, als seine Ordnerregel erlaubt."""

    host: MonitoredHost
    rule: FolderRule
    offline_since: int
    offline_seconds: int
    url: str = ""
    reason: str = ""

    @property
    def name(self) -> str:
        return self.host.name

    @property
    def address(self) -> str:
        return self.host.address

    def placeholders(self, config: AppConfig) -> dict[str, str]:
        fmt = config.behaviour.datetime_format
        values = {
            "host": self.host.name,
            "host_name": self.host.name,
            "hostname": self.host.name,
            "address": self.host.address,
            "ip": self.host.address,
            "alias": self.host.alias,
            "state": self.host.state_name,
            "folder": self.host.folder,
            "folder_title": self.rule.title,
            "rule_folder": self.rule.path,
            "offline_since": _format_timestamp(self.offline_since, fmt),
            "offline_for": format_duration(self.offline_seconds),
            "offline_seconds": str(self.offline_seconds),
            "threshold": format_duration(self.rule.offline_for),
            "last_check": _format_timestamp(self.host.last_check, fmt),
            "output": self.host.plugin_output,
            "url": self.url,
            "reason": self.reason,
        }
        for attribute, value in self.host.attributes.items():
            values[attribute_placeholder(attribute)] = value
        return values


@dataclass
class NotificationGroup:
    """Eine zu versendende Mail (ein Ordner oder eine Sammelmail)."""

    key: str
    title: str
    rule: FolderRule | None
    hosts: list[OfflineHost] = field(default_factory=list)

    @property
    def recipients_override(self) -> list[str]:
        return list(self.rule.recipients) if self.rule and self.rule.recipients else []

    @property
    def subject_override(self) -> str:
        return self.rule.subject if self.rule and self.rule.subject else ""


@dataclass
class EvaluationResult:
    matched: list[OfflineHost] = field(default_factory=list)
    due: list[OfflineHost] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    unmatched_folders: set[str] = field(default_factory=set)
    without_folder: list[str] = field(default_factory=list)

    def note_skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def enrich_hosts(
    hosts: Sequence[MonitoredHost],
    details: dict[str, HostConfigInfo],
    custom_attributes: Sequence[str],
) -> None:
    """Ergaenzt Monitoring-Hosts um Ordner, Custom-Attribute und ggf. die IP aus dem Setup."""
    for host in hosts:
        info = details.get(host.name)
        if info is None:
            continue
        host.folder = info.folder
        host.attributes = {name: info.attributes.get(name, "") for name in custom_attributes}
        if not host.address and info.ipaddress:
            host.address = info.ipaddress
        if not host.alias and info.alias:
            host.alias = info.alias


def evaluate(
    hosts: Iterable[MonitoredHost],
    config: AppConfig,
    state: NotificationState,
    now: int,
    host_url: Callable[[str], str] | None = None,
) -> EvaluationResult:
    """Filtert Hosts nach Ordnerregeln, Schwellwerten und bereits versandten Mails."""
    result = EvaluationResult()

    for host in hosts:
        if config.behaviour.skip_pending and not host.has_been_checked:
            result.note_skip("noch nicht geprueft")
            continue
        if config.behaviour.skip_in_downtime and host.in_downtime:
            result.note_skip("in Wartungszeit")
            continue
        if config.behaviour.skip_acknowledged and host.is_acknowledged:
            result.note_skip("quittiert")
            continue
        if config.behaviour.skip_notifications_disabled and not host.notifications_enabled:
            result.note_skip("Benachrichtigungen deaktiviert")
            continue
        if not host.folder:
            result.without_folder.append(host.name)
            result.note_skip("kein Ordner ermittelbar")
            continue

        rule = config.rule_for(host.folder)
        if rule is None:
            result.unmatched_folders.add(host.folder)
            result.note_skip("Ordner nicht konfiguriert")
            continue

        # Attributfilter: globale zuerst, danach die des Ordners. Alle muessen zutreffen.
        blocking = next(
            (entry for entry in (*config.filters, *rule.filters) if not entry.accepts(host.attributes)),
            None,
        )
        if blocking is not None:
            LOG.debug(
                "Host '%s' wird durch Attributfilter uebersprungen (%s, Wert: '%s')",
                host.name,
                blocking.describe(),
                host.attributes.get(blocking.attribute, ""),
            )
            result.note_skip(f"Attributfilter ({blocking.describe()})")
            continue

        offline_since = _offline_since(host, config.behaviour.offline_since_source)
        if offline_since <= 0:
            LOG.warning(
                "Fuer Host '%s' ist kein Offline-Zeitpunkt bekannt - Host wird uebersprungen", host.name
            )
            result.note_skip("Offline-Zeitpunkt unbekannt")
            continue

        offline_seconds = max(0, now - offline_since)
        if offline_seconds < rule.offline_for:
            result.note_skip("Schwellwert noch nicht erreicht")
            continue

        entry = OfflineHost(
            host=host,
            rule=rule,
            offline_since=offline_since,
            offline_seconds=offline_seconds,
            url=host_url(host.name) if host_url else "",
        )
        result.matched.append(entry)

        repeat_after = rule.repeat_after if rule.repeat_after is not None else config.behaviour.repeat_after
        notify, reason = state.should_notify(host.name, offline_since, repeat_after, now)
        entry.reason = reason
        if notify:
            result.due.append(entry)
        else:
            state.touch(host.name, offline_since, host.folder)
            LOG.debug("Host '%s' wird nicht erneut gemeldet (%s)", host.name, reason)

    result.matched.sort(key=lambda item: (-item.offline_seconds, item.name.lower()))
    result.due.sort(key=lambda item: (-item.offline_seconds, item.name.lower()))
    return result


def group_notifications(due: Sequence[OfflineHost], config: AppConfig) -> list[NotificationGroup]:
    """Bildet aus den faelligen Hosts die einzelnen Mails."""
    if not due:
        return []

    if config.behaviour.group_by == "host":
        # Eine eigene Mail je Host.
        return [
            NotificationGroup(key=entry.name, title=entry.rule.title, rule=entry.rule, hosts=[entry])
            for entry in due
        ]

    if config.behaviour.group_by == "single":
        groups = [NotificationGroup(key="__all__", title="Alle Ordner", rule=None, hosts=list(due))]
    else:
        by_rule: dict[str, NotificationGroup] = {}
        for entry in due:
            group = by_rule.get(entry.rule.path)
            if group is None:
                group = NotificationGroup(key=entry.rule.path, title=entry.rule.title, rule=entry.rule)
                by_rule[entry.rule.path] = group
            group.hosts.append(entry)
        groups = [by_rule[key] for key in sorted(by_rule)]

    limit = config.behaviour.max_hosts_per_mail
    if limit <= 0:
        return groups

    chunked: list[NotificationGroup] = []
    for group in groups:
        if len(group.hosts) <= limit:
            chunked.append(group)
            continue
        for offset in range(0, len(group.hosts), limit):
            part = group.hosts[offset : offset + limit]
            chunked.append(
                NotificationGroup(
                    key=f"{group.key}#{offset // limit + 1}",
                    title=group.title,
                    rule=group.rule,
                    hosts=part,
                )
            )
    return chunked


def group_placeholders(group: NotificationGroup, config: AppConfig, now: int) -> dict[str, str]:
    """Platzhalter fuer Betreff und Mailrumpf einer Gruppe."""
    fmt = config.behaviour.datetime_format
    names = [entry.name for entry in group.hosts]
    addresses = [entry.address for entry in group.hosts if entry.address]
    first = group.hosts[0] if group.hosts else None

    values = {
        "folder": group.title,
        "folder_path": group.rule.path if group.rule else "",
        "folder_title": group.title,
        "host_count": str(len(group.hosts)),
        "hosts": ", ".join(names),
        "host": names[0] if names else "",
        "host_name": names[0] if names else "",
        "hostname": names[0] if names else "",
        "first_host": names[0] if names else "",
        "addresses": ", ".join(_unique(addresses)),
        "address": addresses[0] if addresses else "",
        "ip": addresses[0] if addresses else "",
        "first_address": addresses[0] if addresses else "",
        "alias": first.host.alias if first else "",
        "state": first.host.state_name if first else "",
        "threshold": format_duration(group.rule.offline_for) if group.rule else "",
        "offline_since": _format_timestamp(first.offline_since, fmt) if first else "",
        "offline_for": format_duration(first.offline_seconds) if first else "",
        "url": first.url if first else "",
        "now": _format_timestamp(now, fmt),
        "date": _format_timestamp(now, "%d.%m.%Y"),
        "time": _format_timestamp(now, "%H:%M"),
        "site": config.checkmk.site,
        "checkmk_url": config.checkmk.gui_url,
    }

    for attribute in config.checkmk.custom_attributes:
        collected = _unique(
            entry.host.attributes.get(attribute, "") for entry in group.hosts if entry.host.attributes.get(attribute)
        )
        values[attribute_placeholder(attribute)] = ", ".join(collected)
    return values


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _offline_since(host: MonitoredHost, source: str) -> int:
    if source == "last_time_up":
        return host.last_time_up
    if source == "last_state_change":
        return host.last_state_change
    return host.last_time_up or host.last_state_change


def _format_timestamp(timestamp: int, fmt: str) -> str:
    if not timestamp:
        return "unbekannt"
    try:
        return datetime.fromtimestamp(timestamp).strftime(fmt)
    except (OSError, OverflowError, ValueError):  # pragma: no cover - defensiv
        return "unbekannt"
