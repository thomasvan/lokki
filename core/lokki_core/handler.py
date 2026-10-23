"""The HTTP surface: request guards, the JSON API, and the listener setup."""

import contextlib
import itertools
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (activity, config, control, core, engine, images, passthrough, renamer,
               routing, settings_api, trace)
from .quota import poller
from .core import (ResponseError, _BufSink, config_fingerprint, error_object,
                   error_payload, extract_prompt, normalise_input, read_runs,
                   revision, sse_send, verify_chain)
from .config import ConfigError
from .engine import run_upstream

__all__ = ["Handler", "main", "start_listeners"]

_STARTED_AT = time.time()

# The addresses this process actually bound at start. `config.LISTEN` follows
# every reload (a PUT of `listen` lands in the file for the next start), so what
# answers "where am I listening" and the Host guard read this instead.
BOUND_LISTEN = ()

# The payload keys the relay itself consumes or sets. Everything else the caller
# sent is either carried upstream (renamer.FORWARDED_PARAMS) or named in the record
# as dropped, and this set is what keeps the relay's own controls out of that list.
#
# `store` is here rather than in FORWARDED_PARAMS because the relay owns it: the
# relay aborts the upstream stream before the response is finalized and stores
# nothing, so a client asking for `store: true` is refused (with the reason) rather
# than silently given the opposite of what it asked for.
RELAY_OWNED_PARAMS = frozenset((
    "prompt", "input", "tools", "model", "stream", "mode", "abort_after",
    "seg_budget", "total_cap", "max_segments", "payload_cap", "imagegen_cut",
    "merge_text", "text_calls", "text_calls_pad", "pad_tools", "retries", "previous_response_id", "store",
))

# The relay's own tuning knobs: the keys `resolve_knobs` returns. Pass-through
# removes them from a client body before it goes upstream, where they mean nothing;
# the request's own keys in RELAY_OWNED_PARAMS stay.
RELAY_KNOB_PARAMS = frozenset((
    "mode", "abort_after", "seg_budget", "total_cap", "max_segments", "payload_cap",
    "imagegen_cut", "merge_text", "text_calls", "text_calls_pad", "pad_tools", "retries",
))

# The mode ids the engine reads, and the one an unrecognised value falls back
# to. A caller's typo must not bill a full turn (`control`) or strip the abort
# surface (`pad_tools` follows the mode), so an unknown id is not an error --
# but it must not reach the engine either, or the run record names a mode the
# engine never ran while the marker scan and the padding are silently off.
MODES = ("control", "marker", "after-deltas", "segmented")
DEFAULT_MODE = "segmented"


def resolve_mode(raw):
    """The mode the engine will run, given whatever the caller sent."""
    return raw if raw in MODES else DEFAULT_MODE


# A client controls its own key names, and the dropped list is written to a
# *committed* evidence file, so only identifier-shaped names are recorded. A name
# is not a body value, but a name is still a string from outside this repository
# and the invariant is about not committing client-controlled text.
_PARAM_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_MAX_DROPPED_NAMES = 20
# `model` rides into the run record verbatim, so it is held to the same rule as
# a dropped parameter's name: an identifier, not whatever JSON the caller sent.
# Parentheses are part of an id here: cliproxy reads `model(level)` /
# `model(budget)` as a thinking suffix, e.g. `gpt-5.1(high)`.
_MODEL_NAME = re.compile(r"^[A-Za-z0-9_.:/()-]{1,100}$")
DEFAULT_MODEL = "gpt-5.5"

# Capture file names are unique per process, not per connection: a per-handler
# counter restarted at 0 on every connection, so two captures in one second
# collided on the name and the second was refused by O_EXCL.
_capture_seq = itertools.count()


def dropped_param_names(payload):
    """The caller's keys that this relay did not forward, safely named.

    Names only, never values: `cfg["extra"]` carries the values upstream and is
    deliberately absent from the run record."""
    names = sorted(k for k in payload
                   if k not in RELAY_OWNED_PARAMS
                   and k not in renamer.FORWARDED_PARAMS
                   and isinstance(k, str) and _PARAM_NAME.match(k))
    if len(names) > _MAX_DROPPED_NAMES:
        return names[:_MAX_DROPPED_NAMES] + [f"... and {len(names) - _MAX_DROPPED_NAMES} more"]
    return names

# --- draining (W3.7) --------------------------------------------------------
# SIGTERM used to kill the process mid-stream, so every in-flight client saw a
# truncated stream — on the endpoint whose whole point is that a truncated stream
# must not look like a short success. The replacement: refuse new work, let the
# streams already running finish inside a bounded window, then exit. The bound is
# as load-bearing as the drain: an unbounded graceful shutdown hangs a deploy on
# one stuck client.
STOPPING = threading.Event()

_active_cond = threading.Condition()
_active_runs = 0


@contextlib.contextmanager
def active_run():
    """Count the streams that are mid-flight, for the SIGTERM drain.

    A *run* is the unit that must not be cut. An idle keep-alive connection is
    not a run, which is why the count is taken here and not at accept time:
    waiting on idle connections would turn every restart into a full timeout."""
    global _active_runs
    with _active_cond:
        _active_runs += 1
    try:
        yield
    finally:
        with _active_cond:
            _active_runs -= 1
            if _active_runs == 0:
                _active_cond.notify_all()


def active_run_count():
    with _active_cond:
        return _active_runs


def wait_for_active_runs(timeout):
    """True if every in-flight run finished inside `timeout`."""
    deadline = time.monotonic() + max(0.0, timeout)
    with _active_cond:
        while _active_runs > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _active_cond.wait(remaining)
        return True


# --- reload and drain, for a signal and a route alike ----------------------------
# A signal handler only asks: SIGHUP and `POST /api/reload` set one Event, and one
# thread reads the file and applies it, under the settings write lock so a reload
# and a `PUT /api/settings` never interleave. SIGTERM and `POST /api/shutdown` call
# one drain function.
RELOAD_REQUESTED = threading.Event()
_reload_done = threading.Condition()
# `started` counts the reloads the thread has begun and `done` the one it last
# finished; a request waits for a reload that began after it, so its answer never
# comes from a reload that read the file before the request was made.
_reload_state = {"started": 0, "done": 0, "ok": True, "error": None, "thread": None}


def _reload_loop():
    while True:
        RELOAD_REQUESTED.wait()
        with _reload_done:
            RELOAD_REQUESTED.clear()
            _reload_state["started"] += 1
            run = _reload_state["started"]
        messages = []
        try:
            with settings_api.LOCK:
                applied = config.reload_settings(log=messages.append)
        except Exception as e:  # noqa: BLE001 - the thread must outlive one bad reload
            messages.append(f"reload failed, keeping the running configuration: {type(e).__name__}: {e}")
            applied = None
        for line in messages:
            config._default_log(line)          # the same stamped line a reload prints
        with _reload_done:
            _reload_state.update(done=run, ok=applied is not None,
                                 error=None if applied is not None else "; ".join(messages))
            _reload_done.notify_all()


def _start_reload_thread():
    with _reload_done:
        if _reload_state["thread"] is None:
            _reload_state["thread"] = threading.Thread(target=_reload_loop, name="reload", daemon=True)
            _reload_state["thread"].start()


def request_reload(timeout=5.0):
    """Ask the reload thread for a reload and wait for it: `{ok, error}`."""
    _start_reload_thread()
    with _reload_done:
        wanted = _reload_state["started"] + 1
        RELOAD_REQUESTED.set()
        if not _reload_done.wait_for(lambda: _reload_state["done"] >= wanted, timeout):
            return {"ok": False, "error": "the reload did not finish in time"}
        return {"ok": _reload_state["ok"], "error": _reload_state["error"]}


_drain = {"thread": None, "servers": (), "once": threading.Lock()}


def begin_drain():
    """Refuse new requests, let streams finish up to `drain` seconds, then exit.
    False when a drain has already begun. No I/O here: a signal handler calls it."""
    if not _drain["once"].acquire(blocking=False):
        return False
    STOPPING.set()
    _drain["thread"] = threading.Thread(target=_drain_and_exit, args=(_drain["servers"],), daemon=True)
    _drain["thread"].start()
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # StreamRequestHandler applies this to the connection, so an idle or slow
    # client cannot hold a thread forever. Without it a single connection that
    # never finishes its request line is a permanent thread.
    timeout = config.CLIENT_TIMEOUT
    # W0: filled by _authorized with the configured token's name. Class-level so
    # a handler that somehow skipped the gate reads None rather than raising.
    client_name = None
    # Whether the token that authorised this request may set the relay's own
    # knobs. Left False so a handler that skipped the gate cannot tune.
    client_operator = False

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The client vanished mid-request. Real clients do this constantly, and
            # socketserver's default is to print a traceback per occurrence, which
            # buries the log line an operator is actually looking for. The run
            # record carries the detail.
            self.close_connection = True

    def _authorized(self):
        """Shared-secret gate. Callers must present a configured token via
        X-Relay-Token or Authorization: Bearer, otherwise the request is refused
        before any upstream call spends quota.

        W0: the token that matched decides the client name, which rides into the
        run record. Every candidate is compared even after a match, with
        `compare_digest`, so the loop leaks neither which token matched by timing
        nor how many are configured. The 401 is one message whether the token was
        absent or wrong, so it cannot be used to enumerate names.
        """
        supplied = self.headers.get("X-Relay-Token", "")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[len("Bearer "):]
        matched = None
        operator = False
        # Compared as bytes. `compare_digest` raises TypeError on a str with any
        # non-ASCII character, and a header is decoded as latin-1, so one such
        # byte in the header was an unhandled exception rather than a 401.
        # Encoding the header back as latin-1 recovers the bytes the client
        # sent; a configured token is text, sent by a client as UTF-8.
        try:
            supplied_b = supplied.encode("latin-1")
        except UnicodeEncodeError:
            supplied_b = b""
        if supplied_b:
            for entry in config.TOKENS:
                name, token = entry[0], entry[1]
                # A 2-tuple is not built by this code; it is tolerated as a
                # *client*, which is the safe direction to guess in.
                role = entry[2] if len(entry) > 2 else "client"
                if secrets.compare_digest(supplied_b, token.encode("utf-8")):
                    matched = name
                    operator = role == "operator"
        if matched is not None:
            self.client_name = matched
            self.client_operator = operator
            return True
        self.client_name = None
        self.client_operator = False
        self._error(401, "missing or invalid relay token (send X-Relay-Token)")
        return False

    def _sse_error(self, message, code="internal_error"):
        """An in-band error once the headers are out.

        No status is possible any more, so the only honest signal left is an SSE
        `error` event; without it the client sees a truncated stream that looks
        like a short success. Tolerates a client that is already gone.

        Deliberately without a `sequence_number`: this path fires where no stream
        was numbered at all (a request that failed before the engine ran), and
        inventing a `0` would be worse than its absence. The engine's own error
        frame, which sits inside a numbered stream, carries one.
        """
        try:
            sse_send(self.wfile, "error", error_payload(message, code=code))
        except Exception:  # noqa: BLE001 - nothing left to report to
            pass

    def _error(self, status, message, retry_after=None):
        if getattr(self, "_response_started", False):
            # Headers are long gone, so a status would corrupt the stream.
            self._sse_error(message)
            return
        body = json.dumps(error_object(message, status=status)).encode()
        self._drop_unread_body()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if retry_after:
            self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", str(len(body)))
        # Close after an error: on a 413 the body was never read, and on any error
        # path leaving unread bytes on a keep-alive socket makes the *next*
        # request on that connection be parsed as if they were its request line.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.error):
            pass

    def _capture(self, raw):
        """Write the client's raw body for a deliberate capture, or do nothing.

        Off unless `capture_dir` is configured, and the config refuses a directory
        inside the repository, so a captured body cannot be committed by accident.
        0600, one file per request, and the path is logged so the operator knows
        what exists and can delete it. This is the mechanism W1.1's "round trip on
        a captured client body" and W1.5's "list derived from a capture" need; it
        is not a logging feature and it is not on by default."""
        if not config.CAPTURE_DIR or not raw:
            return None
        try:
            os.makedirs(config.CAPTURE_DIR, mode=0o700, exist_ok=True)
            name = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            path = os.path.join(config.CAPTURE_DIR,
                                f"request-{name}-{os.getpid()}-{next(_capture_seq)}.json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            print(f"captured a client body to {path}", file=sys.stderr, flush=True)
            return path
        except OSError as e:
            print(f"capture failed: {e}", file=sys.stderr, flush=True)
            return None

    def _read_raw_body(self, limit=None):
        """The request body's bytes, held to `limit` (`MAX_BODY_BYTES` unless the route
        has a cap of its own), whichever way the client framed it."""
        self._body_unread = False
        limit = config.MAX_BODY_BYTES if limit is None else limit
        transfer = self.headers.get("Transfer-Encoding")
        if transfer is not None:
            # Both framings at once is the request-smuggling shape: two parsers
            # can disagree on where this body ends. Refused rather than resolved.
            if self.headers.get("Content-Length") is not None:
                raise ResponseError(
                    400, "send either Transfer-Encoding or Content-Length, not both")
            if transfer.strip().lower() != "chunked":
                raise ResponseError(
                    400, f"unsupported Transfer-Encoding {transfer.strip()!r}; "
                         "send 'chunked' or a Content-Length")
            return self._read_chunked(limit)
        return self._read_sized(limit)

    # Draining a body no one read stops once its bytes stop coming for this long, or after
    # the whole wait: an error is never held up by a client that is not sending.
    _DRAIN_IDLE_SECONDS = 0.25
    _DRAIN_SECONDS = 5.0

    def _drop_unread_body(self):
        """Reads and drops a body sized by `Content-Length` that nothing read, up to
        `MAX_BODY_BYTES`, before an error closes the connection. A close over bytes it
        never read resets the connection, and a client can lose the answer to the reset:
        one that sent a body with no token got a reset instead of its 401. A chunked or
        larger body, or one still unsent when the drain stops, is closed over."""
        if not getattr(self, "_body_unread", False):
            return
        self._body_unread = False
        if self.headers.get("Transfer-Encoding") is not None:
            return
        try:
            left = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return
        if left > config.MAX_BODY_BYTES:
            return
        end = time.monotonic() + self._DRAIN_SECONDS
        timeout = self.connection.gettimeout()
        self.connection.settimeout(self._DRAIN_IDLE_SECONDS)
        try:
            while left > 0 and time.monotonic() < end:
                chunk = self.rfile.read1(min(left, 1 << 16))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(timeout)

    def _read_json_body(self, capture=True, return_raw=False):
        """Parse the request body, answering 400 instead of raising so the
        client always gets a status rather than a dropped connection. With
        `return_raw`, `(raw bytes, payload)`: pass-through forwards the bytes."""
        raw = self._read_raw_body()
        if not raw.strip():
            raise ResponseError(400, "empty request body")
        # Off by default, and refused inside the repository by config: this is the
        # deliberate capture the plan owes a real-client round trip, not logging.
        # Only a client's request is captured; an operator's settings body is not one.
        if capture:
            self._capture(raw)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ResponseError(400, f"invalid JSON body: {e}")
        if not isinstance(payload, dict):
            raise ResponseError(400, "request body must be a JSON object")
        return (raw, payload) if return_raw else payload

    def _too_large(self, size, limit):
        return ResponseError(413, f"request body is {size} bytes; the limit is "
                                  f"{limit}")

    def _read_sized(self, limit):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            raise ResponseError(400, "invalid Content-Length header")
        if length > limit:
            # Refuse before buffering: an unbounded read here is a memory
            # exhaustion primitive, not a parsing problem.
            raise self._too_large(length, limit)
        return self.rfile.read(length) if length > 0 else b""

    # A chunk-size line is hex digits plus optional extensions; a trailer line is
    # a header. Neither has a reason to be long, and an unbounded `readline` is the
    # same memory primitive the body cap closes.
    _MAX_CHUNK_LINE = 1024
    _MAX_TRAILERS = 64
    # The byte cap alone left the *count* open: 8 MB as one-byte chunks is ~48 MB
    # on the wire and ~11 s of parsing per connection (measured on the PR #2
    # build), against nothing for a sized body. 16384 is a judgment, not a
    # measurement of any client: it carries the full cap at 512 bytes a chunk,
    # and a client that streams smaller pieces gets a 400 naming the remedy
    # (larger chunks, or a Content-Length). The boundary is pinned by
    # test_offline.py's `POSTS` rows at its real value, unmocked.
    _MAX_CHUNKS = 16384

    def _read_chunked(self, limit):
        """A chunked body (RFC 9112 §7.1), held to `limit` like any other.

        It used to be answered "empty request body": with no Content-Length the
        length read as 0, so a client whose HTTP stack streams its upload got a
        400 for a request it had sent whole."""
        body = bytearray()
        chunks = 0
        while True:
            line = self.rfile.readline(self._MAX_CHUNK_LINE + 1)
            if not line.endswith(b"\n"):
                raise ResponseError(400, "malformed chunked body: bad chunk-size line")
            size_text = line.split(b";", 1)[0].strip()
            if not size_text or any(c not in b"0123456789abcdefABCDEF"
                                    for c in size_text):
                raise ResponseError(400, "malformed chunked body: bad chunk size")
            size = int(size_text, 16)
            if size == 0:
                break
            chunks += 1
            if chunks > self._MAX_CHUNKS:
                raise ResponseError(
                    400, f"chunked body has more than {self._MAX_CHUNKS} chunks; "
                         "send larger chunks or a Content-Length")
            if len(body) + size > limit:
                raise self._too_large(len(body) + size, limit)
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.readline(3) not in (b"\r\n", b"\n"):
                raise ResponseError(400, "malformed chunked body: truncated chunk")
            body += chunk
        for _ in range(self._MAX_TRAILERS):
            line = self.rfile.readline(self._MAX_CHUNK_LINE + 1)
            if line in (b"\r\n", b"\n", b""):
                return bytes(body)
        raise ResponseError(400, "malformed chunked body: too many trailer lines")

    def do_GET(self):
        self._response_started = False
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError, socket.error):
            pass
        except ResponseError as e:
            if not self._response_started:
                self._error(e.status, e.message)
        except Exception as e:  # never leave a client without a status
            self._internal_error(e)

    def _internal_error(self, exc):
        """A 500 that names nothing. The detail goes to the operator's log: GET
        reaches here without a token (`/healthz`), and an exception's text is
        not the caller's to read."""
        print(f"unhandled {self.command} {self.path!r}: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        self._error(500, "internal error in the relay; the operator's log has the "
                         "detail")

    # `/healthz` is deliberately outside the token gate: a supervisor or a monitor
    # has to be able to ask "is it up" cheaply, and the answer must carry nothing
    # a caller should not have. `/version` carries the serving revision, so it
    # stays gated.
    _PUBLIC_GET = ("/healthz",)
    # The operator's views. `/api/runs` and `/api/activity` are every client's records,
    # `/api/chain` walks all of them, and `/version` names every client and operator: a
    # client token is one tenant's credential and reads none of that.
    _OPERATOR_GET = ("/version", "/api/chain", "/api/runs", "/api/activity", "/api/settings",
                     "/api/status", "/api/endpoint", "/api/schema", "/api/accounts", "/api/models")

    def _guard_origin(self):
        """Host and Origin: a local server with a token in a browser must not be
        drivable from another site (DNS rebinding, CSRF). Every /api/* route and
        /version. /healthz and /v1/* are not covered: a client SDK sends whatever
        Host its base URL says. What is bound, not `config.LISTEN`: a PUT of
        `listen` moves that for the next start, and this process still answers
        on the addresses it bound."""
        host = self.headers.get("Host", "")
        if not control.host_allowed(host, BOUND_LISTEN or config.LISTEN):
            self._error(403, "the Host header is not one of this relay's addresses")
            return False
        if not control.origin_allowed(self.headers.get("Origin"), host):
            self._error(403, "the Origin is not this relay's own")
            return False
        return True

    def _raw(self, json_body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(json_body)))
        self.end_headers()
        self.wfile.write(json_body)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _do_GET(self):
        # A real monitor appends a query string, and a trailing slash is a normal
        # typo. Comparing self.path literally made both of those fall through to
        # the token gate and answer 401 — which the supervisor then reads as "the
        # relay is down".
        raw = urllib.parse.urlsplit(self.path).path
        path = raw.rstrip("/") or "/"
        if path == "/" or raw == "/static" or raw.startswith("/static/"):
            # The UI, public: it carries no data, and sends the operator token on
            # every API call it makes. Matched before the slash is stripped, so
            # `/static/` is a missing file and not an unknown route.
            found = control.static_file(path if path == "/" else raw)
            if found is None:
                self._error(404, "not found")
                return
            body, content_type = found
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if (path.startswith("/api/") or path == "/version") and not self._guard_origin():
            return
        if path not in self._PUBLIC_GET and not self._authorized():
            return
        # The operator tuple is exact matches; the login status route is a prefix.
        if (path in self._OPERATOR_GET or path.startswith(control.OAUTH_PREFIX)) and not self.client_operator:
            self._error(403, "this endpoint needs an operator token")
            return
        if path == "/healthz":
            # No upstream call, no client data: this is the liveness probe the
            # five-minute supervisor uses, and it answers before anything that
            # could be slow. `routing` is the one piece of configuration it
            # carries: when every request goes through at full price because the
            # quota is unknown, a monitor without the UI has to see it, and its two
            # fields name no secret, no account and no address. Decided now, as for
            # a request, so the probe sees the current state, not the last request's.
            d = routing.decide_now()
            body = {"status": "draining" if STOPPING.is_set() else "ok",
                    "uptime_s": round(time.time() - _STARTED_AT, 1),
                    "routing": {"relay_on": d.relay_on, "reason": d.reason}}
            self._json(body)
        elif path == "/version":
            # What the supervisor records a run against. The
            # client names are here because "a token was added and took effect
            # without a restart" has to be checkable on the running process.
            self._json({"revision": revision(),
                        "config_fingerprint": config_fingerprint(),
                        "clients": [entry[0] for entry in config.TOKENS],
                        "operators": [entry[0] for entry in config.TOKENS
                                      if len(entry) > 2 and entry[2] == "operator"],
                        "uptime_s": round(time.time() - _STARTED_AT, 1)})
        elif path == "/v1/models":
            try:
                req = urllib.request.Request(
                    config.UPSTREAM + "/v1/models",
                    headers={"Authorization": f"Bearer {config.API_KEY}"})
                body = urllib.request.urlopen(req, timeout=30).read()
            except Exception as e:
                # A failed catalogue fetch used to return HTTP 200 with an error
                # object, so a client could not tell it from an empty catalogue —
                # the same dishonesty this relay exists to remove.
                self._error(502, f"upstream model list unavailable: {e}")
                return
            self._raw(json_body=body)
        elif path == "/api/chain":
            # W4.2: the operator's check on the evidence chain, live, against the
            # same files the supervisor will read. Token-gated like /api/runs.
            self._json(verify_chain())
        elif path == "/api/settings":
            self._json(settings_api.view(config._settings, os.environ))
        elif path == "/api/status":
            d = routing.decide_now()
            self._json(control.status(
                decision={"relay_on": d.relay_on, "reason": d.reason,
                          "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                poll=poller.POLLER.status()))
        elif path.startswith(control.OAUTH_PREFIX):
            self._json(control.oauth_status(path[len(control.OAUTH_PREFIX):]))
        elif path == "/api/accounts":
            snap = poller.POLLER.latest()
            self._json(control.accounts(routing.decide_now(snap), snap))
        elif path == "/api/models":
            self._json(control.models())
        elif path == "/api/endpoint":
            reveal = "reveal=1" in urllib.parse.urlsplit(self.path).query.split("&")
            self._json(control.endpoint(reveal, BOUND_LISTEN or config.LISTEN))
        elif path == "/api/schema":
            self._json(control.schema_listing())
        elif path == "/api/activity":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            window = query.get("window", ["24h"])
            if len(window) != 1 or window[0] not in activity.WINDOWS:
                self._error(400, "window must be 24h or 7d")
                return
            self._json(activity.answer(window[0]))
        elif path == "/api/runs":
            body = json.dumps(read_runs(), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            # Through `_error`, for its Content-Length and `Connection: close`: a
            # bare 404 on an HTTP/1.1 keep-alive connection has no end a client
            # can find, so the client waited for its own timeout.
            self._error(404, "not found")

    def do_POST(self):
        self._response_started = False
        self._body_unread = True
        try:
            self._do_POST()
        except ResponseError as e:
            self._error(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The client is gone; there is nobody to tell. Deliberately NOT
            # `socket.error`, which is `OSError` and would also swallow a real
            # failure such as log_run running out of disk.
            pass
        except Exception as e:  # never leave a client without a status
            self._internal_error(e)

    def do_PUT(self):
        self._response_started = False
        self._body_unread = True
        try:
            self._do_PUT()
        except ResponseError as e:
            self._error(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # never leave a client without a status
            self._internal_error(e)

    def _do_PUT(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path != "/api/settings":
            self._error(404, "not found")
            return
        if not self._guard_origin() or not self._authorized():
            return
        if not self.client_operator:
            self._error(403, "this endpoint needs an operator token")
            return
        body = self._read_json_body(capture=False)   # after the role check: a client token never reaches the parser
        try:
            result = settings_api.put(body, config._settings.get("config_path"), os.environ)
        except settings_api.Refused as e:
            payload = error_object(e.message, status=e.status)
            payload["key"] = e.key
            self._json(payload, status=e.status)
            return
        self._json(result)

    def _images(self, path):
        """`/v1/images/generations` and `/v1/images/edits`: the cliproxy's image routes behind
        the relay's token gate (lokki_core/images.py). The gate and the drain come first, as for
        every other route, and the body is read only for a caller that passed them."""
        if not self._authorized():
            return
        if STOPPING.is_set():
            self._error(503, "the relay is restarting; retry shortly", retry_after=5)
            return
        raw = self._read_raw_body(config.MAX_IMAGE_BODY_BYTES)
        if not raw.strip():
            raise ResponseError(400, "empty request body")
        rec = images.new_record(path, raw, self.headers.get("Content-Type"),
                                getattr(self, "client_name", None))
        with active_run():
            images.serve(self, path, raw, rec)

    def _do_POST(self):
        # Normalised the same way GET is: a client that appends `?api-version=`
        # should not get a 404 where a monitor appending `?ts=` gets an answer.
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path in images.IMAGE_PATHS:
            self._images(path)
            return
        if path in ("/api/reload", "/api/shutdown", "/api/accounts/refresh", control.OAUTH_START):
            if not self._guard_origin() or not self._authorized():
                return
            if not self.client_operator:
                self._error(403, "this endpoint needs an operator token")
                return
            if path == "/api/reload":
                self._json(request_reload())
            elif path == "/api/accounts/refresh":
                snap = poller.POLLER.refresh(timeout=15.0)
                self._json(control.accounts(routing.decide_now(snap), snap))
            elif path == control.OAUTH_START:
                raw = self._read_raw_body()      # after the role check, as for every operator body
                try:
                    empty = not raw.strip() or json.loads(raw) == {}
                except ValueError:
                    empty = False
                if not empty:
                    raise ResponseError(400, "this route takes an empty body or {}")
                self._json(control.oauth_start())
            else:
                self._json({"stopping": begin_drain(), "drain_seconds": config.DRAIN}, status=202)
            return
        if path.startswith(control.OAUTH_PREFIX):
            if not self._guard_origin() or not self._authorized():
                return
            if not self.client_operator:
                self._error(403, "this endpoint needs an operator token")
                return
            # After the role check, and never captured: it carries the login's code.
            raw = self._read_raw_body(control.OAUTH_SEND_LIMIT)
            self._json(control.oauth_send(path[len(control.OAUTH_PREFIX):], raw))
            return
        if path not in ("/api/run", "/v1/responses"):
            self._error(404, "not found")
            return
        # The measurement tools send a loopback Host and no Origin; /v1/responses
        # is a client SDK's and stays unguarded.
        if path == "/api/run" and not self._guard_origin():
            return
        if not self._authorized():
            return
        if STOPPING.is_set():
            # Draining: new work would not be waited for. A status with a retry
            # hint is honest; accepting it and then cutting it is the failure this
            # relay exists to remove. Deliberately after the token check, so a
            # drain does not change what an unauthorised caller sees, and
            # deliberately NOT at the socket level, so /healthz keeps answering
            # and the supervisor can tell a draining relay from a wedged one.
            self._error(503, "the relay is restarting; retry shortly",
                        retry_after=5)
            return

        raw, payload = self._read_json_body(return_raw=True)

        # The decision is read once, here: a request in flight is not switched.
        # `/api/run` always runs the engine, as the measurement tools expect.
        if path == "/v1/responses":
            decision = routing.decide_now(model=payload.get("model"))
            if not decision.relay_on:
                with active_run():
                    passthrough.serve(self, raw, payload, decision)
                return

        # W1.4: the relay aborts upstream before `response.completed` and sets
        # `store: false`, so there is no finalized server-side response to chain
        # onto. Two things follow, and they are separate:
        #
        #   * the request is always *recorded* as having carried the field, even
        #     when it is accepted, so "which source uses server-side history?" is
        #     answerable from the evidence rather than guessed;
        #   * by default it is refused, loudly, naming the fix. The dangerous
        #     alternative — accept it and drop it — gives the model a single turn
        #     with no context and returns a confidently wrong answer, which is the
        #     failure class this relay exists to remove.
        previous_seen = payload.get("previous_response_id") is not None
        if previous_seen and config.REJECT_PREVIOUS_RESPONSE_ID:
            # A refused request never reaches the run log, so this is the only
            # place the operator can find out *which* source keeps its history
            # server-side and now gets a 400. The client's name, never its token.
            print(f"refused previous_response_id from client="
                  f"{getattr(self, 'client_name', None)}", file=sys.stderr,
                  flush=True)
            raise ResponseError(
                400, "'previous_response_id' is not supported by this relay: it "
                     "ends the upstream stream before the response is finalized, "
                     "so there is no stored response to continue from. Send the "
                     "full conversation in 'input' instead (inline history).")

        # `store` is the relay's, not the caller's, and the two values are not
        # equally acceptable. `false` agrees with what the relay does anyway. `true`
        # asks for a persisted response, which this relay can never produce: it cuts
        # the upstream stream before the provider finalizes a response, so there is
        # nothing to store — and accepting the field while sending `false` would give
        # the client the opposite of what it asked for and let it discover that by
        # chaining onto a response that does not exist.
        if payload.get("store"):
            raise ResponseError(
                400, "'store': true is not supported by this relay: it ends the "
                     "upstream stream before the response is finalized, so nothing "
                     "is stored and 'previous_response_id' would have nothing to "
                     "chain onto. Send 'store': false and the full conversation in "
                     "'input' instead (inline history).")

        # Two shapes arrive here and they are not interchangeable. `/api/run` takes
        # a plain `prompt` string. A real client speaks the Responses API and sends
        # `input`, whose structure has to survive to upstream: roles, tool calls and
        # tool results. Flattening it (the old behaviour) dropped every non-text
        # item, so a client's `function_call_output` never reached the model.
        prompt = None
        inp = None
        if payload.get("prompt"):
            prompt = payload["prompt"]
            if not isinstance(prompt, str) or not prompt.strip():
                raise ResponseError(400, "the 'prompt' string is empty")
        else:
            inp = normalise_input(payload.get("input"))

        tools = payload.get("tools")
        if tools is not None:
            if not isinstance(tools, list):
                raise ResponseError(400, "'tools' must be a list")
            for i, t in enumerate(tools):
                if not isinstance(t, dict) or not isinstance(t.get("type"), str):
                    raise ResponseError(
                        400, f"tools[{i}] must be an object with a 'type'")

        # W1.3: the caller's sampling and behaviour parameters, whitelisted into
        # the upstream body. The ones this build does not carry are *named in the
        # record* rather than dropped in silence — a client that asked for a
        # temperature and got 1.0 has no other way to find that out.
        extra = {k: payload[k] for k in renamer.FORWARDED_PARAMS if k in payload}
        dropped = dropped_param_names(payload)

        # W1.5: forward the client's own headers from an allow-list. The relay
        # replaces them with its upstream credential, never passes it through.
        headers = {}
        for name in engine.FORWARDED_HEADERS:
            value = self.headers.get(name)
            if value:
                headers[name] = value

        model = payload.get("model") or DEFAULT_MODEL
        if not isinstance(model, str) or not _MODEL_NAME.match(model):
            raise ResponseError(
                400, "'model' must be a model id: letters, digits and . _ : / ( ) -, "
                     "at most 100 characters")

        # `mode` is set by `resolve_knobs` below, from the caller only where
        # tuning is allowed.
        cfg = {
            "prompt": prompt if prompt is not None else extract_prompt(inp),
            "input": inp if inp is not None else prompt,
            "tools": tools,
            "model": model,
            # W0 attribution: which configured token authorised this request.
            "client": getattr(self, "client_name", None),
            "previous_response_id_seen": previous_seen,
            "extra": extra,
            "dropped_params": dropped,
            "headers": headers,
            # Operator policy, not a request knob: a caller cannot ask for it.
            "session_pin_id": (core.session_pin_id(getattr(self, "client_name", None))
                               if config.DEFAULT_SESSION_PIN else None),
        }
        # Review M7: on the client-facing path the relay's own knobs are the
        # operator's, not the caller's.
        cfg.update(resolve_knobs(
            payload,
            config.ALLOW_CLIENT_TUNING
            or (path == "/api/run" and self.client_operator)))

        if payload.get("stream", True):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # The stream has no Content-Length and is not chunked, so its end is
            # the connection's close — which is what happens below. Saying
            # `keep-alive` here contradicted that.
            self.send_header("Connection", "close")
            self.end_headers()
            self._response_started = True
            with active_run():
                run_upstream(cfg, self.wfile)
            self.close_connection = True
        else:
            # non-streaming client: run the same engine into a buffer, then
            # return a standard Responses-API JSON object
            sink = _BufSink()
            with active_run():
                rec = run_upstream(cfg, sink)
            completed = None
            error_message = None
            for line in sink.buf.decode("utf-8", "replace").splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if obj.get("type") in ("response.completed", "response.incomplete"):
                    # An incomplete response is still the provider's answer, and
                    # a Responses-API client reads its `status` to know.
                    completed = obj.get("response")
                elif obj.get("type") == "error" and isinstance(obj.get("message"), str):
                    error_message = obj["message"]
            if rec.get("cut") in ("error", "client_gone"):
                # Neither an upstream failure nor a lost connection is an empty
                # successful answer. A provider 4xx is a complaint about this
                # client's own request, so it keeps its status and the
                # provider's message (the stream's `error` frame carries it;
                # the run record only ever holds the identifier form). Except
                # 401/403/407: those are about the relay's upstream credential,
                # and passing them on would tell the client its own token is bad.
                # A 429 does pass, once the retry budget is spent: it is the
                # status a client's SDK backs off on, and the streaming path
                # already hands it the same provider text in its `error` frame.
                upstream = rec.get("upstream_error") or {}
                status = upstream.get("status")
                if (not isinstance(status, int) or not 400 <= status < 500
                        or status in (401, 403, 407)):
                    status = 502
                self._error(status, error_message or rec.get("error")
                            or "the client connection closed before the response "
                               "completed")
                return
            if not isinstance(completed, dict):
                # The engine closes every non-error run with a terminal frame
                # whose `output` is its own view of what the client was shown.
                # Rebuilding that here — from the deltas a second time, or from a
                # fresh synthetic frame — would be a second home for the same
                # fact, and the two would drift. A missing terminal frame is an
                # internal fault, and the honest answer to it is a status.
                self._error(502, "the relay produced no terminal frame")
                return
            body = json.dumps(completed).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

class _ReusableServer(ThreadingHTTPServer):
    # SO_REUSEADDR on Windows lets a second listener bind a port in use, which would hide a busy port;
    # the TIME_WAIT reuse it buys on POSIX is not needed there.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True
    # The stdlib default is 5. Under any real concurrency that refuses clients
    # before the process even sees them.
    request_queue_size = config.LISTEN_BACKLOG

    def server_bind(self):
        # http.server's version also calls socket.getfqdn() to fill server_name, which
        # nothing here reads; on macOS that reverse lookup stalls startup for many seconds.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # ThreadingHTTPServer spawns a thread per connection with no ceiling. A cap
        # with an explicit 503 is a bound an operator can measure; unbounded
        # threading is a process that dies under a hostile client instead.
        self._slots = threading.BoundedSemaphore(config.MAX_CLIENT_CONNECTIONS)

    def process_request(self, request, client_address):
        # Acquire here and release in process_request_thread's finally, so exactly
        # the connections that took a slot give it back, and the rejected path —
        # which never spawns a thread — never releases one it did not take.
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                # On POSIX the close after a 503 is orderly without a read. Windows resets a
                # connection closed with unread data, and the client then loses the 503, so
                # there the refusal reads first; the read is bounded because this runs on
                # the accept thread.
                if os.name == "nt":
                    request.settimeout(0.05)
                    request.recv(65536)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def start_listeners(addresses, handler=Handler, strict=True):
    """Bind the configured addresses.

    `strict` (the default) means a configured address is **required**: failing to
    bind one refuses startup and names it. The old behaviour warned and skipped,
    which meant a relay could come up loopback-only because Tailscale was not up
    yet and nothing would say so. Pass `strict=False` only for a caller that has
    deliberately marked an address optional.
    """
    servers = []
    for addr in addresses:
        host, _, port = addr.rpartition(":")
        try:
            srv = _ReusableServer((host, int(port)), handler)
        except OSError as e:
            if strict:
                for _, s in servers:
                    s.server_close()
                raise ConfigError(
                    f"cannot bind {addr}: {e.strerror or e}. Every configured "
                    f"listen address is required; mark it optional or fix the "
                    f"address.")
            print(f"warning: cannot bind {addr}: {e.strerror or e}; skipping",
                  file=sys.stderr, flush=True)
            continue
        servers.append((addr, srv))
    if not servers:
        raise ConfigError(
            f"none of the configured addresses could be bound: {list(addresses)}")
    return servers

def resolve_knobs(payload, tuning):
    """The relay's own tuning knobs, from the caller only where that is allowed.

    `/api/run` is the operator's path and always keeps them. `/v1/responses`
    is the client-facing path, where the cut policy belongs to the operator: a
    caller choosing `mode: control` bills the account, and `pad_tools: false`
    removes the abort surface the relay exists for. `allow_client_tuning` decides,
    and it defaults to false because the relay is the client-facing endpoint — an
    explicit `true` (config or `RELAY_ALLOW_CLIENT_TUNING`) re-opens the knobs.
    """
    def pick(name, default):
        return payload.get(name, default) if tuning else default

    def as_int(name, default, zero_means_off=False):
        """A caller-supplied knob that is not a number is a client error.

        `int()` on caller JSON used to escape as whatever it raised, so a
        `seg_budget: "many"` from an operator or a re-opened caller answered 500
        from the catch-all instead of naming the field.

        `zero_means_off` is for the knobs whose zero is a *setting* rather than
        "unset". The budgets keep the other reading on purpose: `0` there means
        "no bound", which is the direction that leaks the answer this relay exists
        to cut (the engine's predicates are truthiness-guarded, `if total_cap
        and ...`), so an absent-or-zero budget resolves to the configured default
        instead. `payload_cap` is the reverse — `0` is the documented escape hatch
        for a client whose real images the cap would break, and answering the
        default to an explicit `0` would be the same silent no-op as a config key
        `load_settings` does not read. `retries` is the other: `0` asks for no
        retries, and it used to buy the configured six; the engine makes at least
        one attempt either way.

        A negative value is a client error for every knob. No knob has a meaning
        below zero, and a negative budget used to be accepted and cut on the
        first delta of every segment.
        """
        raw = pick(name, default)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ResponseError(400, f"'{name}' must be an integer") from None
        if value < 0:
            raise ResponseError(400, f"'{name}' cannot be negative")
        if not zero_means_off and not value:
            return default
        return value

    return {
        "mode": resolve_mode(payload.get("mode")) if tuning else DEFAULT_MODE,
        "abort_after": as_int("abort_after", 0),
        "seg_budget": as_int("seg_budget", config.DEFAULT_SEG_BUDGET),
        "total_cap": as_int("total_cap", config.DEFAULT_TOTAL_CAP),
        "max_segments": as_int("max_segments", config.DEFAULT_MAX_SEGMENTS),
        # Operator-owned with the rest of the cut policy: this one decides how
        # much of an oversized item payload reaches the client, and the client's
        # own copy of it is what the account pays to re-read on every later turn.
        "payload_cap": as_int("payload_cap", config.DEFAULT_PAYLOAD_CAP,
                              zero_means_off=True),
        # The capability cut, and the largest single lever here: the run ends when
        # upstream names an image item, before the image is generated. A caller
        # that needs its images has to say so — this is the one knob whose *off*
        # is the expensive state.
        "imagegen_cut": bool(pick("imagegen_cut", config.DEFAULT_IMAGEGEN_CUT)),
        # Whether a budget-continued text answer stays one message item. Operator
        # policy with the rest: it changes the shape of the stream a client sees.
        "merge_text": bool(pick("merge_text", config.DEFAULT_MERGE_TEXT)),
        # On unless turned off: the model writes a tool call as text and the
        # relay builds the call and cuts there (lokki_core/textcalls.py).
        "text_calls": bool(pick("text_calls", config.DEFAULT_TEXT_CALLS)),
        "text_calls_pad": bool(pick("text_calls_pad", config.DEFAULT_TEXT_CALLS_PAD)),
        "pad_tools": bool(pick("pad_tools", True)),
        "retries": as_int("retries", config.RETRIES, zero_means_off=True),
    }


def startup_notes():
    """Lines to print at start-up about a configuration that is allowed and known to cost
    quality. Notes, not refusals: nothing here is unsafe."""
    notes = []
    if config.TAIL_CHARS < config.DEFAULT_SEG_BUDGET:
        notes.append(
            f"note: tail_chars ({config.TAIL_CHARS}) is below seg_budget "
            f"({config.DEFAULT_SEG_BUDGET}), so a continuation is shown less than the segment "
            "it continues. In one pair of rounds at 1,500 under 4,000, 4 of 10 900-word answers "
            "ran past their length and were cut at total_cap; at tail_chars 8,000 none did. "
            "Only the start-up value is checked here, "
            "not a budget a request sets under allow_client_tuning.")
    return notes


def wildcard_addresses(addresses):
    """The configured addresses that bind every interface.

    An empty host, `0.0.0.0`, `::` or `*`. Exposing one of these puts an endpoint
    that forwards the operator's upstream credential on every interface, which
    WORKSPACE_PROTOCOL reserves to the Human — so main() refuses unless
    `allow_public_bind` is set. Split out as a pure function so the rule is
    testable without binding anything.
    """
    return [a for a in addresses
            if a.rpartition(":")[0] in ("", "0.0.0.0", "::", "*")]


class _ReaderMayBeGone:
    """A standard stream whose write or flush never raises.

    The shell reads the core's output through pipes, and a shell that dies without
    stopping the core takes their read ends with it. A write then raises (`EPIPE` on
    POSIX, `EINVAL` on Windows), and a print that raises stops whatever was printing:
    the drain before it reaches `os._exit`, a request handler mid-request. A line with
    no reader is dropped instead."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        try:
            return self._stream.write(text)
        except (OSError, ValueError):
            return len(text)

    def flush(self):
        try:
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def main(argv=None):
    """Parse --config, resolve settings, and serve every configured address."""
    global BOUND_LISTEN
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, _ReaderMayBeGone):
            setattr(sys, name, _ReaderMayBeGone(stream))
    argv = list(sys.argv[1:] if argv is None else argv)
    explicit_config = None
    if argv and argv[0] in ("-c", "--config"):
        if len(argv) < 2:
            print("--config needs a path", file=sys.stderr, flush=True)
            return 2
        explicit_config = argv[1]
        os.environ["RELAY_CONFIG"] = explicit_config
        config.CONFIG_PATH = explicit_config
    try:
        config.ensure_settings(force=explicit_config is not None,
                               config_path=explicit_config)
    except config.ConfigError as e:
        print(f"refusing to start: {e}", file=sys.stderr, flush=True)
        return 2
    if not config.TOKENS:
        print("refusing to start: no client token. Set auth.tokens: "
              "[{name, token}] in " + str(config.CONFIG_PATH) + " (see "
              "config.example.yaml), or the older auth.token, or export "
              "RELAY_TOKEN/RELAY_TOKENS. The relay injects the operator's upstream "
              "credential, so it does not run unauthenticated.",
              file=sys.stderr, flush=True)
        return 2

    # S4.2. The run log is a startup setting (see `config.apply_settings`), applied
    # only when a config file or the environment named one. A config that names
    # none must not repoint a caller that already chose its own — the test suite
    # passes `RELAY_RUNS_LOG` and would otherwise be redirected into whatever the
    # default happened to be.
    try:
        if config._settings.get("runs_log_source", "default") != "default":
            core.set_runs_log(config._settings["runs_log"])
    except config.ConfigError as e:
        print(f"refusing to start: {e}", file=sys.stderr, flush=True)
        return 2

    try:
        core.acquire_runs_lock()
    except core.RunLogBusy as e:
        print(f"refusing to start: {e}", file=sys.stderr, flush=True)
        return 2

    # The diagnostic trace is a startup setting, like the run log and for the
    # same reason (one run's frames must not be split across two files). It is
    # off unless a path was named; a path that cannot be opened refuses startup
    # rather than silently serving untraced, because an operator who asked for a
    # trace must not discover an empty file after the failure they wanted it for.
    try:
        if config.DEBUG_LOG:
            trace.enable(config.DEBUG_LOG, config.DEBUG_MAX_BYTES,
                         config.DEBUG_KEEP)
    except OSError as e:
        print(f"refusing to start: cannot open debug_log "
              f"{config.DEBUG_LOG!r}: {e}", file=sys.stderr, flush=True)
        return 2

    wildcard = wildcard_addresses(config.LISTEN)
    if wildcard and not config.ALLOW_PUBLIC_BIND:
        # The protocol says loopback plus Tailscale, never 0.0.0.0. Warning and
        # continuing let a typo expose an endpoint that forwards the operator's
        # upstream credential; refusing makes it a decision instead.
        print(f"refusing to start: {wildcard} bind every interface. That exposes "
              "an endpoint holding the operator's upstream credential. Set "
              "allow_public_bind: true in the config (a WORKSPACE_PROTOCOL Human "
              "decision) if that is really intended.", file=sys.stderr, flush=True)
        return 2

    for addr in config.LISTEN:
        host = addr.rpartition(":")[0]
        if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""):
            print(f"note: binding {host!r} ({addr}), not loopback. Loopback plus "
                  "the Tailscale address is the documented posture.", flush=True)
    for note in startup_notes():
        print(note, flush=True)
    try:
        servers = start_listeners(config.LISTEN)
    except config.ConfigError as e:
        print(f"refusing to start: {e}", file=sys.stderr, flush=True)
        return 2
    BOUND_LISTEN = tuple(addr for addr, _ in servers)

    where = ", ".join(f"http://{addr}" for addr, _ in servers)
    print(f"relay listening on {where}  upstream={config.UPSTREAM}  "
          f"revision={revision()}  "
          f"clients={','.join(entry[0] for entry in config.TOKENS)}  "
          # Where the evidence goes was never in the banner, so "which file am I
          # reading?" needed a doc. It is one of the four things an operator needs
          # at a glance, and the only one that moved in Slice 4.
          f"runs={core.RUNS_LOG}  "
          f"debug={config.DEBUG_LOG or '(off)'}  "
          f"config={config._settings.get('config_path') or '(env only)'}",
          flush=True)

    _drain["servers"] = servers
    _start_reload_thread()
    poller.POLLER.start()

    def on_signal(signum, _frame):
        # A signal handler runs in the main thread, which is inside
        # serve_forever() — and shutdown() waits for serve_forever to return, so
        # calling it here would deadlock the shutdown it is trying to perform.
        # The handler therefore only records the request; the drain thread owns
        # the part that is allowed to block, and the printing.
        if not begin_drain():
            with contextlib.suppress(OSError):
                os.write(2, f"signal {signum} again: exiting now\n".encode())
            os._exit(1)

    def on_hup(_signum, _frame):
        """W0: add or revoke a client token without dropping the process.

        SIGHUP's default action is to terminate, so installing this handler is
        also what stops a well-meant reload from killing the relay. The reload
        thread does the reading; a reload that cannot be parsed keeps the running
        configuration and says so.
        """
        RELOAD_REQUESTED.set()

    # None on Windows: SIGHUP does not exist there and SIGTERM is a hard kill, so
    # `POST /api/reload` and `POST /api/shutdown` are the way in, and Ctrl+C reaches
    # the drain as the KeyboardInterrupt below.
    if os.name != "nt":
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGHUP, on_hup)

    for _, srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        servers[0][1].serve_forever()
    except KeyboardInterrupt:
        on_signal("KeyboardInterrupt", None)
    finally:
        if STOPPING.is_set() and _drain["thread"] is not None:
            # The drain thread owns the listener shutdown and ends with os._exit.
            # Without this join the interpreter can reach teardown first and kill
            # it mid-report, which is how a supervisor loses the one line that
            # says whether anything was cut.
            _drain["thread"].join(config.DRAIN + 5)
        elif not STOPPING.is_set():
            for _, srv in servers:
                srv.shutdown()
                srv.server_close()
    return 0


def _drain_and_exit(servers):
    """Wait for in-flight streams, then leave. Runs on its own thread.

    The settle pause closes the one race the run counter cannot: a connection
    accepted a moment before SIGTERM whose handler had not yet entered a run
    would otherwise be invisible to the drain and cut.

    Whatever raises on the way, the process leaves: a drain that dies on this thread
    leaves a core that refuses work and never exits.
    """
    try:
        print(f"stopping: refusing new requests, draining up to {config.DRAIN}s", flush=True)
        time.sleep(0.25)
        if wait_for_active_runs(config.DRAIN):
            print(f"drained: no stream was cut ({config.DRAIN}s window)",
                  flush=True)
        else:
            print(f"drain window of {config.DRAIN}s expired with "
                  f"{active_run_count()} stream(s) still running; exiting anyway",
                  file=sys.stderr, flush=True)
        for _, srv in servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:  # noqa: BLE001 - we are leaving regardless
                pass
        print("stopped", flush=True)
    finally:
        # os._exit: the request threads are daemons and the loopback sockets are
        # already closed. Trusting interpreter teardown here risks hanging a deploy
        # on a thread that will never join, which is the failure the bound exists for.
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
