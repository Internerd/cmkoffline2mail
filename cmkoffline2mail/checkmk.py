"""Minimaler Client fuer die Checkmk REST-API (getestet gegen 2.3.0p20)."""

from __future__ import annotations

import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .config import CheckmkConfig, normalize_folder

LOG = logging.getLogger(__name__)

#: Livestatus-Spalten, die fuer die Auswertung benoetigt werden.
HOST_COLUMNS = (
    "name",
    "state",
    "last_time_up",
    "last_state_change",
    "last_check",
    "scheduled_downtime_depth",
    "acknowledged",
    "notifications_enabled",
    "has_been_checked",
    "address",
    "alias",
    "plugin_output",
)

STATE_NAMES = {0: "UP", 1: "DOWN", 2: "UNREACHABLE"}


class CheckmkError(Exception):
    """Fehler bei der Kommunikation mit Checkmk."""


@dataclass
class MonitoredHost:
    """Ein Host samt Monitoring-Status aus Livestatus."""

    name: str
    state: int = 0
    last_time_up: int = 0
    last_state_change: int = 0
    last_check: int = 0
    scheduled_downtime_depth: int = 0
    acknowledged: int = 0
    notifications_enabled: int = 1
    has_been_checked: int = 1
    address: str = ""
    alias: str = ""
    plugin_output: str = ""
    folder: str = ""
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def state_name(self) -> str:
        return STATE_NAMES.get(self.state, f"STATE {self.state}")

    @property
    def in_downtime(self) -> bool:
        return self.scheduled_downtime_depth > 0

    @property
    def is_acknowledged(self) -> bool:
        return bool(self.acknowledged)

    @classmethod
    def from_api(cls, entry: dict[str, Any]) -> "MonitoredHost":
        extensions = entry.get("extensions") or {}
        name = extensions.get("name") or entry.get("id") or entry.get("title") or ""

        def as_int(key: str, default: int = 0) -> int:
            value = extensions.get(key, default)
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def as_str(key: str) -> str:
            value = extensions.get(key, "")
            return "" if value is None else str(value)

        return cls(
            name=str(name),
            state=as_int("state"),
            last_time_up=as_int("last_time_up"),
            last_state_change=as_int("last_state_change"),
            last_check=as_int("last_check"),
            scheduled_downtime_depth=as_int("scheduled_downtime_depth"),
            acknowledged=as_int("acknowledged"),
            notifications_enabled=as_int("notifications_enabled", 1),
            has_been_checked=as_int("has_been_checked", 1),
            address=as_str("address"),
            alias=as_str("alias"),
            plugin_output=as_str("plugin_output"),
        )


@dataclass
class HostConfigInfo:
    """Setup-Daten eines Hosts (Ordner, IP, Custom-Attribute)."""

    folder: str
    ipaddress: str = ""
    alias: str = ""
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class CheckmkFolder:
    """Ein Ordner aus der Setup-Konfiguration."""

    path: str
    title: str = ""
    host_count: int | None = None


@dataclass
class CheckmkClient:
    """Liest Host- und Ordnerdaten ueber die REST-API aus."""

    config: CheckmkConfig
    _opener: urllib.request.OpenerDirector | None = field(default=None, init=False, repr=False)

    # -- HTTP ---------------------------------------------------------------

    def _build_opener(self) -> urllib.request.OpenerDirector:
        if self._opener is not None:
            return self._opener

        context = ssl.create_default_context()
        if self.config.ca_bundle:
            try:
                context.load_verify_locations(cafile=str(self.config.ca_bundle))
            except OSError as exc:
                raise CheckmkError(f"CA-Bundle '{self.config.ca_bundle}' nicht lesbar: {exc}") from exc
        if not self.config.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            LOG.warning("TLS-Zertifikatspruefung fuer Checkmk ist deaktiviert (checkmk.verify_tls = false)")

        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
        return self._opener

    def _request(self, endpoint: str, params: Sequence[tuple[str, str]] | None = None) -> dict[str, Any]:
        url = f"{self.config.api_url}{endpoint}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {self.config.username} {self.config.secret}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "cmkoffline2mail")

        opener = self._build_opener()
        last_error: Exception | None = None

        for attempt in range(self.config.retries + 1):
            if attempt:
                delay = min(2 ** attempt, 30)
                LOG.warning("Wiederhole Checkmk-Abfrage in %s s (Versuch %s): %s", delay, attempt + 1, last_error)
                time.sleep(delay)
            try:
                LOG.debug("GET %s", url)
                with opener.open(request, timeout=self.config.timeout) as response:
                    payload = response.read()
                return _decode_json(payload, url)
            except urllib.error.HTTPError as exc:
                body = _read_error_body(exc)
                if exc.code in (401, 403):
                    raise CheckmkError(
                        f"Anmeldung an Checkmk fehlgeschlagen (HTTP {exc.code}). "
                        f"Bitte Automationsbenutzer '{self.config.username}' und Passwort pruefen. {body}"
                    ) from exc
                if exc.code == 404:
                    raise CheckmkError(
                        f"Checkmk-Endpunkt nicht gefunden (HTTP 404): {url}. "
                        f"Stimmt 'checkmk.url' inklusive Site-Name? {body}"
                    ) from exc
                if 400 <= exc.code < 500:
                    raise CheckmkError(f"Checkmk lehnte die Anfrage ab (HTTP {exc.code}): {body}") from exc
                last_error = CheckmkError(f"Checkmk antwortete mit HTTP {exc.code}: {body}")
            except urllib.error.URLError as exc:
                last_error = CheckmkError(f"Checkmk unter '{url}' nicht erreichbar: {exc.reason}")
            except (socket.timeout, TimeoutError) as exc:
                last_error = CheckmkError(f"Zeitueberschreitung bei der Abfrage von '{url}': {exc}")

        raise last_error if last_error else CheckmkError(f"Abfrage von '{url}' fehlgeschlagen")

    # -- Abfragen -----------------------------------------------------------

    def get_offline_hosts(self, *, include_unreachable: bool = True) -> list[MonitoredHost]:
        """Liefert alle Hosts, die aktuell nicht UP sind."""
        states = ["1", "2"] if include_unreachable else ["1"]
        if len(states) == 1:
            query: dict[str, Any] = {"op": "=", "left": "state", "right": states[0]}
        else:
            query = {
                "op": "or",
                "expr": [{"op": "=", "left": "state", "right": state} for state in states],
            }

        params: list[tuple[str, str]] = [("columns", column) for column in HOST_COLUMNS]
        params.append(("query", json.dumps(query)))
        for site in self.config.sites:
            params.append(("sites", site))

        payload = self._request("/domain-types/host/collections/all", params)
        hosts = [MonitoredHost.from_api(entry) for entry in payload.get("value", [])]
        LOG.info("Checkmk meldet %s Host(s) im Zustand DOWN/UNREACHABLE", len(hosts))
        return [host for host in hosts if host.name]

    def get_host_details(
        self, host_names: Iterable[str], custom_attributes: Sequence[str] = ()
    ) -> dict[str, "HostConfigInfo"]:
        """Liest Setup-Ordner und gewuenschte Custom-Attribute der genannten Hosts."""
        names = sorted({name for name in host_names if name})
        if not names:
            return {}

        threshold = self.config.folder_lookup_threshold
        if threshold and len(names) <= threshold:
            details: dict[str, HostConfigInfo] = {}
            for name in names:
                info = self._get_single_host_config(name, custom_attributes)
                if info is not None:
                    details[name] = info
            return details

        wanted = set(names)
        return {
            name: info
            for name, info in self._get_all_host_configs(custom_attributes).items()
            if name in wanted
        }

    @property
    def _effective_attributes(self) -> str:
        """Vererbte (Ordner-)Attribute mitliefern lassen."""
        return "true" if self.config.effective_attributes else "false"

    def _get_single_host_config(
        self, host_name: str, custom_attributes: Sequence[str]
    ) -> "HostConfigInfo | None":
        endpoint = f"/objects/host_config/{urllib.parse.quote(host_name, safe='')}"
        try:
            payload = self._request(endpoint, [("effective_attributes", self._effective_attributes)])
        except CheckmkError as exc:
            LOG.warning("Setup-Daten von Host '%s' konnten nicht gelesen werden: %s", host_name, exc)
            return None
        return _host_config_info(payload, custom_attributes)

    def _get_all_host_configs(self, custom_attributes: Sequence[str]) -> dict[str, "HostConfigInfo"]:
        payload = self._request(
            "/domain-types/host_config/collections/all",
            [("effective_attributes", self._effective_attributes)],
        )
        infos: dict[str, HostConfigInfo] = {}
        for entry in payload.get("value", []):
            name = entry.get("id") or entry.get("title")
            info = _host_config_info(entry, custom_attributes)
            if name and info is not None:
                infos[str(name)] = info
        LOG.debug("Setup-Daten fuer %s Host(s) geladen", len(infos))
        return infos

    def list_folders(self) -> list[CheckmkFolder]:
        """Alle Setup-Ordner - hilfreich zum Befuellen der Konfiguration."""
        payload = self._request(
            "/domain-types/folder_config/collections/all",
            [("recursive", "true"), ("show_hosts", "false")],
        )
        folders: list[CheckmkFolder] = []
        for entry in payload.get("value", []):
            extensions = entry.get("extensions") or {}
            raw_path = extensions.get("path") or entry.get("id") or ""
            folders.append(
                CheckmkFolder(
                    path=normalize_folder(str(raw_path)),
                    title=str(extensions.get("title") or entry.get("title") or ""),
                )
            )
        return sorted(folders, key=lambda folder: folder.path)

    def host_url(self, host_name: str) -> str:
        """GUI-Link auf die Host-Statusseite."""
        query = urllib.parse.urlencode({"view_name": "hoststatus", "host": host_name})
        return f"{self.config.gui_url}/view.py?{query}"


def _host_config_info(entry: dict[str, Any], custom_attributes: Sequence[str]) -> "HostConfigInfo | None":
    """Baut aus einer host_config-Antwort Ordner, IP-Adresse und Custom-Attribute."""
    extensions = entry.get("extensions") or {}
    attributes = extensions.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}

    folder = extensions.get("folder")
    if folder is None:
        folder = attributes.get("folder")
    if folder is None:
        return None

    ipaddress = attributes.get("ipaddress") or attributes.get("ipv6address") or ""
    values: dict[str, str] = {}
    for name in custom_attributes:
        value = _lookup_attribute(attributes, name)
        values[name] = "" if value is None else _stringify(value)

    return HostConfigInfo(
        folder=normalize_folder(str(folder)),
        ipaddress=str(ipaddress),
        alias=str(attributes.get("alias") or ""),
        attributes=values,
    )


def _lookup_attribute(attributes: dict[str, Any], name: str) -> Any:
    """Sucht ein Attribut als Custom-Attribut, Label oder Host-Tag."""
    if name in attributes:
        return attributes[name]

    labels = attributes.get("labels")
    if isinstance(labels, dict) and name in labels:
        return labels[name]

    if not name.startswith("tag_") and f"tag_{name}" in attributes:
        return attributes[f"tag_{name}"]

    lowered = name.lower()
    for key, value in attributes.items():
        if key.lower() == lowered:
            return value
    return None


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, (list, tuple)):
        return ", ".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}: {_stringify(item)}" for key, item in value.items())
    return str(value)


def _decode_json(payload: bytes, url: str) -> dict[str, Any]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckmkError(
            f"Antwort von '{url}' ist kein gueltiges JSON - antwortet dort wirklich die Checkmk-API? ({exc})"
        ) from exc
    if not isinstance(data, dict):
        raise CheckmkError(f"Unerwartete Antwortstruktur von '{url}'")
    return data


def _read_error_body(exc: urllib.error.HTTPError, limit: int = 500) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:  # pragma: no cover - defensiv
        return ""
    if not body:
        return ""
    try:
        parsed = json.loads(body)
        detail = parsed.get("detail") or parsed.get("title") or body
        fields = parsed.get("fields")
        if fields:
            detail = f"{detail} ({fields})"
        body = str(detail)
    except (json.JSONDecodeError, AttributeError):
        pass
    return body[:limit]
