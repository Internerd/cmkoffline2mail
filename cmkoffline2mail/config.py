"""Einlesen und Pruefen der lokalen Konfigurationsdatei (TOML)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .duration import DurationError, parse_duration

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Fallback fuer aeltere Interpreter
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]


class ConfigError(Exception):
    """Fehler in der Konfigurationsdatei."""


def format_toml_error(path: Path, exc: Exception) -> str:
    """Bereitet einen TOML-Fehler auf: Datei, Meldung, betroffene Zeile, Hinweise."""
    message = str(exc)
    detail = ""

    match = re.search(r"line (\d+), column (\d+)", message)
    if match:
        line_number, column = int(match.group(1)), int(match.group(2))
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        if 0 < line_number <= len(lines):
            marker = " " * max(0, column - 1) + "^"
            detail = f"\n  {line_number:>4} | {lines[line_number - 1]}\n       | {marker}"

    hint = (
        "\nHaeufige Ursachen:"
        "\n  - Werte muessen in Anfuehrungszeichen stehen:  format = \"html\"  (nicht: format = html)"
        "\n  - mehrzeiliger Text gehoert zwischen dreifache Anfuehrungszeichen (\"\"\")"
        "\n  - ein abschliessendes \"\"\" fehlt"
        "\n  - Backslashes in normalen Zeichenketten verdoppeln (C:\\\\Pfad) oder "
        "'einfache Anfuehrungszeichen' verwenden"
    )
    return f"'{path}' ist kein gueltiges TOML: {message}{detail}{hint}"


def install_root() -> Path:
    """Basisverzeichnis der Installation (der Ordner ueber dem Python-Paket).

    Bei einer Kopier-Installation nach z. B. ``C:\\FIRMA\\cmkoffline2mail`` ist das
    genau dieser Ordner - Konfiguration, Status und Protokoll liegen dann
    standardmaessig darunter. Nur wenn das Paket per pip in ein site-packages
    installiert wurde, wird auf %PROGRAMDATA% ausgewichen.
    """
    root = Path(__file__).resolve().parent.parent
    if any(part in ("site-packages", "dist-packages") for part in root.parts):
        return Path(os.environ.get("PROGRAMDATA", str(Path.home()))) / "cmkoffline2mail"
    return root


def default_config_path() -> Path:
    """Sucht die Konfiguration an den ueblichen Stellen (fuer den Aufruf ohne --config)."""
    candidates = []
    from_env = os.environ.get("CMKOFFLINE2MAIL_CONFIG")
    if from_env:
        candidates.append(Path(os.path.expandvars(from_env)).expanduser())

    root = install_root()
    candidates.append(root / "config" / "config.toml")
    candidates.append(root / "config.toml")
    candidates.append(Path(os.environ.get("PROGRAMDATA", ".")) / "cmkoffline2mail" / "config.toml")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0] if from_env else root / "config" / "config.toml"


def normalize_folder(path: str) -> str:
    """Bringt Checkmk-Ordnerpfade auf eine einheitliche Form (``/a/b``)."""
    text = str(path).strip().replace("\\", "/").replace("~", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if not text.startswith("/"):
        text = "/" + text
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


class _Section:
    """Kleiner Helfer fuer typsichere Zugriffe mit sprechenden Fehlermeldungen."""

    def __init__(
        self,
        data: dict[str, Any],
        path: str,
        warnings: list[str],
        base_dir: Path | None = None,
    ):
        self._data = data
        self._path = path
        self._warnings = warnings
        self._base_dir = base_dir
        self._seen: set[str] = set()

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def _where(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key

    def _get(self, key: str, default: Any, required: bool) -> Any:
        self._seen.add(key)
        if key in self._data:
            return self._data[key]
        if required:
            raise ConfigError(f"Pflichtangabe '{self._where(key)}' fehlt")
        return default

    def str(self, key: str, default: str = "", *, required: bool = False) -> str:
        value = self._get(key, default, required)
        if not isinstance(value, str):
            raise ConfigError(f"'{self._where(key)}' muss Text sein")
        value = value.strip()
        if required and not value:
            raise ConfigError(f"'{self._where(key)}' darf nicht leer sein")
        return value

    def choice(self, key: str, allowed: Sequence[str], default: str) -> str:
        value = self.str(key, default).lower()
        if value not in allowed:
            raise ConfigError(
                f"'{self._where(key)}' muss einer der Werte {', '.join(allowed)} sein (ist: '{value}')"
            )
        return value

    def int(self, key: str, default: int = 0, *, minimum: int | None = None) -> int:
        value = self._get(key, default, False)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"'{self._where(key)}' muss eine ganze Zahl sein")
        if minimum is not None and value < minimum:
            raise ConfigError(f"'{self._where(key)}' muss mindestens {minimum} sein")
        return value

    def bool(self, key: str, default: bool = False) -> bool:
        value = self._get(key, default, False)
        if not isinstance(value, bool):
            raise ConfigError(f"'{self._where(key)}' muss true oder false sein")
        return value

    def duration(self, key: str, default: Any = 0, *, required: bool = False) -> int:
        value = self._get(key, default, required)
        try:
            return parse_duration(value, context=self._where(key))
        except DurationError as exc:
            raise ConfigError(str(exc)) from exc

    def str_list(self, key: str, default: Iterable[str] = ()) -> list[str]:
        value = self._get(key, None, False)
        if value is None:
            return [item for item in default]
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",")]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"'{self._where(key)}' muss eine Liste von Texten sein")
        return [item.strip() for item in value if item.strip()]

    def path(self, key: str, default: str = "", *, required: bool = False) -> Path | None:
        """Liest einen Pfad; relative Angaben gelten relativ zur Konfigurationsdatei."""
        value = self.str(key, default, required=required)
        if not value:
            return None
        resolved = Path(os.path.expandvars(value)).expanduser()
        if not resolved.is_absolute() and self._base_dir is not None:
            resolved = self._base_dir / resolved
        return Path(os.path.normpath(resolved))

    def section(self, key: str, *, required: bool = False) -> "_Section":
        self._seen.add(key)
        value = self._data.get(key)
        if value is None:
            if required:
                raise ConfigError(f"Abschnitt '[{self._where(key)}]' fehlt")
            value = {}
        if not isinstance(value, dict):
            raise ConfigError(f"'{self._where(key)}' muss ein Abschnitt sein")
        return _Section(value, self._where(key), self._warnings, self._base_dir)

    def secret(self, key: str) -> str:
        """Liest ein Geheimnis aus ``key``, ``key_env`` oder ``key_file``."""
        direct = self.str(key)
        env_name = self.str(f"{key}_env")
        file_name = self.path(f"{key}_file")

        if env_name:
            value = os.environ.get(env_name)
            if value is None:
                raise ConfigError(
                    f"Umgebungsvariable '{env_name}' aus '{self._where(key)}_env' ist nicht gesetzt"
                )
            return value.strip()
        if file_name:
            try:
                return file_name.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(
                    f"Datei aus '{self._where(key)}_file' nicht lesbar: {exc}"
                ) from exc
        return direct

    def warn_unknown(self, extra_allowed: Iterable[str] = ()) -> None:
        allowed = self._seen | set(extra_allowed)
        for key in sorted(set(self._data) - allowed):
            self._warnings.append(f"Unbekannter Konfigurationsschluessel '{self._where(key)}' wird ignoriert")


@dataclass
class CheckmkConfig:
    url: str
    username: str
    secret: str
    site: str = ""
    verify_tls: bool = True
    ca_bundle: Path | None = None
    timeout: int = 30
    retries: int = 2
    sites: list[str] = field(default_factory=list)
    folder_lookup_threshold: int = 50
    custom_attributes: list[str] = field(default_factory=list)
    effective_attributes: bool = True

    @property
    def api_url(self) -> str:
        base = self.url.rstrip("/")
        if base.endswith("/check_mk/api/1.0"):
            return base
        if base.endswith("/check_mk"):
            return base + "/api/1.0"
        return base + "/check_mk/api/1.0"

    @property
    def gui_url(self) -> str:
        base = self.url.rstrip("/")
        for suffix in ("/check_mk/api/1.0", "/check_mk"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        return base + "/check_mk"


@dataclass
class AttributeFilter:
    """Benachrichtigt nur (oder gerade nicht), wenn ein Custom-Attribut passt."""

    attribute: str
    equals: list[str] = field(default_factory=list)
    contains: list[str] = field(default_factory=list)
    regex: str = ""
    not_empty: bool = False
    mode: str = "include"          # include = nur bei Treffer, exclude = nicht bei Treffer
    ignore_case: bool = True
    on_missing: str = ""           # "" = automatisch (include -> skip, exclude -> notify)
    source: str = "attribute_filters"

    def __post_init__(self) -> None:
        self._pattern = None
        if self.regex:
            try:
                self._pattern = re.compile(self.regex, re.IGNORECASE if self.ignore_case else 0)
            except re.error as exc:
                raise ConfigError(f"'{self.source}.regex' ist kein gueltiger regulaerer Ausdruck: {exc}") from exc

    @property
    def criteria(self) -> str:
        """Kurzbeschreibung fuer Protokoll und --validate."""
        parts: list[str] = []
        if self.equals:
            parts.append("ist " + " oder ".join(f"'{value}'" for value in self.equals))
        if self.contains:
            parts.append("enthaelt " + " oder ".join(f"'{value}'" for value in self.contains))
        if self.regex:
            parts.append(f"passt auf /{self.regex}/")
        if self.not_empty:
            parts.append("ist gesetzt")
        return " oder ".join(parts) if parts else "(keine Bedingung)"

    def _value_matches(self, value: str) -> bool:
        haystack = value.lower() if self.ignore_case else value

        for expected in self.equals:
            if haystack == (expected.lower() if self.ignore_case else expected):
                return True
        for part in self.contains:
            if (part.lower() if self.ignore_case else part) in haystack:
                return True
        if self._pattern is not None and self._pattern.search(value):
            return True
        return bool(self.not_empty)

    def accepts(self, attributes: Mapping[str, str]) -> bool:
        """True = der Host darf gemeldet werden."""
        value = (attributes.get(self.attribute) or "").strip()
        if not value:
            fallback = self.on_missing or ("skip" if self.mode == "include" else "notify")
            return fallback == "notify"
        matched = self._value_matches(value)
        return matched if self.mode == "include" else not matched

    def describe(self) -> str:
        wording = "nur wenn" if self.mode == "include" else "nicht wenn"
        return f"{wording} {self.attribute} {self.criteria}"


@dataclass
class FolderRule:
    path: str
    offline_for: int
    include_subfolders: bool = True
    recipients: list[str] = field(default_factory=list)
    subject: str = ""
    repeat_after: int | None = None
    enabled: bool = True
    label: str = ""
    filters: list[AttributeFilter] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.label or self.path

    def matches(self, host_folder: str) -> bool:
        rule = self.path.lower()
        folder = normalize_folder(host_folder).lower()
        if folder == rule:
            return True
        if not self.include_subfolders:
            return False
        prefix = rule if rule.endswith("/") else rule + "/"
        return folder.startswith(prefix)

    @property
    def specificity(self) -> int:
        return len(self.path.rstrip("/"))


@dataclass
class BehaviourConfig:
    group_by: str = "host"
    include_unreachable: bool = True
    skip_in_downtime: bool = True
    skip_acknowledged: bool = False
    skip_notifications_disabled: bool = True
    skip_pending: bool = True
    repeat_after: int = 86400
    max_hosts_per_mail: int = 0
    datetime_format: str = "%d.%m.%Y %H:%M"
    offline_since_source: str = "auto"


@dataclass
class MailConfig:
    sender: str
    sender_name: str = ""
    recipients: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: str = ""
    subject: str = "[Checkmk] {{ host_count }} Host(s) offline in {{ folder }}"
    template: Path | None = None


@dataclass
class SmtpConfig:
    host: str
    port: int = 25
    security: str = "none"
    username: str = ""
    password: str = ""
    timeout: int = 30
    helo_name: str = ""
    verify_tls: bool = True
    ca_bundle: Path | None = None


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: Path | None = None
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5
    console: bool = True


@dataclass
class AppConfig:
    checkmk: CheckmkConfig
    behaviour: BehaviourConfig
    folders: list[FolderRule]
    mail: MailConfig
    smtp: SmtpConfig
    logging: LoggingConfig
    state_file: Path
    source: Path
    filters: list[AttributeFilter] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def rule_for(self, host_folder: str) -> FolderRule | None:
        """Liefert die passendste (laengste) aktive Ordnerregel."""
        candidates = [rule for rule in self.folders if rule.enabled and rule.matches(host_folder)]
        if not candidates:
            return None
        return max(candidates, key=lambda rule: rule.specificity)




def load_config(path: str | os.PathLike[str]) -> AppConfig:
    """Liest die Hauptkonfiguration ein und validiert sie."""
    if tomllib is None:  # pragma: no cover
        raise ConfigError("Python 3.11+ oder das Paket 'tomli' wird zum Lesen der TOML-Datei benoetigt")

    config_path = Path(path).expanduser()
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Konfigurationsdatei '{config_path}' nicht gefunden") from exc
    except OSError as exc:
        raise ConfigError(f"Konfigurationsdatei '{config_path}' nicht lesbar: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(format_toml_error(config_path, exc)) from exc

    warnings: list[str] = []
    base_dir = config_path.resolve().parent
    root = _Section(data, "", warnings, base_dir)

    checkmk = _parse_checkmk(root.section("checkmk", required=True))
    behaviour = _parse_behaviour(root.section("behaviour"))
    mail = _parse_mail(root.section("mail", required=True), config_path)
    smtp = _parse_smtp(root.section("smtp", required=True))
    log_cfg = _parse_logging(root.section("logging"))

    state_section = root.section("state")
    default_state = install_root() / "data" / "state.json"
    state_file = state_section.path("file", str(default_state)) or default_state
    state_section.warn_unknown()

    folders = _parse_folders(data.get("folders"), behaviour, warnings)
    filters = _parse_attribute_filters(data.get("attribute_filters"), "attribute_filters", warnings)

    root.warn_unknown({"folders", "attribute_filters"})

    # Gefiltert werden kann nur, was auch ausgelesen wird: fehlende Attribute
    # automatisch ergaenzen, sonst waere der Wert immer leer.
    for entry in [*filters, *(f for rule in folders for f in rule.filters)]:
        if entry.attribute not in checkmk.custom_attributes:
            checkmk.custom_attributes.append(entry.attribute)
            warnings.append(
                f"Attribut '{entry.attribute}' aus '{entry.source}' wurde automatisch zu "
                f"'checkmk.custom_attributes' ergaenzt"
            )

    # Dienstkonten haben im Basisordner ueblicherweise nur Leserechte.
    base = install_root()
    for label, target, empfohlen in (
        ("state.file", state_file, "data"),
        ("logging.file", log_cfg.file, "logs"),
    ):
        if target and target.parent == base:
            warnings.append(
                f"'{label}' zeigt direkt in den Basisordner ({target}). Dort hat das "
                f"Dienstkonto in der Regel nur Leserechte - empfohlen: "
                f"{base / empfohlen / target.name}"
            )

    return AppConfig(
        checkmk=checkmk,
        behaviour=behaviour,
        folders=folders,
        mail=mail,
        smtp=smtp,
        logging=log_cfg,
        state_file=state_file,
        source=config_path,
        filters=filters,
        warnings=warnings,
    )


def _parse_checkmk(section: _Section) -> CheckmkConfig:
    url = section.str("url", required=True)
    if not url.lower().startswith(("http://", "https://")):
        raise ConfigError("'checkmk.url' muss mit http:// oder https:// beginnen")

    secret = section.secret("secret")
    if not secret:
        raise ConfigError(
            "Automationsgeheimnis fehlt: bitte 'checkmk.secret', 'checkmk.secret_env' "
            "oder 'checkmk.secret_file' setzen"
        )

    site = section.str("site")
    if not site:
        # Site-Name aus dem ersten Pfadsegment der URL ableiten (https://server/mysite)
        parts = [part for part in url.split("://", 1)[-1].split("/")[1:] if part]
        if parts and parts[0] not in ("check_mk",):
            site = parts[0]

    config = CheckmkConfig(
        url=url,
        username=section.str("username", required=True),
        secret=secret,
        site=site,
        verify_tls=section.bool("verify_tls", True),
        ca_bundle=section.path("ca_bundle"),
        timeout=section.int("timeout", 30, minimum=1),
        retries=section.int("retries", 2, minimum=0),
        sites=section.str_list("sites"),
        folder_lookup_threshold=section.int("folder_lookup_threshold", 50, minimum=0),
        custom_attributes=section.str_list("custom_attributes"),
        effective_attributes=section.bool("effective_attributes", True),
    )
    section.warn_unknown({"secret_env", "secret_file"})
    return config


def _parse_behaviour(section: _Section) -> BehaviourConfig:
    config = BehaviourConfig(
        group_by=section.choice("group_by", ("host", "folder", "single"), "host"),
        include_unreachable=section.bool("include_unreachable", True),
        skip_in_downtime=section.bool("skip_in_downtime", True),
        skip_acknowledged=section.bool("skip_acknowledged", False),
        skip_notifications_disabled=section.bool("skip_notifications_disabled", True),
        skip_pending=section.bool("skip_pending", True),
        repeat_after=section.duration("repeat_after", "24h"),
        max_hosts_per_mail=section.int("max_hosts_per_mail", 0, minimum=0),
        datetime_format=section.str("datetime_format", "%d.%m.%Y %H:%M"),
        offline_since_source=section.choice(
            "offline_since_source", ("auto", "last_time_up", "last_state_change"), "auto"
        ),
    )
    section.warn_unknown()
    return config


def _parse_mail(section: _Section, config_path: Path) -> MailConfig:
    template = section.path("template")
    if template is None:
        template = config_path.resolve().parent / "mailtemplate.toml"

    config = MailConfig(
        sender=section.str("from", required=True),
        sender_name=section.str("from_name"),
        recipients=section.str_list("to"),
        cc=section.str_list("cc"),
        bcc=section.str_list("bcc"),
        reply_to=section.str("reply_to"),
        subject=section.str("subject", "[Checkmk] {{ host_count }} Host(s) offline in {{ folder }}"),
        template=template,
    )
    if not config.recipients and not config.cc and not config.bcc:
        raise ConfigError("Es ist kein Empfaenger konfiguriert ('mail.to')")
    section.warn_unknown()
    return config


def _parse_smtp(section: _Section) -> SmtpConfig:
    security = section.choice("security", ("none", "starttls", "ssl"), "none")
    default_port = {"none": 25, "starttls": 587, "ssl": 465}[security]
    config = SmtpConfig(
        host=section.str("host", required=True),
        port=section.int("port", default_port, minimum=1),
        security=security,
        username=section.str("username"),
        password=section.secret("password"),
        timeout=section.int("timeout", 30, minimum=1),
        helo_name=section.str("helo_name"),
        verify_tls=section.bool("verify_tls", True),
        ca_bundle=section.path("ca_bundle"),
    )
    if config.username and not config.password:
        raise ConfigError(
            "'smtp.username' ist gesetzt, aber kein Passwort ('smtp.password', "
            "'smtp.password_env' oder 'smtp.password_file')"
        )
    section.warn_unknown({"password_env", "password_file"})
    return config


def _parse_logging(section: _Section) -> LoggingConfig:
    level = section.str("level", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"'logging.level' ist ungueltig: {level}")
    config = LoggingConfig(
        level=level,
        file=section.path("file", str(install_root() / "logs" / "cmkoffline2mail.log")),
        max_bytes=section.int("max_bytes", 5 * 1024 * 1024, minimum=0),
        backup_count=section.int("backup_count", 5, minimum=0),
        console=section.bool("console", True),
    )
    section.warn_unknown()
    return config


def _parse_attribute_filters(raw: Any, label: str, warnings: list[str]) -> list[AttributeFilter]:
    """Liest [[attribute_filters]] bzw. [[folders.attribute_filters]]."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ConfigError(f"'{label}' muss aus [[{label}]]-Abschnitten bestehen")

    filters: list[AttributeFilter] = []
    for index, item in enumerate(raw, start=1):
        where = f"{label}[{index}]"
        section = _Section(item, where, warnings)
        entry = AttributeFilter(
            attribute=section.str("attribute", required=True),
            equals=section.str_list("equals"),
            contains=section.str_list("contains"),
            regex=section.str("regex"),
            not_empty=section.bool("not_empty", False),
            mode=section.choice("mode", ("include", "exclude"), "include"),
            ignore_case=section.bool("ignore_case", True),
            on_missing=section.choice("on_missing", ("", "skip", "notify"), ""),
            source=where,
        )
        if not (entry.equals or entry.contains or entry.regex or entry.not_empty):
            raise ConfigError(
                f"'{where}' braucht mindestens eine Bedingung: equals, contains, regex oder not_empty"
            )
        section.warn_unknown()
        filters.append(entry)
    return filters


def _parse_folders(raw: Any, behaviour: BehaviourConfig, warnings: list[str]) -> list[FolderRule]:
    if raw is None:
        raise ConfigError("Es ist kein Ordner konfiguriert - mindestens ein [[folders]]-Abschnitt wird benoetigt")
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ConfigError("'folders' muss aus [[folders]]-Abschnitten bestehen")

    rules: list[FolderRule] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(raw, start=1):
        section = _Section(item, f"folders[{index}]", warnings)
        path = normalize_folder(section.str("path", required=True))
        if path.lower() in seen:
            raise ConfigError(
                f"Ordner '{path}' ist mehrfach konfiguriert (folders[{seen[path.lower()]}] und folders[{index}])"
            )
        seen[path.lower()] = index

        repeat_raw = "repeat_after" in item
        rule = FolderRule(
            path=path,
            offline_for=section.duration("offline_for", required=True),
            include_subfolders=section.bool("include_subfolders", True),
            recipients=section.str_list("recipients"),
            subject=section.str("subject"),
            repeat_after=section.duration("repeat_after") if repeat_raw else None,
            enabled=section.bool("enabled", True),
            label=section.str("label"),
            filters=_parse_attribute_filters(
                item.get("attribute_filters"), f"folders[{index}].attribute_filters", warnings
            ),
        )
        if rule.offline_for <= 0:
            raise ConfigError(f"'folders[{index}].offline_for' muss groesser als 0 sein")
        section.warn_unknown({"attribute_filters"})
        rules.append(rule)

    if not any(rule.enabled for rule in rules):
        raise ConfigError("Alle konfigurierten Ordner sind deaktiviert (enabled = false)")
    return rules
