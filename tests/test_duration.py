import unittest

from cmkoffline2mail.duration import DurationError, format_duration, parse_duration


class ParseDurationTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("45s"), 45)
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("90min"), 5400)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("3d"), 259200)
        self.assertEqual(parse_duration("2w"), 1209600)

    def test_combined_and_case_insensitive(self):
        self.assertEqual(parse_duration("1d12h"), 129600)
        self.assertEqual(parse_duration("1D 12H"), 129600)
        self.assertEqual(parse_duration("4 Std"), 14400)

    def test_numbers_are_seconds(self):
        self.assertEqual(parse_duration(3600), 3600)
        self.assertEqual(parse_duration("3600"), 3600)

    def test_disabled_values(self):
        for value in ("0", "off", "aus", "nie", "never", ""):
            self.assertEqual(parse_duration(value), 0)

    def test_invalid(self):
        for value in ("zwei Stunden", "5x", "-1h", True, None):
            with self.assertRaises(DurationError):
                parse_duration(value)
        with self.assertRaises(DurationError):
            parse_duration(-5)

    def test_error_mentions_context(self):
        with self.assertRaises(DurationError) as ctx:
            parse_duration("5x", context="folders[1].offline_for")
        self.assertIn("folders[1].offline_for", str(ctx.exception))


class FormatDurationTest(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(format_duration(30), "30 Sek.")
        self.assertEqual(format_duration(1800), "30 Min.")
        self.assertEqual(format_duration(5400), "1 Std. 30 Min.")
        self.assertEqual(format_duration(86400), "1 Tag")
        self.assertEqual(format_duration(180000), "2 Tage 2 Std.")
        self.assertEqual(format_duration(-5), "0 Sek.")


if __name__ == "__main__":
    unittest.main()
