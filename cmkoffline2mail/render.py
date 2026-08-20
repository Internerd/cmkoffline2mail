"""Zweite Konfigurationsdatei: Mail-Vorlage (HTML und/oder reiner Text)."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

LOG = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

#: Platzhalter, die bereits fertiges Markup enthalten und nicht maskiert werden.
RAW_PLACEHOLDERS = frozenset({"host_rows", "host_table"})

DEFAULT_TEXT_BODY = """Folgende Hosts im Ordner {{ folder }} sind laenger als {{ threshold }} nicht erreichbar:

{{ host_rows }}
Stand: {{ now }}
Checkmk: {{ checkmk_url }}
"""

DEFAULT_TEXT_ROW = "- {{ host }} ({{ ip }}) - {{ state }} seit {{ offline_since }} ({{ offline_for }})\n"

DEFAULT_HTML_BODY = """<html><body style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;">
<p>Folgende Hosts im Ordner <b>{{ folder }}</b> sind laenger als {{ threshold }} nicht erreichbar:</p>
<table cellpadding="6" cellspacing="0" border="1" style="border-collapse:collapse;">
<tr style="background:#eeeeee;"><th>Host</th><th>IP</th><th>Status</th><th>Offline seit</th><th>Dauer</th></tr>
{{ host_rows }}
</table>
<p style="color:#666666;">Stand: {{ now }} - <a href="{{ checkmk_url }}">Checkmk oeffnen</a></p>
</body></html>
"""

DEFAULT_HTML_ROW = (
    "<tr><td><a href=\"{{ url }}\">{{ host }}</a></td><td>{{ ip }}</td><td>{{ state }}</td>"
    "<td>{{ offline_since }}</td><td>{{ offline_for }}</td></tr>\n"
)


class TemplateError(Exception):
    """Fehler in der Mail-Vorlage."""


def render_string(
    template: str,
    values: Mapping[str, str],
    *,
    escape: bool = False,
    raw_keys: Iterable[str] = RAW_PLACEHOLDERS,
    context: str = "Vorlage",
) -> str:
    """Ersetzt ``{{ platzhalter }}`` durch die passenden Werte."""
    raw = set(raw_keys)
    unknown: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            unknown.append(key)
            return match.group(0)
        value = values[key] or ""
        if escape and key not in raw:
            return html.escape(value)
        return value

    result = _PLACEHOLDER.sub(replace, template)
    if unknown:
        available = ", ".join(sorted(values))
        raise TemplateError(
            f"Unbekannte Platzhalter in {context}: {', '.join(sorted(set(unknown)))}. "
            f"Verfuegbar sind: {available}"
        )
    return result


@dataclass
class MailTemplate:
    """Inhalt der Vorlagendatei."""

    format: str = "both"
    text_body: str = DEFAULT_TEXT_BODY
    text_row: str = DEFAULT_TEXT_ROW
    html_body: str = DEFAULT_HTML_BODY
    html_row: str = DEFAULT_HTML_ROW
    source: Path | None = None

    @property
    def wants_text(self) -> bool:
        return self.format in ("text", "both")

    @property
    def wants_html(self) -> bool:
        return self.format in ("html", "both")

    @classmethod
    def load(cls, path: str | Path) -> "MailTemplate":
        if tomllib is None:  # pragma: no cover
            raise TemplateError("Python 3.11+ oder das Paket 'tomli' wird zum Lesen der Vorlage benoetigt")

        template_path = Path(path).expanduser()
        try:
            with template_path.open("rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError as exc:
            raise TemplateError(f"Vorlagendatei '{template_path}' nicht gefunden") from exc
        except OSError as exc:
            raise TemplateError(f"Vorlagendatei '{template_path}' nicht lesbar: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise TemplateError(f"Vorlagendatei '{template_path}' ist kein gueltiges TOML: {exc}") from exc

        fmt = str(data.get("format", "both")).strip().lower()
        if fmt not in ("text", "html", "both"):
            raise TemplateError(f"'format' in '{template_path}' muss text, html oder both sein (ist: '{fmt}')")

        text_section = data.get("text") or {}
        html_section = data.get("html") or {}
        if not isinstance(text_section, dict) or not isinstance(html_section, dict):
            raise TemplateError(f"Die Abschnitte [text] und [html] in '{template_path}' muessen Tabellen sein")

        template = cls(
            format=fmt,
            text_body=str(text_section.get("body", DEFAULT_TEXT_BODY)),
            text_row=str(text_section.get("host_row", DEFAULT_TEXT_ROW)),
            html_body=str(html_section.get("body", DEFAULT_HTML_BODY)),
            html_row=str(html_section.get("host_row", DEFAULT_HTML_ROW)),
            source=template_path,
        )

        if template.wants_text and not template.text_body.strip():
            raise TemplateError(f"[text].body in '{template_path}' ist leer")
        if template.wants_html and not template.html_body.strip():
            raise TemplateError(f"[html].body in '{template_path}' ist leer")
        return template

    def render(
        self,
        group_values: Mapping[str, str],
        host_values: Sequence[Mapping[str, str]],
    ) -> tuple[str | None, str | None]:
        """Erzeugt (Textkoerper, HTML-Koerper) - jeweils ``None``, wenn nicht gewuenscht."""
        text = html_body = None

        if self.wants_text:
            rows = "".join(
                render_string(self.text_row, values, context="[text].host_row") for values in host_values
            )
            text = render_string(
                self.text_body, {**group_values, "host_rows": rows, "host_table": rows}, context="[text].body"
            )

        if self.wants_html:
            rows = "".join(
                render_string(self.html_row, values, escape=True, context="[html].host_row")
                for values in host_values
            )
            html_body = render_string(
                self.html_body,
                {**group_values, "host_rows": rows, "host_table": rows},
                escape=True,
                context="[html].body",
            )

        return text, html_body
