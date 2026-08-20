import unittest

from cmkoffline2mail.checkmk import MonitoredHost, _host_config_info, _lookup_attribute
from cmkoffline2mail.config import CheckmkConfig


def make_config(url="https://cmk.example.com/site1", **kwargs):
    return CheckmkConfig(url=url, username="automation", secret="s3cr3t", **kwargs)


class MonitoredHostTest(unittest.TestCase):
    def test_from_api(self):
        host = MonitoredHost.from_api(
            {
                "id": "srv01",
                "extensions": {
                    "name": "srv01",
                    "state": 1,
                    "last_time_up": 1700000000,
                    "scheduled_downtime_depth": 2,
                    "address": "10.0.0.1",
                    "plugin_output": None,
                },
            }
        )
        self.assertEqual(host.name, "srv01")
        self.assertEqual(host.state_name, "DOWN")
        self.assertTrue(host.in_downtime)
        self.assertEqual(host.address, "10.0.0.1")
        self.assertEqual(host.plugin_output, "")
        self.assertEqual(host.notifications_enabled, 1)

    def test_state_names(self):
        self.assertEqual(MonitoredHost(name="a", state=0).state_name, "UP")
        self.assertEqual(MonitoredHost(name="a", state=2).state_name, "UNREACHABLE")
        self.assertEqual(MonitoredHost(name="a", state=9).state_name, "STATE 9")

    def test_broken_values_do_not_crash(self):
        host = MonitoredHost.from_api({"extensions": {"name": "srv", "state": "kaputt"}})
        self.assertEqual(host.state, 0)


class HostConfigInfoTest(unittest.TestCase):
    def test_folder_ip_and_custom_attributes(self):
        info = _host_config_info(
            {
                "extensions": {
                    "folder": "~produktion~nord",
                    "attributes": {
                        "ipaddress": "192.0.2.5",
                        "alias": "Serverchen",
                        "standort": "Werk Nord",
                        "labels": {"betreiber": "Team A"},
                        "tag_kritikalitaet": "hoch",
                    },
                }
            },
            ["standort", "betreiber", "kritikalitaet", "fehlt"],
        )
        self.assertEqual(info.folder, "/produktion/nord")
        self.assertEqual(info.ipaddress, "192.0.2.5")
        self.assertEqual(info.alias, "Serverchen")
        self.assertEqual(info.attributes["standort"], "Werk Nord")
        self.assertEqual(info.attributes["betreiber"], "Team A")
        self.assertEqual(info.attributes["kritikalitaet"], "hoch")
        self.assertEqual(info.attributes["fehlt"], "")

    def test_without_folder(self):
        self.assertIsNone(_host_config_info({"extensions": {"attributes": {}}}, []))

    def test_attribute_value_types(self):
        info = _host_config_info(
            {"extensions": {"folder": "/", "attributes": {"aktiv": True, "kontakte": ["a", "b"]}}},
            ["aktiv", "kontakte"],
        )
        self.assertEqual(info.attributes["aktiv"], "ja")
        self.assertEqual(info.attributes["kontakte"], "a, b")

    def test_lookup_is_case_insensitive(self):
        self.assertEqual(_lookup_attribute({"Standort": "Nord"}, "standort"), "Nord")


class UrlTest(unittest.TestCase):
    def test_gui_and_api_url(self):
        config = make_config("https://cmk.example.com/site1/check_mk/api/1.0")
        self.assertEqual(config.gui_url, "https://cmk.example.com/site1/check_mk")
        self.assertEqual(config.api_url, "https://cmk.example.com/site1/check_mk/api/1.0")


if __name__ == "__main__":
    unittest.main()
