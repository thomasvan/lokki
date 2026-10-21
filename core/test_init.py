#!/usr/bin/env python3
"""First run belongs to the core: the data directory, four distinct secrets, two
config files that agree, a busy port moved, a second run that changes nothing."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import config, init, migrate  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _load(path):
    return yaml.safe_load(_read(path))


def _tree_text(root):
    """Every file under `root`, name and content, so a test can look for a value anywhere."""
    out = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            out.append(_read(os.path.join(base, name)))
    return out


class InitTests(unittest.TestCase):
    def setUp(self):
        # `root`, not only `data/`, is removed at cleanup: `init` writes secrets
        # under it, and tests also create siblings of `data/` there.
        root = tempfile.mkdtemp(prefix="lokki-init-")
        self.addCleanup(shutil.rmtree, root, True)
        self.dir = os.path.join(root, "data")

    def test_a_second_init_renames_a_moved_key_once(self):
        """A moved key in a file `init` made is rewritten under its new name once,
        whole, and reported."""
        first = init.run(self.dir, core_port=0, cliproxy_port=0)
        path = first["core_config"]
        before = _load(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write("retries_old: 2\n")
        with mock.patch.object(migrate, "RENAMES", [("retries_old", "retries")]):
            second = init.run(self.dir, core_port=0, cliproxy_port=0)
            self.assertFalse(second["created"])
            self.assertEqual(second["migrated"], ["retries_old -> retries"])
            self.assertEqual(_load(path), {**before, "retries": 2})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            mtime = os.stat(path).st_mtime_ns
            third = init.run(self.dir, core_port=0, cliproxy_port=0)
        self.assertEqual(third["migrated"], [])
        self.assertEqual(os.stat(path).st_mtime_ns, mtime)

    def test_the_layout_the_secrets_and_the_two_files(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0, cliproxy_version="8.0.23")
        self.assertTrue(out["created"])
        for rel in ("config.yaml", "cliproxy/config.yaml", "cliproxy/auth", "secrets", "logs"):
            self.assertTrue(os.path.exists(os.path.join(self.dir, rel)), rel)
        core_cfg = _load(out["core_config"])
        cp_cfg = _load(out["cliproxy_config"])
        tokens = {t["name"]: t for t in core_cfg["auth"]["tokens"]}
        self.assertTrue(tokens["app"]["operator"])
        self.assertFalse(tokens["default"].get("operator", False))
        mgmt = _read(os.path.join(self.dir, "secrets", "cliproxy-mgmt.secret")).strip()
        self.assertEqual(len({tokens["app"]["token"], tokens["default"]["token"], core_cfg["cli_proxy_api_key"], mgmt}), 4)
        self.assertEqual(cp_cfg["server"]["port"], core_cfg["cliproxy"]["port"])
        self.assertNotEqual(core_cfg["cliproxy"]["port"], out["core_port"])
        self.assertEqual(cp_cfg["access"]["api-keys"], [core_cfg["cli_proxy_api_key"]])
        self.assertEqual(cp_cfg["management"]["secret-key"], mgmt)
        self.assertFalse(cp_cfg["management"]["allow-remote"])
        self.assertEqual(cp_cfg["server"]["host"], "127.0.0.1")
        self.assertIs(cp_cfg["management"]["disable-control-panel"], True)
        self.assertIs(core_cfg["cliproxy"]["panel"], False)
        self.assertEqual(cp_cfg["oauth"]["auth-dir"], os.path.join(self.dir, "cliproxy", "auth"))
        self.assertEqual(core_cfg["listen"], [f"127.0.0.1:{out['core_port']}"])
        self.assertEqual(core_cfg["runs_log"], os.path.join(self.dir, "runs.jsonl"))
        self.assertEqual(core_cfg["routing"], "auto")
        self.assertNotIn("low_quota", core_cfg)
        self.assertEqual(core_cfg["cliproxy"]["version"], "8.0.23")
        for knob in ("seg_budget", "pacing_seconds", "max_concurrent_upstream"):
            self.assertNotIn(knob, core_cfg)
        config.load_settings(config_path=out["core_config"], env={})     # it parses and validates
        if os.name != "nt":
            self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(os.path.join(self.dir, "secrets")).st_mode & 0o777, 0o700)

    def test_a_second_init_drops_the_retired_key_once_and_says_so(self):
        """An installed app's file names its secret file, so the key decides nothing and goes; a
        copy of the old example (Auto, no secret file) cut every request, so it becomes Relay."""
        # (label, what the file gets, the keys it holds after, the note)
        rows = [("an installed app", {"low_quota": {"enabled": False}}, {},
                 "low_quota.enabled -> (dropped)"),
                ("the old example", {"routing": "auto", "low_quota": {"enabled": False},
                                     "cliproxy": {"management_secret_file": ""}},
                 {"routing": "relay", "cliproxy": {"management_secret_file": ""}},
                 "low_quota.enabled -> routing: relay")]
        def merged(doc, patch):          # one level deep: a section is updated, not replaced
            return {**doc, **{k: {**doc[k], **v} if isinstance(doc.get(k), dict) else v for k, v in patch.items()}}

        for label, patch, after, note in rows:
            with self.subTest(label):
                data = os.path.join(self.dir, label.replace(" ", "-"))
                path = init.run(data, core_port=0, cliproxy_port=0)["core_config"]
                doc = _load(path)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(yaml.safe_dump(merged(doc, patch), sort_keys=False))
                second = init.run(data, core_port=0, cliproxy_port=0)
                self.assertEqual(second["migrated"], [note])
                self.assertTrue(second["note"].endswith(f"settings updated: {note}"), second["note"])
                self.assertEqual(_load(path), merged(doc, after))
                self.assertEqual(config.load_settings(config_path=path, env={})["routing"],
                                 after.get("routing", "auto"))
                mtime = os.stat(path).st_mtime_ns
                self.assertEqual(init.run(data, core_port=0, cliproxy_port=0)["migrated"], [])
                self.assertEqual(os.stat(path).st_mtime_ns, mtime)

    def test_a_busy_port_moves_to_the_next_free_one(self):
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        self.addCleanup(held.close)
        busy = held.getsockname()[1]
        self.assertGreater(init.free_port(busy), busy)

    def test_a_second_init_changes_nothing(self):
        first = init.run(self.dir, core_port=0, cliproxy_port=0)
        before = _read(first["core_config"])
        second = init.run(self.dir, core_port=0, cliproxy_port=0)
        self.assertFalse(second["created"])
        self.assertEqual(second["core_port"], first["core_port"])
        self.assertEqual(_read(first["core_config"]), before)

    def _set_core(self, out, patch):
        doc = _load(out["core_config"])
        doc["cliproxy"].update(patch)
        with open(out["core_config"], "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump(doc, sort_keys=False))

    def test_sync_cliproxy_writes_the_three_keys_and_keeps_the_rest(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        self._set_core(out, {"port": 9123, "panel": True})
        doc = _load(out["cliproxy_config"])
        doc["server"]["host"] = ""
        with open(out["cliproxy_config"], "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump({**doc, "extra": {"kept": True, "name": "é"}}, allow_unicode=True))
        init.sync_cliproxy(self.dir)
        cp_cfg = _load(out["cliproxy_config"])
        self.assertEqual(cp_cfg["server"], {"port": 9123, "host": "127.0.0.1"})
        self.assertIs(cp_cfg["management"]["disable-control-panel"], False)
        self.assertEqual(cp_cfg["extra"], {"kept": True, "name": "é"})
        self.assertEqual(cp_cfg["management"]["secret-key"], doc["management"]["secret-key"])
        before = _read(out["cliproxy_config"])
        init.sync_cliproxy(self.dir)
        self.assertEqual(_read(out["cliproxy_config"]), before)

    def test_the_cli_runs_init_and_prints_the_ports(self):
        out = subprocess.run([sys.executable, "-m", "lokki_core", "init", "--data-dir", self.dir,
                              "--core-port", "0", "--cliproxy-port", "0"],
                             capture_output=True, text=True, timeout=30, cwd=HERE)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("core_port", out.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "config.yaml")))

    def test_the_cli_serves_with_config_as_server_py_does(self):
        out = subprocess.run([sys.executable, "-m", "lokki_core", "--config", os.path.join(self.dir, "nonexistent.yaml")],
                             capture_output=True, text=True, timeout=30, cwd=HERE)
        self.assertEqual(out.returncode, 2)
        self.assertIn("refusing to start", out.stderr)

    def _secrets_of(self, out):
        core_cfg = _load(out["core_config"])
        return [t["token"] for t in core_cfg["auth"]["tokens"]] + [
            core_cfg["cli_proxy_api_key"],
            _read(os.path.join(self.dir, "secrets", "cliproxy-mgmt.secret")).strip()]

    def test_the_two_ports_are_distinct_even_when_asked_for_the_same_one(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        self.assertNotEqual(out["core_port"], out["cliproxy_port"])
        again = os.path.join(os.path.dirname(self.dir), "same")
        port = init.free_port(0)
        out = init.run(again, core_port=port, cliproxy_port=port)
        self.assertNotEqual(out["core_port"], out["cliproxy_port"])

    @unittest.skipIf(os.name == "nt", "POSIX modes: Windows has no 0600/0700")
    def test_secret_files_are_0600_and_the_directories_0700(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        for rel in ("config.yaml", "cliproxy/config.yaml", "secrets/cliproxy-mgmt.secret"):
            self.assertEqual(os.stat(os.path.join(self.dir, rel)).st_mode & 0o777, 0o600, rel)
        for rel in ("cliproxy", "cliproxy/auth", "logs", "secrets"):
            self.assertEqual(os.stat(os.path.join(self.dir, rel)).st_mode & 0o777, 0o700, rel)

    def test_the_core_file_names_only_the_keys_the_app_owns(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        self.assertEqual(set(_load(out["core_config"])),
                         {"listen", "auth", "cli_proxy_api_key", "cliproxy", "routing", "runs_log"})
        self.assertEqual(set(_load(out["core_config"])["cliproxy"]),
                         {"port", "version", "management_secret_file", "panel"})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "runs.jsonl")))

    def test_the_result_and_the_cli_output_carry_no_secret(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        secrets_ = self._secrets_of(out)
        self.assertEqual(set(out), {"created", "core_port", "cliproxy_port", "core_config", "cliproxy_config"})
        for text in (repr(out),):
            for s in secrets_:
                self.assertNotIn(s, text)
        fresh = os.path.join(os.path.dirname(self.dir), "cli")
        run = subprocess.run([sys.executable, "-m", "lokki_core", "init", "--data-dir", fresh,
                              "--core-port", "0", "--cliproxy-port", "0"],
                             capture_output=True, text=True, timeout=30, cwd=HERE)
        self.assertEqual(run.returncode, 0, run.stderr)
        printed = yaml.safe_load(run.stdout)
        self.assertEqual(set(printed), set(out))
        cfg = _load(os.path.join(fresh, "config.yaml"))
        leaked = [t["token"] for t in cfg["auth"]["tokens"]] + [cfg["cli_proxy_api_key"],
                  _read(os.path.join(fresh, "secrets", "cliproxy-mgmt.secret")).strip()]
        for s in leaked:
            self.assertNotIn(s, run.stdout)
            self.assertNotIn(s, run.stderr)

    def test_the_cli_says_so_on_a_second_run_and_exits_0(self):
        args = [sys.executable, "-m", "lokki_core", "init", "--data-dir", self.dir,
                "--core-port", "0", "--cliproxy-port", "0"]
        first = subprocess.run(args, capture_output=True, text=True, timeout=30, cwd=HERE)
        before = [_read(os.path.join(self.dir, r)) for r in ("config.yaml", "cliproxy/config.yaml")]
        second = subprocess.run(args, capture_output=True, text=True, timeout=30, cwd=HERE)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already initialised", second.stderr)
        self.assertEqual(yaml.safe_load(second.stdout)["created"], False)
        self.assertEqual(yaml.safe_load(second.stdout)["core_port"], yaml.safe_load(first.stdout)["core_port"])
        self.assertEqual(before, [_read(os.path.join(self.dir, r)) for r in ("config.yaml", "cliproxy/config.yaml")])

    @unittest.skipIf(os.name == "nt", "POSIX modes: Windows has no 0600/0700")
    def test_sync_cliproxy_keeps_the_files_mode(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        os.chmod(out["cliproxy_config"], 0o640)
        self._set_core(out, {"port": 9123})
        init.sync_cliproxy(self.dir)
        self.assertEqual(os.stat(out["cliproxy_config"]).st_mode & 0o777, 0o640)
        self.assertEqual(os.stat(out["core_config"]).st_mode & 0o777, 0o600)

    def test_both_files_are_utf_8(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0, cliproxy_version="é-8")
        init.sync_cliproxy(self.dir)
        for path in (out["core_config"], out["cliproxy_config"]):
            with open(path, "rb") as f:
                f.read().decode("utf-8")
        self.assertEqual(_load(out["core_config"])["cliproxy"]["version"], "é-8")
        config.load_settings(config_path=out["core_config"], env={})

    def _snapshot(self):
        """Every file under the data directory with its content and mode."""
        out = {}
        for base, _dirs, files in os.walk(os.path.dirname(self.dir)):
            for name in files:
                path = os.path.join(base, name)
                out[path] = (_read(path), os.stat(path).st_mode)
        return out

    def test_sync_cliproxy_with_a_broken_file_changes_nothing(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        self._set_core(out, {"port": 9123})
        for broken in (out["cliproxy_config"], out["core_config"]):
            saved = _read(broken)
            states = [None, "- a list\n", "a: [unclosed\n", "server: 3\nmanagement: 3\ncliproxy: 3\n"]
            if broken == out["core_config"]:
                states.append("listen: [127.0.0.1:9001]\ncliproxy: {port: 70000}\n")
            for state in states:
                if state is None:
                    os.unlink(broken)
                else:
                    with open(broken, "w", encoding="utf-8") as f:
                        f.write(state)
                before = self._snapshot()
                with self.assertRaises(init.InitError, msg=f"{broken} {state!r}"):
                    init.sync_cliproxy(self.dir)
                self.assertEqual(self._snapshot(), before)
            with open(broken, "w", encoding="utf-8") as f:
                f.write(saved)

    def test_shell_config_prints_what_the_shell_needs_and_nothing_else(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        self._set_core(out, {"panel": True})
        core_cfg = _load(out["core_config"])
        app_token = {t["name"]: t["token"] for t in core_cfg["auth"]["tokens"]}["app"]
        run = subprocess.run([sys.executable, "-m", "lokki_core", "shell-config", "--data-dir", self.dir],
                             capture_output=True, text=True, timeout=30, cwd=HERE)
        self.assertEqual(run.returncode, 0, "shell-config failed")
        self.assertEqual(run.stdout.count("\n"), 1)
        printed = json.loads(run.stdout)
        self.assertEqual(set(printed), {"core_port", "cliproxy_port", "operator_token",
                                        "management_secret_file", "panel"})
        self.assertEqual((printed["core_port"], printed["cliproxy_port"], printed["panel"]),
                         (out["core_port"], out["cliproxy_port"], True))
        self.assertEqual(printed["management_secret_file"], core_cfg["cliproxy"]["management_secret_file"])
        self.assertTrue(printed["operator_token"] == app_token, "the operator token is not the app entry's")
        others = [s for s in self._secrets_of(out) if s != app_token]
        self.assertFalse(any(s in run.stdout or s in run.stderr for s in others), "a secret besides the operator token was printed")
        self.assertTrue(run.stderr == "", "shell-config wrote to stderr")
        with mock.patch.dict(os.environ, {"RELAY_LISTEN": "127.0.0.1:9555"}):
            self.assertEqual(init.shell_config(self.dir)["core_port"], 9555)

    def test_shell_config_refuses_a_file_init_did_not_make(self):
        os.makedirs(self.dir)
        path = os.path.join(self.dir, "config.yaml")
        with self.assertRaises(init.InitError):
            init.shell_config(self.dir)
        for text in ("routing: auto\n", "listen: [127.0.0.1:9001]\ncliproxy: {port: 9002}\n",
                     "auth: {tokens: [{name: x, token: t}]}\nlisten: [127.0.0.1:9001]\ncliproxy: {port: 9002}\n"):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            with self.assertRaises(init.InitError, msg=text):
                init.shell_config(self.dir)

    def test_a_bad_port_is_refused_before_anything_is_created(self):
        for bad in (-1, 65536, 70000):
            with self.assertRaises(ValueError):
                init.free_port(bad)
            with self.assertRaises(ValueError):
                init.run(self.dir, core_port=bad, cliproxy_port=0)
            with self.assertRaises(ValueError):
                init.run(self.dir, core_port=0, cliproxy_port=bad)
        self.assertFalse(os.path.exists(self.dir))

    def test_free_port_stops_at_65535(self):
        held = socket.socket()
        try:
            held.bind(("127.0.0.1", 65535))
        except OSError:
            self.skipTest("65535 is not free on this host")
        self.addCleanup(held.close)
        with self.assertRaises(RuntimeError):
            init.free_port(65535)

    def test_the_cli_refuses_a_bad_port_with_one_sentence_and_exit_2(self):
        for args in (["init", "--core-port", "70000"], ["init", "--core-port", "-1"],
                     ["init", "--cliproxy-port", "70000"], ["init", "--cliproxy-port", "x"]):
            cmd = [sys.executable, "-m", "lokki_core", args[0], "--data-dir", self.dir] + args[1:]
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=HERE)
            self.assertEqual(run.returncode, 2, (args, run.stderr))
            self.assertIn("port must be", run.stderr)
            self.assertNotIn("Traceback", run.stderr)
        self.assertFalse(os.path.exists(self.dir))

    def test_the_cli_reports_a_sync_failure_in_a_sentence_and_exit_1(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        os.unlink(out["cliproxy_config"])
        for cmd, gone in (("sync-cliproxy", ""), ("shell-config", out["core_config"])):
            if gone:
                os.unlink(gone)
            run = subprocess.run([sys.executable, "-m", "lokki_core", cmd, "--data-dir", self.dir],
                                 capture_output=True, text=True, timeout=30, cwd=HERE)
            self.assertEqual(run.returncode, 1, cmd)
            self.assertTrue(run.stdout == "", f"{cmd} wrote to stdout")
            self.assertTrue("Traceback" not in run.stderr, f"{cmd} raised")
            self.assertEqual(len(run.stderr.strip().splitlines()), 1, cmd)
            if cmd == "sync-cliproxy":
                self.assertIn("cannot read", run.stderr)

    def test_a_bare_string_listen_is_read_whole(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("listen: 127.0.0.1:9001\ncliproxy: {port: 9002}\n")
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        self.assertFalse(out["created"])
        self.assertEqual((out["core_port"], out["cliproxy_port"]), (9001, 9002))

    def test_a_core_file_init_did_not_make_is_left_alone_and_said_so(self):
        os.makedirs(self.dir)
        for text in ("routing: auto\n", "listen: [127.0.0.1:9001]\n", "- a list\n",
                     "a: [unclosed\n", "listen: [127.0.0.1:9001]\ncliproxy: {port: 70000}\n"):
            with open(os.path.join(self.dir, "config.yaml"), "w", encoding="utf-8") as f:
                f.write(text)
            before = self._snapshot()
            args = [sys.executable, "-m", "lokki_core", "init", "--data-dir", self.dir,
                    "--core-port", "0", "--cliproxy-port", "0"]
            run = subprocess.run(args, capture_output=True, text=True, timeout=30, cwd=HERE)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("not made by init", run.stderr)
            self.assertEqual(self._snapshot(), before, text)
            self.assertFalse(init.run(self.dir, core_port=0, cliproxy_port=0)["created"])

    def test_either_config_file_alone_makes_the_directory_existing(self):
        out = init.run(self.dir, core_port=0, cliproxy_port=0)
        for keep, drop in ((out["core_config"], out["cliproxy_config"]),
                           (out["cliproxy_config"], out["core_config"])):
            shutil.rmtree(self.dir)
            os.makedirs(os.path.dirname(keep), exist_ok=True)
            with open(keep, "w", encoding="utf-8") as f:
                f.write("server: {port: 9002}\nextra: é\n" if "cliproxy" in keep
                        else "listen: [127.0.0.1:9001]\ncliproxy: {port: 9002}\n")
            before = self._snapshot()
            args = [sys.executable, "-m", "lokki_core", "init", "--data-dir", self.dir,
                    "--core-port", "0", "--cliproxy-port", "0"]
            run = subprocess.run(args, capture_output=True, text=True, timeout=30, cwd=HERE)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("nothing changed", run.stderr)
            self.assertEqual(self._snapshot(), before)
            self.assertFalse(os.path.exists(drop))

    def test_schema_example_is_the_generated_listing_byte_for_byte(self):
        from lokki_core import schema
        run = subprocess.run([sys.executable, "-m", "lokki_core", "schema", "--example"],
                             capture_output=True, timeout=30, cwd=HERE)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, schema.example_yaml().encode("utf-8"))
        # The checked-out file may have CRLF line ends (git's autocrlf on Windows); the
        # output itself is LF, so the file is compared by its lines.
        with open(os.path.join(HERE, "config.example.yaml"), "rb") as f:
            self.assertEqual(f.read().replace(b"\r\n", b"\n"), run.stdout)


if __name__ == "__main__":
    unittest.main()
