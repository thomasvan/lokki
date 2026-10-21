#!/usr/bin/env python3
"""The control routes the UI and the shell read, the static files, and the guard
that keeps a browser on another site from driving a local relay."""
import contextlib
import importlib.metadata
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import activity, config, control, core, handler, routing  # noqa: E402
from test_offline import TOKEN, _restore_globals, _ServingHarness, _snapshot_globals, json_get, json_post  # noqa: E402
from test_quota_reader import SECRET, FakeManagement, browse, free_port, port_is_free  # noqa: E402

CLIENT_TOKEN = "cli-tok"
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as _f:
    CHECKOUT_VERSION = _f.read().strip()
LISTEN_HOST = "100.64.0.9"      # stands for the owner's Tailscale address; never connected to


def get_with_headers(port, path, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, e.read()


def exchange(port, method, path, data=None, token=TOKEN):
    """Status and JSON body of an operator call, an error's body included."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"X-Relay-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"null")


def send(port, method, path, data=None, token=TOKEN):
    """Status and raw body of a call, whatever the body is: what a leak test reads."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"X-Relay-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read().decode("utf-8", "replace")


CODE = "test-code-from-the-provider"
GONE = "this login is no longer waiting; start again"
NOT_THE_ADDRESS = "this is not the address the login ended on: it starts http://localhost:1455/auth/callback?"
NOT_A_BODY = 'the body must be {"url": "<the address the browser ended on>"}'


def not_the_callback(port, state, code="c0de", port_text="abc", host_text="nothost"):
    """Each body the paste route refuses with a 400, as `(label, body, sentence)`: an address
    that is not the callback's or cannot be parsed, and a body that is not `{"url": <string>}`.
    `port_text` and `host_text` go where the parser quotes what it could not read."""
    q = f"code={code}&state={state}"
    at = f"http://localhost:{port}/auth/callback"
    addresses = (
        ("https", f"https://localhost:{port}/auth/callback?{q}"),
        ("another host", f"http://example.com:{port}/auth/callback?{q}"),
        ("another port", f"http://localhost:{port + 1}/auth/callback?{q}"),
        ("no port", f"http://localhost/auth/callback?{q}"),
        ("another path", f"http://localhost:{port}/auth/other?{q}"),
        ("a user part", f"http://user@localhost:{port}/auth/callback?{q}"),
        ("a fragment", f"{at}?{q}#top"),
        ("no state", f"{at}?code={code}"),
        ("a blank state", f"{at}?code={code}&state="),
        ("no code or error", f"{at}?state={state}"),
        ("both code and error", f"{at}?{q}&error=access_denied"),
        ("a blank error is present", f"{at}?{q}&error="),
        ("state twice", f"{at}?{q}&state={state}"),
        ("state and a blank state", f"{at}?{q}&state="),
        ("no query", at),
        ("empty", ""),
        ("a port that is not a number", f"http://localhost:{port_text}/auth/callback?{q}"),
        ("a port out of range", f"http://localhost:99999/auth/callback?{q}"),
        ("an unclosed bracketed host", f"http://[{host_text}/auth/callback?{q}"),
        ("a bracketed host that is not an address", f"http://[{host_text}]:{port}/auth/callback?{q}"),
    )
    rows = [(label, json.dumps({"url": url}).encode(), NOT_THE_ADDRESS) for label, url in addresses]
    return rows + [("not JSON", f'{{"url": "{at}?{q}'.encode(), NOT_A_BODY),
                   ("not an object", json.dumps([f"{at}?{q}"]).encode(), NOT_A_BODY),
                   ("a number url", b'{"url": 1455}', NOT_A_BODY),
                   ("no url", json.dumps({"address": f"{at}?{q}"}).encode(), NOT_A_BODY)]


def get(port, path, headers):
    status, _, body = get_with_headers(port, path, headers)
    return status, body


def call(port, method, path, headers, data=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers,
                                 data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        with e:
            return e.code


class GuardTests(unittest.TestCase):
    def test_loopback_hosts_with_and_without_a_port_in_any_case_are_allowed(self):
        for host in ("127.0.0.1:8381", "127.0.0.1", "localhost", "Localhost:1", "[::1]:8381", "LOCALHOST"):
            self.assertTrue(control.host_allowed(host, ("127.0.0.1:8381",)), host)

    def test_a_listen_host_is_allowed_and_a_foreign_one_is_not(self):
        listen = ("127.0.0.1:8381", "100.64.0.9:8381")
        self.assertTrue(control.host_allowed("100.64.0.9:8381", listen))
        self.assertTrue(control.host_allowed("100.64.0.9", listen))
        self.assertFalse(control.host_allowed("127.0.0.1.evil.example", listen))
        self.assertFalse(control.host_allowed("127.0.0.1.evil.example:8381", listen))
        self.assertFalse(control.host_allowed("relay.example.com", listen))
        self.assertFalse(control.host_allowed("", listen))
        self.assertFalse(control.host_allowed(None, listen))

    def test_after_a_bracketed_host_only_a_port_may_follow(self):
        listen = ("127.0.0.1:8381", "[fd7a::9]:8381")
        self.assertTrue(control.host_allowed("[fd7a::9]:8381", listen))
        self.assertTrue(control.host_allowed("[::1]", listen))
        for host in ("[::1]evil", "[::1]evil:8381", "[::1].evil.example", "[::1]:8381x", "[::1]:",
                     "[::1", "[fd7a::9]evil", "x[::1]", "localhost:x", "localhost:8381:8381"):
            self.assertFalse(control.host_allowed(host, listen), host)

    def test_an_origin_must_be_the_cores_own(self):
        self.assertTrue(control.origin_allowed("http://127.0.0.1:8381", "127.0.0.1:8381"))
        self.assertTrue(control.origin_allowed(None, "127.0.0.1:8381"))
        self.assertFalse(control.origin_allowed("http://evil.example", "127.0.0.1:8381"))
        self.assertFalse(control.origin_allowed("http://127.0.0.1:9999", "127.0.0.1:8381"))
        self.assertFalse(control.origin_allowed("null", "127.0.0.1:8381"))
        self.assertFalse(control.origin_allowed("http://[::1", "[::1]:8381"))

    def test_an_origin_on_another_scheme_is_another_site(self):
        """The core serves http; an https page on the same host and port is not it."""
        self.assertFalse(control.origin_allowed("https://127.0.0.1:8381", "127.0.0.1:8381"))
        self.assertFalse(control.origin_allowed("ws://127.0.0.1:8381", "127.0.0.1:8381"))
        self.assertTrue(control.origin_allowed("HTTP://127.0.0.1:8381", "127.0.0.1:8381"))


class VersionTests(unittest.TestCase):
    def test_a_checkout_reports_its_own_version_over_an_installed_one(self):
        with mock.patch("importlib.metadata.version", return_value="0.0.1"):
            self.assertEqual(control.version(), CHECKOUT_VERSION)

    def test_an_installed_package_reports_its_distributions_version(self):
        """Installed, the package sits in site-packages: no checkout beside it,
        and a VERSION file there is some other package's."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "VERSION"), "w") as f:
                f.write("7.7.7\n")
            with mock.patch.object(control, "_CHECKOUT", d):
                with mock.patch("importlib.metadata.version", return_value="9.9.9"):
                    self.assertEqual(control.version(), "9.9.9")

    def test_the_images_layout_reports_the_version_file_beside_the_package(self):
        """The runtime image copies lokki_core/ and VERSION, not pyproject.toml,
        and installs nothing (deploy/relay/Dockerfile)."""
        not_installed = mock.patch("importlib.metadata.version",
                                   side_effect=importlib.metadata.PackageNotFoundError("lokki-core"))
        with tempfile.TemporaryDirectory() as d, not_installed:
            with mock.patch.object(control, "_CHECKOUT", d):
                self.assertEqual(control.version(), "unknown")
                with open(os.path.join(d, "VERSION"), "w") as f:
                    f.write("7.7.7\n")
                self.assertEqual(control.version(), "7.7.7")


class _ControlHarness(_ServingHarness):
    """The harness, with the upstream a listener this test owns: `/api/status`
    connects to it, and must never reach a port this host's services use."""

    def setUp(self):
        super().setUp()
        self.upstream = socket.socket()
        self.upstream.bind(("127.0.0.1", 0))
        self.upstream.listen(4)
        self.addCleanup(self.upstream.close)
        saved = config.UPSTREAM
        config.UPSTREAM = f"http://127.0.0.1:{self.upstream.getsockname()[1]}"
        self.addCleanup(setattr, config, "UPSTREAM", saved)
        # A test that calls `fresh_routing` sets `routing_lines` to what its first decision
        # logs; any other logs the default configuration's decision on a change, or nothing.
        self.routing_lines = None
        self.err = io.StringIO()
        patch = mock.patch("sys.stderr", self.err)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._the_routing_lines)

    def fresh_routing(self):
        """No decision made yet, so the next one logs; the state found is put back."""
        fresh = mock.patch.object(routing, "_last", None)
        fresh.start()
        self.addCleanup(fresh.stop)

    def _the_routing_lines(self):
        logged = [l for l in self.err.getvalue().splitlines() if l.startswith("[routing]")]
        if self.routing_lines is None:
            self.assertIn(logged, ([], ["[routing] relay on: routing: relay"]))
        else:
            self.assertEqual(logged, self.routing_lines)


class ControlRouteTests(_ControlHarness):
    def test_status_names_the_route_counts_the_revision_and_whether_cliproxy_answers(self):
        status, body = json_get(self.port, "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(set(body["runs_since_start"]), {"relay", "passthrough", "images"})
        self.assertEqual(body["revision"], core.revision())
        self.assertEqual(body["version"], CHECKOUT_VERSION)
        self.assertEqual(set(body["decision"]), {"relay_on", "reason", "computed_at"})
        self.assertEqual(set(body["poll"]), {"last_ok_at", "last_error", "reads", "running"})
        self.assertEqual(body["routing"], config.ROUTING)
        self.assertIsInstance(body["unknown_keys"], list)
        self.assertIs(body["cliproxy"]["reachable"], True)

    def test_status_reports_the_running_upstream_not_the_loaded_one(self):
        """A reload holds a port-derived upstream (config.apply_settings), so the
        loaded `upstream` can name a port nothing listens on yet."""
        saved = config._settings
        config._settings = dict(saved, upstream="http://127.0.0.1:9")
        self.addCleanup(setattr, config, "_settings", saved)
        status, body = json_get(self.port, "/api/status")
        self.assertEqual(body["cliproxy"]["url"], config.UPSTREAM)
        self.assertIs(body["cliproxy"]["reachable"], True)

    def test_status_says_unreachable_for_a_port_nothing_listens_on(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        self.assertFalse(control.cliproxy_reachable(f"http://127.0.0.1:{port}", timeout=1.0))
        self.assertFalse(control.cliproxy_reachable("not a url"))

    def test_status_names_no_credential_in_the_upstream(self):
        port = self.upstream.getsockname()[1]
        config.UPSTREAM = f"http://user:pw-secret@127.0.0.1:{port}"
        status, raw = get(self.port, "/api/status", {"X-Relay-Token": TOKEN})
        self.assertEqual(status, 200)
        self.assertNotIn(b"pw-secret", raw)
        body = json.loads(raw)
        self.assertEqual(body["cliproxy"]["url"], f"http://127.0.0.1:{port}")
        self.assertIs(body["cliproxy"]["reachable"], True)

    def test_an_unparsable_upstream_is_unreachable_and_shows_no_credential(self):
        """CLIPROXY_URL and the old `upstream` key load unvalidated."""
        config.UPSTREAM = "http://user:pw-secret@[::1"
        self.fresh_routing()
        self.routing_lines = ["[routing] relay on: routing: relay"]
        status, raw = get(self.port, "/api/status", {"X-Relay-Token": TOKEN})
        self.assertEqual(status, 200)
        self.assertNotIn(b"pw-secret", raw)
        self.assertIs(json.loads(raw)["cliproxy"]["reachable"], False)

    def test_an_unhandled_failure_names_nothing_to_the_caller(self):
        """A 500 that names nothing: GET reaches it without a token (`/healthz`), and the
        exception's text is the operator's log's, not the caller's."""
        said = io.StringIO()
        with mock.patch("lokki_core.handler.verify_chain", side_effect=RuntimeError("secret-detail")), \
                contextlib.redirect_stderr(said):
            status, _, body = get_with_headers(self.port, "/api/chain", {"X-Relay-Token": TOKEN})
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret-detail", body)
        self.assertEqual(said.getvalue(), "unhandled GET '/api/chain': RuntimeError: secret-detail\n")

    def test_endpoint_masks_the_default_token_and_reveals_on_request(self):
        saved = config.TOKENS
        config.TOKENS = (("app", TOKEN, "operator"), ("default", "sk-client-abcd", "client"))
        self.addCleanup(setattr, config, "TOKENS", saved)
        status, body = json_get(self.port, "/api/endpoint")
        self.assertEqual(body["key_masked"], "…abcd")
        self.assertNotIn("key", body)
        self.assertEqual(body["urls"], [f"http://127.0.0.1:{self.port}/v1"])
        status, body = json_get(self.port, "/api/endpoint?reveal=1")
        self.assertEqual(body["key"], "sk-client-abcd")
        # The operator's own token is never the one shown.
        self.assertNotIn(TOKEN, json.dumps(body))

    def test_endpoint_without_a_default_token_names_no_key(self):
        status, body = json_get(self.port, "/api/endpoint?reveal=1")
        self.assertEqual(status, 200)
        self.assertIsNone(body["key_masked"])
        self.assertNotIn("key", body)

    def test_schema_lists_every_key(self):
        status, body = json_get(self.port, "/api/schema")
        self.assertEqual(status, 200)
        self.assertIn("routing", {r["key"] for r in body})

    def test_activity_answers_24h_by_default_7d_on_request_and_400_otherwise(self):
        runs = tempfile.TemporaryDirectory()
        self.addCleanup(runs.cleanup)
        log = mock.patch.object(core, "RUNS_LOG", os.path.join(runs.name, "runs.jsonl"))
        log.start()
        self.addCleanup(log.stop)
        activity.clear_cache()
        self.addCleanup(activity.clear_cache)
        for query, window, buckets in (("", "24h", 24), ("?window=24h", "24h", 24), ("?window=7d&x=1", "7d", 28)):
            status, body = json_get(self.port, "/api/activity" + query)
            self.assertEqual((status, body["window"], len(body["buckets"])), (200, window, buckets), query)
        for query in ("?window=30d", "?window=", "?window", "?window=24h&window=7d"):
            status, body = exchange(self.port, "GET", "/api/activity" + query)
            self.assertEqual(status, 400, query)
            self.assertEqual(body["error"]["message"], "window must be 24h or 7d", query)

    def test_the_ui_is_public_404_when_absent_and_never_outside_its_directory(self):
        with tempfile.TemporaryDirectory() as d:
            empty = os.path.join(d, "empty")
            os.mkdir(empty)
            static = os.path.join(d, "static")
            os.mkdir(static)
            with open(os.path.join(static, "index.html"), "w") as f:
                f.write("<html>lokki</html>")
            with open(os.path.join(d, "secret.txt"), "w") as f:
                f.write("outside")
            saved = control.STATIC_DIR
            self.addCleanup(setattr, control, "STATIC_DIR", saved)
            control.STATIC_DIR = empty
            self.assertEqual(get(self.port, "/", {})[0], 404)
            self.assertEqual(get(self.port, "/static/index.html", {})[0], 404)
            control.STATIC_DIR = static
            self.assertEqual(get(self.port, "/", {}), (200, b"<html>lokki</html>"))
            self.assertEqual(get(self.port, "/static/index.html", {})[0], 200)
            # Public: no token, and a foreign Host does not matter here.
            self.assertEqual(get(self.port, "/static/index.html", {"Host": "evil.example"})[0], 200)
            self.assertEqual(get(self.port, "/static/../secret.txt", {})[0], 404)
            self.assertEqual(get(self.port, "/static/%2e%2e/secret.txt", {})[0], 404)
            self.assertEqual(get(self.port, "/static/%2e%2e%2fsecret.txt", {})[0], 404)
            self.assertEqual(get(self.port, "/static/..%5csecret.txt", {})[0], 404)
            self.assertEqual(get(self.port, "/static/" + os.path.join(d, "secret.txt").replace(os.sep, "/"), {})[0], 404)
            # A null byte, and the directory itself: missing files like any other, served to no one.
            for path in ("/static/a%00b", "/static/%00", "/static/", "/static", "/static//", "/static/?x=1"):
                self.assertEqual(get(self.port, path, {}), (404, get(self.port, "/static/nothing", {})[1]), path)

    def test_a_built_ui_is_served_with_the_content_types_a_browser_runs_it_by(self):
        """The UI build is index.html at `/` and its scripts under `/static/assets/`; a
        browser refuses a module script served as anything but JavaScript."""
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "assets"))
            with open(os.path.join(d, "index.html"), "w") as f:
                f.write('<script type="module" src="/static/assets/x.js"></script>')
            with open(os.path.join(d, "assets", "x.js"), "w") as f:
                f.write("export {};")
            saved = control.STATIC_DIR
            self.addCleanup(setattr, control, "STATIC_DIR", saved)
            control.STATIC_DIR = d
            status, headers, _ = get_with_headers(self.port, "/", {})
            self.assertEqual(status, 200)
            self.assertEqual(headers.get_content_type(), "text/html")
            status, headers, body = get_with_headers(self.port, "/static/assets/x.js", {})
            self.assertEqual(status, 200)
            self.assertIn(headers.get_content_type(), ("text/javascript", "application/javascript"))
            self.assertEqual(body, b"export {};")


class UnknownKeyTests(_ControlHarness):
    def setUp(self):
        super().setUp()
        self.addCleanup(_restore_globals, _snapshot_globals())

    def test_a_key_the_schema_does_not_know_is_named_in_status(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.yaml")
            with open(path, "w") as f:
                f.write(f"auth:\n  tokens:\n    - {{name: app, token: '{TOKEN}', operator: true}}\n"
                        "    - {name: cli, token: 'cli-tok'}\n"
                        "no_such_key: 1\ncliproxy:\n  no_such_sub: 2\n")
            config.ensure_settings(force=True, config_path=path, env={})
        status, body = json_get(self.port, "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["unknown_keys"], ["no_such_key", "cliproxy.no_such_sub"])


# Every route the guard covers, with a request each that the route answers without
# side effects once the guard and the token let it through.
GUARDED = (
    ("GET", "/version", None),
    ("GET", "/api/status", None),
    ("GET", "/api/endpoint", None),
    ("GET", "/api/schema", None),
    ("GET", "/api/settings", None),
    ("GET", "/api/runs", None),
    ("GET", "/api/chain", None),
    ("GET", "/api/accounts", None),
    ("GET", "/api/models", None),               # the upstream is a closed port here: its 502
    ("GET", "/api/activity", None),
    ("PUT", "/api/settings", b"{}"),          # an empty body is a 400, written nowhere
    ("POST", "/api/reload", b"{}"),
    ("POST", "/api/accounts/refresh", b"{}"),   # the poller is not running here: the latest snapshot, at once
    ("POST", "/api/shutdown", b"{}"),
    ("POST", "/api/run", b"{not json"),       # a 400 once through, before any upstream call
    ("POST", "/api/accounts/oauth", b"{}"),     # no readable management secret here: a 409 before anything binds
    ("GET", "/api/accounts/oauth/bad", None),   # too short for a state: a 400 before CLIProxyAPI is asked
    ("POST", "/api/accounts/oauth/abcdefgh", b'{"url": ""}'),   # an empty address: a 400 before any hand-on
)
OPERATOR_ONLY = {p for m, p, _ in GUARDED if p != "/api/run"}


class RouteMatrixTests(_ControlHarness):
    """The guard and the token gate, route by route. `/api/reload` and
    `/api/shutdown` are stubbed: a test that let a shutdown through would end the
    test process."""

    def setUp(self):
        super().setUp()
        config.TOKENS = (("app", TOKEN, "operator"), ("default", CLIENT_TOKEN, "client"))
        handler.BOUND_LISTEN = (f"127.0.0.1:{self.port}", f"{LISTEN_HOST}:{self.port}")
        runs = tempfile.TemporaryDirectory()
        self.addCleanup(runs.cleanup)
        log = mock.patch.object(core, "RUNS_LOG", os.path.join(runs.name, "runs.jsonl"))
        log.start()
        self.addCleanup(log.stop)
        self.addCleanup(setattr, config, "CLIPROXY", config.CLIPROXY)
        config.CLIPROXY = dict(config.CLIPROXY, management_secret_file="/nonexistent/cliproxy-mgmt.secret")
        # A route that reads CLIProxyAPI reaches a closed port, never the default 127.0.0.1:8317.
        self.addCleanup(setattr, config, "UPSTREAM", config.UPSTREAM)
        config.UPSTREAM = f"http://127.0.0.1:{free_port()}"
        from lokki_core.quota import oauth
        # The missing secret answers before anything binds; the port is injected all the same,
        # so no change of that order can make the suite bind the real 1455.
        port = mock.patch.object(oauth, "CALLBACK_PORT", free_port())
        port.start()
        self.addCleanup(port.stop)
        self.addCleanup(oauth.close_pending)
        for name, value in (("request_reload", {"ok": True, "error": None}), ("begin_drain", False)):
            patcher = mock.patch.object(handler, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _each(self, headers):
        for method, path, data in GUARDED:
            yield (method, path), call(self.port, method, path, dict(headers), data)

    def test_a_foreign_origin_is_403_on_every_guarded_route(self):
        for route, status in self._each({"X-Relay-Token": TOKEN, "Origin": "http://evil.example"}):
            self.assertEqual(status, 403, route)
        self.assertEqual(handler.request_reload.call_count + handler.begin_drain.call_count, 0)

    def test_a_foreign_host_is_403_on_every_guarded_route(self):
        for host in ("127.0.0.1.evil.example", f"127.0.0.1.evil.example:{self.port}", "relay.example.com"):
            for route, status in self._each({"X-Relay-Token": TOKEN, "Host": host}):
                self.assertEqual(status, 403, (host, route))
        self.assertEqual(handler.request_reload.call_count + handler.begin_drain.call_count, 0)

    def test_a_listen_host_and_its_own_origin_pass_the_guard(self):
        host = f"{LISTEN_HOST}:{self.port}"
        for headers in ({"Host": host}, {"Host": host, "Origin": f"http://{host}"},
                        {"Host": f"LOCALHOST:{self.port}", "Origin": f"http://localhost:{self.port}"}):
            for route, status in self._each(dict(headers, **{"X-Relay-Token": TOKEN})):
                self.assertNotIn(status, (401, 403, 404), (headers, route))

    def test_no_token_is_401_on_every_guarded_route(self):
        for route, status in self._each({}):
            self.assertEqual(status, 401, route)

    def test_a_client_token_is_403_on_every_operator_route(self):
        for route, status in self._each({"X-Relay-Token": CLIENT_TOKEN}):
            if route[1] in OPERATOR_ONLY:
                self.assertEqual(status, 403, route)
            else:                                   # /api/run takes a client, as before
                self.assertEqual(status, 400, route)
        self.assertEqual(handler.request_reload.call_count + handler.begin_drain.call_count, 0)

    def test_an_operator_token_passes_every_route(self):
        expected = {"/api/shutdown": 202, "/api/run": 400, "/api/accounts/oauth": 409,
                    "/api/accounts/oauth/bad": 400, "/api/models": 502, "/api/accounts/oauth/abcdefgh": 400}
        for (method, path), status in self._each({"X-Relay-Token": TOKEN}):
            want = 400 if method == "PUT" else expected.get(path, 200)
            self.assertEqual(status, want, (method, path))

    def test_healthz_and_v1_are_not_guarded(self):
        bad = {"Host": "127.0.0.1.evil.example", "Origin": "http://evil.example"}
        self.assertEqual(call(self.port, "GET", "/healthz", bad), 200)
        # The token gate answers, not the guard: a client SDK sends whatever Host its base URL says.
        self.assertEqual(call(self.port, "POST", "/v1/responses", bad, b"{}"), 401)
        self.assertEqual(call(self.port, "POST", "/v1/responses",
                              dict(bad, **{"X-Relay-Token": CLIENT_TOKEN}), b"{not json"), 400)


class AccountsRouteTests(_ControlHarness):
    def setUp(self):
        super().setUp()
        self.fresh_routing()
        from lokki_core.quota import poller
        self.saved = (config.LOW_QUOTA, config.ROUTING, poller.POLLER._latest)
        config.LOW_QUOTA = {"threshold_percent": 30, "rule": "all", "accounts": [], "poll_seconds": 60}
        config.ROUTING = "auto"
        with poller.POLLER._cond:
            poller.POLLER._latest = {"read_at": time.time(), "accounts": {
                "a1": {"label": "acct-1", "email_masked": "a***@x", "enabled": True, "used_percent": 80, "reset_at": time.time() + 3600, "window_seconds": 18000, "error": None},
                "b2": {"label": "acct-2", "email_masked": "b***@x", "enabled": True, "used_percent": None, "reset_at": None, "window_seconds": None, "error": "timeout"}}}
        self.addCleanup(self._restore)

    def _restore(self):
        from lokki_core.quota import poller
        config.LOW_QUOTA, config.ROUTING, latest = self.saved
        with poller.POLLER._cond:
            poller.POLLER._latest = latest

    def test_accounts_lists_each_with_its_state_and_the_decision(self):
        self.routing_lines = ["[routing] relay off: a watched account could not be read"]
        status, body = json_get(self.port, "/api/accounts")
        self.assertEqual(status, 200)
        rows = {r["auth_index"]: r for r in body["accounts"]}
        self.assertEqual(rows["a1"]["state"], "low")
        self.assertEqual(rows["a1"]["remaining_percent"], 20)
        self.assertTrue(rows["a1"]["in_scope"])
        self.assertEqual(rows["b2"]["state"], "unknown")
        self.assertEqual(rows["b2"]["error"], "timeout")
        self.assertEqual(body["decision"], {"relay_on": False, "reason": "a watched account could not be read"})
        self.assertLess(body["age_s"], 5)
        self.assertEqual(set(body["poll"]), {"last_ok_at", "last_error", "reads", "running"})

    def test_an_account_out_of_scope_carries_its_own_state_and_is_not_in_scope(self):
        self.routing_lines = ["[routing] relay on: every watched account is low"]
        from lokki_core.quota import poller
        config.LOW_QUOTA = dict(config.LOW_QUOTA, accounts=["a1"])
        with poller.POLLER._cond:
            poller.POLLER._latest["accounts"]["b2"].update(used_percent=10, reset_at=time.time() + 3600,
                                                           window_seconds=18000, error=None)
        status, body = json_get(self.port, "/api/accounts")
        rows = {r["auth_index"]: r for r in body["accounts"]}
        self.assertEqual((rows["a1"]["state"], rows["a1"]["in_scope"]), ("low", True))
        self.assertEqual((rows["b2"]["state"], rows["b2"]["in_scope"]), ("ok", False))
        self.assertEqual(rows["b2"]["remaining_percent"], 90)

    def test_a_raising_rule_never_answers_500_on_healthz_or_v1_responses(self):
        from lokki_core import passthrough
        from lokki_core.quota import policy
        seen = []
        no_raise_yet = mock.patch.object(routing, "_raised", None)    # the once rule starts fresh, and is put back
        no_raise_yet.start()
        self.addCleanup(no_raise_yet.stop)

        def serve(handler, raw, payload, decision):
            seen.append(decision.reason)
            handler._json({"served": True})

        with mock.patch.object(policy, "decide", side_effect=ValueError("secret-text")), \
                mock.patch.object(passthrough, "serve", side_effect=serve):
            status, body = json_get(self.port, "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(body["routing"], {"relay_on": False, "reason": policy.FAILED})
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/responses", method="POST",
                                         data=b'{"model": "m", "input": "x"}',
                                         headers={"X-Relay-Token": TOKEN, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)
        self.assertEqual(seen, [policy.FAILED])
        self.assertNotIn("secret-text", self.err.getvalue())
        self.routing_lines = ["[routing] the quota rule raised ValueError", f"[routing] relay off: {policy.FAILED}"]

    def test_one_snapshot_serves_the_rows_and_the_states(self):
        """A poll that finishes mid-request must not give rows from one snapshot and
        states from another: `latest()` answers a different snapshot on each call."""
        self.routing_lines = ["[routing] relay on: every watched account is low"]
        from lokki_core.quota import poller
        def snap(idx):
            return {"read_at": time.time(), "accounts": {idx: {
                "label": idx, "email_masked": "z***@x", "enabled": True, "used_percent": 80,
                "reset_at": time.time() + 3600, "window_seconds": 18000, "error": None}}}
        with mock.patch.object(poller.POLLER, "latest", side_effect=[snap("first"), snap("second")] * 3):
            for path in ("/api/accounts", "/api/accounts/refresh"):
                with mock.patch.object(poller.POLLER, "refresh", side_effect=lambda timeout: poller.POLLER.latest()):
                    status, body = (json_get(self.port, path) if path == "/api/accounts"
                                    else json_post(self.port, path, {}))
                self.assertEqual(status, 200)
                rows = body["accounts"]
                self.assertEqual(len(rows), 1, path)
                self.assertTrue(rows[0]["in_scope"], path)
                self.assertEqual(rows[0]["state"], "low", path)

    def test_status_carries_the_decision_and_the_poll(self):
        self.routing_lines = ["[routing] relay off: a watched account could not be read",
                              "[routing] relay off: the quota cannot be read"]
        status, body = json_get(self.port, "/api/status")
        self.assertEqual(body["decision"]["reason"], "a watched account could not be read")
        self.assertIn("computed_at", body["decision"])
        self.assertEqual(set(body["poll"]), {"last_ok_at", "last_error", "reads", "running"})
        self.assertEqual(set(body["low_quota"]), {"threshold_percent", "rule", "accounts", "models", "poll_seconds"})
        # /healthz answers the quota decision, which reads the poller's error when it holds no snapshot.
        from lokki_core.quota import poller
        with poller.POLLER._cond:
            poller.POLLER._latest = None
        with mock.patch.dict(poller.POLLER._status, last_error="no management secret file is set"):
            status, body = json_get(self.port, "/healthz")
        self.assertEqual(body["routing"], {"relay_on": False, "reason": "the quota cannot be read"})

    def test_refresh_is_an_operator_route_that_answers_with_a_snapshot(self):
        self.routing_lines = ["[routing] relay off: a watched account could not be read"]
        from lokki_core.quota import poller
        with mock.patch.object(poller.POLLER, "refresh", return_value=poller.POLLER.latest()) as m:
            status, body = json_post(self.port, "/api/accounts/refresh", {})
            self.assertEqual(status, 200)
            m.assert_called_once_with(timeout=15.0)
        self.assertIn("accounts", body)
        self.assertEqual(set(body["poll"]), {"last_ok_at", "last_error", "reads", "running"})
        config.TOKENS = (("app", TOKEN, "operator"), ("default", "cli-tok", "client"))
        with mock.patch.object(poller.POLLER, "refresh") as m:
            self.assertEqual(json_post(self.port, "/api/accounts/refresh", {}, token="cli-tok")[0], 403)
            m.assert_not_called()                   # a client token never starts a read
        self.assertEqual(json_get(self.port, "/api/accounts", token="cli-tok")[0], 403)

    def cliproxy(self):
        """A fake CLIProxyAPI as the upstream, its secret in the file the core reads, and
        the callback listener on a free port: the suite never binds 1455."""
        from lokki_core.quota import oauth, poller
        self.fake = FakeManagement()
        self.addCleanup(self.fake.close)
        config.UPSTREAM = self.fake.url
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        secret_file = os.path.join(d.name, "cliproxy-mgmt.secret")
        with open(secret_file, "w") as f:
            f.write(SECRET + "\n")
        self.addCleanup(setattr, config, "CLIPROXY", config.CLIPROXY)
        config.CLIPROXY = dict(config.CLIPROXY, management_secret_file=secret_file)
        self.callback_port = free_port()
        for patcher in (mock.patch.object(oauth, "CALLBACK_PORT", self.callback_port),
                        mock.patch.object(poller.POLLER, "wake")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(oauth.close_pending)
        return poller.POLLER.wake

    def asked(self, route):
        return [s for s in self.fake.seen if s[1].startswith(route)]

    def login(self):
        """A new login through the route, its listener bound on the injected port: its state."""
        status, flow = exchange(self.port, "POST", "/api/accounts/oauth", b"{}")
        self.assertEqual(status, 200)
        return flow["state"]

    def address(self, state, code=CODE):
        """The address the login's browser ends on, at the injected port."""
        return f"http://localhost:{self.callback_port}/auth/callback?code={code}&state={state}"

    def paste(self, state, url, token=TOKEN):
        """The route's status and its sentence, None for a 200 `{"handed_on": true}`."""
        status, body = exchange(self.port, "POST", f"/api/accounts/oauth/{state}",
                                json.dumps({"url": url}).encode(), token=token)
        if status == 200:
            self.assertEqual(body, {"handed_on": True})
            return status, None
        return status, body["error"]["message"]

    def open_page(self, url):
        """What the listener shows the browser that ends on `url`: status and sentence."""
        status, page = browse(self.callback_port, url[url.index("/auth/"):])
        return status, page.rsplit("<p>", 1)[-1].removesuffix("</p>").replace("&#x27;", "'")

    def expire(self, state=None):
        """The pending login's 300 s have passed."""
        from lokki_core.quota import oauth
        with oauth._pending_lock:
            oauth._pending.deadline = 0.0

    def hand_on_rows(self):
        """Each hand-on result, what brings it about for a fresh login's state, the listener's
        page and the route's answer."""
        from lokki_core.quota import oauth
        def answer(status, error="x"):
            return lambda state: self.fake.callback_answers.append((status, {"status": "error", "error": error}))
        return (
            ("accepted", lambda state: None, (200, "You can close this tab."), (200, None)),
            ("not waiting", self.expire, (409, "This login expired; start again."), (409, GONE)),
            ("another login", lambda state: self.login(),
             (409, "This page belongs to another login; start again from lokki."), (409, GONE)),
            ("already answered", lambda state: oauth.hand_on(state, "an-earlier-code", ""),
             (409, "lokki already has this login's answer; you can close this tab."),
             (409, "lokki already has this login's answer; wait for it to finish")),
            ("expired", answer(404), (409, "This login expired; start again."), (409, "this login expired; start again")),
            ("refused", answer(409), (409, "CLIProxyAPI did not take this login; start again."),
             (409, "CLIProxyAPI did not take this login; start again")),
            ("unreachable", answer(500), (502, "CLIProxyAPI did not take this login; start again."),
             (502, "CLIProxyAPI did not take this login")),
        )

    def test_each_hand_on_result_answers_the_listener_and_the_route_alike(self):
        from lokki_core.quota import oauth
        self.cliproxy()
        for result, make, page, routed in self.hand_on_rows():
            for entry, want in (("listener", page), ("route", routed)):
                with self.subTest(result=result, entry=entry):
                    state = self.login()
                    make(state)
                    with mock.patch.object(oauth, "hand_on", wraps=oauth.hand_on) as hand_on:
                        got = (self.open_page(self.address(state)) if entry == "listener"
                               else self.paste(state, self.address(state)))
                    self.assertEqual(got, want)
                    hand_on.assert_called_once_with(state, CODE, "")
        # Before any hand-on, the route refuses an address whose state is not the path's.
        state = self.login()
        handed = len(self.fake.callbacks)
        self.assertEqual(self.paste(state, self.address("state-9-abcdefgh")),
                         (409, "this address is from another login; paste the address of the login started last"))
        self.assertEqual(len(self.fake.callbacks), handed)

    def test_a_replaced_logins_callback_is_refused_and_never_reaches_cliproxy(self):
        self.cliproxy()
        first = self.login()
        self.login()
        self.assertEqual(self.paste(first, self.address(first)), (409, GONE))
        self.assertEqual(self.open_page(self.address(first)),
                         (409, "This page belongs to another login; start again from lokki."))
        self.assertEqual(self.fake.callbacks, [])

    def test_an_unanswered_hand_on_can_be_sent_again_and_an_answered_one_cannot(self):
        self.cliproxy()
        state = self.login()
        self.fake.callback_answers.append((500, {"status": "error", "error": "failed to persist oauth callback"}))
        self.assertEqual(self.paste(state, self.address(state)), (502, "CLIProxyAPI did not take this login"))
        self.assertEqual(self.paste(state, self.address(state)), (200, None))
        self.assertEqual(self.paste(state, self.address(state)),
                         (409, "lokki already has this login's answer; wait for it to finish"))
        self.assertEqual(self.open_page(self.address(state)),
                         (409, "lokki already has this login's answer; you can close this tab."))
        self.assertEqual([b["state"] for _, b in self.fake.callbacks], [state, state])
        # A post that fails in any way is unanswered: its exception is nowhere, and the login re-armed.
        from lokki_core.quota import oauth
        for entry in ("route", "listener"):
            with self.subTest(entry=entry):
                state = self.login()
                url = self.address(state)
                with mock.patch.object(oauth, "_request", side_effect=RuntimeError("raised-text-x9")):
                    got = self.paste(state, url) if entry == "route" else self.open_page(url)
                self.assertEqual(got, (502, "CLIProxyAPI did not take this login") if entry == "route"
                                 else (502, "CLIProxyAPI did not take this login; start again."))
                self.assertEqual(self.paste(state, url), (200, None))
        self.assertNotIn("raised-text-x9", self.err.getvalue())
        # CLIProxyAPI's 404 or 409 ends the login: the same answer again, through either entry, never
        # "already has this login's answer", and nothing handed on again.
        ends = ((404, (409, "this login expired; start again"), (409, "This login expired; start again.")),
                (409, (409, "CLIProxyAPI did not take this login; start again"),
                 (409, "CLIProxyAPI did not take this login; start again.")))
        for status, routed, page in ends:
            for first in ("route", "listener"):
                with self.subTest(cliproxy=status, first=first):
                    state = self.login()
                    url = self.address(state)
                    handed = len(self.fake.callbacks)
                    self.fake.callback_answers.append((status, {"status": "error", "error": "x"}))
                    self.assertEqual(self.paste(state, url) if first == "route" else self.open_page(url),
                                     routed if first == "route" else page)
                    self.assertEqual((self.paste(state, url), self.open_page(url)), (routed, page))
                    self.assertEqual(len(self.fake.callbacks), handed + 1)
        # A replaced login's post that fails late re-arms that login, never the one that replaced it.
        first = self.login()
        self.fake.callback_gates[first] = gate = threading.Event()
        self.addCleanup(gate.set)
        answered = []
        sent = threading.Thread(target=lambda: answered.append(self.paste(first, self.address(first))))
        sent.start()
        self.assertTrue(self.fake.callback_arrived.wait(5), "the first login's post never reached CLIProxyAPI")
        second = self.login()
        self.assertEqual(self.paste(second, self.address(second)), (200, None))
        gate.set()
        sent.join(10)
        self.assertEqual(answered, [(502, "CLIProxyAPI did not take this login")])
        self.assertEqual(self.paste(second, self.address(second)),
                         (409, "lokki already has this login's answer; wait for it to finish"))
        self.assertEqual([b["state"] for _, b in self.fake.callbacks].count(second), 1)

    def test_the_paste_route_refuses_each_address_that_is_not_the_callback(self):
        self.cliproxy()
        state = self.login()
        for label, body, sentence in not_the_callback(self.callback_port, state):
            with self.subTest(label):
                status, answer = exchange(self.port, "POST", f"/api/accounts/oauth/{state}", body)
                self.assertEqual((status, answer["error"]["message"]), (400, sentence))
        for bad in ("short", "x" * 129, "dots.are.not.in.it"):
            status, answer = exchange(self.port, "POST", f"/api/accounts/oauth/{bad}",
                                      json.dumps({"url": self.address(state)}).encode())
            self.assertEqual((status, answer["error"]["message"]), (400, "the state must match [A-Za-z0-9_-]{8,128}"), bad)
        status, answer = exchange(self.port, "POST", f"/api/accounts/oauth/{state}",
                                  json.dumps({"url": self.address(state) + "&pad=" + "x" * 16384}).encode())
        self.assertEqual(status, 413)
        self.assertIn("the limit is 16384", answer["error"]["message"])
        self.assertEqual(self.fake.callbacks, [])
        # The address as a copy brings it, with spaces or a newline round it, is the callback's.
        for label, around in (("a leading space", " {}"), ("a trailing space", "{} "), ("a trailing newline", "{}\n")):
            with self.subTest(label):
                state = self.login()
                self.assertEqual(self.paste(state, around.format(self.address(state))), (200, None))
        # A wrapped paste: tabs and line breaks inside it are dropped, and the code handed on whole.
        state = self.login()
        wrapped = self.address(state, "c0\r\nde").replace("/auth/", "/au\tth/")
        self.assertEqual(self.paste(state, wrapped), (200, None))
        self.assertEqual(self.fake.callbacks[-1][1], {"code": "c0de", "state": state, "error": ""})

    def test_a_client_token_is_refused_before_the_paste_body_is_read(self):
        self.cliproxy()
        config.TOKENS = (("app", TOKEN, "operator"), ("default", CLIENT_TOKEN, "client"))
        state = self.login()
        big = json.dumps({"url": self.address(state) + "&pad=" + "x" * 16384}).encode()
        self.assertEqual(exchange(self.port, "POST", f"/api/accounts/oauth/{state}", big, token=CLIENT_TOKEN)[0], 403)
        self.assertEqual(self.paste(state, self.address(state), token=CLIENT_TOKEN)[0], 403)
        self.assertEqual(exchange(self.port, "POST", f"/api/accounts/oauth/{state}", big)[0], 413)
        self.assertEqual(self.fake.callbacks, [])

    def test_the_login_status_passes_only_cliproxys_fixed_sentences(self):
        self.cliproxy()
        fixed = ("Timeout waiting for OAuth callback", "Bad request", "Bad Request", "State code error",
                 "Failed to exchange authorization code for tokens", "Failed to save authentication tokens",
                 "Authentication failed")
        failed = "the login failed; CLIProxyAPI's log has the detail"
        rows = [(text, text) for text in fixed] + [
            ("Failed to exchange authorization code for tokens: token exchange failed with status 400: {}",
             "Failed to exchange authorization code for tokens; CLIProxyAPI's log has the detail"),
            ("Authentication failed: state mismatch", failed), ("access_denied", failed),
            ("unknown or expired state", "CLIProxyAPI no longer holds this login: it expired, or it finished "
                                         "more than a minute ago; check Accounts, else start again"),
            ("bad request", failed), (" State code error", failed)]
        for text, want in rows:
            with self.subTest(text):
                self.fake.auth_status["state-1-abcdefgh"] = {"status": "error", "error": text}
                self.assertEqual(exchange(self.port, "GET", "/api/accounts/oauth/state-1-abcdefgh"),
                                 (200, {"status": "error", "error": want}))

    def test_a_sentinel_code_reaches_no_answer_page_or_log(self):
        from lokki_core import trace
        from lokki_core.quota import oauth
        self.cliproxy()
        sentinel = "sentinel-code-q7Rz"
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        captures, traced = os.path.join(d.name, "captures"), os.path.join(d.name, "trace.jsonl")
        self.addCleanup(setattr, config, "CAPTURE_DIR", config.CAPTURE_DIR)
        config.CAPTURE_DIR = captures
        trace.enable(traced)
        self.addCleanup(trace.disable)
        # The control: a trace that was never on would hold no sentinel too.
        trace.record_note("control")
        with open(traced) as f:
            control_lines = f.read().splitlines()
        self.assertEqual(len(control_lines), 1, "the trace is not on: the rows below would prove nothing")
        self.assertIn('"control"', control_lines[0])
        config.TOKENS = (("app", TOKEN, "operator"), ("default", CLIENT_TOKEN, "client"))
        said = []                               # every answer and page, as the caller reads it

        def post(state, body, token=TOKEN):
            status, text = send(self.port, "POST", f"/api/accounts/oauth/{state}", body, token=token)
            said.append(text)
            self.assertNotEqual(status, 500, text)

        state = self.login()
        for label, body, _ in not_the_callback(self.callback_port, state, code=sentinel,
                                               port_text=sentinel, host_text=sentinel):
            post(state, body)
        address = self.address(state, sentinel)
        post(state, json.dumps({"url": address + "&pad=" + "x" * 16384}).encode())          # 413
        post(state, json.dumps({"url": address + "&pad=" + "x" * 16384}).encode(), CLIENT_TOKEN)
        post(state, json.dumps({"url": address}).encode(), CLIENT_TOKEN)
        post("short", json.dumps({"url": address}).encode())
        post(state, json.dumps({"url": self.address("state-9-abcdefgh", sentinel)}).encode())
        with_sentinel = {"expired": 404, "refused": 409, "unreachable": 500}
        for result, make, _, _ in self.hand_on_rows():
            for entry in ("listener", "route"):
                state = self.login()
                if result in with_sentinel:     # CLIProxyAPI's own body says the sentinel
                    self.fake.callback_answers.append((with_sentinel[result], {"status": "error", "error": sentinel}))
                else:
                    make(state)
                if entry == "listener":
                    status, page = browse(self.callback_port, self.address(state, sentinel)[len(f"http://localhost:{self.callback_port}"):])
                    said.append(page)
                    self.assertNotEqual(status, 500, page)
                else:
                    post(state, json.dumps({"url": self.address(state, sentinel)}).encode())
        self.assertIn(sentinel, json.dumps(self.fake.callbacks), "no row handed the sentinel on")
        self.fake.auth_status[state] = {"status": "error", "error": "Failed to exchange authorization code for tokens: "
                                        f"token exchange failed with status 400: {{\"code\": \"{sentinel}\"}}"}
        status, text = send(self.port, "GET", f"/api/accounts/oauth/{state}")
        said.append(text)
        self.assertEqual(status, 200)
        oauth.close_pending()
        for text in said:
            self.assertNotIn(sentinel, text)
        self.assertNotIn(sentinel, self.err.getvalue())
        self.assertFalse(os.path.exists(captures) and os.listdir(captures), "a body was captured")
        with open(traced) as f:
            self.assertNotIn(sentinel, f.read())

    def test_oauth_start_returns_what_cliproxy_gave_and_opens_the_loopback_listener(self):
        self.cliproxy()
        for n, body in enumerate((None, b"{}"), 1):          # no body, or an empty object
            status, flow = exchange(self.port, "POST", "/api/accounts/oauth", body)
            self.assertEqual(status, 200)
            state = f"state-{n}-abcdefgh"                     # a second start replaces the first flow's listener
            self.assertEqual(flow, {"state": state, "url": f"https://auth.example/oauth/authorize?state={state}"})
        self.assertEqual([s[1] for s in self.asked("/v0/management/codex-auth-url")],
                         ["/v0/management/codex-auth-url"] * 2)
        self.assertEqual(browse(self.callback_port, "/auth/callback?error=access_denied&state=state-2-abcdefgh")[0], 200)
        self.assertEqual([b["state"] for _, b in self.fake.callbacks], ["state-2-abcdefgh"])

    def test_oauth_status_passes_through_and_wakes_the_poller_on_ok(self):
        wake = self.cliproxy()
        self.fake.auth_status = {"state-1-abcdefgh": {"status": "wait"},
                                 "state-2-abcdefgh": {"status": "error", "error": "Bad Request"}}
        exchange(self.port, "POST", "/api/accounts/oauth", b"{}")
        self.assertEqual(exchange(self.port, "GET", "/api/accounts/oauth/state-1-abcdefgh"),
                         (200, {"status": "pending", "error": None}))
        self.assertFalse(port_is_free(self.callback_port), "a pending flow lost its listener")
        self.fake.auth_status["state-1-abcdefgh"] = {"status": "ok"}
        self.assertEqual(exchange(self.port, "GET", "/api/accounts/oauth/state-1-abcdefgh"),
                         (200, {"status": "ok", "error": None}))
        wake.assert_called_once_with()
        self.assertTrue(port_is_free(self.callback_port), "the listener outlived its flow")
        exchange(self.port, "POST", "/api/accounts/oauth", b"{}")
        self.assertEqual(exchange(self.port, "GET", "/api/accounts/oauth/state-2-abcdefgh"),
                         (200, {"status": "error", "error": "Bad Request"}))
        self.assertTrue(port_is_free(self.callback_port), "the listener outlived its flow")
        exchange(self.port, "POST", "/api/accounts/oauth", b"{}")
        # An earlier flow's answer is about that flow: the pending one keeps its listener.
        self.assertEqual(exchange(self.port, "GET", "/api/accounts/oauth/state-2-abcdefgh")[1]["status"], "error")
        self.assertFalse(port_is_free(self.callback_port), "an earlier flow's error closed the pending one")
        wake.assert_called_once_with()

    def test_a_wait_for_a_login_lokki_no_longer_waits_for_is_an_error(self):
        from lokki_core.quota import oauth
        self.cliproxy()
        gone = (200, {"status": "error", "error": "this login is no longer waiting; start again"})
        pending = (200, {"status": "pending", "error": None})
        poll = lambda n: exchange(self.port, "GET", f"/api/accounts/oauth/state-{n}-abcdefgh")
        self.fake.auth_status = {f"state-{n}-abcdefgh": {"status": "wait"} for n in (1, 2, 9)}
        with self.subTest("no login pending"):          # CLIProxyAPI still holds a login this core never started
            oauth.close_pending()
            self.assertEqual(poll(9), gone)
        with self.subTest("a replaced login"):          # CLIProxyAPI holds the first for its own five minutes
            exchange(self.port, "POST", "/api/accounts/oauth", b"{}")
            exchange(self.port, "POST", "/api/accounts/oauth", b"{}")
            self.assertEqual(poll(1), gone)
            self.assertFalse(port_is_free(self.callback_port), "the replaced login's answer closed the pending one")
            self.assertEqual(poll(2), pending)          # the pending login's own wait stays a wait
        with self.subTest("its deadline passed"):
            oauth._pending.deadline = time.monotonic() - 1
            self.assertEqual(poll(2), gone)
            self.assertTrue(port_is_free(self.callback_port), "the listener outlived its flow")
        with self.subTest("an accepted hand-on stays a wait until CLIProxyAPI answers"):
            exchange(self.port, "POST", "/api/accounts/oauth", b"{}")
            self.assertEqual(browse(self.callback_port, "/auth/callback?code=c&state=state-3-abcdefgh")[0], 200)
            self.fake.auth_status["state-3-abcdefgh"] = {"status": "wait"}
            self.assertEqual(poll(3), pending)
            self.fake.auth_status["state-3-abcdefgh"] = {"status": "ok"}
            self.assertEqual(poll(3), (200, {"status": "ok", "error": None}))

    def test_models_lists_cliproxys_ids_sorted_once_and_names_each_failure(self):
        self.cliproxy()
        self.addCleanup(setattr, config, "API_KEY", config.API_KEY)
        config.API_KEY = "core-key-for-cliproxy"
        self.fake.models_answer = (200, json.dumps({"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}, {"id": 7}, {"x": 1}, "c"]}).encode())
        self.assertEqual(exchange(self.port, "GET", "/api/models"), (200, {"models": ["a", "b"]}))
        self.assertEqual(self.fake.model_auth, ["Bearer core-key-for-cliproxy"])
        self.fake.models_answer = None
        self.fake.models = []
        self.assertEqual(exchange(self.port, "GET", "/api/models"), (200, {"models": []}))
        rows = [("a status that is not 200", (500, b"{}"), "CLIProxyAPI answered 500"),
                ("a 401", (401, b"{}"), "CLIProxyAPI answered 401"),
                ("no data", (200, b'{"object": "list"}'), "CLIProxyAPI's model list has no data"),
                ("data that is not a list", (200, b'{"data": {"id": "a"}}'), "CLIProxyAPI's model list has no data"),
                ("a body that is not an object", (200, b"[1]"), "CLIProxyAPI's model list has no data"),
                ("a body that is not JSON", (200, b"<html>"), "CLIProxyAPI's model list has no data")]
        for label, answer, sentence in rows:
            with self.subTest(label):
                self.fake.models_answer = answer
                status, body = exchange(self.port, "GET", "/api/models")
                self.assertEqual((status, body["error"]["message"]), (502, sentence))
        with self.subTest("a closed port"):
            config.UPSTREAM = f"http://127.0.0.1:{free_port()}"
            status, body = exchange(self.port, "GET", "/api/models")
            self.assertEqual((status, body["error"]["message"]), (502, "CLIProxyAPI did not answer"))

    def test_models_failures_carry_no_exception_text_and_log_no_key(self):
        """An exception's text and the core's key reach neither the answer nor stderr."""
        from lokki_core.quota import reader
        self.cliproxy()
        self.addCleanup(setattr, config, "API_KEY", config.API_KEY)
        config.API_KEY = "core-key-for-cliproxy"
        for exc in (OSError("sentinel-text-from-the-socket"), TimeoutError("sentinel-text-from-the-socket"),
                    ValueError("sentinel-text-from-the-socket")):
            with self.subTest(type(exc).__name__):
                err = io.StringIO()
                with mock.patch.object(reader._OPENER, "open", side_effect=exc), contextlib.redirect_stderr(err):
                    status, body = exchange(self.port, "GET", "/api/models")
                self.assertEqual((status, body["error"]["message"]), (502, "CLIProxyAPI did not answer"))
                for leak in ("sentinel-text-from-the-socket", "core-key-for-cliproxy"):
                    self.assertNotIn(leak, json.dumps(body))
                    self.assertNotIn(leak, err.getvalue())

    def test_models_goes_through_the_readers_opener_not_the_system_proxy(self):
        from lokki_core.quota import reader
        self.cliproxy()
        self.fake.models = ["a"]
        with mock.patch.object(reader._OPENER, "open", wraps=reader._OPENER.open) as opened:
            self.assertEqual(exchange(self.port, "GET", "/api/models")[1], {"models": ["a"]})
        self.assertEqual(opened.call_args.kwargs.get("timeout"), 10.0)

    def test_a_bad_state_is_400_a_busy_port_is_409_and_cliproxy_down_or_refusing_is_502(self):
        self.cliproxy()
        for state in ("short", "x" * 129, "dots.are.not.in.it", "abcdefgh/more", "spaces%20in%20it"):
            self.assertEqual(exchange(self.port, "GET", f"/api/accounts/oauth/{state}")[0], 400, state)
        self.assertEqual(self.asked("/v0/management/get-auth-status"), [])
        held = socket.socket()
        held.bind(("127.0.0.1", self.callback_port))
        held.listen(1)
        status, body = exchange(self.port, "POST", "/api/accounts/oauth", b"{}")
        held.close()
        self.assertEqual(status, 409)
        self.assertIn("another login is in progress", body["error"]["message"])
        self.assertEqual(self.asked("/v0/management/codex-auth-url"), [])
        secret_file = config.CLIPROXY["management_secret_file"]
        config.CLIPROXY = dict(config.CLIPROXY, management_secret_file=secret_file + ".missing")
        status, body = exchange(self.port, "POST", "/api/accounts/oauth", b"{}")
        self.assertEqual(status, 409)
        self.assertIn("error", body)
        self.assertTrue(port_is_free(self.callback_port), "the listener bound before the secret was read")
        self.assertEqual(self.asked("/v0/management/codex-auth-url"), [])
        config.CLIPROXY = dict(config.CLIPROXY, management_secret_file=secret_file)
        wrong_file = secret_file + ".wrong"
        with open(wrong_file, "w") as f:
            f.write("not-the-secret\n")
        right = (config.CLIPROXY, config.UPSTREAM)
        for name, change in (("wrong secret", lambda: setattr(config, "CLIPROXY", dict(config.CLIPROXY, management_secret_file=wrong_file))),
                             ("banned", lambda: setattr(self.fake, "banned", True)),
                             ("down", lambda: setattr(config, "UPSTREAM", f"http://127.0.0.1:{free_port()}"))):
            config.CLIPROXY, config.UPSTREAM = right
            self.fake.banned = False
            change()
            for method, path in (("POST", "/api/accounts/oauth"), ("GET", "/api/accounts/oauth/state-1-abcdefgh")):
                status, body = exchange(self.port, method, path, b"{}" if method == "POST" else None)
                self.assertEqual(status, 502, (name, method))
                self.assertIn("error", body)
                self.assertFalse(SECRET in json.dumps(body), "an answer carries the management secret")
            self.assertTrue(port_is_free(self.callback_port), f"{name}: the listener stayed after a 502")


if __name__ == "__main__":
    unittest.main()
