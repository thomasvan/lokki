#!/usr/bin/env python3
"""The settings write path: an allow-list, one lock, nothing written on a
refusal, a symlink kept, a read-only file, an env override or a value another
source decides refused, a port the next start could not listen on refused, and
no secret in any answer."""
import contextlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import config, handler, schema, settings_api  # noqa: E402
from test_offline import TOKEN, _ServingHarness, _restore_globals, _snapshot_globals, assert_mode, json_get  # noqa: E402

CLIENT_TOKEN = "cli-tok"
API_KEY = "sk-settings-test-key"
BASE = ("auth:\n  tokens:\n    - {name: app, token: '%s', operator: true}\n"
        "    - {name: cli, token: '%s'}\ncli_proxy_api_key: '%s'\n" % (TOKEN, CLIENT_TOKEN, API_KEY))

# One valid value other than the default for every writable key, each PUT on its own.
# The set of keys is compared with the schema's, so a key made writable there is a
# key this table has to name. The two ports are ones the OS picks, filled in by the test.
ROUND_TRIP = {
    "listen": None,
    "cliproxy.port": None,
    "cliproxy.panel": True,
    "cliproxy.url": "http://127.0.0.1:9002",
    "routing": "direct",
    "low_quota.threshold_percent": 40,
    "low_quota.rule": "any",
    "low_quota.accounts": ["a1"],
    "low_quota.models": ["gpt-x"],
    "low_quota.poll_seconds": 120,
    "seg_budget": 3000,
    "total_cap": 9000,
    "max_segments": 4,
    "tail_chars": 2000,
    "merge_text": False,
    "session_pin": True,
    "text_calls": True,
    "text_calls_pad": False,
    "imagegen_cut": False,
    "payload_cap": 0,
    "pacing_seconds": 0.5,
    "max_concurrent_upstream": 3,
    "retries": 2,
    "drain_seconds": 5,
}

# URLs `urllib.parse.urlsplit` raises on rather than parses: a bracket that does not
# close, and text after one that is not a port.
UNPARSABLE_URLS = ("http://[::1", "http://[::1]x")


def put(port, body, token=TOKEN, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Relay-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/settings", data=data, method="PUT",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


def nested(key, value):
    top, _, rest = key.partition(".")
    return {top: {rest: value}} if rest else {top: value}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def free_ports(n):
    """`n` distinct ports nothing listens on, as the OS picks them."""
    with contextlib.ExitStack() as stack:
        socks = [stack.enter_context(socket.socket()) for _ in range(n)]
        for s in socks:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]


@contextlib.contextmanager
def holding(listen=True):
    """A port another program holds: accepting connections, or bound and not accepting."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        if listen:
            s.listen(1)
        yield s.getsockname()[1]


class SettingsApiTests(_ServingHarness):
    def setUp(self):
        super().setUp()
        self.addCleanup(_restore_globals, _snapshot_globals())
        self.dir = tempfile.mkdtemp(prefix="settings-api-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.secret_file = os.path.join(self.dir, "mgmt.secret")
        with open(self.secret_file, "w", encoding="utf-8") as f:
            f.write("mgmt-secret-value\n")
        self.path = os.path.join(self.dir, "config.yaml")
        self.write(BASE + "seg_budget: 1500\n# a comment\n")
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for var in [k for k in os.environ if k.startswith("RELAY_") or k in ("CLIPROXY_URL", "CLI_PROXY_API_KEY")]:
            if var != "RELAY_RUNS_LOG":
                del os.environ[var]
        self.load()

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def load(self, path=None):
        config.ensure_settings(force=True, config_path=path or self.path, env=os.environ)

    def test_a_writable_key_round_trips_and_is_applied(self):
        status, body = put(self.port, {"routing": "direct", "seg_budget": 4000})
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(body["applied"]), ["routing", "seg_budget"])
        self.assertEqual(body["restart_required"], [])
        self.assertTrue(body["comments_dropped"])
        self.assertEqual(config.ROUTING, "direct")
        self.assertEqual(config.DEFAULT_SEG_BUDGET, 4000)
        self.assertIn("routing: direct", read(self.path))
        status, view = json_get(self.port, "/api/settings")
        self.assertEqual(view["routing"], "direct")
        self.assertEqual(view["auth"]["tokens"][0]["token"], "***")

    def test_every_writable_key_round_trips(self):
        self.write(BASE + f"cliproxy:\n  management_secret_file: '{self.secret_file}'\n")
        self.load()
        self.assertEqual(set(ROUND_TRIP), {f.key for f in schema.FIELDS if f.writable})
        core_port, cliproxy_port = free_ports(2)
        values = dict(ROUND_TRIP, **{"listen": [f"127.0.0.1:{core_port}"], "cliproxy.port": cliproxy_port})
        for f in schema.FIELDS:
            if not f.writable:
                continue
            status, body = put(self.port, nested(f.key, values[f.key]))
            self.assertEqual(status, 200, (f.key, body))
            self.assertEqual(body["restart_required"] if f.startup_only else body["applied"], [f.key])
            status, view = json_get(self.port, "/api/settings")
            top, _, rest = f.key.partition(".")
            self.assertEqual(view[top][rest] if rest else view[f.key], values[f.key], f.key)

    def test_a_nested_key_merges_without_dropping_its_siblings(self):
        put(self.port, {"low_quota": {"threshold_percent": 40}})
        status, body = put(self.port, {"low_quota": {"rule": "any"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(config.LOW_QUOTA["threshold_percent"], 40)
        self.assertEqual(config.LOW_QUOTA["rule"], "any")

    def test_a_never_writable_key_is_refused_and_nothing_is_written(self):
        before = read(self.path)
        for body in ({"allow_public_bind": True}, {"auth": {"tokens": []}}, {"runs_log": "/x"},
                     {"cli_proxy_api_key": "k"}, {"cliproxy": {"management_secret_file": "/x"}},
                     {"allow_client_tuning": True}, {"capture_dir": "/x"}, {"debug_log": "/x"},
                     {"auth": {"token": "t"}}, {"upstream": "http://127.0.0.1:1"},
                     {"cliproxy": {"version": "8.0.24"}}, {"reject_previous_response_id": False}):
            status, payload = put(self.port, body)
            self.assertEqual(status, 400, body)
            self.assertIn("not settable", payload["error"]["message"])
        self.assertEqual(read(self.path), before)

    def test_an_unknown_key_is_refused(self):
        before = read(self.path)
        for body, said in (({"no_such_key": 1}, "no_such_key is not a setting"),
                           ({"cliproxy": {"no_such_key": 1}}, "cliproxy.no_such_key is not a setting"),
                           ({"low_quota": {"enabled": True}}, "low_quota.enabled is not a setting"),
                           ({}, None)):
            status, payload = put(self.port, body)
            self.assertEqual(status, 400, body)
            if said:
                self.assertEqual(payload["error"]["message"], said)
        self.assertEqual(read(self.path), before)

    def test_a_write_leaves_the_retired_key_out_and_keeps_the_mode_it_chose(self):
        """`put` migrates the file it read before it merges, so the mode the retired key chose
        is written down with the next write, whatever that write names."""
        secret = f"cliproxy:\n  management_secret_file: '{self.secret_file}'\n"
        # (label, the file, the body, the routing written)
        rows = [("on with no routing", BASE + secret + "low_quota:\n  enabled: true\n",
                 {"low_quota": {"threshold_percent": 40}}, "auto"),
                ("the old example", BASE + "routing: auto\ncliproxy:\n  management_secret_file: ''\n"
                                           "low_quota:\n  enabled: false\n  threshold_percent: 30\n",
                 {"seg_budget": 2000}, "relay")]
        for label, text, body, routing in rows:
            with self.subTest(label):
                self.write(text)
                self.load()
                status, answer = put(self.port, body)
                self.assertEqual(status, 200, answer)
                written = yaml.safe_load(read(self.path))
                self.assertEqual(written["routing"], routing)
                self.assertNotIn("enabled", written.get("low_quota") or {})
                self.assertEqual(config.ROUTING, routing)

    def test_an_invalid_merge_writes_nothing(self):
        before = read(self.path)
        status, payload = put(self.port, {"low_quota": {"threshold_percent": 150}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["key"], "low_quota.threshold_percent")
        self.assertEqual(read(self.path), before)

    def test_a_file_that_resolves_to_no_token_is_not_written(self):
        """The running process keeps its tokens; a file edited since to hold none
        would be written and then refused by the reload, so it is refused first."""
        self.write("seg_budget: 1500\n")
        status, _ = put(self.port, {"routing": "direct"})
        self.assertEqual(status, 409)
        self.assertEqual(read(self.path), "seg_budget: 1500\n")

    def test_a_file_that_does_not_load_is_a_409_and_not_written(self):
        """A hand-edited file the loader refuses is answered with the loader's reason."""
        for text, why in (("a: [unclosed\n", "is not valid YAML"),
                          ("- a\n- b\n", "must contain a mapping at the top level")):
            self.write(text)
            status, payload = put(self.port, {"routing": "direct"})
            self.assertEqual(status, 409, payload)
            self.assertIn(why, payload["error"]["message"])
            self.assertEqual(read(self.path), text)

    def test_a_startup_only_key_is_written_and_reported_not_bound(self):
        bound, upstream = handler.BOUND_LISTEN, config.UPSTREAM
        core_port, cliproxy_port = free_ports(2)
        status, body = put(self.port, {"listen": [f"127.0.0.1:{core_port}"], "cliproxy": {"port": cliproxy_port}})
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(body["restart_required"]), ["cliproxy.port", "listen"])
        self.assertEqual(handler.BOUND_LISTEN, bound)          # what is bound did not move
        self.assertEqual(config.UPSTREAM, upstream)            # nor the upstream: nothing listens on the new port yet
        self.assertIn("listen:", read(self.path))

    def test_a_put_of_listen_moves_the_file_not_what_the_guard_reads(self):
        """config.LISTEN follows the reload; the Host guard keeps reading what this
        process bound. A loopback Host passes the guard either way, so the bound
        set here also holds a non-loopback address (the owner's Tailscale one)."""
        handler.BOUND_LISTEN = (f"127.0.0.1:{self.port}", f"100.64.0.9:{self.port}")
        moved = f"127.0.0.1:{free_ports(1)[0]}"
        status, body = put(self.port, {"listen": [moved]})
        self.assertEqual(status, 200, body)
        self.assertEqual(list(config.LISTEN), [moved])
        self.assertEqual(handler.BOUND_LISTEN, (f"127.0.0.1:{self.port}", f"100.64.0.9:{self.port}"))
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/version",
                                     headers={"X-Relay-Token": TOKEN, "Host": f"100.64.0.9:{self.port}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        status, endpoint = json_get(self.port, "/api/endpoint")
        self.assertEqual(endpoint["urls"], [f"http://{addr}/v1" for addr in handler.BOUND_LISTEN])

    def test_listen_accepts_loopback_only(self):
        status, payload = put(self.port, {"listen": ["192.168.1.5:8381"]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["key"], "listen")

    def test_a_bad_cliproxy_url_is_a_400_naming_it_and_writes_nothing(self):
        before = read(self.path)
        for bad in ("10.0.0.2:8317", "http://10.0.0.2:8317/v1", "http://user:pw@10.0.0.2:8317",
                    *UNPARSABLE_URLS):
            status, payload = put(self.port, {"cliproxy": {"url": bad}})
            self.assertEqual(status, 400, (bad, payload))
            self.assertEqual(payload["key"], "cliproxy.url")
            self.assertNotIn(bad, payload["error"]["message"])
        self.assertEqual(read(self.path), before)

    def test_a_file_with_a_bad_cliproxy_url_does_not_load(self):
        for bad in ("http://10.0.0.2:8317/v1", *UNPARSABLE_URLS):
            self.write(BASE + f"cliproxy:\n  url: '{bad}'\n")
            with self.assertRaises(config.ConfigError, msg=bad) as caught:
                config.load_settings(config_path=self.path, env={})
            self.assertIn("cliproxy.url", str(caught.exception))
            self.assertNotIn(bad, str(caught.exception))

    def test_a_port_the_next_start_could_not_use_is_refused_naming_its_key(self):
        """Either port, moved, is refused at the write when its service could not listen
        there, with the reason beside the field; the restart would otherwise stop the
        running service and fail to start it again."""
        free_core, free_cliproxy = free_ports(2)
        self.write(BASE + f"listen: ['127.0.0.1:{free_core}']\ncliproxy:\n  port: {free_cliproxy}\n")
        self.load()
        # (label, holder of the port, the body for it, its key, status, said)
        rows = [("listen, a port that accepts", True, lambda p: {"listen": [f"127.0.0.1:{p}"]},
                 "listen", 409, "is in use by another program"),
                ("cliproxy.port, a port that accepts", True, lambda p: {"cliproxy": {"port": p}},
                 "cliproxy.port", 409, "is in use by another program"),
                ("listen, a port bound and not accepting", False, lambda p: {"listen": [f"127.0.0.1:{p}"]},
                 "listen", 409, "Lokki cannot listen on 127.0.0.1:"),
                ("the core on CLIProxyAPI's port", None, lambda _: {"listen": [f"127.0.0.1:{free_cliproxy}"]},
                 "listen", 400, f"cannot both use port {free_cliproxy}"),
                ("CLIProxyAPI on the core's port", None, lambda _: {"cliproxy": {"port": free_core}},
                 "cliproxy.port", 400, f"cannot both use port {free_core}"),
                ("listen on localhost, a port that accepts", True, lambda p: {"listen": [f"localhost:{p}"]},
                 "listen", 409, "is in use by another program"),
                ("listen on localhost, a port bound and not accepting", False, lambda p: {"listen": [f"localhost:{p}"]},
                 "listen", 409, "Lokki cannot listen on localhost:"),
                ("listen on ::1, which the IPv4 listener never binds", None, lambda _: {"listen": [f"::1:{free_core}"]},
                 "listen", 400, "the core listens on IPv4 only"),
                ("listen on [::1], which the IPv4 listener never binds", None, lambda _: {"listen": [f"[::1]:{free_core}"]},
                 "listen", 400, "the core listens on IPv4 only"),
                ("listen, a port past 65535", None, lambda _: {"listen": ["127.0.0.1:70000"]},
                 "listen", 400, "from 1 to 65535"),
                ("listen, port 0", None, lambda _: {"listen": ["127.0.0.1:0"]},
                 "listen", 400, "from 1 to 65535")]
        before = read(self.path)
        for label, listening, body, key, code, said in rows:
            with self.subTest(label), (holding(listening) if listening is not None
                                       else contextlib.nullcontext(None)) as port:
                status, payload = put(self.port, body(port))
                self.assertEqual((status, payload.get("key")), (code, key), payload)
                self.assertIn(said, payload["error"]["message"])
                self.assertEqual(read(self.path), before)
        moved = free_ports(1)[0]
        status, payload = put(self.port, {"listen": [f"localhost:{moved}"]})
        self.assertEqual(status, 200, payload)

    def test_port_1455_is_refused_by_its_value_without_a_bind(self):
        """1455 is where a Codex login returns, held while one waits: refused as a value,
        so the suite never binds it."""
        def no_probe_of_1455(host, port):
            if port == 1455:
                raise AssertionError("1455 was probed")
            return None
        with mock.patch.object(settings_api, "_unusable", side_effect=no_probe_of_1455):
            for body, key in (({"listen": ["127.0.0.1:1455"]}, "listen"), ({"cliproxy": {"port": 1455}}, "cliproxy.port")):
                status, payload = put(self.port, body)
                self.assertEqual((status, payload.get("key")), (400, key), payload)
                self.assertIn("where a Codex login returns", payload["error"]["message"])

    def test_the_port_a_service_listens_on_now_passes_unprobed(self):
        """The running service holds its own port, so the value it has is never probed;
        the same port, held by another program, is refused."""
        with holding() as cliproxy_port:
            self.write(BASE + f"cliproxy:\n  port: {cliproxy_port}\n")
            self.load()            # a start: CLIProxyAPI runs on the file's port
            handler.BOUND_LISTEN = (f"127.0.0.1:{self.port}",)
            for body in ({"listen": [f"127.0.0.1:{self.port}"]}, {"cliproxy": {"port": cliproxy_port}}):
                status, payload = put(self.port, body)
                self.assertEqual(status, 200, (body, payload))
            handler.BOUND_LISTEN = (f"127.0.0.1:{free_ports(1)[0]}",)
            status, payload = put(self.port, {"listen": [f"127.0.0.1:{self.port}"]})
            self.assertEqual((status, payload.get("key")), (409, "listen"), payload)

    def test_url_and_port_in_one_body_is_refused(self):
        status, payload = put(self.port, {"cliproxy": {"port": 9000, "url": "http://10.0.0.2:8317"}})
        self.assertEqual(status, 400)

    def test_no_token_is_a_401_and_writes_nothing(self):
        before = read(self.path)
        self.assertEqual(put(self.port, {"routing": "direct"}, token=None)[0], 401)
        self.assertEqual(put(self.port, None, token=None, raw=b"{not json")[0], 401)
        self.assertEqual(json_get(self.port, "/api/settings", token="")[0], 401)
        self.assertEqual(read(self.path), before)

    def test_a_client_token_is_refused_before_the_body_is_read(self):
        before = read(self.path)
        status, payload = put(self.port, {"routing": "direct"}, token=CLIENT_TOKEN)
        self.assertEqual(status, 403)
        self.assertEqual(config.ROUTING, "relay")             # the default: nothing was written
        # A body the parser would answer 400 still gets 403: the role comes first.
        self.assertEqual(put(self.port, None, token=CLIENT_TOKEN, raw=b"{not json")[0], 403)
        self.assertEqual(json_get(self.port, "/api/settings", token=CLIENT_TOKEN)[0], 403)
        self.assertEqual(read(self.path), before)

    def test_a_read_only_file_is_a_409(self):
        os.chmod(self.path, 0o400)
        self.addCleanup(os.chmod, self.path, 0o600)
        if os.access(self.path, os.W_OK):
            self.skipTest("this user can write a read-only file (root)")
        status, payload = put(self.port, {"routing": "direct"})
        self.assertEqual(status, 409)
        self.assertIn("read-only", payload["error"]["message"])

    def test_an_environment_override_is_a_409_naming_the_key(self):
        os.environ["RELAY_SEG_BUDGET"] = "2000"
        self.load()
        before = read(self.path)
        status, payload = put(self.port, {"seg_budget": 4000})
        self.assertEqual(status, 409)
        self.assertEqual(payload["key"], "seg_budget")
        self.assertIn("RELAY_SEG_BUDGET", payload["error"]["message"])
        self.assertEqual(read(self.path), before)
        status, view = json_get(self.port, "/api/settings")
        self.assertIn("seg_budget", view["env_overrides"])

    # A written value that would not decide the running one is a 409 naming what does,
    # for the same reason as the environment override: the write would read as applied.

    def assert_conflict(self, body, key, winner):
        before = read(self.path)
        status, payload = put(self.port, body)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["key"], key)
        self.assertIn(winner, payload["error"]["message"])
        self.assertEqual(read(self.path), before)

    def test_cliproxy_url_in_the_environment_wins_over_url_and_port(self):
        os.environ["CLIPROXY_URL"] = "http://127.0.0.1:9100"
        self.load()
        self.assert_conflict({"cliproxy": {"url": "http://127.0.0.1:9200"}}, "cliproxy.url", "CLIPROXY_URL")
        self.assert_conflict({"cliproxy": {"port": 9001}}, "cliproxy.port", "CLIPROXY_URL")
        status, view = json_get(self.port, "/api/settings")
        self.assertLessEqual({"upstream", "cliproxy.url", "cliproxy.port"}, set(view["env_overrides"]))

    def test_the_old_upstream_key_wins_over_url_and_port(self):
        self.write(BASE + "upstream: 'http://127.0.0.1:9100'\n")
        self.load()
        self.assert_conflict({"cliproxy": {"url": "http://127.0.0.1:9200"}}, "cliproxy.url", "upstream")
        self.assert_conflict({"cliproxy": {"port": 9001}}, "cliproxy.port", "upstream")

    def test_a_set_cliproxy_url_wins_over_the_port(self):
        self.write(BASE + "cliproxy:\n  url: 'http://127.0.0.1:9100'\n")
        self.load()
        self.assert_conflict({"cliproxy": {"port": 9001}}, "cliproxy.port", "cliproxy.url")
        # Clearing the URL in the same body is how the port is chosen again.
        status, body = put(self.port, {"cliproxy": {"url": "", "port": free_ports(1)[0]}})
        self.assertEqual(status, 200, body)

    def test_clearing_a_running_url_is_reported_for_the_next_start(self):
        """With the URL cleared the upstream derives from the port, which a reload
        does not move; so the URL is not applied, it waits for the next start."""
        self.write(BASE + "cliproxy:\n  url: 'http://127.0.0.1:9100'\n")
        self.load()
        status, body = put(self.port, {"cliproxy": {"url": ""}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["applied"], [])
        self.assertEqual(body["restart_required"], ["cliproxy.url"])
        self.assertEqual(config.UPSTREAM, "http://127.0.0.1:9100")

    def test_no_secret_reaches_a_response(self):
        self.write(BASE + f"cliproxy:\n  management_secret_file: '{self.secret_file}'\n")
        self.load()
        status, view = json_get(self.port, "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(view["cli_proxy_api_key"], "***")
        self.assertEqual([t["token"] for t in view["auth"]["tokens"]], ["***", "***"])
        self.assertEqual([t["operator"] for t in view["auth"]["tokens"]], [True, False])
        self.assertEqual(view["cliproxy"]["management_secret_file"], self.secret_file)   # a path, shown as one
        answers = [json.dumps(view),
                   json.dumps(put(self.port, {"routing": "relay"})[1]),
                   json.dumps(put(self.port, {"cli_proxy_api_key": "x"})[1]),
                   json.dumps(put(self.port, {"low_quota": {"threshold_percent": 0}})[1])]
        for text in answers:
            for secret in (TOKEN, CLIENT_TOKEN, API_KEY, "mgmt-secret-value"):
                self.assertNotIn(secret, text)

    def test_a_password_in_the_upstream_is_not_shown(self):
        """The old upstream key and CLIPROXY_URL load unvalidated, so either can carry
        a user:password@; the view shows the URL without it."""
        url = "http://user:pw-upstream@127.0.0.1:9300"
        for text, env in ((BASE + f"upstream: '{url}'\n", {}), (BASE, {"CLIPROXY_URL": url})):
            with self.subTest(env=bool(env)), mock.patch.dict(os.environ, env):
                self.write(text)
                self.load()
                status, view = json_get(self.port, "/api/settings")
                self.assertEqual(status, 200)
                self.assertEqual(view["upstream"], "http://127.0.0.1:9300")
                self.assertNotIn("pw-upstream", json.dumps(view))

    def test_a_symlinked_file_keeps_its_link(self):
        link = self.path + ".link"
        try:
            os.symlink(self.path, link)
        except (OSError, NotImplementedError):
            self.skipTest("creating a symlink needs a privilege this user lacks (Windows without developer mode)")
        self.addCleanup(os.unlink, link)
        self.load(link)
        status, body = put(self.port, {"routing": "relay"})
        self.assertEqual(status, 200, body)
        self.assertTrue(os.path.islink(link))
        self.assertIn("routing: relay", read(self.path))

    def test_a_settings_body_is_not_captured(self):
        """A capture is of a client's request; an operator's settings body is not one."""
        captures = os.path.join(self.dir, "captures")
        os.mkdir(captures)
        self.write(BASE + f"capture_dir: '{captures}'\n")
        self.load()
        status, body = put(self.port, {"routing": "relay"})
        self.assertEqual(status, 200, body)
        self.assertEqual(os.listdir(captures), [])

    def test_the_mode_is_kept(self):
        os.chmod(self.path, 0o640)
        status, body = put(self.port, {"routing": "relay"})
        self.assertEqual(status, 200, body)
        assert_mode(self, self.path, 0o640)

    def test_a_non_ascii_value_survives_a_write(self):
        """The file is read and written as UTF-8 whatever the platform's encoding."""
        self.write(BASE + "low_quota:\n  accounts: ['café-東京']\n")
        self.load()
        status, body = put(self.port, {"routing": "relay"})
        self.assertEqual(status, 200, body)
        self.assertIn("café-東京", read(self.path))
        self.assertEqual(config.LOW_QUOTA["accounts"], ["café-東京"])

    def test_two_concurrent_puts_both_land(self):
        results = []

        def go(body):
            results.append(put(self.port, body))

        threads = [threading.Thread(target=go, args=({"seg_budget": 3000},)),
                   threading.Thread(target=go, args=({"routing": "direct"},))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual([r[0] for r in results], [200, 200], results)
        text = read(self.path)
        self.assertIn("seg_budget: 3000", text)
        self.assertIn("routing: direct", text)


if __name__ == "__main__":
    unittest.main()
