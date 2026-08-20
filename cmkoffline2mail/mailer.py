"""Mailversand per SMTP."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Sequence

from .config import MailConfig, SmtpConfig

LOG = logging.getLogger(__name__)


class MailError(Exception):
    """Fehler beim Mailversand."""


class Mailer:
    """Haelt eine SMTP-Verbindung offen und versendet die erzeugten Mails."""

    def __init__(self, smtp: SmtpConfig, mail: MailConfig, *, dry_run: bool = False):
        self.smtp = smtp
        self.mail = mail
        self.dry_run = dry_run
        self._connection: smtplib.SMTP | None = None

    # -- Verbindung ---------------------------------------------------------

    def __enter__(self) -> "Mailer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self.smtp.ca_bundle:
            try:
                context.load_verify_locations(cafile=str(self.smtp.ca_bundle))
            except OSError as exc:
                raise MailError(f"CA-Bundle '{self.smtp.ca_bundle}' nicht lesbar: {exc}") from exc
        if not self.smtp.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            LOG.warning("TLS-Zertifikatspruefung fuer SMTP ist deaktiviert (smtp.verify_tls = false)")
        return context

    def connect(self) -> smtplib.SMTP:
        if self._connection is not None:
            return self._connection

        kwargs = {"timeout": self.smtp.timeout}
        if self.smtp.helo_name:
            kwargs["local_hostname"] = self.smtp.helo_name

        try:
            if self.smtp.security == "ssl":
                connection: smtplib.SMTP = smtplib.SMTP_SSL(
                    self.smtp.host, self.smtp.port, context=self._ssl_context(), **kwargs
                )
            else:
                connection = smtplib.SMTP(self.smtp.host, self.smtp.port, **kwargs)
                if self.smtp.security == "starttls":
                    connection.ehlo()
                    connection.starttls(context=self._ssl_context())
                    connection.ehlo()

            if self.smtp.username:
                connection.login(self.smtp.username, self.smtp.password)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(
                f"SMTP-Anmeldung als '{self.smtp.username}' fehlgeschlagen: {exc.smtp_code} "
                f"{_decode(exc.smtp_error)}"
            ) from exc
        except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
            raise MailError(
                f"Keine Verbindung zum SMTP-Server {self.smtp.host}:{self.smtp.port} "
                f"({self.smtp.security}): {exc}"
            ) from exc

        LOG.debug("SMTP-Verbindung zu %s:%s aufgebaut", self.smtp.host, self.smtp.port)
        self._connection = connection
        return connection

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.quit()
            except smtplib.SMTPException:  # pragma: no cover - defensiv
                self._connection.close()
            finally:
                self._connection = None

    # -- Versand ------------------------------------------------------------

    def build_message(
        self,
        subject: str,
        recipients: Sequence[str],
        text_body: str | None,
        html_body: str | None,
        *,
        cc: Sequence[str] = (),
        bcc: Sequence[str] = (),
    ) -> EmailMessage:
        if not text_body and not html_body:
            raise MailError("Die Vorlage hat weder Text- noch HTML-Inhalt erzeugt")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = (
            formataddr((self.mail.sender_name, self.mail.sender)) if self.mail.sender_name else self.mail.sender
        )
        message["To"] = ", ".join(recipients)
        if cc:
            message["Cc"] = ", ".join(cc)
        if self.mail.reply_to:
            message["Reply-To"] = self.mail.reply_to
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=self.mail.sender.split("@")[-1] or None)
        message["Auto-Submitted"] = "auto-generated"
        message["X-Mailer"] = "cmkoffline2mail"

        if text_body and html_body:
            message.set_content(text_body)
            message.add_alternative(html_body, subtype="html")
        elif html_body:
            message.set_content(html_body, subtype="html")
        else:
            message.set_content(text_body or "")
        return message

    def send(
        self,
        subject: str,
        recipients: Sequence[str],
        text_body: str | None,
        html_body: str | None,
        *,
        cc: Sequence[str] = (),
        bcc: Sequence[str] = (),
    ) -> None:
        envelope = list(dict.fromkeys([*recipients, *cc, *bcc]))
        if not envelope:
            raise MailError(f"Mail '{subject}' hat keinen Empfaenger")

        message = self.build_message(subject, recipients, text_body, html_body, cc=cc, bcc=bcc)

        if self.dry_run:
            LOG.info("[Testlauf] Mail an %s wuerde versendet: %s", ", ".join(envelope), subject)
            return

        try:
            connection = self.connect()
            connection.send_message(message, from_addr=self.mail.sender, to_addrs=envelope)
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailError(f"Alle Empfaenger wurden abgelehnt: {exc.recipients}") from exc
        except smtplib.SMTPSenderRefused as exc:
            raise MailError(f"Absender '{self.mail.sender}' wurde abgelehnt: {_decode(exc.smtp_error)}") from exc
        except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
            self._connection = None
            raise MailError(f"Mailversand fehlgeschlagen: {exc}") from exc

        LOG.info("Mail versendet an %s: %s", ", ".join(envelope), subject)


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
