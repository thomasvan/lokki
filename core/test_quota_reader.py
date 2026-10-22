#!/usr/bin/env python3
"""The quota reader: one snapshot of every codex account's meter through
CLIProxyAPI's management API, emails masked, every failure a field or one
exception, bounded in time. And the OAuth calls beside it: the login URL, its
status, and the loopback listener that hands the provider's callback on."""
import contextlib
import http.client
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core.quota import oauth, reader  # noqa: E402

SECRET = "mgmt-test-secret"


class FakeManagement:
    """CLIProxyAPI's management API, the routes the reader and the OAuth calls use.

    `credentials` is the list the credentials route answers; `usage` maps an
    auth_index to the dict the usage call answers with (the provider's body), or
    to an Exception to answer 500, or to a float to sleep that long first.
    `auth_status` maps a state to what `get-auth-status` answers for it (an
    unknown state is `error`, as on the binary); `auth_url` replaces the
    `codex-auth-url` answer with `(status, raw body)`. `banned` answers every keyed
    route 403, as the binary does to an address after repeated failed logins.
    `callbacks` records each `oauth-callback` as `(Authorization header, body)`:
    each takes the next `(status, body)` of `callback_answers` while it holds one, else
    the first 200 for a state is 200 and a later one 409. A callback whose state is in
    `callback_gates` sets `callback_arrived`, waits for that state's event, then closes the
    connection with no answer. `/v1/models` answers `{"data": [{"id": m}…]}`
    for each of `models` (or the `(status, raw body)` of `models_answer`), asks no management
    secret, and records each `Authorization` header it is sent in `model_auth`."""

    def __init__(self, credentials=None, usage=None, secret=SECRET, status_credentials=200):
        self.credentials = credentials if credentials is not None else []
        self.usage = usage or {}
        self.secret = secret
        self.status_credentials = status_credentials
        self.auth_status = {}
        self.auth_url = None
        self.banned = False
        self.flows = 0
        self.callbacks = []
        self.callback_answers = []
        self.callback_gates = {}
        self.callback_arrived = threading.Event()
        self._taken = set()
        self.models = []
        self.models_answer = None
        self.model_auth = []
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, obj):
                self._send_raw(status, json.dumps(obj).encode())

            def _send_raw(self, status, body):
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except ConnectionError:
                    pass                     # the reader gave up (a timeout test); nothing to tell

            def _auth(self):
                if outer.banned:
                    self._send(403, {"error": "IP banned due to too many failed attempts"})
                    return False
                if self.headers.get("Authorization") != f"Bearer {outer.secret}":
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                outer.seen.append(("GET", self.path))
                route = urllib.parse.urlsplit(self.path)
                if route.path == "/v1/models":                 # the core's key, not the management secret
                    outer.model_auth.append(self.headers.get("Authorization"))
                    if outer.models_answer is not None:
                        self._send_raw(*outer.models_answer)
                    else:
                        self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in outer.models]})
                    return
                if not self._auth():
                    return
                if route.path == "/v8/management/credentials":
                    self._send(outer.status_credentials, {"files": outer.credentials})
                elif route.path == "/v0/management/codex-auth-url":
                    if outer.auth_url is not None:
                        self._send_raw(*outer.auth_url)
                        return
                    outer.flows += 1
                    state = f"state-{outer.flows}-abcdefgh"
                    self._send(200, {"status": "ok", "state": state,
                                     "url": f"https://auth.example/oauth/authorize?state={state}"})
                elif route.path == "/v0/management/get-auth-status":
                    state = urllib.parse.parse_qs(route.query).get("state", [""])[0]
                    self._send(200, outer.auth_status.get(state, {"status": "error", "error": "unknown or expired state"}))
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.seen.append(("POST", self.path, body))
                if self.path == "/v0/management/oauth-callback":     # the binary asks no key here
                    gate = outer.callback_gates.get(body.get("state"))
                    if gate is not None:
                        outer.callbacks.append((self.headers.get("Authorization"), body))
                        outer.callback_arrived.set()
                        gate.wait(10)
                        self.close_connection = True
                        return
                    if outer.callback_answers:
                        status, answer = outer.callback_answers.pop(0)
                    elif body.get("state") in outer._taken:
                        status, answer = 409, {"error": "oauth flow is already completed"}
                    else:
                        status, answer = 200, {"status": "ok"}
                    if status == 200:
                        outer._taken.add(body.get("state"))
                    outer.callbacks.append((self.headers.get("Authorization"), body))
                    self._send(status, answer)
                    return
                if not self._auth():
                    return
                if self.path != "/v8/management/requests/api-call":
                    self._send(404, {"error": "not found"})
                    return
                answer = outer.usage.get(body.get("authIndex"))
                if isinstance(answer, float):
                    time.sleep(answer)
                    answer = {"rate_limit": {"primary_window": {"used_percent": 1, "reset_at": 0}}}
                if isinstance(answer, Exception):
                    self._send(500, {"error": str(answer)})
                    return
                if answer is None:
                    self._send(200, {"status_code": 404, "body": "{}"})
                    return
                self._send(200, {"status_code": 200, "body": json.dumps(answer)})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port():
    """A loopback port nothing listens on now: the suite never binds the callback's real one."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_is_free(port):
    """Whether a listener could bind the port now. SO_REUSEADDR, so a closed listener's
    TIME_WAIT connections do not count as a holder; a socket still listening does. On
    Windows the option lets a socket share a port another one holds with it too, so
    this also stands for a second listener that tries to share the callback's."""
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            s.listen(1)
        except OSError:
            return False
        return True


def browse(port, path):
    """What a browser gets from the listener: status and page. http.client, so no
    proxy setting of the test's own reaches this request."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def wait_for(cond, timeout=5.0):
    """Poll `cond` until true or `timeout`; the assertion that follows says what did not happen."""
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.02)


def usage(used, reset_at=4102444800, window=18000):
    return {"plan_type": "plus", "rate_limit": {"primary_window": {
        "used_percent": used, "reset_at": reset_at, "limit_window_seconds": window}}}


CREDS = [
    {"provider": "codex", "auth_index": "a1", "email": "alice@example.com", "disabled": False},
    {"provider": "codex", "auth_index": "b2", "email": "bob@example.com", "disabled": True},
    {"provider": "gemini", "auth_index": "g3", "email": "gem@example.com", "disabled": False},
]


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeManagement(credentials=list(CREDS), usage={"a1": usage(40), "b2": usage(90)})
        self.addCleanup(self.fake.close)

    def read(self, **kw):
        return reader.read(self.fake.url, SECRET, **kw)

    def test_a_snapshot_names_every_codex_account_masked_with_its_meter(self):
        snap = self.read()
        self.assertEqual(set(snap["accounts"]), {"a1", "b2"})          # gemini is not a codex account
        a = snap["accounts"]["a1"]
        self.assertEqual(a["email_masked"], "a***@example.com")
        self.assertNotIn("alice", json.dumps(snap))
        self.assertTrue(a["enabled"])
        self.assertEqual(a["used_percent"], 40)
        self.assertEqual(a["reset_at"], 4102444800)
        self.assertEqual(a["window_seconds"], 18000)
        self.assertIsNone(a["error"])
        self.assertFalse(snap["accounts"]["b2"]["enabled"])
        self.assertAlmostEqual(snap["read_at"], time.time(), delta=5)

    def test_every_enabled_account_and_every_named_one_is_asked(self):
        snap = self.read(accounts=("b2",))
        self.assertEqual(set(snap["accounts"]), {"a1", "b2"})          # every codex account is listed
        self.assertEqual(snap["accounts"]["a1"]["used_percent"], 40)   # enabled, not named: read
        self.assertIsNone(snap["accounts"]["a1"]["error"])
        self.assertEqual(snap["accounts"]["b2"]["used_percent"], 90)   # disabled, but named: read
        posts = [s for s in self.fake.seen if s[0] == "POST"]
        self.assertEqual(sorted(p[2]["authIndex"] for p in posts), ["a1", "b2"])

    def test_a_disabled_account_that_is_not_named_is_not_asked_and_says_disabled(self):
        snap = self.read(accounts=("a1",))
        self.assertEqual(snap["accounts"]["b2"]["error"], "disabled")
        self.assertIsNone(snap["accounts"]["b2"]["used_percent"])
        self.assertEqual([s[2]["authIndex"] for s in self.fake.seen if s[0] == "POST"], ["a1"])

    def test_a_disabled_account_is_not_asked_when_no_scope_is_given(self):
        snap = self.read()
        posts = [s[2]["authIndex"] for s in self.fake.seen if s[0] == "POST"]
        self.assertEqual(posts, ["a1"])
        self.assertEqual(snap["accounts"]["b2"]["error"], "disabled")

    def test_the_usage_call_carries_the_probe_s_headers_and_url(self):
        self.read()
        post = [s for s in self.fake.seen if s[0] == "POST"][0][2]
        self.assertEqual(post["url"], reader.USAGE_URL)
        self.assertEqual(post["method"], "GET")
        self.assertEqual(post["header"], dict(reader.USAGE_HEADERS))
        self.assertIn("$TOKEN$", post["header"]["Authorization"])

    def test_a_per_account_failure_is_a_field_not_an_exception(self):
        self.fake.usage["a1"] = RuntimeError("boom")
        a = self.read()["accounts"]["a1"]
        self.assertIsNone(a["used_percent"])
        self.assertIn("HTTP 500", a["error"])

    def test_a_missing_rate_limit_block_is_an_error_never_zero(self):
        self.fake.usage["a1"] = {"plan_type": "free"}
        a = self.read()["accounts"]["a1"]
        self.assertIsNone(a["used_percent"])
        self.assertEqual(a["error"], "no rate_limit block")

    def test_a_non_200_from_the_provider_is_an_error(self):
        self.fake.usage["a1"] = None            # the fake answers status_code 404
        self.assertEqual(self.read()["accounts"]["a1"]["error"], "HTTP 404")

    def test_a_refused_secret_is_a_management_error_carrying_the_status(self):
        """The poller holds on a 401 or a 403 (a banned address refuses the right secret too)."""
        with self.assertRaises(reader.ManagementError) as caught:
            reader.read(self.fake.url, "wrong")
        self.assertTrue(str(caught.exception).startswith("HTTP 401"))
        self.assertEqual(caught.exception.status, 401)
        self.fake.banned = True
        with self.assertRaises(reader.ManagementError) as caught:
            self.read()
        self.assertEqual(caught.exception.status, 403)

    def test_a_per_account_transport_failure_is_a_field(self):
        from unittest import mock
        import http.client
        # IncompleteRead is an HTTPException and not an OSError: the one class the
        # tuple adds (RemoteDisconnected would pass as an OSError).
        with mock.patch.object(reader, "_request", side_effect=http.client.IncompleteRead(b"")):
            with self.assertRaises(reader.ManagementError):     # the credentials call: the whole read
                self.read()
        real = reader._request

        def flaky(url, secret, data=None, timeout=10.0):
            if data and b'"a1"' in data:
                raise http.client.IncompleteRead(b"")
            return real(url, secret, data, timeout)
        with mock.patch.object(reader, "_request", side_effect=flaky):
            snap = self.read()
        self.assertEqual(snap["accounts"]["a1"]["error"], "IncompleteRead")
        self.assertEqual(snap["accounts"]["b2"]["error"], "disabled")

    def test_an_unreachable_management_api_is_a_management_error(self):
        with self.assertRaises(reader.ManagementError) as caught:
            reader.read("http://127.0.0.1:9", SECRET, per_call_timeout=1.0)
        self.assertIsNone(caught.exception.status)          # no answer: no status to hold on

    def test_a_404_on_the_management_route_names_the_version(self):
        self.fake.status_credentials = 404
        with self.assertRaises(reader.ManagementError) as caught:
            self.read()
        self.assertIn("management route not found: CLIProxyAPI version?", str(caught.exception))

    def test_a_version_of_another_major_is_refused_before_any_call(self):
        with self.assertRaises(reader.ManagementError) as caught:
            self.read(cliproxy_version="9.1.0")
        self.assertIn("major 9", str(caught.exception))
        self.assertEqual(self.fake.seen, [])
        self.read(cliproxy_version="8.0.23")          # the known major passes
        self.read(cliproxy_version="v8.0.23")         # a release tag's leading v is not a major
        self.read(cliproxy_version="")                 # unknown: the check is skipped

    def test_a_slow_account_hits_the_per_call_timeout(self):
        self.fake.usage["a1"] = 3.0
        t0 = time.monotonic()
        a = self.read(per_call_timeout=0.5)["accounts"]["a1"]
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual(a["error"], "timeout")

    def test_accounts_are_read_in_parallel_and_the_deadline_marks_the_rest(self):
        creds = [dict(c, auth_index=f"x{i}", provider="codex", disabled=False) for i, c in enumerate(CREDS * 3)]
        self.fake.credentials = creds
        for c in creds:
            self.fake.usage[c["auth_index"]] = 0.6
        t0 = time.monotonic()
        snap = self.read(per_call_timeout=5.0, deadline=1.5)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 4.0, "nine 0.6 s reads took longer than four in parallel allow")
        states = [a["error"] for a in snap["accounts"].values()]
        self.assertIn(None, states)                    # some were read
        self.assertIn("deadline", states)              # some were not, and say so

    def test_a_rate_limit_that_is_not_an_object_is_a_field_not_an_abort(self):
        self.fake.usage["a1"] = {"rate_limit": "none"}
        snap = self.read()
        self.assertEqual(snap["accounts"]["a1"]["error"], "no rate_limit block")
        self.fake.usage["a1"] = {"rate_limit": ["x"]}
        self.assertEqual(self.read()["accounts"]["a1"]["error"], "no rate_limit block")

    def test_a_credentials_route_that_does_not_answer_a_list_is_a_management_error(self):
        for files in (None, "x", {"a": 1}):
            self.fake.credentials = files
            with self.assertRaises(reader.ManagementError) as caught:
                self.read()
            self.assertIn("did not answer a list", str(caught.exception))

    def test_an_unusable_management_url_is_a_management_error(self):
        for url in ("", "not a url"):
            with self.assertRaises(reader.ManagementError) as caught:
                reader.read(url, SECRET)
            self.assertIn("management URL is unusable", str(caught.exception))

    def test_a_credential_without_an_auth_index_is_skipped(self):
        self.fake.credentials = [{"provider": "codex", "email": "n@example.com"},
                                 {"provider": "codex", "auth_index": 7, "email": "m@example.com"}] + list(CREDS)
        self.assertEqual(set(self.read()["accounts"]), {"a1", "b2"})

    def test_the_management_calls_ignore_a_system_proxy(self):
        """CLIProxyAPI is the core's loopback neighbour: a proxy must not see the secret
        or the provider's code. `urlopen`'s shared opener read the environment when it
        was first built, so it is dropped for the test and built again under the proxy."""
        closed = free_port()
        proxy = {"http_proxy": f"http://127.0.0.1:{closed}", "HTTP_PROXY": f"http://127.0.0.1:{closed}"}
        without_bypass = {k: v for k, v in os.environ.items() if k.lower() != "no_proxy"}
        with mock.patch.dict(os.environ, dict(without_bypass, **proxy), clear=True), \
                mock.patch.object(urllib.request, "_opener", None), \
                mock.patch.object(oauth, "CALLBACK_PORT", free_port()):
            self.assertEqual(self.read()["accounts"]["a1"]["used_percent"], 40)
            flow = oauth.start(self.fake.url, SECRET)
            self.fake.auth_status[flow["state"]] = {"status": "wait"}
            self.assertEqual(oauth.status(self.fake.url, SECRET, flow["state"])[0], "pending")
            listener = oauth.listen(self.fake.url)
            self.addCleanup(oauth.close_pending)
            oauth.named(listener, flow["state"])
            self.assertEqual(browse(listener.address[1], f"/auth/callback?error=access_denied&state={flow['state']}")[0], 200)
        self.assertEqual(len(self.fake.callbacks), 1)

    def test_mask_email(self):
        self.assertEqual(reader.mask_email("alice@example.com"), "a***@example.com")
        self.assertEqual(reader.mask_email("a@b"), "a***@b")
        self.assertEqual(reader.mask_email("noat"), "n***")
        self.assertEqual(reader.mask_email(""), "")


CODE = "test-code-from-the-provider"


class OAuthTests(unittest.TestCase):
    """The browser login CLIProxyAPI runs for the core: the URL, its status, and the
    listener for the provider's callback, which binds an injected free port."""

    def setUp(self):
        self.fake = FakeManagement()
        self.addCleanup(self.fake.close)
        port = mock.patch.object(oauth, "CALLBACK_PORT", free_port())
        port.start()
        self.addCleanup(port.stop)
        self.addCleanup(oauth.close_pending)

    def listener(self, state="state-code-1abc"):
        """The pending login's listener, its state named as CLIProxyAPI names it."""
        cb = oauth.listen(self.fake.url)
        oauth.named(cb, state)
        return cb

    def test_oauth_start_asks_the_auth_url_route_with_the_bearer_and_returns_state_and_url(self):
        flow = oauth.start(self.fake.url, SECRET)
        self.assertEqual(flow, {"state": "state-1-abcdefgh",
                                "url": "https://auth.example/oauth/authorize?state=state-1-abcdefgh"})
        # No `is_webui`: with it the binary opens its own forwarder on 0.0.0.0:1455.
        self.assertEqual(self.fake.seen, [("GET", "/v0/management/codex-auth-url")])
        for answer in ((500, b'{"error": "boom"}'), (200, b"not json"), (200, b'{"state": "state-x-abcdefgh"}'),
                       (200, b'{"url": "https://auth.example/"}'), (200, b"[]")):
            self.fake.auth_url = answer
            with self.assertRaises(reader.ManagementError, msg=answer):
                oauth.start(self.fake.url, SECRET)
        self.fake.auth_url = None
        with self.assertRaises(reader.ManagementError) as caught:
            oauth.start(self.fake.url, "wrong")
        self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(reader.ManagementError) as caught:
            oauth.start(f"http://127.0.0.1:{free_port()}", SECRET)
        self.assertIsNone(caught.exception.status)

    def test_oauth_status_maps_wait_to_pending_and_carries_the_error(self):
        self.fake.auth_status = {"state-wait-1": {"status": "wait"},
                                 "state-wait-2": {"status": "wait", "error": ""},
                                 "state-done-1": {"status": "ok"},
                                 "state-fail-1": {"status": "error", "error": "State code error"},
                                 "state-fail-2": {"status": "error", "error": "access_denied"}}
        failed = "the login failed; CLIProxyAPI's log has the detail"
        for state, want in (("state-wait-1", ("pending", None)), ("state-wait-2", ("pending", None)),
                            ("state-done-1", ("ok", None)), ("state-fail-1", ("error", "State code error")),
                            ("state-fail-2", ("error", failed)),
                            ("state-gone-1", ("error", "CLIProxyAPI no longer holds this login: it expired, or it "
                                                       "finished more than a minute ago; check Accounts, else start again"))):
            self.assertEqual(oauth.status(self.fake.url, SECRET, state), want, state)
        self.assertIn(("GET", "/v0/management/get-auth-status?state=state-wait-1"), self.fake.seen)
        self.fake.banned = True
        with self.assertRaises(reader.ManagementError) as caught:
            oauth.status(self.fake.url, SECRET, "state-wait-1")
        self.assertEqual(caught.exception.status, 403)

    def test_the_callback_is_handed_to_cliproxy_once_and_the_listener_closes(self):
        cb = self.listener("state-code-1abc")
        port = cb.address[1]
        self.assertEqual(port, oauth.CALLBACK_PORT)
        said = io.StringIO()
        with contextlib.redirect_stderr(said):          # the default request log writes the query, code included
            other = browse(port, f"/auth/callback?code={CODE}&state=state-other-1")
            first = browse(port, f"/auth/callback?code={CODE}&state=state-code-1abc")
            again = browse(port, f"/auth/callback?code={CODE}&state=state-code-1abc")
            self.listener("state-deny-1abc")
            denied = browse(port, "/auth/callback?error=access_denied&state=state-deny-1abc")
            refused = {path: browse(port, path)[0] for path in (
                "/auth/callback?state=state-bare-1", "/auth/callback?code=x", "/auth/callback",
                "/elsewhere?code=x&state=state-else-1")}
        self.assertTrue(said.getvalue() == "", "the listener wrote to stderr")
        self.assertEqual(other, (409, other[1]))        # not the pending login's state: never handed on
        self.assertIn("belongs to another login", other[1])
        self.assertEqual(first[0], 200)
        self.assertIn("close this tab", first[1])
        self.assertEqual(again[0], 409)                 # the core's once rule, not CLIProxyAPI's
        self.assertIn("already has this login&#x27;s answer", again[1])
        self.assertEqual(denied[0], 200)
        self.assertEqual(refused, dict.fromkeys(refused, 404))
        # Exactly {code, state, error}, "" for the field the browser did not bring, and no key.
        self.assertTrue(self.fake.callbacks == [(None, {"code": CODE, "state": "state-code-1abc", "error": ""}),
                                                (None, {"code": "", "state": "state-deny-1abc", "error": "access_denied"})],
                        "the callbacks handed on are not the two the pending logins brought, keyless")
        self.assertFalse(any(CODE in page for _, page in (other, first, again, denied)), "a page shows the code")
        oauth.close_pending()
        self.assertTrue(port_is_free(port))
        # CLIProxyAPI waits five minutes for the callback; past its wait the port goes back to `codex login`.
        with mock.patch.object(oauth, "WAIT_SECONDS", 0.2):
            self.listener()
        wait_for(lambda: port_is_free(port))
        self.assertTrue(port_is_free(port), "the listener outlived its wait")

    def test_the_listener_reads_one_value_per_name_and_refuses_both_code_and_error(self):
        port = self.listener("state-one-1abc").address[1]
        q = "state=state-one-1abc"
        refused = (f"{q}&{q}", f"{q}&state=", f"{q}&code=x&code=y", f"{q}&code=x&error=e",
                   f"{q}&code=x&error=", f"{q}&code=&error=e", f"{q}&code=", f"{q}&error=", q,
                   "code=x", "state=&code=x", f"{q}&code=x&scope=a&scope=b")
        for query in refused:
            self.assertEqual(browse(port, f"/auth/callback?{query}"), (404, browse(port, "/elsewhere")[1]), query)
        self.assertEqual(self.fake.callbacks, [])
        # Another name is ignored and never handed on.
        self.assertEqual(browse(port, f"/auth/callback?{q}&code=x&scope=openid")[0], 200)
        self.assertEqual(self.fake.callbacks, [(None, {"code": "x", "state": "state-one-1abc", "error": ""})])

    def test_the_listener_binds_loopback_and_refuses_a_busy_port(self):
        self.assertEqual(self.listener().address[0], "127.0.0.1")
        held = socket.socket()
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        with self.assertRaises(oauth.CallbackPortBusy):
            oauth.Callback(port=held.getsockname()[1]).start(self.fake.url)
        self.assertEqual(self.fake.callbacks, [])

    @unittest.skipUnless(os.name == "nt", "SO_EXCLUSIVEADDRUSE is Windows' own")
    def test_on_windows_no_other_socket_binds_the_waiting_address(self):
        port = self.listener().address[1]
        for option in (None, socket.SO_REUSEADDR, socket.SO_EXCLUSIVEADDRUSE):
            with socket.socket() as s:
                if option is not None:
                    s.setsockopt(socket.SOL_SOCKET, option, 1)
                with self.assertRaises(OSError, msg=f"option {option} bound the waiting address"):
                    s.bind(("127.0.0.1", port))

    @unittest.skipUnless(os.name == "nt", "SO_EXCLUSIVEADDRUSE is Windows' own")
    def test_on_windows_the_next_login_binds_the_port_at_once_after_a_callback(self):
        port = self.listener("state-code-1abc").address[1]
        self.assertEqual(browse(port, f"/auth/callback?code={CODE}&state=state-code-1abc")[0], 200)
        oauth.close_pending()
        self.assertEqual(self.listener("state-next-1abc").address[1], port)    # CallbackPortBusy if still held


if __name__ == "__main__":
    unittest.main()
