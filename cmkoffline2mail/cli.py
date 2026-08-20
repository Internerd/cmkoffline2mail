"""Kommandozeile und Hauptablauf."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from . import __version__
from .checkmk import CheckmkClient, CheckmkError, MonitoredHost
from .config import AppConfig, ConfigError, load_config
from .evaluate import (
    NotificationGroup,
    attribute_placeholder,
    enrich_hosts,
    evaluate,
    group_notifications,
    group_placeholders,
)
from .logging_setup import setup_basic_logging, setup_logging
from .mailer import MailError, Mailer
from .render import MailTemplate, TemplateError, render_string
from .state import NotificationState

LOG = logging.getLogger("cmkoffline2mail")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2

DEFAULT_CONFIG = Path(os.environ.get("PROGRAMDATA", ".")) / "cmkoffline2mail" / "config.toml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmkoffline2mail",
        description=(
            "Liest Hosts aus Checkmk (REST-API) und verschickt eine SMTP-Mail, "
            "wenn Hosts in konfigurierten Ordnern zu lange offline sind."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"Pfad zur Konfigurationsdatei (Standard: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "-t",
        "--template",
        help="Pfad zur Mail-Vorlage (Standard: 'mail.template' aus der Konfiguration)",
    )
    parser.add_argument("--state-file", help="Abweichender Pfad fuer die Statusdatei")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Testlauf: fragt Checkmk ab und zeigt die Mails an, versendet aber nichts",
    )
    parser.add_argument(
        "--no-state-update",
        action="store_true",
        help="Statusdatei nicht schreiben (nuetzlich zum Testen)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Nur Konfiguration und Vorlage pruefen, keine Verbindung aufbauen",
    )
    parser.add_argument(
        "--list-folders",
        action="store_true",
        help="Alle Checkmk-Ordner auflisten (Hilfe beim Befuellen der Konfiguration)",
    )
    parser.add_argument(
        "--show-attributes",
        metavar="HOST",
        help="Alle Setup-Attribute eines Hosts anzeigen (Namen fuer 'custom_attributes' finden)",
    )
    parser.add_argument(
        "--test-mail",
        nargs="?",
        const="",
        metavar="EMPFAENGER",
        help="Testmail mit Beispieldaten versenden (optional an eine abweichende Adresse)",
    )
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Protokollstufe")
    parser.add_argument("--version", action="version", version=f"cmkoffline2mail {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_basic_logging(args.log_level or "INFO")

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        LOG.error("Konfigurationsfehler: %s", exc)
        return EXIT_CONFIG

    if args.state_file:
        config.state_file = Path(args.state_file)

    for message in setup_logging(config.logging, level_override=args.log_level):
        LOG.warning(message)
    for message in config.warnings:
        LOG.warning(message)

    LOG.debug("cmkoffline2mail %s, Konfiguration: %s", __version__, config.source)

    template_path = Path(args.template) if args.template else config.mail.template
    try:
        template = MailTemplate.load(template_path) if template_path else MailTemplate()
    except TemplateError as exc:
        LOG.error("Fehler in der Mail-Vorlage: %s", exc)
        return EXIT_CONFIG

    try:
        if args.validate:
            return _validate(config, template)
        if args.list_folders:
            return _list_folders(config)
        if args.show_attributes:
            return _show_attributes(config, args.show_attributes)
        if args.test_mail is not None:
            return _test_mail(config, template, args.test_mail, dry_run=args.dry_run)
        return _run(config, template, dry_run=args.dry_run, update_state=not args.no_state_update)
    except CheckmkError as exc:
        LOG.error("Checkmk: %s", exc)
        return EXIT_ERROR
    except MailError as exc:
        LOG.error("Mailversand: %s", exc)
        return EXIT_ERROR
    except TemplateError as exc:
        LOG.error("Mail-Vorlage: %s", exc)
        return EXIT_CONFIG
    except KeyboardInterrupt:  # pragma: no cover
        LOG.warning("Abbruch durch Benutzer")
        return EXIT_ERROR
    except Exception:  # pragma: no cover - letzte Sicherheitsnetz-Ebene
        LOG.exception("Unerwarteter Fehler")
        return EXIT_ERROR


# -- Betriebsarten ----------------------------------------------------------


def _validate(config: AppConfig, template: MailTemplate) -> int:
    LOG.info("Konfiguration '%s' ist gueltig", config.source)
    LOG.info("Checkmk-API: %s (Benutzer: %s)", config.checkmk.api_url, config.checkmk.username)
    LOG.info("SMTP: %s:%s (%s)", config.smtp.host, config.smtp.port, config.smtp.security)
    LOG.info("Empfaenger: %s", ", ".join(config.mail.recipients) or "(nur cc/bcc)")
    LOG.info("Statusdatei: %s", config.state_file)
    if config.checkmk.custom_attributes:
        LOG.info(
            "Custom-Attribute: %s",
            ", ".join(
                f"{name} -> {{{{ {attribute_placeholder(name)} }}}}"
                for name in config.checkmk.custom_attributes
            ),
        )
    for rule in config.folders:
        status = "" if rule.enabled else " [deaktiviert]"
        LOG.info(
            "Ordner %s: Meldung ab %s%s%s",
            rule.path,
            _human(rule.offline_for),
            " inkl. Unterordner" if rule.include_subfolders else "",
            status,
        )

    # Vorlage mit Beispieldaten durchspielen, damit Tippfehler sofort auffallen.
    group = _demo_group(config)
    now = int(time.time())
    values = group_placeholders(group, config, now)
    hosts = [entry.placeholders(config) for entry in group.hosts]
    subject = render_string(config.mail.subject, values, context="mail.subject")
    template.render(values, hosts)
    LOG.info("Vorlage '%s' ist gueltig, Beispielbetreff: %s", template.source or "(Standard)", subject)
    return EXIT_OK


def _list_folders(config: AppConfig) -> int:
    client = CheckmkClient(config.checkmk)
    folders = client.list_folders()
    LOG.info("%s Ordner in Checkmk gefunden:", len(folders))
    for folder in folders:
        title = f"  ({folder.title})" if folder.title and folder.title != folder.path else ""
        print(f"{folder.path}{title}")
    return EXIT_OK


def _show_attributes(config: AppConfig, host_name: str) -> int:
    """Zeigt die Setup-Attribute eines Hosts - so findet man die Namen fuer 'custom_attributes'."""
    client = CheckmkClient(config.checkmk)
    payload = client._request(  # noqa: SLF001 - bewusst genutzt, reines Diagnosekommando
        f"/objects/host_config/{host_name}", [("effective_attributes", "true")]
    )
    extensions = payload.get("extensions") or {}
    attributes = extensions.get("attributes") or {}
    print(f"Host:   {host_name}")
    print(f"Ordner: {extensions.get('folder')}")
    print("Attribute (Name -> Wert):")
    for key in sorted(attributes):
        print(f"  {key} = {attributes[key]!r}   ->  {{{{ {attribute_placeholder(key)} }}}}")
    return EXIT_OK


def _test_mail(config: AppConfig, template: MailTemplate, recipient: str, *, dry_run: bool) -> int:
    now = int(time.time())
    group = _demo_group(config)
    values = group_placeholders(group, config, now)
    hosts = [entry.placeholders(config) for entry in group.hosts]

    subject = "[Test] " + render_string(config.mail.subject, values, context="mail.subject")
    text_body, html_body = template.render(values, hosts)
    recipients = [recipient] if recipient else config.mail.recipients

    with Mailer(config.smtp, config.mail, dry_run=dry_run) as mailer:
        if dry_run:
            _print_preview(subject, recipients, text_body, html_body)
        mailer.send(subject, recipients, text_body, html_body)
    LOG.info("Testmail abgeschlossen")
    return EXIT_OK


def _run(config: AppConfig, template: MailTemplate, *, dry_run: bool, update_state: bool) -> int:
    now = int(time.time())
    client = CheckmkClient(config.checkmk)

    hosts = client.get_offline_hosts(include_unreachable=config.behaviour.include_unreachable)
    if hosts:
        details = client.get_host_details([host.name for host in hosts], config.checkmk.custom_attributes)
        enrich_hosts(hosts, details, config.checkmk.custom_attributes)

    state = NotificationState.load(config.state_file)
    result = evaluate(hosts, config, state, now, host_url=client.host_url)

    for reason, count in sorted(result.skipped.items()):
        LOG.info("%s Host(s) uebersprungen: %s", count, reason)
    if result.unmatched_folders:
        LOG.debug("Ordner ohne Regel: %s", ", ".join(sorted(result.unmatched_folders)))
    if result.without_folder:
        LOG.warning(
            "Fuer %s Host(s) war kein Setup-Ordner ermittelbar: %s",
            len(result.without_folder),
            ", ".join(result.without_folder[:10]),
        )

    LOG.info(
        "%s Host(s) ueberschreiten ihren Schwellwert, davon %s zu melden",
        len(result.matched),
        len(result.due),
    )

    groups = group_notifications(result.due, config)
    failures = 0

    if groups:
        with Mailer(config.smtp, config.mail, dry_run=dry_run) as mailer:
            for group in groups:
                try:
                    _send_group(mailer, group, config, template, now, dry_run=dry_run)
                except (MailError, TemplateError) as exc:
                    failures += 1
                    LOG.error("Mail fuer '%s' fehlgeschlagen: %s", group.title, exc)
                    continue
                for entry in group.hosts:
                    state.record_notification(entry.name, entry.offline_since, entry.host.folder, now)

    # Hosts, die den Schwellwert (noch) nicht erreichen, im Status halten,
    # damit spaeter erkannt wird, ob es dieselbe Stoerung ist.
    still_offline = {host.name for host in hosts}
    for entry in result.matched:
        state.touch(entry.name, entry.offline_since, entry.host.folder)
    state.forget_recovered(still_offline)

    if update_state and not dry_run:
        try:
            state.save(now=now)
        except OSError as exc:
            LOG.error("%s", exc)
            return EXIT_ERROR
    elif dry_run:
        LOG.info("[Testlauf] Statusdatei '%s' wurde nicht veraendert", config.state_file)

    return EXIT_ERROR if failures else EXIT_OK


def _send_group(
    mailer: Mailer,
    group: NotificationGroup,
    config: AppConfig,
    template: MailTemplate,
    now: int,
    *,
    dry_run: bool,
) -> None:
    values = group_placeholders(group, config, now)
    host_values = [entry.placeholders(config) for entry in group.hosts]

    subject_template = group.subject_override or config.mail.subject
    subject = render_string(subject_template, values, context="Betreff")
    text_body, html_body = template.render(values, host_values)

    recipients = group.recipients_override or config.mail.recipients
    if dry_run:
        _print_preview(subject, recipients, text_body, html_body)

    mailer.send(subject, recipients, text_body, html_body, cc=config.mail.cc, bcc=config.mail.bcc)


def _print_preview(subject: str, recipients: list[str], text_body: str | None, html_body: str | None) -> None:
    print("=" * 78)
    print(f"An:      {', '.join(recipients)}")
    print(f"Betreff: {subject}")
    print("-" * 78)
    if text_body:
        print(text_body)
    if html_body:
        print("--- HTML ---")
        print(html_body)
    print("=" * 78)


def _demo_group(config: AppConfig) -> NotificationGroup:
    """Beispieldaten fuer --validate und --test-mail."""
    from .evaluate import OfflineHost

    rule = next((rule for rule in config.folders if rule.enabled), config.folders[0])
    now = int(time.time())
    host = MonitoredHost(
        name="beispiel-host01",
        state=1,
        last_time_up=now - rule.offline_for - 3600,
        last_state_change=now - rule.offline_for - 3600,
        last_check=now - 60,
        address="192.0.2.10",
        alias="Beispielserver",
        plugin_output="CRIT - 192.0.2.10: rta nan, lost 100%",
        folder=rule.path,
        attributes={name: f"Beispielwert {name}" for name in config.checkmk.custom_attributes},
    )
    entry = OfflineHost(
        host=host,
        rule=rule,
        offline_since=host.last_time_up,
        offline_seconds=now - host.last_time_up,
        url=f"{config.checkmk.gui_url}/view.py?view_name=hoststatus&host={host.name}",
        reason="Testlauf",
    )
    return NotificationGroup(key=rule.path, title=rule.title, rule=rule, hosts=[entry])


def _human(seconds: int) -> str:
    from .duration import format_duration

    return format_duration(seconds)
