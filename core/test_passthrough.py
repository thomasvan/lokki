#!/usr/bin/env python3
"""Pass-through: the client's bytes reach CLIProxyAPI with the core's credential,
the answer comes back byte for byte, one record says what went where."""
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import config, core, engine, handler, passthrough, routing  # noqa: E402
from test_offline import TOKEN, _ServingHarness  # noqa: E402

FRAMES = (b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi","output_index":0,"item_id":"i0"}\n\n'
          b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}\n\n')


def read_all(port, data, timeout=10):
    """Send bytes, read until the server closes. `test_offline.raw_request` reads
    one `recv`, which is not the whole of a stream."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(data)
        return _read_to_close(s)
    finally:
        s.close()


def _read_to_close(s):
    out = b""
    while True:
        try:
            chunk = s.recv(65536)
        except ConnectionResetError:        # a refusal that left the body unread (Windows resets)
            return out
        if not chunk:
            return out
        out += chunk


def _chunk(data):
    return b"%x\r\n%s\r\n" % (len(data), data)


class StreamingUpstream:
    """Answers POST /v1/responses with FRAMES (or a scripted status), records the request."""

    def __init__(self):
        self.seen = []
        # per request: ("status", code) | ("frames", bytes) | ("break_after", n_bytes) | ("json", obj)
        #            | ("slow", [bytes, ...], delay_s): each piece its own chunk, `delay_s` apart
        #            | ("gated", [bytes, ...], event): the first piece at once, the rest once `event` is set
        self.script = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                outer.seen.append({"path": self.path, "raw": raw, "headers": {k.lower(): v for k, v in self.headers.items()}})
                action = outer.script.pop(0) if outer.script else ("frames", FRAMES)
                if action[0] == "status":
                    body = json.dumps({"error": {"message": "nope", "type": "server_error"}}).encode()
                    self.send_response(action[1])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Retry-After", "1")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if action[0] == "json":
                    # CLIProxyAPI answers `stream: false` with one JSON object, chunked
                    # and without a Content-Length; two chunks so the de-chunking is real.
                    body = json.dumps(action[1]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    half = len(body) // 2
                    self.wfile.write(_chunk(body[:half]) + _chunk(body[half:]) + b"0\r\n\r\n")
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("x-request-id", "req-xyz")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.flush()
                try:
                    if action[0] == "slow":
                        for piece in action[1]:
                            self.wfile.write(_chunk(piece))
                            self.wfile.flush()
                            time.sleep(action[2])
                        self.wfile.write(b"0\r\n\r\n")
                    elif action[0] == "gated":
                        first, *rest = action[1]
                        self.wfile.write(_chunk(first))
                        self.wfile.flush()
                        action[2].wait(30)
                        for piece in rest:
                            self.wfile.write(_chunk(piece))
                        self.wfile.write(b"0\r\n\r\n")
                    else:
                        data = FRAMES[:action[1]] if action[0] == "break_after" else action[1]
                        self.wfile.write(_chunk(data))
                        if action[0] != "break_after":
                            self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass                                   # the core stopped reading: the client left
                self.close_connection = True

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _PassThroughHarness(_ServingHarness):
    def setUp(self):
        super().setUp()
        self.up = StreamingUpstream()
        self.addCleanup(self.up.close)
        tmp = tempfile.mkdtemp(prefix="lokki-passthrough-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.saved = (config.UPSTREAM, core.RUNS_LOG, config.PACING, config.RETRIES, config.ROUTING, config.API_KEY)
        config.UPSTREAM = f"http://127.0.0.1:{self.up.port}"
        core.RUNS_LOG = os.path.join(tmp, "runs.jsonl")
        config.PACING = 0
        config.RETRIES = 3
        config.ROUTING = "direct"
        config.API_KEY = "core-upstream-key"
        engine.set_retry_hook(lambda *a: None)         # no backoff sleep in the suite
        routing.reset()
        self.addCleanup(self._restore)
        # The first request logs the routing decision; a test whose decision differs, or that
        # makes no request, says so in `routing_lines`.
        self.routing_lines = ["[routing] relay off: routing: direct"]
        self.err = io.StringIO()
        patch = mock.patch("sys.stderr", self.err)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._the_routing_lines)

    def _the_routing_lines(self):
        logged = [l for l in self.err.getvalue().splitlines() if l.startswith("[routing]")]
        self.assertEqual(logged, self.routing_lines)

    def _restore(self):
        (config.UPSTREAM, core.RUNS_LOG, config.PACING, config.RETRIES, config.ROUTING, config.API_KEY) = self.saved
        engine.set_retry_hook(None)
        routing.reset()

    def post(self, body, headers=None, token=TOKEN):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        head = (f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Relay-Token: {token}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n")
        for k, v in (headers or {}).items():
            head += f"{k}: {v}\r\n"
        return read_all(self.port, head.encode() + b"\r\n" + data)

    def last_record(self):
        with open(core.RUNS_LOG, encoding="utf-8") as f:
            return json.loads(f.read().splitlines()[-1])


class PassThroughTests(_PassThroughHarness):
    def test_raw_bytes_reach_cliproxy_with_the_cores_key_and_come_back_byte_for_byte(self):
        raw = b'{"model":"gpt-5.5","input":"hi","temperature":1.0,"store":true,"previous_response_id":"r0"}'
        resp = self.post(raw, headers={"session_id": "s1", "Accept-Encoding": "gzip", "originator": "codex",
                                       "Authorization": "Bearer client-secret"})
        self.assertEqual(self.up.seen[0]["raw"], raw)                      # untouched, store and previous_response_id included
        h = self.up.seen[0]["headers"]
        self.assertEqual(h["authorization"], "Bearer core-upstream-key")   # never the client's
        self.assertNotIn("client-secret", json.dumps(h))
        self.assertEqual(h["session_id"], "s1")
        self.assertEqual(h["originator"], "codex")
        self.assertNotEqual(h.get("accept-encoding"), "gzip")             # http.client sends identity, not the client's
        self.assertNotIn("x-relay-token", h)
        head, _, body = resp.partition(b"\r\n\r\n")
        self.assertIn(b"200", head.split(b"\r\n")[0])
        self.assertIn(b"content-type: text/event-stream", head.lower())
        self.assertIn(b"x-request-id: req-xyz", head.lower())
        self.assertNotIn(b"transfer-encoding", head.lower())
        self.assertNotIn(b"content-length", head.lower())
        self.assertEqual(body, FRAMES)
        rec = self.last_record()
        self.assertEqual(rec["route"], "passthrough")
        self.assertEqual(rec["reason"], "routing: direct")
        self.assertEqual(rec["status"], 200)
        self.assertEqual(rec["cut"], "terminal")
        self.assertEqual(rec["usage"]["total_tokens"], 4)
        self.assertEqual(rec["upstream_requests"], 1)
        self.assertFalse(rec.get("reserialized"))
        self.assertEqual(rec["client"], "offline-test-client")
        self.assertNotIn("hi", json.dumps(rec))

    def test_a_body_without_stream_goes_up_without_it_and_the_record_says_none(self):
        raw = b'{"model":"gpt-5.5","input":"x"}'
        self.post(raw)
        self.assertEqual(self.up.seen[0]["raw"], raw)
        self.assertIsNone(self.last_record()["stream"])                  # what CLIProxyAPI saw, not a default

    def test_a_body_without_model_goes_up_without_it_and_the_record_says_none(self):
        raw = b'{"input":"x"}'
        self.post(raw)
        self.assertEqual(self.up.seen[0]["raw"], raw)
        self.assertIsNone(self.last_record()["model"])

    def test_a_non_boolean_stream_is_not_recorded(self):
        self.post({"model": "m", "input": "x", "stream": "client text"})
        rec = self.last_record()
        self.assertIsNone(rec["stream"])                                 # a client's string never reaches the record
        self.assertNotIn("client text", json.dumps(rec))

    def test_a_knob_is_removed_and_the_rest_reserialized(self):
        body = {"model": "gpt-5.5", "input": [{"role": "user", "content": "x"}], "mode": "control", "seg_budget": 5, "temperature": 0.5}
        self.post(body)
        sent = json.loads(self.up.seen[0]["raw"])
        self.assertEqual(sent, {"model": "gpt-5.5", "input": [{"role": "user", "content": "x"}], "temperature": 0.5})
        self.assertTrue(self.last_record()["reserialized"])

    def test_the_knob_set_is_what_resolve_knobs_reads(self):
        self.routing_lines = []                                          # no request, no decision
        self.assertEqual(set(handler.resolve_knobs({}, True)), set(handler.RELAY_KNOB_PARAMS))

    def recv_until(self, s, marker):
        """Read from `s` until `marker` is seen; a closed connection is a failure, not a spin."""
        got = b""
        while marker not in got:
            chunk = s.recv(65536)
            self.assertTrue(chunk, f"the connection closed before {marker!r}: {got[:200]!r}")
            got += chunk
        return got

    def wait_for_record(self):
        """The handler's `finally` writes the record after the client has gone; wait for
        it so it lands in this test's log, not in the one the cleanup restores."""
        for _ in range(200):
            try:
                if self.last_record()["route"] == "passthrough":
                    return
            except (OSError, IndexError, ValueError):
                pass
            time.sleep(0.05)
        self.fail("no pass-through record was written")

    def test_the_first_frame_reaches_the_client_before_the_last(self):
        # The upstream holds the last frame until the client has the first, so a
        # core that waits for the end before writing never gets there: the read times out.
        first, second = FRAMES.split(b"\n\n", 1)
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.up.script = [("gated", [first + b"\n\n", second], gate)]
        data = json.dumps({"model": "m", "input": "x"}).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(s.close)
        s.sendall((f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Relay-Token: {TOKEN}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n").encode() + data)
        got = self.recv_until(s, b"output_text.delta")
        self.assertNotIn(b"response.completed", got)
        gate.set()
        got += _read_to_close(s)
        self.assertTrue(got.endswith(FRAMES))
        self.assertEqual(self.last_record()["cut"], "terminal")

    def test_the_guards_that_run_on_both_paths(self):
        self.assertIn(b"401", self.post({"model": "m", "input": "x"}, token="nope").split(b"\r\n")[0])
        self.assertIn(b"400", self.post(b"{not json").split(b"\r\n")[0])
        self.assertIn(b"400", self.post({"model": "bad model!", "input": "x"}).split(b"\r\n")[0])
        handler.STOPPING.set()
        self.addCleanup(handler.STOPPING.clear)
        self.assertIn(b"503", self.post({"model": "m", "input": "x"}).split(b"\r\n")[0])
        self.assertEqual(self.up.seen, [])

    def test_retry_before_the_first_byte(self):
        self.up.script = [("status", 503), ("frames", FRAMES)]
        resp = self.post({"model": "m", "input": "x"})
        self.assertIn(b"200", resp.split(b"\r\n")[0])
        self.assertEqual(len(self.up.seen), 2)
        rec = self.last_record()
        self.assertEqual((rec["upstream_requests"], rec["last_status"], rec["cut"]), (2, 503, "terminal"))

    def test_a_stream_broken_after_the_first_byte_is_not_retried(self):
        # A retry would be asked for before the core closes the client's connection,
        # so once the response has ended the upstream has seen every request it will see.
        self.up.script = [("break_after", 40), ("frames", FRAMES)]     # a second answer is ready; it must not be asked for
        resp = self.post({"model": "m", "input": "x"})
        self.assertEqual(len(self.up.seen), 1, "a broken stream was retried after the first byte")
        self.assertIn(b"200", resp.split(b"\r\n")[0])                   # the status was already sent
        rec = self.last_record()
        self.assertEqual((rec["route"], rec["cut"], rec["upstream_requests"]), ("passthrough", "error", 1))
        self.assertNotIn("last_status", rec)
        self.assertIsNone(rec["usage"])

    def test_a_4xx_is_passed_through_as_it_is_and_recorded_as_an_error(self):
        self.up.script = [("status", 400)]
        resp = self.post({"model": "m", "input": "x"})
        self.assertIn(b"400", resp.split(b"\r\n")[0])
        self.assertIn(b'"nope"', resp)
        self.assertIn(b"retry-after: 1", resp.partition(b"\r\n\r\n")[0].lower())
        rec = self.last_record()
        self.assertEqual((rec["status"], rec["cut"]), (400, "error"))
        self.assertEqual(rec["upstream_requests"], 1)

    def test_a_refusal_of_the_relays_credential_is_a_502(self):
        for status in (401, 403, 407):
            self.up.script = [("status", status)]
            resp = self.post({"model": "m", "input": "x"})
            self.assertIn(b"502", resp.split(b"\r\n")[0], status)
            self.assertIn(b"refused the relay's credential", resp)
            rec = self.last_record()
            self.assertEqual((rec["status"], rec["cut"]), (status, "error"))

    def test_non_streaming_passes_through_as_one_json_body(self):
        answer = {"id": "resp_2", "object": "response", "usage": {"total_tokens": 7}}
        self.up.script = [("json", answer)]
        resp = self.post({"model": "m", "input": "x", "stream": False})
        head, _, body = resp.partition(b"\r\n\r\n")
        self.assertIn(b"content-length: %d" % len(body), head.lower())
        self.assertNotIn(b"transfer-encoding", head.lower())
        self.assertEqual(body, json.dumps(answer).encode())                 # de-chunked, byte for byte
        rec = self.last_record()
        self.assertEqual((rec["cut"], rec["usage"]["total_tokens"]), ("terminal", 7))

    def test_a_client_that_hangs_up_is_recorded_as_client_gone(self):
        first = FRAMES.split(b"\n\n", 1)[0] + b"\n\n"
        self.up.script = [("slow", [first] * 30, 0.1)]            # 3 s of frames; the client leaves after the first
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        data = json.dumps({"model": "m", "input": "x"}).encode()
        s.sendall((f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Relay-Token: {TOKEN}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n").encode() + data)
        self.recv_until(s, b"output_text.delta")
        s.close()
        self.wait_for_record()
        rec = self.last_record()
        self.assertEqual((rec["route"], rec["cut"]), ("passthrough", "client_gone"))
        self.assertIsNone(rec["usage"])

    def test_the_slot_is_taken_and_released(self):
        saved = config.MAX_CONCURRENT
        config.MAX_CONCURRENT = 1
        self.addCleanup(setattr, config, "MAX_CONCURRENT", saved)
        real, held = engine.upstream_slot, []

        @contextlib.contextmanager
        def watched():
            with real():
                held.append(len(self.up.seen))         # upstream requests made when the slot was taken
                yield
                held.append(len(self.up.seen))         # and when it was given back
        with mock.patch.object(engine, "upstream_slot", watched):
            self.post({"model": "m", "input": "x"})
        self.assertEqual(self.last_record()["route"], "passthrough")
        self.assertEqual(held, [0, 1], "the upstream request was not made inside the slot")
        self.assertTrue(engine._slots.acquire(timeout=4), "the slot was not released")
        engine._slots.release()

    def test_cliproxy_down_is_a_502_a_record_and_a_warning_line(self):
        config.UPSTREAM = "http://127.0.0.1:9"
        config.RETRIES = 1
        resp = self.post({"model": "m", "input": "x"})
        self.assertIn(b"502", resp.split(b"\r\n")[0])
        rec = self.last_record()
        self.assertEqual((rec["route"], rec["cut"]), ("passthrough", "error"))
        self.assertEqual(rec["upstream_requests"], 1)                    # open_upstream's `attempts`: every one failed
        self.assertIn("[passthrough] CLIProxyAPI did not answer", self.err.getvalue())
        self.assertNotIn("core-upstream-key", self.err.getvalue())

    def test_the_relay_path_is_taken_when_the_decision_says_so(self):
        config.ROUTING = "relay"
        self.routing_lines = ["[routing] relay on: routing: relay"]
        self.post({"model": "m", "input": "x"})
        self.assertNotEqual(self.up.seen[0]["raw"], b'{"model": "m", "input": "x"}')   # the engine built its own body
        self.assertEqual(self.last_record()["route"], "relay")

    def test_a_model_not_named_passes_through_when_the_quota_cuts(self):
        from lokki_core.quota import poller
        saved = (config.LOW_QUOTA, poller.POLLER._latest)
        self.addCleanup(self._restore_quota, saved)
        config.ROUTING = "auto"
        config.LOW_QUOTA = {"threshold_percent": 30, "rule": "all", "accounts": [], "poll_seconds": 60,
                            "models": ["m1"]}
        with poller.POLLER._cond:
            poller.POLLER._latest = {"read_at": time.time(), "accounts": {"a1": {
                "label": "x", "email_masked": "x***", "enabled": True, "used_percent": 95,
                "reset_at": time.time() + 3600, "window_seconds": 18000, "error": None}}}
        self.routing_lines = ["[routing] relay on: every watched account is low"]    # one line for both models
        self.post({"model": "m1", "input": "x"})
        self.assertEqual(self.last_record()["route"], "relay")
        self.post({"model": "m2", "input": "x"})
        rec = self.last_record()
        self.assertEqual((rec["route"], rec["reason"]), ("passthrough", "this model is not cut when low"))
        self.post({"input": "x"})                                        # no model: not named
        rec = self.last_record()
        self.assertEqual((rec["route"], rec["reason"]), ("passthrough", "this model is not cut when low"))
        self.assertEqual(routing.last().reason, "every watched account is low")

    def test_relay_cuts_a_model_the_leftover_models_list_does_not_name(self):
        from lokki_core.quota import poller
        saved = (config.LOW_QUOTA, poller.POLLER._latest)
        self.addCleanup(self._restore_quota, saved)
        config.ROUTING = "relay"                                         # models left in the file from an earlier Auto
        config.LOW_QUOTA = dict(config.LOW_QUOTA, models=["m1"])
        self.routing_lines = ["[routing] relay on: routing: relay"]
        self.post({"model": "m2", "input": "x"})
        self.assertEqual(self.last_record()["route"], "relay")

    @staticmethod
    def _restore_quota(saved):
        from lokki_core.quota import poller
        config.LOW_QUOTA = saved[0]
        with poller.POLLER._cond:
            poller.POLLER._latest = saved[1]

    def test_api_run_always_runs_the_engine(self):
        self.routing_lines = []                                          # /api/run takes no routing decision
        data = json.dumps({"prompt": "hello"}).encode()
        head = (f"POST /api/run HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Relay-Token: {TOKEN}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n").encode()
        read_all(self.port, head + data)
        self.assertEqual(self.last_record()["route"], "relay")


    def test_an_unexpected_failure_mid_answer_is_recorded_as_an_error(self):
        with mock.patch.object(passthrough, "_write", side_effect=RuntimeError("boom")):
            self.post({"model": "m", "input": "x"})
        rec = self.last_record()
        self.assertEqual((rec["route"], rec["cut"]), ("passthrough", "error"))
        self.assertIn("unhandled POST", self.err.getvalue())                  # the handler's catch-all answered


class ScannerTests(unittest.TestCase):
    """`_Scanner` reads the stream on its way to the client for the terminal frame
    and its usage; what it misses only costs the record its usage."""

    def scan(self, *chunks, scanner=None):
        s = scanner or passthrough._Scanner()
        for c in chunks:
            s.feed(c)
        return s

    def test_a_terminal_frame_split_across_two_reads(self):
        cut = FRAMES.index(b"response.completed\ndata") + 20
        s = self.scan(FRAMES[:cut], FRAMES[cut:])
        self.assertEqual(s.terminal, "response.completed")
        self.assertEqual(s.usage["total_tokens"], 4)

    def test_crlf_line_ends(self):
        s = self.scan(FRAMES.replace(b"\n", b"\r\n"))
        self.assertEqual((s.terminal, s.usage["total_tokens"]), ("response.completed", 4))

    def test_a_data_payload_over_two_lines(self):
        s = self.scan(b'event: response.completed\ndata: {"type":"response.completed",\n'
                      b'data: "response":{"usage":{"total_tokens":9}}}\n\n')
        self.assertEqual((s.terminal, s.usage["total_tokens"]), ("response.completed", 9))

    def test_a_line_over_the_cap_is_not_scanned(self):
        class Small(passthrough._Scanner):
            MAX_LINE = 4096                       # the real cap is 16 MiB; the rule is the same
        line = (b'data: {"type":"response.completed","response":{"usage":{"total_tokens":1},"pad":"'
                + b"x" * 8192 + b'"}}\n\n')
        s = self.scan(*[line[i:i + 1000] for i in range(0, len(line), 1000)], scanner=Small())
        self.assertIsNone(s.terminal)
        self.assertLessEqual(len(s.buf), Small.MAX_LINE)
        s.feed(FRAMES)                            # the stream after it is read again
        self.assertEqual(s.terminal, "response.completed")

    def test_a_terminal_response_that_is_not_an_object(self):
        s = self.scan(b'data: {"type":"response.completed","response":"text"}\n\n')
        self.assertEqual(s.terminal, "response.completed")
        self.assertIsNone(s.usage)


class RoutingLogTests(_PassThroughHarness):
    def test_a_decision_change_is_one_stderr_line_and_healthz_shows_it(self):
        self.routing_lines = []                                          # `err` holds them
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            routing.decide_now()
            routing.decide_now()
            config.ROUTING = "relay"
            routing.decide_now()
        lines = [l for l in err.getvalue().splitlines() if l.startswith("[routing]")]
        self.assertEqual(len(lines), 2)
        self.assertIn("routing: direct", lines[0])
        self.assertIn("routing: relay", lines[1])
        status, body = read_all(self.port, b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").partition(b"\r\n\r\n")[::2]
        self.assertEqual(json.loads(body)["routing"], {"relay_on": True, "reason": "routing: relay"})


if __name__ == "__main__":
    unittest.main()
