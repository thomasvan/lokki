"""The image routes (lokki_core/images.py), observed at both boundaries: what the upstream received
and what the caller got back, with a fake cliproxy behind a serving relay. Run records go to
a temp file and are read back. No network beyond loopback, no quota."""

import base64
import contextlib
import http.client
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unittest import mock  # noqa: E402

from lokki_core import config, core, engine, handler  # noqa: E402
from test_offline import CLIENT, TOKEN, _ServingHarness, raw_request  # noqa: E402

UPSTREAM_KEY = "upstream-credential-not-the-callers"
PROMPT = "a pelican riding a bicycle, secret-prompt-words"
PIXELS = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"pixel-bytes-" * 200).decode()
ANSWER = json.dumps({
    "created": 1, "data": [{"b64_json": PIXELS, "revised_prompt": "revised " + PROMPT}],
    "usage": {"input_tokens": 12, "output_tokens": 515, "total_tokens": 527,
              "input_tokens_details": {"text_tokens": 12, "image_tokens": 0},
              "note": "free text is not kept"},
    "size": "1024x1024"}).encode()
STREAM = (b"event: image_generation.partial_image\n"
          b"data: " + json.dumps({"type": "image_generation.partial_image",
                                  "b64_json": PIXELS[:80]}).encode() + b"\n\n"
          b"event: image_generation.completed\n"
          b"data: " + json.dumps({"type": "image_generation.completed", "b64_json": PIXELS,
                                  "usage": {"input_tokens": 12, "output_tokens": 515}}).encode()
          + b"\n\n")


class _Fake(unittest.TestCase):
    """A serving relay in front of a fake cliproxy whose behaviour a test sets."""

    def setUp(self):
        self.harness = _ServingHarness("setUp")
        self.harness.setUp()
        self.addCleanup(self.harness._stop)
        self.port = self.harness.port
        self.seen = []                 # (path, headers, body) per request that reached the upstream
        self.respond = lambda h, body: _send(h, 200, "application/json", ANSWER)
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()
        outer = self

        class Cliproxy(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                with outer._lock:
                    outer.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                    outer.in_flight += 1
                    outer.max_in_flight = max(outer.max_in_flight, outer.in_flight)
                try:
                    outer.respond(self, body)
                finally:
                    with outer._lock:
                        outer.in_flight -= 1

            def log_message(self, *a):
                pass

        self.stub = ThreadingHTTPServer(("127.0.0.1", 0), Cliproxy)
        threading.Thread(target=self.stub.serve_forever, daemon=True).start()
        self.addCleanup(self.stub.server_close)
        self.addCleanup(self.stub.shutdown)
        runs = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        runs.close()
        self.addCleanup(os.unlink, runs.name)
        saved = (config.UPSTREAM, config.API_KEY, core.RUNS_LOG, config.PACING, config.MAX_CONCURRENT,
                 config.MAX_IMAGE_BODY_BYTES, config.MAX_IMAGE_RESPONSE_BYTES, config.MAX_CONCURRENT_IMAGES)
        config.UPSTREAM = f"http://127.0.0.1:{self.stub.server_address[1]}"
        config.API_KEY = UPSTREAM_KEY
        core.RUNS_LOG = runs.name
        config.PACING = 0

        def restore():
            (config.UPSTREAM, config.API_KEY, core.RUNS_LOG, config.PACING, config.MAX_CONCURRENT,
             config.MAX_IMAGE_BODY_BYTES, config.MAX_IMAGE_RESPONSE_BYTES, config.MAX_CONCURRENT_IMAGES) = saved
            handler.STOPPING.clear()
        self.addCleanup(restore)

    # -- helpers ---------------------------------------------------------------
    def post(self, path, body, ctype="application/json", token=TOKEN, headers=None, raw_response=False):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs = {"X-Relay-Token": token, "Content-Type": ctype}
        hdrs.update(headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=hdrs,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def records(self):
        with open(core.RUNS_LOG) as f:
            return [json.loads(ln) for ln in f if ln.strip()]


def _send(h, status, ctype, body, extra=None):
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


def multipart(fields, files, boundary="----imgtest"):
    out = b""
    for name, value in fields:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
    for name, filename, data in files:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                'Content-Type: image/png\r\n\r\n').encode() + data + b"\r\n"
    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


class GenerationTests(_Fake):
    def test_an_image_run_is_counted_under_its_route(self):
        before = dict(core.run_counts())
        self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})
        self.assertEqual(core.run_counts()["images"], before["images"] + 1)

    def test_the_request_goes_up_as_it_came_and_the_answer_comes_back_as_it_came(self):
        body = {"model": "gpt-image-2", "prompt": PROMPT, "n": 1, "size": "1024x1024", "quality": "low"}
        status, headers, answer = self.post("/v1/images/generations", body)
        self.assertEqual((status, answer), (200, ANSWER))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Content-Length"], str(len(ANSWER)))
        path, up_headers, up_body = self.seen[0]
        self.assertEqual(path, "/v1/images/generations")
        self.assertEqual(json.loads(up_body), body)
        self.assertEqual(up_headers["content-type"], "application/json")

    def test_the_upstream_gets_the_relays_credential_never_the_callers(self):
        self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"},
                  headers={"Authorization": "Bearer " + TOKEN})
        auth = self.seen[0][1]["authorization"]
        self.assertEqual(auth, "Bearer " + UPSTREAM_KEY)
        self.assertNotIn(TOKEN, json.dumps(self.seen[0][1]))

    def test_a_trailing_slash_and_a_query_string_reach_the_route(self):
        status, _, _ = self.post("/v1/images/generations/?api-version=1", {"prompt": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(self.seen[0][0], "/v1/images/generations")

    def test_the_record_names_counts_and_settings_and_carries_no_content(self):
        self.post("/v1/images/generations",
                  {"model": "gpt-image-2", "prompt": PROMPT, "n": 1, "size": "1024x1024", "quality": "low"})
        rec = self.records()[-1]
        self.assertEqual((rec["route"], rec["path"], rec["client"]), ("images", "/v1/images/generations", CLIENT))
        self.assertEqual((rec["model"], rec["n"], rec["size"], rec["quality"], rec["stream"]),
                         ("gpt-image-2", 1, "1024x1024", "low", None))
        self.assertEqual((rec["status"], rec["images_out"], rec["prompt_chars"]), (200, 1, len(PROMPT)))
        self.assertEqual(rec["response_bytes"], len(ANSWER))
        self.assertEqual(rec["usage_source"], "provider")
        self.assertEqual(rec["usage"], {"input_tokens": 12, "output_tokens": 515, "total_tokens": 527,
                                        "input_tokens_details": {"text_tokens": 12, "image_tokens": 0}})
        text = json.dumps(rec)
        for leaked in ("secret-prompt-words", PIXELS[:40], "free text", UPSTREAM_KEY, TOKEN):
            self.assertNotIn(leaked, text)
        self.assertIn("revision", rec)
        self.assertTrue(core.verify_chain()["ok"])

    def test_a_setting_that_is_not_shaped_like_one_is_not_recorded(self):
        self.post("/v1/images/generations",
                  {"model": "a model with spaces", "prompt": "x", "size": "a pelican", "quality": "Q" * 40,
                   "n": "many"})
        rec = self.records()[-1]
        self.assertEqual((rec["model"], rec["size"], rec["quality"], rec["n"]), (None, None, None, None))

    def test_the_record_is_on_disk_when_the_caller_has_its_answer(self):
        # The answer's headers are read first, the body after: the record has to exist by
        # the time the caller can see a status, the order the text route keeps.
        data = json.dumps({"model": "gpt-image-2", "prompt": "x"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        self.addCleanup(conn.close)
        conn.request("POST", "/v1/images/generations", data,
                     {"X-Relay-Token": TOKEN, "Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.records()), 1)
        resp.read()

    def test_an_upstream_refusal_goes_back_with_its_status_and_the_record_keeps_identifiers_only(self):
        refusal = json.dumps({"error": {"message": "your prompt " + PROMPT + " was refused",
                                        "type": "invalid_request_error", "code": "moderation_blocked"}}).encode()
        self.respond = lambda h, body: _send(h, 400, "application/json", refusal)
        status, _, answer = self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": PROMPT})
        self.assertEqual((status, answer), (400, refusal))
        rec = self.records()[-1]
        self.assertEqual((rec["status"], rec["upstream_error"]),
                         (400, {"code": "moderation_blocked", "type": "invalid_request_error"}))
        self.assertNotIn("secret-prompt-words", json.dumps(rec))

    def test_a_busy_upstream_is_answered_once_and_not_retried(self):
        self.respond = lambda h, body: _send(h, 503, "application/json", b'{"error":{"type":"overloaded"}}',
                                             {"Retry-After": "7"})
        status, headers, _ = self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})
        self.assertEqual((status, headers.get("Retry-After")), (503, "7"))
        self.assertEqual(len(self.seen), 1)

    def test_a_body_that_is_not_json_is_left_to_the_upstream_to_refuse(self):
        self.respond = lambda h, body: _send(h, 400, "application/json", b'{"error":{"message":"bad json"}}')
        status, _, _ = self.post("/v1/images/generations", b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(self.seen[0][2], b"not json")
        self.assertIsNone(self.records()[-1]["model"])

    def test_an_unreachable_upstream_is_a_502_that_names_nothing_internal(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        config.UPSTREAM = f"http://127.0.0.1:{dead}"
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            status, _, answer = self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})
            deadline = time.monotonic() + 5            # the handler prints after it has answered
            while not said.getvalue().endswith("\n") and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(status, 502)
        self.assertRegex(said.getvalue(), r"\Aimage request to the upstream failed: URLError: .*\n\Z")
        self.assertEqual(json.loads(answer)["error"]["message"],
                         "the image service did not answer; the relay's log has the detail")
        rec = self.records()[-1]
        self.assertTrue(rec["error"])
        self.assertIsNone(rec["status"])

    def test_an_answer_that_breaks_off_is_a_502_not_a_short_body(self):
        def respond(h, body):
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(ANSWER)))
            h.end_headers()
            h.wfile.write(ANSWER[:100])
            h.close_connection = True
        self.respond = respond
        status, _, _ = self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})
        self.assertEqual(status, 502)
        self.assertEqual(self.records()[-1]["error"], "IncompleteRead")

    def test_an_answer_over_the_cap_is_a_502_and_one_at_the_cap_is_not(self):
        config.MAX_IMAGE_RESPONSE_BYTES = len(ANSWER)
        status, _, _ = self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})
        self.assertEqual(status, 200)
        config.MAX_IMAGE_RESPONSE_BYTES = len(ANSWER) - 1
        status, _, _ = self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})
        self.assertEqual(status, 502)
        self.assertEqual(self.records()[-1]["error"], "response_too_large")

    def test_accept_goes_up_with_the_request(self):
        self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"},
                  headers={"Accept": "text/event-stream"})
        self.assertEqual(self.seen[0][1]["accept"], "text/event-stream")

    def test_a_count_that_cannot_be_read_is_not_an_error_and_is_not_recorded(self):
        for n in ("\u00b2", "9" * 5000, "1e3", -1, 10 ** 9, True, [1], None):
            status, _, _ = self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x", "n": n})
            self.assertEqual(status, 200, n)
            self.assertIsNone(self.records()[-1]["n"], n)
        body, ctype = multipart([("model", "gpt-image-2"), ("prompt", "x"), ("n", "\u00b2")],
                                [("image", "a.png", b"x")])
        self.assertEqual(self.post("/v1/images/edits", body, ctype)[0], 200)
        self.assertIsNone(self.records()[-1]["n"])

    def test_an_error_message_is_never_recorded_even_when_it_is_shaped_like_an_identifier(self):
        refusal = json.dumps({"error": {"message": "identifier_shaped_message", "type": "invalid_request_error",
                                        "code": "bad_thing"}}).encode()
        self.respond = lambda h, body: _send(h, 400, "application/json", refusal)
        self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})
        rec = self.records()[-1]
        self.assertEqual(rec["upstream_error"], {"code": "bad_thing", "type": "invalid_request_error"})
        self.assertNotIn("identifier_shaped_message", json.dumps(rec))

    def test_an_error_code_that_is_free_text_is_not_recorded(self):
        refusal = json.dumps({"error": {"message": "m", "type": "invalid_request_error",
                                        "code": "your prompt " + PROMPT + " was refused"}}).encode()
        self.respond = lambda h, body: _send(h, 400, "application/json", refusal)
        self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})
        rec = self.records()[-1]
        self.assertEqual(rec["upstream_error"], {"type": "invalid_request_error"})
        self.assertNotIn("secret-prompt-words", json.dumps(rec))

    def test_a_redirect_is_not_followed_and_is_a_502(self):
        self.respond = lambda h, body: _send(h, 302, "text/plain", b"", {"Location": "http://127.0.0.1:1/elsewhere"})
        status, headers, _ = self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})
        self.assertEqual(status, 502)
        self.assertNotIn("Location", headers)
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.records()[-1]["error"], "redirect")


class StreamTests(_Fake):
    def test_a_stream_is_passed_through_byte_for_byte_and_recorded_when_it_ends(self):
        def respond(h, body):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(STREAM)
            h.close_connection = True
        self.respond = respond
        status, headers, answer = self.post("/v1/images/generations",
                                            {"model": "gpt-image-2", "prompt": PROMPT, "stream": True,
                                             "partial_images": 1})
        self.assertEqual((status, answer), (200, STREAM))
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        rec = self.records()[-1]
        self.assertEqual((rec["stream"], rec["events"], rec["images_out"], rec["response_bytes"]),
                         (True, 2, 1, len(STREAM)))
        self.assertEqual(rec["usage"], {"input_tokens": 12, "output_tokens": 515})
        self.assertNotIn(PIXELS[:40], json.dumps(rec))

    def test_a_stream_that_breaks_off_ends_with_an_error_event(self):
        def respond(h, body):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            first = STREAM.split(b"\n\n")[0] + b"\n\n"
            h.wfile.write(f"{len(first):x}\r\n".encode() + first + b"\r\n")
            h.wfile.write(b"ff\r\npartial")          # a chunk that never completes
            h.close_connection = True
        self.respond = respond
        status, _, answer = self.post("/v1/images/generations",
                                      {"model": "gpt-image-2", "prompt": "x", "stream": True})
        self.assertEqual(status, 200)
        self.assertIn(b"event: error", answer)
        self.assertTrue(self.records()[-1]["error"])


class ClientGoneTests(_Fake):
    def test_a_caller_that_leaves_a_stream_is_recorded_as_gone_and_the_upstream_is_let_go(self):
        def respond(h, body):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Connection", "close")
            h.end_headers()
            try:
                for _ in range(300):
                    h.wfile.write(b"event: keepalive\n\n")
                    h.wfile.flush()
                    time.sleep(0.02)
            except OSError:
                self.upstream_dropped = True
            h.close_connection = True
        self.respond = respond
        self.upstream_dropped = False
        body = json.dumps({"model": "gpt-image-2", "prompt": "x", "stream": True}).encode()
        head = (f"POST /v1/images/generations HTTP/1.1\r\nHost: x\r\nX-Relay-Token: {TOKEN}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode()
        sock = raw_request(self.port, head + body, read=False)
        self.assertIn(b" 200 ", sock.recv(4096).split(b"\r\n")[0])
        sock.close()
        deadline = time.time() + 10
        while time.time() < deadline and not (os.path.getsize(core.RUNS_LOG)):
            time.sleep(0.05)
        rec = self.records()[-1]
        self.assertTrue(rec["client_gone"])
        self.assertIsNone(rec["error"])
        self.assertLess(rec["response_bytes"], 300 * len(b"event: keepalive\n\n"))
        deadline = time.time() + 10
        while time.time() < deadline and not self.upstream_dropped:
            time.sleep(0.05)
        self.assertTrue(self.upstream_dropped, "the relay kept reading a stream nobody was listening to")


class EditTests(_Fake):
    def test_a_multipart_edit_goes_up_byte_for_byte_and_is_described_by_its_fields(self):
        picture = b"\x89PNG\r\n\x1a\n" + os.urandom(4096)
        body, ctype = multipart([("model", "gpt-image-2"), ("prompt", PROMPT), ("size", "1024x1024"),
                                 ("n", "2")], [("image[]", "a.png", picture), ("image[]", "b.png", picture)])
        status, _, answer = self.post("/v1/images/edits", body, ctype)
        self.assertEqual((status, answer), (200, ANSWER))
        path, up_headers, up_body = self.seen[0]
        self.assertEqual((path, up_body, up_headers["content-type"]), ("/v1/images/edits", body, ctype))
        rec = self.records()[-1]
        self.assertEqual((rec["request_kind"], rec["model"], rec["size"], rec["n"], rec["prompt_chars"],
                          rec["images_in"]), ("multipart", "gpt-image-2", "1024x1024", 2, len(PROMPT), 2))
        text = json.dumps(rec)
        for leaked in ("secret-prompt-words", "a.png", "b.png"):
            self.assertNotIn(leaked, text)

    def test_text_in_a_prompt_that_quotes_a_field_header_does_not_steer_the_record(self):
        prompt = 'a fox\r\nname="model"\r\n\r\nINJECTED\r\nname="image"\r\n\r\nname="size"\r\n\r\n1x1\r\n'
        body, ctype = multipart([("prompt", prompt), ("model", "gpt-image-2"), ("size", "1024x1024")],
                                [("image", "a.png", b"x")])
        self.post("/v1/images/edits", body, ctype)
        rec = self.records()[-1]
        self.assertEqual((rec["model"], rec["size"], rec["images_in"]), ("gpt-image-2", "1024x1024", 1))
        self.assertNotIn("INJECTED", json.dumps(rec))

    def test_a_chunked_upload_is_held_to_the_image_cap_and_not_to_the_json_cap(self):
        payload = b"x" * (config.MAX_BODY_BYTES + 1024 * 1024)
        pieces = (payload[i:i + 65536] for i in range(0, len(payload), 65536))
        chunked = b"".join(f"{len(c):x}\r\n".encode() + c + b"\r\n" for c in pieces) + b"0\r\n\r\n"
        head = ("POST /v1/images/edits HTTP/1.1\r\nHost: x\r\nContent-Type: multipart/form-data; boundary=b\r\n"
                f"X-Relay-Token: {TOKEN}\r\nTransfer-Encoding: chunked\r\n\r\n").encode()
        sock = raw_request(self.port, head + chunked, read=False, timeout=20)
        self.addCleanup(sock.close)
        status_line = sock.recv(4096).split(b"\r\n")[0]
        self.assertIn(b" 200 ", status_line)
        self.assertEqual(len(self.seen[0][2]), len(payload))

    def test_a_json_edit_counts_the_images_it_carries(self):
        body = {"model": "gpt-image-2", "prompt": "x",
                "images": [{"image_url": "data:image/png;base64," + PIXELS}] * 3}
        self.post("/v1/images/edits", body)
        rec = self.records()[-1]
        self.assertEqual((rec["request_kind"], rec["images_in"]), ("json", 3))
        self.assertNotIn(PIXELS[:40], json.dumps(rec))

    def test_an_edit_is_held_to_the_image_cap_and_not_to_the_json_cap(self):
        size = config.MAX_BODY_BYTES + 1024 * 1024
        body, ctype = multipart([("model", "gpt-image-2"), ("prompt", "x")], [("image", "a.png", b"x" * size)])
        status, _, _ = self.post("/v1/images/edits", body, ctype)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.seen[0][2]), len(body))

    def test_a_body_over_the_image_cap_is_a_413_that_names_the_cap_and_never_reaches_the_upstream(self):
        config.MAX_IMAGE_BODY_BYTES = 2048
        status, _, answer = self.post("/v1/images/edits", b"x" * 4096, "multipart/form-data; boundary=b")
        self.assertEqual(status, 413)
        self.assertIn("2048", answer.decode())
        self.assertEqual(self.seen, [])

    def test_the_other_routes_keep_their_own_cap(self):
        config.MAX_IMAGE_BODY_BYTES = 10 * config.MAX_BODY_BYTES
        head = (f"POST /v1/responses HTTP/1.1\r\nHost: x\r\nX-Relay-Token: {TOKEN}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {config.MAX_BODY_BYTES + 10}\r\n\r\n")
        sock = raw_request(self.port, head.encode(), read=False)
        self.addCleanup(sock.close)
        answer = b""
        while chunk := sock.recv(65536):       # the answer closes the connection
            answer += chunk
        self.assertIn(b" 413 ", answer.split(b"\r\n")[0])
        self.assertIn(str(config.MAX_BODY_BYTES).encode(), answer)


class GateTests(_Fake):
    def test_no_token_and_a_wrong_token_are_refused_before_the_upstream_or_the_body(self):
        for token in ("", "wrong"):
            status, _, _ = self.post("/v1/images/generations", {"prompt": "x"}, token=token)
            self.assertEqual(status, 401)
        self.assertEqual(self.seen, [])
        self.assertEqual(self.records() if os.path.getsize(core.RUNS_LOG) else [], [])

    def test_the_gate_answers_before_the_body_is_read(self):
        # A body is promised and never sent. A relay that read it first would wait out the
        # connection's 30 s timeout; one that checks the token first answers once the drain
        # of the unread body has waited 0.25 s for a byte (RouteMatrixTests pins that bound).
        head = ("POST /v1/images/generations HTTP/1.1\r\nHost: x\r\nX-Relay-Token: wrong\r\n"
                "Content-Type: application/json\r\nContent-Length: 1000000\r\n\r\n").encode()
        sock = raw_request(self.port, head, read=False, timeout=5)
        self.addCleanup(sock.close)
        sock.settimeout(3)
        self.assertIn(b" 401 ", sock.recv(4096).split(b"\r\n")[0])

    def test_the_token_may_come_as_a_bearer_key_as_an_sdk_sends_it(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/images/generations",
                                     data=b'{"prompt":"x"}', method="POST",
                                     headers={"Authorization": "Bearer " + TOKEN,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            self.assertEqual(r.status, 200)

    def test_a_draining_relay_refuses_new_images_with_a_retry_hint(self):
        handler.STOPPING.set()
        status, headers, _ = self.post("/v1/images/generations", {"prompt": "x"})
        self.assertEqual((status, headers.get("Retry-After")), (503, "5"))
        self.assertEqual(self.seen, [])

    def test_an_empty_body_is_a_400(self):
        status, _, _ = self.post("/v1/images/generations", b"")
        self.assertEqual(status, 400)
        self.assertEqual(self.seen, [])

    def test_a_get_on_the_route_is_still_not_found(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/images/generations",
                                     headers={"X-Relay-Token": TOKEN})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        with caught.exception:
            self.assertEqual(caught.exception.code, 404)

    def test_an_unknown_image_route_is_not_found(self):
        status, _, _ = self.post("/v1/images/variations", {"prompt": "x"})
        self.assertEqual(status, 404)


class SlotTests(_Fake):
    def slow(self):
        def respond(h, body):
            time.sleep(0.3)
            _send(h, 200, "application/json", ANSWER)
        self.respond = respond

    def test_image_calls_are_held_to_the_image_slots(self):
        config.MAX_CONCURRENT_IMAGES = 1
        self.slow()
        results = []

        def call():
            results.append(self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})[0])
        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(results, [200, 200, 200])
        self.assertEqual(self.max_in_flight, 1)

    def test_image_calls_do_not_take_the_text_routes_slots(self):
        config.MAX_CONCURRENT = 1
        with engine.upstream_slot():            # the text route's only slot is taken
            status = self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})[0]
        self.assertEqual(status, 200)

    def test_an_image_in_flight_counts_as_a_run_for_the_drain(self):
        started, release = threading.Event(), threading.Event()

        def respond(h, body):
            started.set()
            release.wait(10)
            _send(h, 200, "application/json", ANSWER)
        self.respond = respond
        result = []
        caller = threading.Thread(target=lambda: result.append(
            self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})[0]))
        caller.start()
        self.assertTrue(started.wait(10))
        self.assertEqual(handler.active_run_count(), 1)
        release.set()
        caller.join(20)
        self.assertEqual(result, [200])
        # the count drops when the handler leaves the run, which can follow the client
        # seeing the response
        self.assertTrue(handler.wait_for_active_runs(5))
        self.assertEqual(handler.active_run_count(), 0)

    def test_the_pacing_gap_is_kept_before_the_upstream_is_asked(self):
        with mock.patch.object(engine, "pace") as pace:
            self.post("/v1/images/generations", {"model": "gpt-image-2", "prompt": "x"})
        pace.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
