#!/usr/bin/env python3
"""The settings schema is the one home of defaults, ranges and meanings: the
parser reads them from it, the example file is generated from it, and it lists
exactly the keys the parser reads."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import config, core, migrate, schema  # noqa: E402


class Recording(dict):
    """A config mapping that remembers which top-level keys were asked for."""
    def __init__(self):
        super().__init__()
        self.asked = set()

    def get(self, key, default=None):
        self.asked.add(key)
        return default

    def __contains__(self, key):
        self.asked.add(key)
        return False


class SchemaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write_config(self, body):
        """Write a config file into the test's own directory, removed with the test."""
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yaml", dir=self.dir, delete=False) as f:
            f.write(body)
        return f.name

    def test_every_key_the_parser_reads_is_in_the_schema_and_nothing_else(self):
        raw = Recording()
        config.resolve_settings(raw, env={})
        top_level = {f.key.split(".")[0] for f in schema.FIELDS}
        self.assertEqual(raw.asked, top_level)

    def test_defaults_come_from_the_schema_and_are_the_code_s(self):
        settings = config.resolve_settings({}, env={})
        self.assertEqual(settings["pacing"], 1.0)
        self.assertEqual(settings["max_concurrent"], 2)
        self.assertEqual(settings["retries"], 6)
        self.assertEqual(settings["tail_chars"], 1500)
        self.assertEqual((settings["seg_budget"], settings["total_cap"], settings["max_segments"]), (1500, 8000, 6))
        self.assertEqual(settings["routing"], "relay")
        self.assertEqual(settings["low_quota"]["threshold_percent"], 30)
        self.assertEqual(settings["cliproxy"]["port"], 8317)
        self.assertEqual(settings["upstream"], "http://127.0.0.1:8317")
        self.assertEqual(settings["runs_log"], core.DEFAULT_RUNS_LOG)

    def test_the_fallback_settings_have_the_nested_dicts(self):
        self.assertEqual(config._DEFAULT_SETTINGS["cliproxy"]["port"], 8317)
        self.assertNotIn("enabled", config._DEFAULT_SETTINGS["low_quota"])
        self.assertEqual(config._DEFAULT_SETTINGS["routing"], "relay")
        self.assertEqual(config._DEFAULT_SETTINGS["upstream"], "http://127.0.0.1:8317")
        self.assertEqual(config._DEFAULT_UPSTREAM, "http://127.0.0.1:8317")

    def test_the_example_file_is_the_generated_listing_and_names_no_machine(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "config.example.yaml"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text, schema.example_yaml())
        self.assertNotIn(os.path.expanduser("~"), text)
        loaded = config.load_settings(config_path=os.path.join(here, "config.example.yaml"), env={})
        self.assertEqual(loaded["pacing"], 1.0)        # the generated file loads, with the built-in values
        self.assertEqual(loaded["upstream"], "http://127.0.0.1:8317")

    def test_a_utf8_config_file_loads_whatever_the_platform_encoding(self):
        path = self.write_config("")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Café crème, 東京 …\nauth:\n  tokens:\n    - {name: café, token: t}\n")
        loaded = config.load_settings(config_path=path, env={})
        self.assertEqual([entry[0] for entry in loaded["tokens"]], ["café"])

    def test_the_writable_keys_are_the_specs_allow_list(self):
        """A copy on purpose: what the API may write is a boundary, so moving it takes two edits."""
        self.assertEqual({f.key for f in schema.FIELDS if f.writable}, {
            "routing", "low_quota.threshold_percent", "low_quota.rule",
            "low_quota.accounts", "low_quota.models", "low_quota.poll_seconds", "cliproxy.url", "cliproxy.port", "cliproxy.panel", "listen",
            "seg_budget", "total_cap", "max_segments", "tail_chars", "merge_text", "session_pin",
            "text_calls", "text_calls_pad", "imagegen_cut", "payload_cap", "pacing_seconds",
            "max_concurrent_upstream", "retries", "drain_seconds"})

    def test_the_listing_marks_startup_only_and_writable(self):
        rows = {r["key"]: r for r in schema.listing()}
        for key in ("listen", "runs_log", "debug_log", "allow_public_bind", "cliproxy.port"):
            self.assertTrue(rows[key]["startup_only"], key)
        for key in ("auth.tokens", "cli_proxy_api_key", "cliproxy.management_secret_file",
                    "allow_public_bind", "allow_client_tuning", "runs_log", "capture_dir", "debug_log"):
            self.assertFalse(rows[key]["writable"], key)
        self.assertTrue(rows["routing"]["writable"])
        self.assertEqual(rows["low_quota.poll_seconds"]["minimum"], 30)
        self.assertEqual(rows["upstream"]["env"], "CLIPROXY_URL")
        self.assertIsNone(rows["retries"]["minimum"])                    # read as 1, never refused
        self.assertEqual(rows["routing"]["group"], "saving")
        self.assertEqual(rows["routing"]["choice_labels"], {"relay": "Relay", "auto": "Auto", "direct": "Direct"})
        self.assertEqual(rows["low_quota.rule"]["choice_labels"], {
            "all": "every watched account is low", "any": "any watched account is low"})
        self.assertEqual(rows["auth.tokens"]["group"], "hidden")
        self.assertEqual(rows["low_quota.poll_seconds"]["group"], "tuning.quota_reading")
        json.dumps(schema.listing())                                     # the pairs serialize as objects

    def test_every_row_has_a_label_a_known_group_and_a_label_per_choice(self):
        for row in schema.listing():
            key = row["key"]
            with self.subTest(key):
                self.assertTrue(row["label"] and isinstance(row["label"], str))
                self.assertIn(row["group"], schema.GROUPS)
                self.assertIsInstance(row["choice_labels"], dict)
                self.assertEqual(list(row["choice_labels"]), row["choices"])
                if row["group"] != "hidden":
                    for term in ("auth_index", "operator", "relay on"):
                        self.assertNotIn(term, row["meaning"])

    def test_routing_and_rule_take_only_their_words(self):
        for bad in ({"routing": "sometimes"}, {"low_quota": {"rule": "most"}}):
            with self.assertRaises(config.ConfigError) as caught:
                config.resolve_settings(bad, env={})
            self.assertIn(list(bad)[0], str(caught.exception))

    def test_auto_with_no_readable_secret_loads(self):
        s = config.resolve_settings({"routing": "auto",
                                     "cliproxy": {"management_secret_file": "/nonexistent/x"}}, env={})
        self.assertEqual(s["routing"], "auto")

    def test_url_and_port_are_one_fact(self):
        s = config.resolve_settings({"cliproxy": {"port": 9000}}, env={})
        self.assertEqual(s["upstream"], "http://127.0.0.1:9000")
        s = config.resolve_settings({"cliproxy": {"url": "http://10.0.0.2:8317"}}, env={})
        self.assertEqual(s["upstream"], "http://10.0.0.2:8317")
        s = config.resolve_settings({"upstream": "http://10.0.0.3:8317"}, env={})   # the old key still reads
        self.assertEqual(s["upstream"], "http://10.0.0.3:8317")
        s = config.resolve_settings({}, env={"CLIPROXY_URL": "http://10.0.0.4:8317"})
        self.assertEqual(s["upstream"], "http://10.0.0.4:8317")

    def test_cliproxy_url_is_a_scheme_a_host_and_a_port_and_nothing_else(self):
        for good, want in (("http://10.0.0.2:8317", "http://10.0.0.2:8317"),
                           ("https://cliproxy.example", "https://cliproxy.example"),
                           ("HTTP://[::1]:8317/", "HTTP://[::1]:8317"),
                           ("", "http://127.0.0.1:8317")):            # empty is unset: the port decides
            s = config.resolve_settings({"cliproxy": {"url": good}}, env={})
            self.assertEqual(s["upstream"], want, good)
        for bad in ("10.0.0.2:8317", "ftp://10.0.0.2", "http://", "http://:8317",
                    "http://10.0.0.2:8317/v1", "http://10.0.0.2:8317?x=1", "http://10.0.0.2#f",
                    "http://user:pw@10.0.0.2:8317", "http://10.0.0.2:99999", "http://10.0.0.2:0",
                    "http://10.0.0.2:port", "http://10.0.0.2 :8317"):
            with self.subTest(bad):
                with self.assertRaises(config.ConfigError) as caught:
                    config.resolve_settings({"cliproxy": {"url": bad}}, env={})
                self.assertIn("cliproxy.url", str(caught.exception))
                self.assertNotIn("pw", str(caught.exception))     # the value is not echoed

    def test_the_old_upstream_key_and_cliproxy_url_load_what_they_loaded(self):
        """Not held to cliproxy.url's rule: the live relay names its upstream this
        way, and a refusal at a reload or a start would stop it."""
        for value in ("http://10.0.0.3:8317/v1", "http://user:pw@10.0.0.3:8317"):
            s = config.resolve_settings({"upstream": value}, env={})
            self.assertEqual(s["upstream"], value)
            s = config.resolve_settings({}, env={"CLIPROXY_URL": value})
            self.assertEqual(s["upstream"], value)

    def test_a_moved_key_in_a_file_is_read_under_its_new_name(self):
        """A server deployment reads an older file without `init` rewriting it."""
        path = self.write_config("retries_old: 2\n")
        with mock.patch.object(migrate, "RENAMES", [("retries_old", "retries")]):
            s = config.load_settings(config_path=path, env={})
        self.assertEqual(s["retries"], 2)
        self.assertEqual(s["unknown_keys"], [])

    def test_retries_under_1_is_read_as_1(self):
        self.assertEqual(config.resolve_settings({"retries": 0}, env={})["retries"], 1)

    def test_a_null_section_key_reads_as_unset(self):
        nulls = {"low_quota": dict.fromkeys(("threshold_percent", "rule", "accounts", "poll_seconds")),
                 "cliproxy": dict.fromkeys(("port", "url", "version", "management_secret_file", "panel"))}
        s, unset = config.resolve_settings(nulls, env={}), config.resolve_settings({}, env={})
        for key in ("low_quota", "cliproxy", "upstream"):
            self.assertEqual(s[key], unset[key], key)

    def test_the_secret_file_is_kept_as_the_path_that_was_checked(self):
        s = config.resolve_settings({"cliproxy": {"management_secret_file": "~/cliproxy-mgmt.secret"}}, env={})
        self.assertEqual(s["cliproxy"]["management_secret_file"], os.path.expanduser("~/cliproxy-mgmt.secret"))

    def test_unknown_keys_are_kept_and_named(self):
        s = config.resolve_settings({"colour": "blue", "low_quota": {"threshold_percent": 30, "flavour": 1}}, env={})
        self.assertEqual(s["unknown_keys"], ["colour", "low_quota.flavour"])

    def test_a_port_derived_upstream_is_applied_at_start_and_held_on_reload(self):
        from test_offline import _restore_globals, _snapshot_globals
        self.addCleanup(_restore_globals, _snapshot_globals())
        path = self.write_config("auth:\n  token: t\ncliproxy:\n  port: 8318\n")
        config.ensure_settings(force=True, config_path=path, env={})
        self.assertEqual(config.UPSTREAM, "http://127.0.0.1:8318")
        with open(path, "w", encoding="utf-8") as f:
            f.write("auth:\n  token: t\ncliproxy:\n  port: 8319\n")
        said = []
        self.assertIsNotNone(config.reload_settings(config_path=path, env={}, log=said.append))
        self.assertEqual(config.UPSTREAM, "http://127.0.0.1:8318")      # held until the next start
        self.assertEqual(config.CLIPROXY["port"], 8319)                  # the file's value is loaded
        held = [m for m in said if "http://127.0.0.1:8319" in m]
        self.assertEqual(len(held), 1, said)                             # and the hold is said out loud
        self.assertIn("next start", held[0])
        self.assertIn("http://127.0.0.1:8318", held[0])

    def test_a_named_upstream_replaced_by_a_port_is_held_and_said(self):
        from test_offline import _restore_globals, _snapshot_globals
        self.addCleanup(_restore_globals, _snapshot_globals())
        path = self.write_config("auth:\n  token: t\nupstream: http://10.0.0.5:8317\n")
        config.ensure_settings(force=True, config_path=path, env={})
        self.assertEqual(config.UPSTREAM, "http://10.0.0.5:8317")
        with open(path, "w", encoding="utf-8") as f:
            f.write("auth:\n  token: t\ncliproxy:\n  port: 8319\n")
        said = []
        self.assertIsNotNone(config.reload_settings(config_path=path, env={}, log=said.append))
        self.assertEqual(config.UPSTREAM, "http://10.0.0.5:8317")       # the port is applied at start
        held = [m for m in said if "http://127.0.0.1:8319" in m]
        self.assertEqual(len(held), 1, said)
        self.assertIn("next start", held[0])
        self.assertIn("http://10.0.0.5:8317", held[0])

    def test_a_named_upstream_moves_on_reload(self):
        from test_offline import _restore_globals, _snapshot_globals
        self.addCleanup(_restore_globals, _snapshot_globals())
        file_key = lambda url: f"auth:\n  token: t\nupstream: {url}\n"
        cliproxy_url = lambda url: f"auth:\n  token: t\ncliproxy:\n  url: {url}\n"
        plain = lambda url: "auth:\n  token: t\n"
        for how, body, env in (("upstream", file_key, lambda url: {}),
                               ("cliproxy.url", cliproxy_url, lambda url: {}),
                               ("CLIPROXY_URL", plain, lambda url: {"CLIPROXY_URL": url})):
            with self.subTest(how):
                old, new = "http://10.0.0.5:8317", "http://10.0.0.6:8317"
                path = self.write_config(body(old))
                config.ensure_settings(force=True, config_path=path, env=env(old))
                self.assertEqual(config.UPSTREAM, old)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body(new))
                said = []
                self.assertIsNotNone(config.reload_settings(config_path=path, env=env(new), log=said.append))
                self.assertEqual(config.UPSTREAM, new)                   # a named upstream is the operator's edit
                self.assertFalse([m for m in said if "next start" in m], said)


if __name__ == "__main__":
    unittest.main()
