import tempfile
import unittest
from pathlib import Path

from cmkoffline2mail.render import MailTemplate, TemplateError, render_string

TEMPLATE = """
format = "both"

[text]
body = "Ordner {{ folder }} / {{ host_count }} Host(s)\\n{{ host_rows }}"
host_row = "- {{ host }} ({{ ip }}) {{ attr_standort }}\\n"

[html]
body = "<p>{{ folder }}</p><table>{{ host_rows }}</table>"
host_row = "<tr><td>{{ host }}</td><td>{{ attr_standort }}</td></tr>"
"""


class RenderStringTest(unittest.TestCase):
    def test_replaces_with_and_without_spaces(self):
        self.assertEqual(render_string("{{a}}-{{ a }}-{{  a  }}", {"a": "x"}), "x-x-x")

    def test_unknown_placeholder_raises_with_hint(self):
        with self.assertRaises(TemplateError) as ctx:
            render_string("Hallo {{ tippfehler }}", {"host": "srv01"}, context="[text].body")
        message = str(ctx.exception)
        self.assertIn("tippfehler", message)
        self.assertIn("[text].body", message)
        self.assertIn("host", message)

    def test_html_escaping(self):
        values = {"host": "<b>srv</b> & co"}
        self.assertEqual(render_string("{{ host }}", values, escape=True), "&lt;b&gt;srv&lt;/b&gt; &amp; co")
        self.assertEqual(render_string("{{ host }}", values, escape=False), "<b>srv</b> & co")

    def test_raw_placeholders_are_not_escaped(self):
        rendered = render_string("{{ host_rows }}", {"host_rows": "<tr><td>x</td></tr>"}, escape=True)
        self.assertEqual(rendered, "<tr><td>x</td></tr>")

    def test_none_values_become_empty(self):
        self.assertEqual(render_string("[{{ a }}]", {"a": ""}), "[]")


class MailTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, text, name="mailtemplate.toml"):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def group_values(self):
        return {"folder": "/produktion", "host_count": "2"}

    def host_values(self):
        return [
            {"host": "srv01", "ip": "10.0.0.1", "attr_standort": "Nord"},
            {"host": "srv02", "ip": "10.0.0.2", "attr_standort": "Süd & Ost"},
        ]

    def test_render_both(self):
        template = MailTemplate.load(self.write(TEMPLATE))
        text, html = template.render(self.group_values(), self.host_values())
        self.assertIn("Ordner /produktion / 2 Host(s)", text)
        self.assertIn("- srv01 (10.0.0.1) Nord", text)
        self.assertIn("- srv02 (10.0.0.2) Süd & Ost", text)
        self.assertIn("<td>srv01</td>", html)
        self.assertIn("Süd &amp; Ost", html)

    def test_format_text_only(self):
        template = MailTemplate.load(self.write(TEMPLATE.replace('format = "both"', 'format = "text"')))
        text, html = template.render(self.group_values(), self.host_values())
        self.assertIsNotNone(text)
        self.assertIsNone(html)

    def test_format_html_only(self):
        template = MailTemplate.load(self.write(TEMPLATE.replace('format = "both"', 'format = "html"')))
        text, html = template.render(self.group_values(), self.host_values())
        self.assertIsNone(text)
        self.assertIsNotNone(html)

    def test_defaults_when_sections_missing(self):
        template = MailTemplate.load(self.write('format = "text"'))
        text, _ = template.render(
            {
                "folder": "/a",
                "threshold": "2 Std.",
                "now": "heute",
                "checkmk_url": "https://cmk",
            },
            [
                {
                    "host": "srv01",
                    "ip": "10.0.0.1",
                    "state": "DOWN",
                    "offline_since": "gestern",
                    "offline_for": "3 Std.",
                }
            ],
        )
        self.assertIn("srv01", text)

    def test_invalid_files(self):
        with self.assertRaises(TemplateError):
            MailTemplate.load(self.dir / "fehlt.toml")
        with self.assertRaises(TemplateError):
            MailTemplate.load(self.write("format = ", "kaputt.toml"))
        with self.assertRaises(TemplateError):
            MailTemplate.load(self.write('format = "pdf"', "format.toml"))
        with self.assertRaises(TemplateError):
            MailTemplate.load(self.write('format = "text"\n[text]\nbody = "  "', "leer.toml"))

    def test_unknown_placeholder_in_file(self):
        template = MailTemplate.load(self.write('format = "text"\n[text]\nbody = "{{ nixda }}"', "u.toml"))
        with self.assertRaises(TemplateError):
            template.render(self.group_values(), self.host_values())


if __name__ == "__main__":
    unittest.main()
