import tempfile
import unittest
from pathlib import Path

from cmkoffline2mail.state import NotificationState

NOW = 1_700_000_000


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "unterordner" / "state.json"

    def test_first_run_notifies(self):
        state = NotificationState.load(self.path)
        notify, reason = state.should_notify("srv01", NOW - 7200, 86400, NOW)
        self.assertTrue(notify)
        self.assertEqual(reason, "neu")

    def test_no_repeat_within_interval(self):
        state = NotificationState.load(self.path)
        state.record_notification("srv01", NOW - 7200, "/produktion", NOW)
        notify, _ = state.should_notify("srv01", NOW - 7200, 86400, NOW + 3600)
        self.assertFalse(notify)

    def test_repeat_after_interval(self):
        state = NotificationState.load(self.path)
        state.record_notification("srv01", NOW - 7200, "/produktion", NOW)
        notify, reason = state.should_notify("srv01", NOW - 7200, 86400, NOW + 86400)
        self.assertTrue(notify)
        self.assertEqual(reason, "Wiederholung")

    def test_repeat_disabled(self):
        state = NotificationState.load(self.path)
        state.record_notification("srv01", NOW - 7200, "/produktion", NOW)
        notify, _ = state.should_notify("srv01", NOW - 7200, 0, NOW + 10 * 86400)
        self.assertFalse(notify)

    def test_new_outage_notifies_again(self):
        state = NotificationState.load(self.path)
        state.record_notification("srv01", NOW - 7200, "/produktion", NOW)
        notify, reason = state.should_notify("srv01", NOW + 3600, 86400, NOW + 4000)
        self.assertTrue(notify)
        self.assertEqual(reason, "erneut offline")

    def test_small_timestamp_drift_is_tolerated(self):
        state = NotificationState.load(self.path)
        state.record_notification("srv01", NOW - 7200, "/produktion", NOW)
        notify, _ = state.should_notify("srv01", NOW - 7200 + 30, 86400, NOW + 60)
        self.assertFalse(notify)

    def test_roundtrip_and_directory_creation(self):
        state = NotificationState.load(self.path)
        state.record_notification("srv01", NOW - 7200, "/produktion", NOW)
        state.save(now=NOW)
        self.assertTrue(self.path.exists())

        reloaded = NotificationState.load(self.path)
        self.assertEqual(reloaded.last_run, NOW)
        self.assertEqual(reloaded.hosts["srv01"].notify_count, 1)
        self.assertEqual(reloaded.hosts["srv01"].folder, "/produktion")

    def test_forget_recovered(self):
        state = NotificationState.load(self.path)
        state.record_notification("srv01", NOW, "/a", NOW)
        state.record_notification("srv02", NOW, "/a", NOW)
        self.assertEqual(state.forget_recovered({"srv01"}), ["srv02"])
        self.assertNotIn("srv02", state.hosts)

    def test_touch_resets_on_new_outage(self):
        state = NotificationState.load(self.path)
        state.record_notification("srv01", NOW - 7200, "/a", NOW)
        state.touch("srv01", NOW + 5000, "/a")
        self.assertEqual(state.hosts["srv01"].last_notified, 0)
        self.assertEqual(state.hosts["srv01"].notify_count, 0)

    def test_broken_state_file_starts_over(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{kaputt", encoding="utf-8")
        state = NotificationState.load(self.path)
        self.assertEqual(state.hosts, {})

    def test_unexpected_structure_starts_over(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(NotificationState.load(self.path).hosts, {})


if __name__ == "__main__":
    unittest.main()
