"""A request the decision sends straight through: the client's bytes to
CLIProxyAPI with the core's credential, the answer back byte for byte, one run
record. No instructions, no padding tool, no marker scan; retry only before the
first byte; the record in a finally."""
import http.client
import json
import sys
import time
import urllib.error
import urllib.request

from . import config, core, engine
from .core import log_run, summarise_usage
from .sse import iter_sse_frames

ANSWER_HEADERS = ("Content-Type", "Retry-After", "x-request-id")
# Statuses about the core's own upstream credential, not the client's request:
# passed on, they would tell the client its token is bad (the relay's
# non-streaming path maps the same three).
CREDENTIAL_STATUSES = (401, 403, 407)
_CHUNK = 64 * 1024
_TERMINALS = ("response.completed",) + engine.PROVIDER_TERMINALS


def _body_for_upstream(raw, payload, knobs):
    """The raw bytes unless a relay knob is present; then the parsed body without
    the knobs, re-serialized. Returns (bytes, reserialized)."""
    present = [k for k in knobs if k in payload]
    if not present:
        return raw, False
    cleaned = {k: v for k, v in payload.items() if k not in knobs}
    return json.dumps(cleaned, ensure_ascii=False).encode(), True


def _headers(h):
    headers = {"Authorization": f"Bearer {config.API_KEY}",
               "Content-Type": h.headers.get("Content-Type") or "application/json"}
    for name in engine.FORWARDED_HEADERS:
        value = h.headers.get(name)
        if value and name not in headers:
            headers[name] = value
    return headers


class _Scanner:
    """Looks at the bytes on their way to the client for the terminal event and
    its usage; never alters them. Frames are parsed by `sse.iter_sse_frames`, the
    one SSE parser (CRLF, multi-line `data:`), fed one complete frame at a time."""

    # A terminal frame carries the response's output (an image item can be
    # megabytes), so the caps are generous: a line or a frame beyond them is not
    # scanned, the stream still reaches the client whole, and the record says
    # `eof` with no usage rather than holding the whole stream in memory.
    MAX_LINE = 16 << 20
    MAX_FRAME_LINES = 4096

    def __init__(self):
        self.buf = bytearray()      # the unfinished line; appended in place, a long line arrives in many reads
        self.lines = []             # the unfinished frame
        self.skipping = False       # the unfinished line went past MAX_LINE: dropped up to its newline
        self.terminal = None
        self.usage = None

    def feed(self, chunk):
        self.buf += chunk
        end = self.buf.rfind(b"\n") if b"\n" in chunk else -1
        if end >= 0:
            done = bytes(self.buf[:end]).split(b"\n")
            del self.buf[:end + 1]
            if self.skipping:
                del done[0]
                self.skipping = False
            for line in done:
                if len(self.lines) < self.MAX_FRAME_LINES:
                    self.lines.append(line)
                if line.rstrip(b"\r") == b"":
                    for _, data in iter_sse_frames(self.lines):
                        self._frame(data)
                    self.lines = []
        if len(self.buf) > self.MAX_LINE:
            self.buf.clear()                        # a line past the cap is not kept, nor scanned
            self.skipping = True

    def _frame(self, data):
        try:
            obj = json.loads(data) if data else None
        except ValueError:
            return
        t = obj.get("type") if isinstance(obj, dict) else None
        if t in _TERMINALS:
            response = obj.get("response")
            self.terminal = t
            self.usage = summarise_usage(response.get("usage") if isinstance(response, dict) else None)


def serve(h, raw, payload, decision):
    from .handler import RELAY_KNOB_PARAMS, _MODEL_NAME   # local: handler imports this module
    # The record says what the client sent, which is what CLIProxyAPI sees: an
    # absent `model` or `stream` is None, not the relay's default.
    model = payload.get("model")
    if model is not None and (not isinstance(model, str) or not _MODEL_NAME.match(model)):
        raise core.ResponseError(400, "'model' must be a model id: letters, digits and . _ : / ( ) -, at most 100 characters")
    stream = payload.get("stream")
    body, reserialized = _body_for_upstream(raw, payload, RELAY_KNOB_PARAMS)
    rec = {"route": "passthrough", "reason": decision.reason, "model": model,
           "client": getattr(h, "client_name", None), "reserialized": reserialized,
           "stream": stream if isinstance(stream, bool) else None, "upstream_input_chars": len(body),
           "upstream_requests": 0, "status": None, "cut": None, "usage": None, "usage_source": None}
    started = time.monotonic()
    req = urllib.request.Request(config.UPSTREAM + "/v1/responses", data=body, headers=_headers(h), method="POST")
    try:
        with engine.upstream_slot():
            try:
                resp = engine.open_upstream(req, rec, config.RETRIES)
            except urllib.error.HTTPError as e:
                resp = e                                   # a refusal is an answer: passed through as it is
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                rec["error"] = type(getattr(e, "reason", None) or e).__name__
                rec["cut"] = "error"
                rec["upstream_requests"] = rec.get("attempts", 0)
                # Unknown → direct with CLIProxyAPI down is the fail-open case: every
                # request is a full-price one while the endpoint looks healthy, so it is
                # said. The exception's text, never the request or the credential.
                print(f"[passthrough] CLIProxyAPI did not answer ({rec['error']}): "
                      f"{getattr(e, 'reason', e)}", file=sys.stderr, flush=True)
                h._error(502, "CLIProxyAPI did not answer; the relay's log has the detail")
                return
            rec["upstream_requests"] = rec.get("attempts", 1)
            try:
                _relay_response(h, resp, rec)
            except Exception as e:
                # Not one of the failures named below; the handler's catch-all answers
                # the client, and the record still says the answer did not end well.
                rec["error"], rec["cut"] = type(e).__name__, "error"
                raise
    finally:
        rec["elapsed_s"] = round(time.monotonic() - started, 3)
        log_run(rec)


def _send_head(h, resp, status, length=None):
    h.send_response(status)
    for name in ANSWER_HEADERS:
        if resp.headers.get(name):
            h.send_header(name, resp.headers[name])
    if length is not None:
        h.send_header("Content-Length", str(length))
    h.send_header("Connection", "close")
    h.end_headers()
    h._response_started = True


def _relay_response(h, resp, rec):
    status = getattr(resp, "status", None) or resp.code
    rec["status"] = status
    if status in CREDENTIAL_STATUSES:
        rec["cut"] = "error"
        with resp:
            pass
        h._error(502, "CLIProxyAPI refused the relay's credential; the relay's log has the detail")
        return
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if not ctype.startswith("text/event-stream"):
        # One body, read whole before the status goes out: a read that breaks off
        # is still a 502 the client can see.
        try:
            with resp:
                body = resp.read()
        except (OSError, http.client.HTTPException) as e:
            rec["error"], rec["cut"] = type(e).__name__, "error"
            h._error(502, "CLIProxyAPI's answer broke off; the relay's log has the detail")
            return
        _send_head(h, resp, status, len(body))
        if status < 400:
            try:
                obj = json.loads(body)
                rec["usage"] = summarise_usage(obj.get("usage")) if isinstance(obj, dict) else None
            except ValueError:
                pass
        rec["usage_source"] = "provider" if rec["usage"] else None
        rec["cut"] = "error" if status >= 400 else "terminal"
        _write(h, body, rec)
        return
    _send_head(h, resp, status)
    scanner = _Scanner()
    try:
        with resp:
            while True:
                chunk = resp.read1(_CHUNK)           # read1: what has arrived, not a full _CHUNK (a stream is live)
                if not chunk:
                    break
                scanner.feed(chunk)
                _write(h, chunk, rec)
                if rec.get("cut") == "client_gone":
                    return
    except (OSError, http.client.HTTPException) as e:
        rec["error"], rec["cut"] = type(e).__name__, "error"
        return
    rec["usage"] = scanner.usage
    rec["usage_source"] = "provider" if scanner.usage else None
    rec["cut"] = "error" if status >= 400 else "terminal" if scanner.terminal else "eof"


def _write(h, data, rec):
    try:
        h.wfile.write(data)
        h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
        rec["cut"] = "client_gone"
