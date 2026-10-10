import tempfile
import unittest
from pathlib import Path

from cmkoffline2mail.checkmk import HostConfigInfo, MonitoredHost
from cmkoffline2mail.config import (
    AppConfig,
    AttributeFilter,
    BehaviourConfig,
    CheckmkConfig,
    FolderRule,
    LoggingConfig,
    MailConfig,
    SmtpConfig,
)
from cmkoffline2mail.evaluate import (
    attribute_placeholder,
    enrich_hosts,
    evaluate,
    group_notifications,
    group_placeholders,
)
from cmkoffline2mail.state import NotificationState

NOW = 1_700_000_000


def make_config(filters=(), folder_filters=(), **behaviour):
    return AppConfig(
        checkmk=CheckmkConfig(
            url="https://cmk.example.com/site1",
            username="automation",
            secret="s",
            site="site1",
            custom_attributes=["standort"],
        ),
        behaviour=BehaviourConfig(**behaviour),
        folders=[
            FolderRule(path="/produktion", offline_for=1800, filters=list(folder_filters)),
            FolderRule(path="/produktion/aussenstellen", offline_for=14400, recipients=["aussen@example.com"]),
        ],
        filters=list(filters),
        mail=MailConfig(sender="cmk@example.com", recipients=["it@example.com"]),
        smtp=SmtpConfig(host="smtp"),
        logging=LoggingConfig(),
        state_file=Path("state.json"),
        source=Path("config.toml"),
    )


def make_host(name, folder="/produktion", offline=7200, **kwargs):
    values = {
        "state": 1,
        "last_time_up": NOW - offline,
        "last_state_change": NOW - offline,
        "address": "10.0.0.1",
        "folder": folder,
        "attributes": {"standort": "Nord"},
    }
    values.update(kwargs)
    return MonitoredHost(name=name, **values)


def fresh_state():
    tmp = tempfile.TemporaryDirectory()
    return NotificationState(path=Path(tmp.name) / "state.json"), tmp


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.state, tmp = fresh_state()
        self.addCleanup(tmp.cleanup)

    def evaluate(self, hosts, config=None):
        return evaluate(hosts, config or make_config(), self.state, NOW)

    def test_host_over_threshold_is_due(self):
        result = self.evaluate([make_host("srv01")])
        self.assertEqual([entry.name for entry in result.due], ["srv01"])
        self.assertEqual(result.due[0].offline_seconds, 7200)
        self.assertEqual(result.due[0].rule.path, "/produktion")

    def test_host_below_threshold_is_skipped(self):
        result = self.evaluate([make_host("srv01", offline=600)])
        self.assertEqual(result.due, [])
        self.assertIn("Schwellwert noch nicht erreicht", result.skipped)

    def test_most_specific_rule_wins(self):
        hosts = [make_host("aussen01", folder="/produktion/aussenstellen/nord", offline=7200)]
        result = self.evaluate(hosts)
        self.assertEqual(result.due, [])  # 2h < 4h der Unterordner-Regel

    def test_downtime_acknowledged_and_pending(self):
        config = make_config(skip_acknowledged=True)
        hosts = [
            make_host("wartung", scheduled_downtime_depth=1),
            make_host("quittiert", acknowledged=1),
            make_host("pending", has_been_checked=0),
            make_host("stumm", notifications_enabled=0),
            make_host("normal"),
        ]
        result = evaluate(hosts, config, self.state, NOW)
        self.assertEqual([entry.name for entry in result.due], ["normal"])

    def test_downtime_can_be_included(self):
        config = make_config(skip_in_downtime=False)
        result = evaluate([make_host("wartung", scheduled_downtime_depth=1)], config, self.state, NOW)
        self.assertEqual([entry.name for entry in result.due], ["wartung"])

    def test_unconfigured_folder_is_reported(self):
        result = self.evaluate([make_host("srv01", folder="/sonstiges")])
        self.assertEqual(result.due, [])
        self.assertIn("/sonstiges", result.unmatched_folders)

    def test_host_without_folder(self):
        result = self.evaluate([make_host("srv01", folder="")])
        self.assertEqual(result.without_folder, ["srv01"])

    def test_missing_offline_timestamp(self):
        host = make_host("srv01")
        host.last_time_up = 0
        host.last_state_change = 0
        result = self.evaluate([host])
        self.assertEqual(result.due, [])
        self.assertIn("Offline-Zeitpunkt unbekannt", result.skipped)

    def test_offline_since_source(self):
        host = make_host("srv01")
        host.last_time_up = 0
        host.last_state_change = NOW - 3600
        result = self.evaluate([host])
        self.assertEqual(result.due[0].offline_seconds, 3600)  # auto faellt zurueck

        config = make_config(offline_since_source="last_time_up")
        state, tmp = fresh_state()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(evaluate([host], config, state, NOW).due, [])

    def test_already_notified_hosts_are_not_due_again(self):
        hosts = [make_host("srv01")]
        self.assertEqual(len(self.evaluate(hosts).due), 1)
        self.state.record_notification("srv01", NOW - 7200, "/produktion", NOW)
        self.assertEqual(self.evaluate(hosts).due, [])

    def test_sorted_by_duration(self):
        hosts = [make_host("kurz", offline=3600), make_host("lang", offline=99999)]
        self.assertEqual([entry.name for entry in self.evaluate(hosts).due], ["lang", "kurz"])


class AttributeFilterTest(unittest.TestCase):
    """Nur melden, wenn ein Custom-Attribut den erwarteten Inhalt hat."""

    def setUp(self):
        self.state, tmp = fresh_state()
        self.addCleanup(tmp.cleanup)

    def due(self, config, hosts):
        state, tmp = fresh_state()
        self.addCleanup(tmp.cleanup)
        return [entry.name for entry in evaluate(hosts, config, state, NOW).due]

    def hosts(self):
        nord = make_host("srv-nord")
        sued = make_host("srv-sued")
        sued.attributes = {"standort": "Werk Sued"}
        ohne = make_host("srv-ohne")
        ohne.attributes = {"standort": ""}
        return [nord, sued, ohne]

    def test_equals_include(self):
        config = make_config(filters=[AttributeFilter(attribute="standort", equals=["Nord"])])
        self.assertEqual(self.due(config, self.hosts()), ["srv-nord"])

    def test_contains_is_case_insensitive_by_default(self):
        config = make_config(filters=[AttributeFilter(attribute="standort", contains=["nord"])])
        self.assertEqual(self.due(config, self.hosts()), ["srv-nord"])

    def test_case_sensitive_when_requested(self):
        config = make_config(
            filters=[AttributeFilter(attribute="standort", contains=["nord"], ignore_case=False)]
        )
        self.assertEqual(self.due(config, self.hosts()), [])

    def test_regex(self):
        config = make_config(filters=[AttributeFilter(attribute="standort", regex="^Werk ")])
        self.assertEqual(self.due(config, self.hosts()), ["srv-sued"])

    def test_not_empty(self):
        config = make_config(filters=[AttributeFilter(attribute="standort", not_empty=True)])
        self.assertEqual(sorted(self.due(config, self.hosts())), ["srv-nord", "srv-sued"])

    def test_exclude_mode(self):
        config = make_config(
            filters=[AttributeFilter(attribute="standort", contains=["Sued"], mode="exclude")]
        )
        # Leeres Attribut wird bei exclude weiterhin gemeldet
        self.assertEqual(sorted(self.due(config, self.hosts())), ["srv-nord", "srv-ohne"])

    def test_missing_value_can_still_notify(self):
        config = make_config(
            filters=[AttributeFilter(attribute="standort", equals=["Nord"], on_missing="notify")]
        )
        self.assertEqual(sorted(self.due(config, self.hosts())), ["srv-nord", "srv-ohne"])

    def test_several_filters_must_all_match(self):
        hosts = [make_host("srv-a"), make_host("srv-b")]
        hosts[0].attributes = {"standort": "Nord", "kritisch": "ja"}
        hosts[1].attributes = {"standort": "Nord", "kritisch": "nein"}
        config = make_config(filters=[
            AttributeFilter(attribute="standort", equals=["Nord"]),
            AttributeFilter(attribute="kritisch", equals=["ja"]),
        ])
        self.assertEqual(self.due(config, hosts), ["srv-a"])

    def test_folder_filter_applies_only_to_its_folder(self):
        hosts = [make_host("srv-prod"), make_host("aussen01", folder="/produktion/aussenstellen", offline=99999)]
        hosts[0].attributes = {"standort": "Sued"}
        hosts[1].attributes = {"standort": "Sued"}
        config = make_config(folder_filters=[AttributeFilter(attribute="standort", equals=["Nord"])])
        self.assertEqual(self.due(config, hosts), ["aussen01"])

    def test_skip_reason_is_reported(self):
        config = make_config(filters=[AttributeFilter(attribute="standort", equals=["Nord"])])
        state, tmp = fresh_state()
        self.addCleanup(tmp.cleanup)
        result = evaluate(self.hosts(), config, state, NOW)
        self.assertTrue(any("Attributfilter" in reason for reason in result.skipped), result.skipped)

    def test_no_filters_means_everything_passes(self):
        self.assertEqual(len(self.due(make_config(), self.hosts())), 3)


class GroupingTest(unittest.TestCase):
    def setUp(self):
        self.state, tmp = fresh_state()
        self.addCleanup(tmp.cleanup)
        self.hosts = [
            make_host("srv01"),
            make_host("srv02"),
            make_host("aussen01", folder="/produktion/aussenstellen", offline=99999),
        ]

    def groups(self, group_by):
        config = make_config(group_by=group_by)
        due = evaluate(self.hosts, config, self.state, NOW).due
        return config, group_notifications(due, config)

    def test_one_mail_per_host(self):
        config, groups = self.groups("host")
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(len(group.hosts) == 1 for group in groups))
        aussen = next(group for group in groups if group.hosts[0].name == "aussen01")
        self.assertEqual(aussen.recipients_override, ["aussen@example.com"])

    def test_one_mail_per_folder(self):
        _, groups = self.groups("folder")
        self.assertEqual(len(groups), 2)
        by_key = {group.key: len(group.hosts) for group in groups}
        self.assertEqual(by_key, {"/produktion": 2, "/produktion/aussenstellen": 1})

    def test_single_summary_mail(self):
        _, groups = self.groups("single")
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].hosts), 3)

    def test_max_hosts_per_mail_splits(self):
        config = make_config(group_by="single", max_hosts_per_mail=2)
        due = evaluate(self.hosts, config, self.state, NOW).due
        groups = group_notifications(due, config)
        self.assertEqual([len(group.hosts) for group in groups], [2, 1])

    def test_no_due_hosts_no_groups(self):
        self.assertEqual(group_notifications([], make_config()), [])


class PlaceholderTest(unittest.TestCase):
    def setUp(self):
        self.state, tmp = fresh_state()
        self.addCleanup(tmp.cleanup)

    def test_attribute_placeholder_names(self):
        self.assertEqual(attribute_placeholder("standort"), "attr_standort")
        self.assertEqual(attribute_placeholder("Wartungs-Fenster"), "attr_wartungs_fenster")
        self.assertEqual(attribute_placeholder("!!"), "attr_unbekannt")

    def test_host_placeholders(self):
        config = make_config()
        due = evaluate([make_host("srv01")], config, self.state, NOW).due
        values = due[0].placeholders(config)
        self.assertEqual(values["host"], "srv01")
        self.assertEqual(values["ip"], "10.0.0.1")
        self.assertEqual(values["state"], "DOWN")
        self.assertEqual(values["attr_standort"], "Nord")
        self.assertEqual(values["offline_for"], "2 Std.")
        self.assertEqual(values["threshold"], "30 Min.")

    def test_group_placeholders_for_single_host_mail(self):
        config = make_config(group_by="host")
        due = evaluate([make_host("srv01")], config, self.state, NOW).due
        group = group_notifications(due, config)[0]
        values = group_placeholders(group, config, NOW)
        self.assertEqual(values["host"], "srv01")
        self.assertEqual(values["ip"], "10.0.0.1")
        self.assertEqual(values["host_count"], "1")
        self.assertEqual(values["attr_standort"], "Nord")
        self.assertEqual(values["site"], "site1")

    def test_group_placeholders_merge_attributes(self):
        config = make_config(group_by="folder")
        hosts = [make_host("srv01"), make_host("srv02")]
        hosts[1].attributes = {"standort": "Sued"}
        hosts[1].address = "10.0.0.2"
        due = evaluate(hosts, config, self.state, NOW).due
        values = group_placeholders(group_notifications(due, config)[0], config, NOW)
        self.assertEqual(values["host_count"], "2")
        self.assertEqual(sorted(values["attr_standort"].split(", ")), ["Nord", "Sued"])
        self.assertEqual(sorted(values["addresses"].split(", ")), ["10.0.0.1", "10.0.0.2"])


class EnrichTest(unittest.TestCase):
    def test_enrich_adds_folder_attributes_and_fallback_ip(self):
        host = MonitoredHost(name="srv01", state=1, address="", alias="")
        enrich_hosts(
            [host],
            {"srv01": HostConfigInfo(folder="/produktion", ipaddress="10.9.9.9", alias="Alias",
                                     attributes={"standort": "Nord"})},
            ["standort", "fehlt"],
        )
        self.assertEqual(host.folder, "/produktion")
        self.assertEqual(host.address, "10.9.9.9")
        self.assertEqual(host.alias, "Alias")
        self.assertEqual(host.attributes, {"standort": "Nord", "fehlt": ""})

    def test_livestatus_address_wins(self):
        host = MonitoredHost(name="srv01", address="10.0.0.1")
        enrich_hosts([host], {"srv01": HostConfigInfo(folder="/a", ipaddress="10.9.9.9")}, [])
        self.assertEqual(host.address, "10.0.0.1")

    def test_unknown_host_is_left_alone(self):
        host = MonitoredHost(name="srv01")
        enrich_hosts([host], {}, ["standort"])
        self.assertEqual(host.folder, "")


if __name__ == "__main__":
    unittest.main()
