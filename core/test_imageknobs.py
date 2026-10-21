#!/usr/bin/env python3
"""Offline tests for `imagegen_cut` and `payload_cap` as settings of the config file.

They were always the relay's request knobs, with built-in defaults and no key in `config.yaml`,
so an operator whose client needs the pictures it asks for had no way to say so without a code
change. A fake upstream, no quota. These pin: the defaults are what the relay always ran with;
the two keys (and `RELAY_IMAGEGEN_CUT`, `RELAY_PAYLOAD_CAP`) are read, a bad value is refused
and a typo does not turn the cut off; a reload sets and clears them; a caller cannot change
them where tuning is closed; and with both named, an image item reaches the client whole.

Run:  python3 core/test_imageknobs.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lokki_core import config, core, engine, handler  # noqa: E402
from test_engine import FakeResponse, FakeSink, _EngineHarness, make_cfg, sse  # noqa: E402

BASE = "auth:\n  token: 't'\ncli_proxy_api_key: 'k'\n"
HUGE = "A" * 100000        # stands in for a ~2 MB base64 result; above the built-in cap (65,536)
# every module global `apply_settings` and the loaders set: a test that applies settings gives them back
SETTING_GLOBALS = (
    "UPSTREAM", "API_KEY", "LISTEN", "RELAY_TOKEN", "TOKENS", "RETRIES", "PACING", "MAX_CONCURRENT",
    "ALLOW_PUBLIC_BIND", "ALLOW_CLIENT_TUNING", "CAPTURE_DIR", "DEBUG_LOG", "TAIL_CHARS", "DRAIN",
    "REJECT_PREVIOUS_RESPONSE_ID", "DEFAULT_SEG_BUDGET", "DEFAULT_TOTAL_CAP", "DEFAULT_MAX_SEGMENTS",
    "DEFAULT_MERGE_TEXT", "DEFAULT_SESSION_PIN", "DEFAULT_TEXT_CALLS", "DEFAULT_PAYLOAD_CAP",
    "DEFAULT_IMAGEGEN_CUT", "_settings", "_settings_resolved_for", "_config_error")


def guard_settings(test):
    """Put the config module's globals back when the test ends, whatever it applied or reloaded."""
    saved = {name: getattr(config, name) for name in SETTING_GLOBALS}

    def restore():
        for name, value in saved.items():
            setattr(config, name, value)
    test.addCleanup(restore)


def image_frames(result, item_id="ig1", index=1):
    item = {"id": item_id, "type": "image_generation_call", "status": "completed",
            "revised_prompt": "a pelican"}
    return (sse("response.output_item.added",
                {"type": "response.output_item.added", "output_index": index, "item": dict(item)})
            + sse("response.image_generation_call.in_progress",
                  {"type": "response.image_generation_call.in_progress",
                   "item_id": item_id, "output_index": index})
            + sse("response.output_item.done",
                  {"type": "response.output_item.done", "output_index": index,
                   "item": dict(item, result=result)})
            + sse("response.output_text.delta",
                  {"type": "response.output_text.delta", "delta": "after the image",
                   "output_index": 2, "item_id": "m1"})
            + sse("response.completed",
                  {"type": "response.completed", "response": {"id": "r1", "usage": {}, "output": []}}))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        guard_settings(self)

    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yaml", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def load(self, extra="", env=None):
        return config.load_settings(config_path=self.write(BASE + extra), env=env or {})

    def test_the_image_cut_is_on_unless_a_setting_turns_it_off(self):
        """A relay with no setting must still end a run at an image item: the cut
        is what keeps a ~2M-character base64 result from being produced,
        delivered and billed. Turning it off is the operator's decision."""
        self.assertIs(config.BUILTIN_IMAGEGEN_CUT, True)
        self.assertIs(self.load()["imagegen_cut"], True)
        self.assertIs(config._DEFAULT_SETTINGS["imagegen_cut"], True)

    def test_payload_cap_is_read_from_the_file_and_the_environment_and_zero_is_a_setting(self):
        self.assertEqual(self.load("payload_cap: 0\n")["payload_cap"], 0)
        self.assertEqual(self.load("payload_cap: 2048\n")["payload_cap"], 2048)
        self.assertEqual(self.load("payload_cap: 2048\n", {"RELAY_PAYLOAD_CAP": "0"})["payload_cap"], 0)
        self.assertEqual(self.load("", {"RELAY_PAYLOAD_CAP": "4096"})["payload_cap"], 4096)
        self.assertEqual(self.load("", {"RELAY_PAYLOAD_CAP": "  "})["payload_cap"],
                         config.BUILTIN_PAYLOAD_CAP)

    def test_a_payload_cap_that_is_not_a_count_is_refused(self):
        # `int()` would read a fraction and a boolean as 1, and `.inf` raises OverflowError,
        # which a reload does not catch
        for text in ("payload_cap: many\n", "payload_cap: -1\n", "payload_cap: 1.5x\n", "payload_cap: 1.5\n",
                     "payload_cap: true\n", "payload_cap: .inf\n", "payload_cap: -.inf\n", "payload_cap: .nan\n",
                     "payload_cap: [1]\n"):
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError):
                    self.load(text)
        for env in ({"RELAY_PAYLOAD_CAP": "1.5"}, {"RELAY_PAYLOAD_CAP": "true"}, {"RELAY_PAYLOAD_CAP": "inf"}):
            with self.subTest(env=env):
                with self.assertRaises(config.ConfigError):
                    self.load("", env)
        self.assertEqual(self.load("payload_cap: 1000.0\n")["payload_cap"], 1000)

    def test_a_reload_with_a_bad_value_is_refused_and_does_not_raise(self):
        said = []
        config.apply_settings(dict(config._DEFAULT_SETTINGS, payload_cap=123))
        for text in ("payload_cap: .inf\n", "payload_cap: true\n", "imagegen_cut: maybe\n"):
            with self.subTest(text=text):
                self.assertIsNone(config.reload_settings(config_path=self.write(BASE + text), env={}, log=said.append))
                self.assertEqual(config.DEFAULT_PAYLOAD_CAP, 123)
        self.assertEqual(len(said), 3)

    def test_a_reload_with_the_keys_sets_them_and_a_reload_without_them_clears_them(self):
        said = []
        config.reload_settings(config_path=self.write(BASE + "imagegen_cut: false\npayload_cap: 0\n"), env={},
                               log=said.append)
        self.assertIs(config.DEFAULT_IMAGEGEN_CUT, False)
        self.assertEqual(config.DEFAULT_PAYLOAD_CAP, 0)
        config.reload_settings(config_path=self.write(BASE), env={}, log=said.append)
        self.assertIs(config.DEFAULT_IMAGEGEN_CUT, config.BUILTIN_IMAGEGEN_CUT)
        self.assertEqual(config.DEFAULT_PAYLOAD_CAP, config.BUILTIN_PAYLOAD_CAP)

    def test_start_up_refuses_a_bad_value_instead_of_running_with_it(self):
        for text in ("payload_cap: -5\n", "imagegen_cut: flase\n"):
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError):
                    config.ensure_settings(force=True, config_path=self.write(BASE + text), env={})

    def test_imagegen_cut_is_read_from_the_file_and_the_environment(self):
        for extra, env, want in (("", {}, True), ("imagegen_cut: false\n", {}, False),
                                 ("imagegen_cut: true\n", {}, True), ("imagegen_cut: 'no'\n", {}, False),
                                 ("imagegen_cut: 'off'\n", {}, False), ("imagegen_cut: 'yes'\n", {}, True),
                                 ("", {"RELAY_IMAGEGEN_CUT": "0"}, False),
                                 ("imagegen_cut: false\n", {"RELAY_IMAGEGEN_CUT": "1"}, True),
                                 ("imagegen_cut: true\n", {"RELAY_IMAGEGEN_CUT": "false"}, False)):
            with self.subTest(extra=extra, env=env):
                self.assertIs(self.load(extra, env)["imagegen_cut"], want)

    def test_a_typo_does_not_turn_the_cut_off(self):
        # the off state is the one that bills a generated image, so only a clear word reaches it
        for extra, env in (("imagegen_cut: maybe\n", {}), ("", {"RELAY_IMAGEGEN_CUT": "flase"})):
            with self.subTest(extra=extra, env=env):
                with self.assertRaises(config.ConfigError):
                    self.load(extra, env)

    def test_a_reload_that_cannot_be_read_keeps_the_running_values(self):
        said = []
        path = self.write(BASE + "imagegen_cut: maybe\n")
        config.apply_settings(dict(config._DEFAULT_SETTINGS, imagegen_cut=False, payload_cap=0))
        self.assertIsNone(config.reload_settings(config_path=path, env={}, log=said.append))
        self.assertFalse(config.DEFAULT_IMAGEGEN_CUT)
        self.assertEqual(config.DEFAULT_PAYLOAD_CAP, 0)
        self.assertTrue(any("imagegen_cut" in line for line in said))

    def test_a_caller_cannot_change_them_where_tuning_is_closed(self):
        config.apply_settings(dict(config._DEFAULT_SETTINGS, imagegen_cut=False, payload_cap=0))
        body = {"imagegen_cut": True, "payload_cap": 1000}
        closed = handler.resolve_knobs(body, tuning=False)
        self.assertIs(closed["imagegen_cut"], False)
        self.assertEqual(closed["payload_cap"], 0)
        opened = handler.resolve_knobs(body, tuning=True)
        self.assertIs(opened["imagegen_cut"], True)
        self.assertEqual(opened["payload_cap"], 1000)

    def test_the_fingerprint_follows_the_running_values(self):
        config.apply_settings(dict(config._DEFAULT_SETTINGS, imagegen_cut=False, payload_cap=0))
        fingerprint = core.config_fingerprint()
        self.assertIs(fingerprint["imagegen_cut"], False)
        self.assertEqual(fingerprint["payload_cap"], 0)


class PassThroughTests(_EngineHarness):
    def setUp(self):
        super().setUp()
        guard_settings(self)

    def run_with_settings(self, **settings):
        """The way a request gets its knobs: from the operator's settings, not from the caller."""
        config.apply_settings(dict(config._DEFAULT_SETTINGS, **settings))
        knobs = handler.resolve_knobs({}, tuning=False)
        self.install([FakeResponse(image_frames(HUGE))])
        cfg = make_cfg(mode="after-deltas", abort_after=0, max_segments=1,
                       imagegen_cut=knobs["imagegen_cut"], payload_cap=knobs["payload_cap"])
        sink = FakeSink()
        return engine.run_upstream(cfg, sink), sink

    def test_the_defaults_end_the_run_at_the_image(self):
        rec, sink = self.run_with_settings()
        self.assertEqual(rec["cut"], "imagegen")
        self.assertNotIn(HUGE, sink.text())

    def test_both_named_the_image_reaches_the_client_whole_and_the_run_goes_on(self):
        rec, sink = self.run_with_settings(imagegen_cut=False, payload_cap=0)
        self.assertNotIn("imagegen", rec["cuts"])
        self.assertTrue(rec["real_completed"])
        self.assertIn(HUGE, sink.text())
        self.assertEqual(rec["payload_capped"], 0)
        self.assertEqual(rec["payload_chars_dropped"], 0)
        self.assertEqual(rec["knobs"]["imagegen_cut"], False)
        self.assertEqual(rec["knobs"]["payload_cap"], 0)

    def test_the_cut_off_alone_still_replaces_the_picture_with_the_placeholder(self):
        # the two are read together on purpose: the first alone produces an image and hands the
        # client a 1x1 PNG
        rec, sink = self.run_with_settings(imagegen_cut=False)
        self.assertNotIn("imagegen", rec["cuts"])
        self.assertNotIn(HUGE, sink.text())
        self.assertEqual(rec["payload_capped"], 1)


if __name__ == "__main__":
    unittest.main()
