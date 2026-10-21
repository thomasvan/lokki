import copy
import os
import sys
import tempfile
import time
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import config, migrate  # noqa: E402
from lokki_core.quota import poller, policy, reader  # noqa: E402

# The three keys of `config.example.yaml` before `low_quota.enabled` was retired, built here
# and never read from the regenerated file: a copy of that file is what `migrate` must keep
# cutting every request.
OLD_EXAMPLE = {
    "cliproxy": {"management_secret_file": ""},
    "routing": "auto",
    "low_quota": {"enabled": False, "threshold_percent": 30, "rule": "all", "accounts": [], "poll_seconds": 60},
}


class MigrateTests(unittest.TestCase):
    def test_each_rename_moves_the_value_once(self):
        # (label, renames, before, after, applied)
        rows = [
            ("a top-level key moves",
             [("retries_old", "retries")], {"retries_old": 3}, {"retries": 3}, [("retries_old", "retries")]),
            ("a key moves into a section",
             [("poll_seconds", "low_quota.poll_seconds")], {"poll_seconds": 30},
             {"low_quota": {"poll_seconds": 30}}, [("poll_seconds", "low_quota.poll_seconds")]),
            ("a key moves out of a section, and the empty section goes",
             [("cliproxy.panel_on", "panel")], {"cliproxy": {"panel_on": True}}, {"panel": True},
             [("cliproxy.panel_on", "panel")]),
            ("a key moves between sections",
             [("a.x", "b.x")], {"a": {"x": 1, "y": 2}}, {"a": {"y": 2}, "b": {"x": 1}}, [("a.x", "b.x")]),
            ("nothing to move",
             [("old", "new")], {"new": 1}, {"new": 1}, []),
            ("a key present under both names keeps the new one",
             [("old", "new")], {"old": 1, "new": 2}, {"new": 2}, [("old", "new")]),
            ("an old name whose section is not a mapping is left alone",
             [("a.x", "b")], {"a": "text"}, {"a": "text"}, []),
            ("two renames apply in table order",
             [("a", "b"), ("b", "c")], {"a": 1}, {"c": 1}, [("a", "b"), ("b", "c")]),
        ]
        for label, renames, before, after, applied in rows:
            with self.subTest(label):
                doc = dict(before)
                self.assertEqual(migrate.migrate(doc, renames=renames), applied)
                self.assertEqual(doc, after)

    def test_the_retired_key_sets_the_mode_a_file_chose(self):
        secret = {"cliproxy": {"management_secret_file": "/data/secrets/cliproxy-mgmt.secret"}}
        # (label, before, after, applied)
        rows = [
            ("on with no routing chose Auto under the old default",
             {"low_quota": {"enabled": True, "rule": "any"}},
             {"low_quota": {"rule": "any"}, "routing": "auto"}, [("low_quota.enabled", "routing: auto")]),
            ("on with a routing keeps that routing",
             {"routing": "direct", "low_quota": {"enabled": True}}, {"routing": "direct"},
             [("low_quota.enabled", None)]),
            ("off under auto with no secret file cut every request",
             {"routing": "auto", "low_quota": {"enabled": False}}, {"routing": "relay"},
             [("low_quota.enabled", "routing: relay")]),
            ("off under auto with an empty secret file name cut every request",
             {"routing": "auto", "low_quota": {"enabled": False}, "cliproxy": {"management_secret_file": ""}},
             {"routing": "relay", "cliproxy": {"management_secret_file": ""}},
             [("low_quota.enabled", "routing: relay")]),
            ("off under auto with a secret file named follows Auto's rule",
             {"routing": "auto", "low_quota": {"enabled": False}, **secret}, {"routing": "auto", **secret},
             [("low_quota.enabled", None)]),
            ("off under relay with no secret keeps relay",
             {"routing": "relay", "low_quota": {"enabled": False}}, {"routing": "relay"},
             [("low_quota.enabled", None)]),
            ("off under direct with no secret keeps direct",
             {"routing": "direct", "low_quota": {"enabled": False}}, {"routing": "direct"},
             [("low_quota.enabled", None)]),
            ("off with no routing sets none: the default decides",
             {"low_quota": {"enabled": False}}, {}, [("low_quota.enabled", None)]),
            ("the old example cuts every request",
             OLD_EXAMPLE, {**{k: v for k, v in OLD_EXAMPLE.items() if k != "low_quota"}, "routing": "relay",
                           "low_quota": {"threshold_percent": 30, "rule": "all", "accounts": [], "poll_seconds": 60}},
             [("low_quota.enabled", "routing: relay")]),
            ("no retired key changes nothing",
             {"routing": "auto"}, {"routing": "auto"}, []),
        ]
        # Read as `config._flag` reads a flag: these words are true in any case; anything else,
        # a typo included, is not true and never refuses the file.
        for word, chose in (("yes", "auto"), ("on", "auto"), ("1", "auto"), ("TRUE", "auto"), (True, "auto"),
                            ("maybe", "relay"), ("off", "relay"), (None, "relay"), (0, "relay")):
            rows.append((f"{word!r} under auto with no secret", {"routing": "auto", "low_quota": {"enabled": word}},
                         {"routing": "auto" if chose == "auto" else "relay"},
                         [("low_quota.enabled", None if chose == "auto" else "routing: relay")]))
            rows.append((f"{word!r} with no routing", {"low_quota": {"enabled": word}},
                         {"routing": "auto"} if chose == "auto" else {},
                         [("low_quota.enabled", "routing: auto" if chose == "auto" else None)]))
        for label, before, after, applied in rows:
            with self.subTest(label):
                doc = copy.deepcopy(before)
                self.assertEqual(migrate.migrate(doc), applied)
                self.assertEqual(doc, after)
                self.assertEqual(migrate.migrate(doc), [])          # once
        # Each pair as `init` notes it.
        self.assertEqual([migrate.said(p) for p in (("low_quota.enabled", "routing: relay"),
                                                    ("low_quota.enabled", "routing: auto"),
                                                    ("low_quota.enabled", None),
                                                    ("retries_old", "retries"))],
                         ["low_quota.enabled -> routing: relay", "low_quota.enabled -> routing: auto",
                          "low_quota.enabled -> (dropped)", "retries_old -> retries"])


    def test_the_retired_key_is_read_as_config_reads_a_flag(self):
        """`migrate` keeps its own copy of `config._flag`'s truth set, since `config` imports it;
        this holds the copy to the loader's, so neither can change alone."""
        for value in (True, False, None, 1, 0, 2, "1", "true", "yes", "on", "TRUE", " Yes ", "On",
                      "0", "false", "no", "off", "", "maybe", "tru", "yes!", "y", "enabled"):
            with self.subTest(value=value):
                self.assertEqual(migrate._true(value), config._flag(value))


class LoadTests(unittest.TestCase):
    """`config.load_settings` runs `migrate` before it resolves, so the retired key decides
    nothing at load but the mode `migrate` set from it."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def load(self, doc):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yaml", dir=self.dir, delete=False) as f:
            yaml.safe_dump(doc, f)
        settings = config.load_settings(config_path=f.name, env={})
        settings.pop("config_path")
        return settings

    def decide(self, settings):
        """What Auto answers with no snapshot, given the read error the poller would record."""
        try:
            poller.management_secret(settings["cliproxy"])
            error = None
        except reader.ManagementError as e:
            error = str(e)
        return policy.decide(None, settings["low_quota"], settings["routing"], time.time(), error)

    def test_the_retired_key_is_read_dropped_and_decides_nothing_at_load(self):
        unreadable = {"cliproxy": {"management_secret_file": os.path.join(self.dir, "missing.secret")}}
        for value in (True, False, "yes", "maybe", None, 7, [1]):
            for routing in (None, "auto", "relay", "direct"):
                with self.subTest(value=value, routing=routing):
                    doc = {"low_quota": {"enabled": value, "threshold_percent": 40}, **unreadable}
                    if routing:
                        doc["routing"] = routing
                    expected = copy.deepcopy(doc)
                    migrate.migrate(expected)
                    self.assertNotIn("enabled", expected.get("low_quota", {}))
                    loaded = self.load(doc)
                    self.assertEqual(loaded["unknown_keys"], [])
                    self.assertEqual(loaded, self.load(expected))
                    self.assertNotIn("enabled", loaded["low_quota"])
        self.assertEqual(self.load(OLD_EXAMPLE)["routing"], "relay")
        self.assertEqual(self.load({})["routing"], "relay")                 # no routing, no low_quota
        on = self.load({"routing": "auto", "low_quota": {"enabled": True}, **unreadable})
        self.assertEqual((on["routing"], self.decide(on).reason), ("auto", "the quota cannot be read"))
        bare = self.load({"routing": "auto"})
        self.assertEqual((bare["routing"], self.decide(bare).reason), ("auto", "the quota cannot be read"))


if __name__ == "__main__":
    unittest.main()
