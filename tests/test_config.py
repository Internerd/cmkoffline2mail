import os
import tempfile
import unittest
from pathlib import Path

from cmkoffline2mail.config import (
    ConfigError,
    default_config_path,
    install_root,
    load_config,
    normalize_folder,
)

MINIMAL = """
[checkmk]
url = "https://cmk.example.com/site1"
username = "automation"
secret = "s3cr3t"

[[folders]]
path = "/produktion"
offline_for = "30m"

[mail]
from = "checkmk@example.com"
to = ["it@example.com"]

[smtp]
host = "smtp.example.com"
"""


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, text: str, name: str = "config.toml") -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_minimal_config(self):
        config = load_config(self.write(MINIMAL))
        self.assertEqual(config.checkmk.api_url, "https://cmk.example.com/site1/check_mk/api/1.0")
        self.assertEqual(config.checkmk.gui_url, "https://cmk.example.com/site1/check_mk")
        self.assertEqual(config.checkmk.site, "site1")
        self.assertEqual(config.behaviour.group_by, "host")
        self.assertEqual(config.folders[0].offline_for, 1800)
        self.assertEqual(config.mail.recipients, ["it@example.com"])
        self.assertEqual(config.smtp.port, 25)
        self.assertEqual(config.mail.template, self.dir / "mailtemplate.toml")

    def test_api_url_variants(self):
        for url, expected in [
            ("https://cmk/site1/", "https://cmk/site1/check_mk/api/1.0"),
            ("https://cmk/site1/check_mk", "https://cmk/site1/check_mk/api/1.0"),
            ("https://cmk/site1/check_mk/api/1.0", "https://cmk/site1/check_mk/api/1.0"),
        ]:
            config = load_config(self.write(MINIMAL.replace("https://cmk.example.com/site1", url)))
            self.assertEqual(config.checkmk.api_url, expected)

    def test_smtp_default_port_follows_security(self):
        config = load_config(self.write(MINIMAL + '\nsecurity = "starttls"\n'))
        self.assertEqual(config.smtp.port, 587)
        config = load_config(self.write(MINIMAL + '\nsecurity = "ssl"\n'))
        self.assertEqual(config.smtp.port, 465)

    def test_secret_from_env_and_file(self):
        os.environ["TEST_CMK_SECRET"] = "aus-env"
        self.addCleanup(os.environ.pop, "TEST_CMK_SECRET", None)
        config = load_config(
            self.write(MINIMAL.replace('secret = "s3cr3t"', 'secret_env = "TEST_CMK_SECRET"'))
        )
        self.assertEqual(config.checkmk.secret, "aus-env")

        secret_file = self.dir / "secret.txt"
        secret_file.write_text("aus-datei\n", encoding="utf-8")
        config = load_config(
            self.write(MINIMAL.replace('secret = "s3cr3t"', f'secret_file = "{secret_file}"'))
        )
        self.assertEqual(config.checkmk.secret, "aus-datei")

    def test_rule_for_prefers_most_specific(self):
        text = MINIMAL + """
[[folders]]
path = "/produktion/aussenstellen"
offline_for = "4h"

[[folders]]
path = "/archiv"
offline_for = "7d"
include_subfolders = false
"""
        config = load_config(self.write(text))
        self.assertEqual(config.rule_for("/produktion/webserver").offline_for, 1800)
        self.assertEqual(config.rule_for("/produktion/aussenstellen/nord").offline_for, 14400)
        self.assertEqual(config.rule_for("/archiv").offline_for, 604800)
        self.assertIsNone(config.rule_for("/archiv/2019"))
        self.assertIsNone(config.rule_for("/sonstiges"))

    def test_folder_matching_is_case_insensitive_and_tolerant(self):
        config = load_config(self.write(MINIMAL))
        for variant in ("/Produktion", "produktion", "~produktion", "/produktion/"):
            self.assertIsNotNone(config.rule_for(variant), variant)

    def test_unknown_keys_are_reported_as_warning(self):
        config = load_config(self.write(MINIMAL + '\n[behaviour]\ntippfehler = true\n'))
        self.assertTrue(any("tippfehler" in warning for warning in config.warnings))

    def test_errors(self):
        cases = {
            "missing folder": MINIMAL.replace('[[folders]]\npath = "/produktion"\noffline_for = "30m"\n', ""),
            "bad duration": MINIMAL.replace('"30m"', '"zwei Stunden"'),
            "zero duration": MINIMAL.replace('"30m"', '"0"'),
            "no recipient": MINIMAL.replace('to = ["it@example.com"]', "to = []"),
            "bad url": MINIMAL.replace("https://cmk.example.com/site1", "cmk.example.com"),
            "no secret": MINIMAL.replace('secret = "s3cr3t"', ""),
            "duplicate folder": MINIMAL + '\n[[folders]]\npath = "/Produktion"\noffline_for = "1h"\n',
            "user without password": MINIMAL + '\nusername = "mailer"\n',
            "all folders disabled": MINIMAL.replace('offline_for = "30m"', 'offline_for = "30m"\nenabled = false'),
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(ConfigError):
                    load_config(self.write(text, f"{abs(hash(name))}.toml"))

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(self.dir / "gibtsnicht.toml")

    def test_broken_toml(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("[checkmk\nurl ="))


class PathResolutionTest(unittest.TestCase):
    """Alle Pfade sollen relativ zur Konfigurationsdatei gelten."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "config").mkdir()
        self.config_path = self.dir / "config" / "config.toml"

    def write(self, extra: str = "") -> Path:
        self.config_path.write_text(MINIMAL + extra, encoding="utf-8")
        return self.config_path

    def test_relative_paths_are_relative_to_the_config_file(self):
        config = load_config(
            self.write('\n[state]\nfile = "../data/state.json"\n\n[logging]\nfile = "../logs/app.log"\n')
        )
        self.assertEqual(config.state_file, self.dir / "data" / "state.json")
        self.assertEqual(config.logging.file, self.dir / "logs" / "app.log")

    def test_template_defaults_next_to_the_config_file(self):
        config = load_config(self.write())
        self.assertEqual(config.mail.template, self.dir / "config" / "mailtemplate.toml")

    def test_relative_template(self):
        self.config_path.write_text(
            MINIMAL.replace('to = ["it@example.com"]', 'to = ["it@example.com"]\ntemplate = "vorlagen/mail.toml"'),
            encoding="utf-8",
        )
        config = load_config(self.config_path)
        self.assertEqual(config.mail.template, self.dir / "config" / "vorlagen" / "mail.toml")

    def test_absolute_paths_are_kept(self):
        absolute = Path(self.tmp.name).resolve() / "woanders" / "state.json"
        config = load_config(self.write(f'\n[state]\nfile = "{absolute.as_posix()}"\n'))
        self.assertEqual(config.state_file, absolute)

    def test_relative_secret_file(self):
        (self.dir / "config" / "secret.txt").write_text("geheim\n", encoding="utf-8")
        self.config_path.write_text(
            MINIMAL.replace('secret = "s3cr3t"', 'secret_file = "secret.txt"'), encoding="utf-8"
        )
        config = load_config(self.config_path)
        self.assertEqual(config.checkmk.secret, "geheim")

    def test_defaults_land_under_the_install_root(self):
        config = load_config(self.write())
        root = install_root()
        self.assertEqual(config.state_file, root / "data" / "state.json")
        self.assertEqual(config.logging.file, root / "logs" / "cmkoffline2mail.log")

    def test_environment_variable_selects_the_config(self):
        os.environ["CMKOFFLINE2MAIL_CONFIG"] = str(self.write())
        self.addCleanup(os.environ.pop, "CMKOFFLINE2MAIL_CONFIG", None)
        self.assertEqual(default_config_path(), self.config_path)


class NormalizeFolderTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(normalize_folder("~produktion~nord"), "/produktion/nord")
        self.assertEqual(normalize_folder("produktion//nord/"), "/produktion/nord")
        self.assertEqual(normalize_folder("\\produktion\\nord"), "/produktion/nord")
        self.assertEqual(normalize_folder("/"), "/")


if __name__ == "__main__":
    unittest.main()
