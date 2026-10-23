#!/usr/bin/env python3
"""Offline tests for the process lifecycle: the SIGTERM drain and the
SIGHUP reload.

The drain is the part whose failure is *destructive*: a restart
that cuts live streams turns a routine deploy into the exact silent truncation
this relay exists to remove. So the important test here starts a real relay, in a
real subprocess, mid-stream, and sends it a real SIGTERM.

No upstream, no quota: the upstream is a stub that streams slowly on purpose.
"""

import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import config, handler, routing, settings_api  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
TOKEN = "lifecycle-test-token"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for_healthz(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz",
                                        timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - the child may not be listening yet
            time.sleep(0.1)
    return False


class SlowUpstream:
    """An upstream that answers one request slowly: a delta, a pause, a terminal.

    The pause is what makes the drain observable. It is deliberately inside the
    stream, after the relay has already forwarded bytes, which is the window an
    undrained SIGTERM cuts."""

    def __init__(self, pause=1.5):
        self.pause = pause
        self.seen = []
        self.closing = threading.Event()
        self.started = threading.Event()
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                outer.seen.append(self.path)
                outer.started.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    self.wfile.write(
                        ('event: response.output_text.delta\n'
                         'data: {"type":"response.output_text.delta","delta":"first ",'
                         '"output_index":0,"item_id":"i0"}\n\n').encode())
                    self.wfile.flush()
                    outer.closing.wait(outer.pause)
                    self.wfile.write(
                        ('event: response.output_text.delta\n'
                         'data: {"type":"response.output_text.delta","delta":"second",'
                         '"output_index":0,"item_id":"i0"}\n\n').encode())
                    self.wfile.write(
                        ('event: response.completed\n'
                         'data: {"type":"response.completed","response":'
                         '{"id":"resp_stub","usage":{"input_tokens":1,'
                         '"output_tokens":2,"total_tokens":3}}}\n\n').encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass          # the relay under test was stopped mid-stream
                self.close_connection = True

            def log_message(self, *a):
                pass

        class _Server(ThreadingHTTPServer):
            daemon_threads = False        # server_close() joins the handler threads

        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        """Wakes a handler held in its pause, then waits for every handler thread, so
        none outlives the test that started it."""
        self.closing.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


class RelayProcess:
    """A real relay in a real subprocess, with its own config and runs file."""

    def __init__(self, test, upstream, drain_seconds=3, pipes=False):
        """`pipes`: the relay writes its output to pipes this process reads, as the shell
        runs the core, and not to a log file; `output()` then has nothing to show."""
        self.test = test
        self.drain_seconds = drain_seconds
        self.port = free_port()
        self.dir = tempfile.mkdtemp(prefix="lifecycle-test-")
        self.config_path = os.path.join(self.dir, "config.yaml")
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(f"listen: '127.0.0.1:{self.port}'\n"
                    f"auth:\n  token: {TOKEN}\n"
                    f"upstream: 'http://127.0.0.1:{upstream.port}'\n"
                    "pacing_seconds: 0\nmax_concurrent_upstream: 1\nretries: 1\n"
                    f"drain_seconds: {drain_seconds}\n")
        self.log = open(os.path.join(self.dir, "relay.log"), "w+")
        self.runs = os.path.join(self.dir, "runs.jsonl")
        self.proc = subprocess.Popen(
            [sys.executable, SERVER, "--config", self.config_path],
            stdout=subprocess.PIPE if pipes else self.log,
            stderr=subprocess.PIPE if pipes else subprocess.STDOUT, cwd=HERE,
            # RELAY_RUNS_LOG keeps these synthetic runs out of the evidence file
            # that the repository commits.
            env=dict(os.environ, RELAY_CONFIG=self.config_path,
                     RELAY_RUNS_LOG=self.runs, PYTHONUNBUFFERED="1"))
        # Registered before `stop`, so it runs after it (cleanups run last in,
        # first out): the directory holds the relay's config, with the test token,
        # and its log, and is removed once the process is gone and the log closed.
        test.addCleanup(shutil.rmtree, self.dir, True)
        test.addCleanup(self.stop)

    def stop(self):
        if self.log.closed:      # stop() is registered as a cleanup as well
            return
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        for pipe in (self.proc.stdout, self.proc.stderr):
            if pipe:
                pipe.close()
        self.log.flush()
        self.log.close()

    def output(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read()

    def wait_ready(self):
        if not wait_for_healthz(self.port):
            self.test.fail(f"the relay never answered /healthz (exit status {self.proc.poll()}):\n"
                           + self.output())

    def term(self):
        self.proc.send_signal(signal.SIGTERM)

    def wait_exit(self, timeout=15):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.test.fail(f"the relay did not exit within {timeout}s after "
                           f"SIGTERM:\n{self.output()}")


class BindTests(unittest.TestCase):
    def test_binding_does_not_resolve_the_host_name(self):
        """http.server's server_bind asks `socket.getfqdn` for a name nothing reads, and on
        macOS that reverse lookup stalls startup for many seconds."""
        def refuse(*_a, **_k):
            raise AssertionError("socket.getfqdn was called while binding")
        with mock.patch("socket.getfqdn", refuse):
            server = handler._ReusableServer(("127.0.0.1", 0), handler.Handler)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_port, server.server_address[1])


class DrainUnitTests(unittest.TestCase):
    """The pieces the subprocess test depends on, asserted directly."""

    def setUp(self):
        self._saved = handler.STOPPING.is_set()
        handler.STOPPING.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved:
            handler.STOPPING.set()
        else:
            handler.STOPPING.clear()

    def test_an_in_flight_run_is_counted_and_waited_for(self):
        started = threading.Event()
        release = threading.Event()

        def work():
            with handler.active_run():
                started.set()
                release.wait(5)

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(started.wait(5))
        self.assertEqual(handler.active_run_count(), 1)
        self.assertFalse(handler.wait_for_active_runs(0.2),
                         "the drain did not time out on a held run")
        release.set()
        thread.join(5)
        self.assertEqual(handler.active_run_count(), 0)
        self.assertTrue(handler.wait_for_active_runs(1),
                        "the drain did not clear when the run finished")

    def test_a_connection_during_the_drain_is_refused_with_a_retry_hint(self):
        saved = config.TOKENS
        config.TOKENS = (("lifecycle", TOKEN, "operator"),)
        self.addCleanup(setattr, config, "TOKENS", saved)
        servers = handler.start_listeners(("127.0.0.1:0",), strict=False)
        self.addCleanup(lambda: [s.server_close() for _, s in servers])
        port = servers[0][1].server_address[1]
        threading.Thread(target=servers[0][1].serve_forever, daemon=True).start()
        self.addCleanup(servers[0][1].shutdown)
        # The first decision announces itself on stderr: here, whichever test ran first.
        fresh = mock.patch.object(routing, "_last", None)
        said = io.StringIO()
        handler.STOPPING.set()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/run", data=b"{}",
                headers={"X-Relay-Token": TOKEN, "Content-Type": "application/json"},
                method="POST")
            with fresh, mock.patch("sys.stderr", said):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                with caught.exception:
                    self.assertEqual(caught.exception.code, 503)
                    self.assertEqual(caught.exception.headers.get("Retry-After"), "5")
                # ...and /healthz still answers, so the supervisor can tell a
                # draining relay from a wedged one.
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual(json.loads(r.read())["status"], "draining")
            self.assertEqual(said.getvalue(), "[routing] relay on: routing: relay\n")
        finally:
            handler.STOPPING.clear()


@unittest.skipIf(os.name == "nt", "SIGTERM is a hard kill on Windows, so there is no signal drain to test (Task 7 tests the route)")
class SigtermDrainTests(unittest.TestCase):
    def setUp(self):
        self.upstream = SlowUpstream(pause=2.0)
        self.addCleanup(self.upstream.close)
        self.relay = RelayProcess(self, self.upstream, drain_seconds=6)
        self.relay.wait_ready()
        # A fresh read of the config the child actually resolved.
        self.assertEqual(self.relay.proc.poll(), None)

    def test_sigterm_exits_cleanly_when_nothing_is_running(self):
        self.relay.term()
        code = self.relay.wait_exit(20)
        self.assertEqual(code, 0, self.relay.output())
        self.assertIn("stopped", self.relay.output())

    def test_sigterm_does_not_cut_an_in_flight_stream(self):
        """The claim the drain exists for: a restart finishes the stream it is
        streaming. Without the drain the client sees a truncated answer and the
        process dies on the signal."""
        received = []
        failed = []

        def client():
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.relay.port}/v1/responses",
                data=json.dumps({"model": "gpt-6-astra",
                                 "input": "hello"}).encode(),
                headers={"X-Relay-Token": TOKEN,
                         "Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=30) as r:
                    for raw in r:
                        received.append(raw.decode("utf-8", "replace"))
            except Exception as e:  # noqa: BLE001 - reported as the failure
                failed.append(repr(e))

        thread = threading.Thread(target=client)
        thread.start()
        self.assertTrue(self.upstream.started.wait(10),
                        "the relay never reached the stub upstream")
        time.sleep(0.4)                       # the first delta is on the wire
        self.relay.term()
        thread.join(40)

        self.assertEqual(failed, [], "the client's stream was cut by SIGTERM")
        body = "".join(received)
        self.assertIn("response.completed", body,
                      "the drained stream never reached a terminal frame")
        self.assertIn("second", body,
                      "the answer the upstream produced after the signal never "
                      "reached the client")
        self.assertEqual(self.relay.wait_exit(20), 0, self.relay.output())
        self.assertIn("drained", self.relay.output())

    def test_sigterm_is_bounded_when_a_run_never_finishes(self):
        """The drain has a deadline on purpose: an unbounded graceful shutdown
        hangs a deploy on one stuck client."""
        self.upstream.pause = 60              # longer than the drain window
        received = []

        def client():
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.relay.port}/v1/responses",
                data=json.dumps({"model": "gpt-6-astra",
                                 "input": "hello"}).encode(),
                headers={"X-Relay-Token": TOKEN,
                         "Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=40) as r:
                    received.extend(r)
            except Exception:  # noqa: BLE001 - the point is the relay's exit
                pass

        thread = threading.Thread(target=client, daemon=True)
        thread.start()
        self.assertTrue(self.upstream.started.wait(10))
        started = time.time()
        self.relay.term()
        code = self.relay.wait_exit(30)
        elapsed = time.time() - started
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 20, "the drain outlived its own window")
        self.assertIn("drain window", self.relay.output())
        self.assertIn("exiting anyway", self.relay.output())


@unittest.skipIf(os.name == "nt", "SIGHUP does not exist on Windows (Task 7 tests the reload route)")
class ReloadTests(unittest.TestCase):
    """The SIGHUP reload path, exercised against a real process."""

    def setUp(self):
        self.upstream = SlowUpstream(pause=0)
        self.addCleanup(self.upstream.close)
        self.relay = RelayProcess(self, self.upstream)
        self.relay.wait_ready()

    def _version(self, token):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.relay.port}/version",
            headers={"X-Relay-Token": token})
        with urllib.request.urlopen(request, timeout=5) as r:
            return json.loads(r.read())

    def test_sighup_adds_a_client_without_dropping_the_process(self):
        self.assertEqual(self._version(TOKEN)["clients"], ["default"])
        with open(self.relay.config_path, encoding="utf-8") as f:
            body = f.read()
        with open(self.relay.config_path, "w", encoding="utf-8") as f:
            f.write(body.replace(
                f"auth:\n  token: {TOKEN}\n",
                "auth:\n  tokens:\n"
                f"    - {{name: first, token: {TOKEN}, operator: true}}\n"
                "    - {name: second, token: second-token}\n"))
        self.relay.proc.send_signal(signal.SIGHUP)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if self._version(TOKEN)["clients"] == ["first", "second"]:
                    break
            except urllib.error.HTTPError as e:
                e.close()
            time.sleep(0.1)
        self.assertEqual(self._version(TOKEN)["clients"], ["first", "second"],
                         "SIGHUP did not take effect:\n" + self.relay.output())
        # `/version` is the operator's; a client token that is live gets 403
        # (known, not an operator) where an unknown one gets 401.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._version("second-token")
        with caught.exception:
            self.assertEqual(caught.exception.code, 403,
                             "the reloaded client token was not recognised")
        self.assertIsNone(self.relay.proc.poll(), "the reload dropped the process")
        self.assertIn("reloaded: 2 client token(s): first, second",
                      self.relay.output())

    def test_a_broken_reload_keeps_serving_the_previous_tokens(self):
        with open(self.relay.config_path, "w", encoding="utf-8") as f:
            f.write("listen: '127.0.0.1:1'\nauth:\n  tokens: {not: a list}\n")
        self.relay.proc.send_signal(signal.SIGHUP)
        time.sleep(1)
        self.assertIsNone(self.relay.proc.poll())
        self.assertIn("reload refused", self.relay.output())
        self.assertEqual(self._version(TOKEN)["clients"], ["default"],
                         "a broken reload changed the live token list")


class RunLogTargetTests(unittest.TestCase):
    """The run log's target, against a real process, because that is where a
    default inside the repository does its damage: the relay appends live evidence
    into the working tree, so the checkout is permanently dirty and any git
    operation races the running relay."""

    def setUp(self):
        self.upstream = SlowUpstream(pause=0)
        self.addCleanup(self.upstream.close)
        self.here = os.path.dirname(os.path.abspath(__file__))
        self.seed = os.path.join(self.here, "runs.jsonl")

    def test_the_banner_names_the_run_log_and_the_record_lands_there(self):
        """Both halves of the claim: an operator can see which file is in force
        without reading a doc, and the committed seed is not what gets appended."""
        relay = RelayProcess(self, self.upstream)
        relay.wait_ready()
        self.assertIn(f"runs={os.path.realpath(relay.runs)}", relay.output(),
                      "the startup banner does not name the run log:\n"
                      + relay.output())
        before = os.stat(self.seed)
        request = urllib.request.Request(
            f"http://127.0.0.1:{relay.port}/v1/responses",
            data=json.dumps({"model": "gpt-6-astra", "input": "hello"}).encode(),
            headers={"X-Relay-Token": TOKEN, "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(request, timeout=30) as r:
            r.read()
        relay.stop()
        self.assertTrue(os.path.getsize(relay.runs),
                        "the relay wrote no run record to the path it named")
        after = os.stat(self.seed)
        self.assertEqual((before.st_size, before.st_mtime_ns),
                         (after.st_size, after.st_mtime_ns),
                         "a served run appended to the committed seed")

    def test_a_config_pointing_the_evidence_into_the_checkout_is_refused(self):
        d = tempfile.mkdtemp(prefix="in-repo-runs-")
        self.addCleanup(shutil.rmtree, d, True)
        cfg = os.path.join(d, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(f"listen: '127.0.0.1:0'\nauth:\n  token: {TOKEN}\n"
                    f"upstream: 'http://127.0.0.1:1'\nruns_log: {self.seed}\n")
        env = dict(os.environ)
        env.pop("RELAY_RUNS_LOG", None)
        proc = subprocess.run([sys.executable, SERVER, "--config", cfg],
                              cwd=HERE, env=env, capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("inside the repository", proc.stderr)

    def test_the_environment_source_is_refused_the_same_clean_way(self):
        """The environment source is refused the same clean way as the config-file
        one. A refusal at *import* would give the operator a traceback where the
        message belongs, so the assertion is on the absence of a traceback as much
        as on the exit code."""
        d = tempfile.mkdtemp(prefix="in-repo-runs-env-")
        self.addCleanup(shutil.rmtree, d, True)
        cfg = os.path.join(d, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(f"listen: '127.0.0.1:0'\nauth:\n  token: {TOKEN}\n"
                    "upstream: 'http://127.0.0.1:1'\n")
        env = dict(os.environ, RELAY_RUNS_LOG=self.seed)
        proc = subprocess.run([sys.executable, SERVER, "--config", cfg],
                              cwd=HERE, env=env, capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("refusing to start", proc.stderr)
        self.assertIn("inside the repository", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr,
                         "a refusal printed a traceback instead of a sentence")
        # And the module still imports: a bad path must not break `import lokki_core`.
        probe = subprocess.run(
            [sys.executable, "-c",
             "import lokki_core.config as c\n"
             "try:\n"
             "    c.ensure_settings()\n"
             "    print('NOT-RAISED')\n"
             "except c.ConfigError as e:\n"
             "    print('raised', 'inside the repository' in str(e))\n"],
            cwd=HERE, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("raised True", probe.stdout)
        self.assertNotIn("Traceback", probe.stderr)


def _call(port, path, token, method="GET", body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Relay-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, None


class RouteLifecycleTests(unittest.TestCase):
    """The same claims the signal tests make, through the routes, on every OS."""

    def setUp(self):
        self.upstream = SlowUpstream(pause=1.5)
        self.addCleanup(self.upstream.close)
        self.relay = RelayProcess(self, self.upstream)
        self.relay.wait_ready()

    def _two_tokens(self):
        with open(self.relay.config_path, encoding="utf-8") as f:
            body = f.read()
        with open(self.relay.config_path, "w", encoding="utf-8") as f:
            f.write(body.replace(f"auth:\n  token: {TOKEN}\n",
                                 f"auth:\n  tokens:\n    - {{name: first, token: {TOKEN}, operator: true}}\n"
                                 "    - {name: second, token: second-token}\n"))

    def test_post_reload_adds_a_client_without_dropping_the_process(self):
        self.assertEqual(_call(self.relay.port, "/version", TOKEN)[1]["clients"], ["default"])
        self._two_tokens()
        status, body = _call(self.relay.port, "/api/reload", TOKEN, "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"], body)
        self.assertEqual(_call(self.relay.port, "/version", TOKEN)[1]["clients"], ["first", "second"])
        self.assertEqual(_call(self.relay.port, "/version", "second-token")[0], 403)   # live, a client
        self.assertIsNone(self.relay.proc.poll(), "the reload dropped the process")
        self.assertIn("reloaded: 2 client token(s): first, second", self.relay.output())

    def test_post_reload_reports_a_broken_file_and_keeps_serving(self):
        with open(self.relay.config_path, "w", encoding="utf-8") as f:
            f.write("listen: '127.0.0.1:1'\nauth:\n  tokens: {not: a list}\n")
        status, body = _call(self.relay.port, "/api/reload", TOKEN, "POST", {})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("reload refused", body["error"])
        self.assertEqual(_call(self.relay.port, "/version", TOKEN)[1]["clients"], ["default"])

    def test_post_shutdown_drains_an_in_flight_stream_then_exits(self):
        got = {}

        def stream():
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.relay.port}/v1/responses",
                data=json.dumps({"model": "m", "input": "x"}).encode(),
                headers={"X-Relay-Token": TOKEN, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                got["body"] = r.read()

        t = threading.Thread(target=stream)
        t.start()
        self.assertTrue(self.upstream.started.wait(10), "the stream never reached the upstream")
        status, body = _call(self.relay.port, "/api/shutdown", TOKEN, "POST", {})
        self.assertEqual(status, 202)
        self.assertTrue(body["stopping"])
        self.assertEqual(_call(self.relay.port, "/v1/responses", TOKEN, "POST", {"model": "m", "input": "x"})[0], 503)
        t.join(20)
        self.assertIn(b"response.completed", got.get("body", b""), self.relay.output())
        self.assertEqual(self.relay.wait_exit(), 0)
        self.assertIn("drained: no stream was cut", self.relay.output())

    def test_shutdown_and_reload_need_an_operator_token(self):
        self._two_tokens()
        self.assertTrue(_call(self.relay.port, "/api/reload", TOKEN, "POST", {})[1]["ok"])
        for path in ("/api/shutdown", "/api/reload"):
            self.assertEqual(_call(self.relay.port, path, "second-token", "POST", {})[0], 403, path)
            # The role is decided before any body is looked at.
            self.assertEqual(_call(self.relay.port, path, "second-token", "POST", raw=b"{not json")[0], 403, path)
            self.assertEqual(_call(self.relay.port, path, None, "POST", {})[0], 401, path)
        self.assertIsNone(self.relay.proc.poll())
        self.assertEqual(_call(self.relay.port, "/healthz", None)[1]["status"], "ok")


class OutputReaderGoneTests(unittest.TestCase):
    """A shell that dies without stopping its core breaks the pipes it read the core's
    output from. The core must still leave on the stop it is sent, or it holds its port
    with no shell left to stop it."""

    def setUp(self):
        self.upstream = SlowUpstream(pause=0)
        self.addCleanup(self.upstream.close)

    # One test per stop, so that a verbose run names each one's result.
    def test_a_core_whose_output_reader_is_gone_still_stops_on_post_shutdown(self):
        self._stops(lambda relay: _call(relay.port, "/api/shutdown", TOKEN, "POST", {}))

    @unittest.skipIf(os.name == "nt", "SIGTERM is a hard kill on Windows")
    def test_a_core_whose_output_reader_is_gone_still_stops_on_sigterm(self):
        self._stops(RelayProcess.term)

    def _stops(self, stop):
        relay = RelayProcess(self, self.upstream, pipes=True)
        relay.wait_ready()
        relay.proc.stdout.close()
        relay.proc.stderr.close()
        stop(relay)
        try:
            code = relay.proc.wait(timeout=relay.drain_seconds + 5)
        except subprocess.TimeoutExpired:
            self.fail("the core did not exit within its drain window")
        self.assertEqual(code, 0)

    def test_a_line_with_no_reader_is_dropped(self):
        def no_reader():
            r, w = os.pipe()
            os.close(r)
            return open(w, "w", encoding="utf-8", buffering=1)

        def closed():
            stream = open(os.devnull, "w", encoding="utf-8")
            stream.close()
            return stream

        for label, make in [("a pipe whose read end is gone", no_reader), ("a closed stream", closed)]:
            with self.subTest(label):
                stream = make()
                self.addCleanup(_close, stream)
                gone = handler._ReaderMayBeGone(stream)
                self.assertEqual(gone.write("a line\n"), len("a line\n"))
                gone.flush()

    def test_a_drain_that_raises_still_leaves(self):
        """The wrapper keeps a print from raising; anything else that raises on the drain's
        thread must still end the process."""
        exits = []

        def dies(_seconds):
            raise RuntimeError("the drain died")

        with mock.patch.object(handler.os, "_exit", exits.append), \
                mock.patch.object(handler, "wait_for_active_runs", dies), \
                mock.patch.object(handler.time, "sleep", lambda _s: None), \
                mock.patch("builtins.print"):
            with self.assertRaises(RuntimeError):
                handler._drain_and_exit([])
        self.assertEqual(exits, [0])


def _close(stream):
    """Closes `stream`, whose last flush may raise: the file is closed either way."""
    try:
        stream.close()
    except (OSError, ValueError):
        pass


class ReloadThreadTests(unittest.TestCase):
    """The one reload thread, in process: it reloads under the settings write lock,
    and a request answers with a reload that started after it was made."""

    def setUp(self):
        quiet = mock.patch.object(config, "_default_log", lambda _line: None)
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_a_reload_waits_for_the_settings_lock(self):
        calls, out = [], []
        with mock.patch.object(config, "reload_settings", lambda log=None: calls.append(1) or {}):
            with settings_api.LOCK:
                t = threading.Thread(target=lambda: out.append(handler.request_reload(5)))
                t.start()
                time.sleep(0.3)
                self.assertEqual(calls, [], "the reload ran while a settings write held the lock")
            t.join(5)
        self.assertEqual(out, [{"ok": True, "error": None}])

    def test_a_request_made_during_a_reload_waits_for_the_next_one(self):
        entered, release = threading.Event(), threading.Event()
        results = iter([None, {}])

        def reload(log=None):
            result = next(results)
            if result is None:
                entered.set()
                release.wait(5)
                log("reload refused, keeping the running configuration: the first")
            else:
                time.sleep(0.3)        # the first result is the one a waiter would see early
            return result

        first, second = [], []
        with mock.patch.object(config, "reload_settings", reload):
            a = threading.Thread(target=lambda: first.append(handler.request_reload(5)))
            a.start()
            self.assertTrue(entered.wait(5))
            b = threading.Thread(target=lambda: second.append(handler.request_reload(5)))
            b.start()
            time.sleep(0.2)
            release.set()
            a.join(5)
            b.join(5)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [{"ok": True, "error": None}],
                         "a request answered with a reload that read the file before it was made")


if __name__ == "__main__":
    unittest.main(verbosity=2)
