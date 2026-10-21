"""The image routes: `POST /v1/images/generations` and `POST /v1/images/edits`.

The cliproxy behind the relay serves them (measured on the clone, 2026-10-08: the models
`gpt-image-2`, `gpt-image-2.5`, `gpt-image-1.5` and the two `2.5` variants, JSON for a
generation, JSON or multipart for an edit, a server-sent stream when the request asks for
one). The relay puts in front what it puts in front of every other route and changes nothing
else: its token gate, its own upstream credential in place of the caller's, the drain on
SIGTERM, one of the upstream slots and the pacing gap, and a record in the run log. The
request goes up as it came and the answer comes back as it came, so a client that talks to
the cliproxy talks to the relay by changing the address and the key.

What the relay does not do here, on purpose:

- **No retry.** The text route retries a 429, 502 or 503 that arrived before any output,
  because then nothing was done. An image request that fails at a gateway may have been
  generated and billed already, and a second attempt would bill it again, so the status goes
  back to the caller, who chose to ask.
- **No cut.** There is nothing to cut: an image is billed when it is generated, and the
  `imagegen_cut` and `payload_cap` settings belong to the Responses route, where the
  provider's hosted image tool opens inside a text turn.
- **No content in the record.** The record names the route, the client, the model, the
  size, the counts and the token usage the cliproxy reported; never the prompt, a revised
  prompt, an image or a file name.
"""

import http.client
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from . import config, engine
from .core import error_payload, log_run, sse_send
from .sse import iter_sse_frames

__all__ = ["IMAGE_PATHS", "new_record", "serve"]

IMAGE_PATHS = ("/v1/images/generations", "/v1/images/edits")

# Image calls take their own slots, `MAX_CONCURRENT_IMAGES` of them, and not the text route's
# `max_concurrent_upstream`: one image holds its slot for 30 to 40 s, and a handful of them
# must not queue the Codex traffic behind them. The pacing gap is shared (`engine.pace`).
_slots_lock = threading.Lock()
_slots = None
_slots_for = None


@contextmanager
def image_slot():
    global _slots, _slots_for
    with _slots_lock:
        if _slots is None or _slots_for != config.MAX_CONCURRENT_IMAGES:
            _slots = threading.BoundedSemaphore(max(1, config.MAX_CONCURRENT_IMAGES))
            _slots_for = config.MAX_CONCURRENT_IMAGES
        slots = _slots           # the one acquired is the one released, as in engine.upstream_slot
    slots.acquire()
    try:
        yield
    finally:
        slots.release()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The cliproxy is trusted with the relay's credential; wherever it might send the request
    on is not. A redirect is an answer the relay does not follow (see `_answer`)."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

# The upstream's headers that go back to the caller. An allow-list: whatever else the
# cliproxy sends (a cookie, a server banner) is not the relay's to pass on.
_ANSWER_HEADERS = ("Retry-After",)

# A request's settings enter the record only when they have the shape of a setting, the same
# rule the run log keeps for `model` and for the names of dropped parameters: a client
# chooses these values, and the record is a file that must not carry what a client wrote.
_IDENT = re.compile(r"[A-Za-z0-9_.:/()-]{1,100}")
_SIZE = re.compile(r"auto|[0-9]{2,5}x[0-9]{2,5}")
_WORD = re.compile(r"[a-z0-9_-]{1,32}")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _boundary(content_type):
    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type or "", re.IGNORECASE)
    return (m.group(1) or m.group(2)).encode("latin-1", "replace") if m else None


def _form_value(raw, name, boundary):
    """The value of a simple multipart field, or None. Found by position in the bytes: the
    body may be 30 MB of image, and parsing it into parts would copy all of it. Only a part's
    own header counts, the boundary line and then `Content-Disposition`, so text inside another
    part's value (a prompt that quotes a field header) is not read as a field."""
    if not boundary:
        return None
    marker = b"--" + boundary + b'\r\nContent-Disposition: form-data; name="' + name + b'"\r\n\r\n'
    at = raw.find(marker)
    if at < 0:
        return None
    start = at + len(marker)
    end = raw.find(b"\r\n--" + boundary, start)
    return raw[start:end if end >= 0 else start + 200]


def _setting(value, shape):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value if isinstance(value, str) and shape.fullmatch(value) else None


def _count(value):
    """A small whole number a client chose (`n`), or None. Never raises: it runs before the
    request is answered, and a value it cannot read is the cliproxy's to refuse."""
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    if isinstance(value, str):
        value = value.strip()
        value = int(value) if value.isascii() and value.isdigit() and len(value) <= 6 else None
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000 else None


def new_record(path, raw, content_type, client):
    """The run record of an image request, as far as the request tells: settings that have
    the shape of a setting, and sizes. The answer fills in the rest."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    rec = {
        "route": "images",
        "path": path,
        "client": client,
        "request_kind": "multipart" if ctype == "multipart/form-data" else
                        "json" if ctype in ("application/json", "") else "other",
        "request_bytes": len(raw),
        "model": None, "n": None, "size": None, "quality": None, "stream": None,
        "prompt_chars": None, "images_in": None,
        "status": None,
        "response_bytes": 0,
        "images_out": None,
        "events": 0,
        "usage": None,
        "usage_source": None,
        "error": None,
        "upstream_error": None,
        "client_gone": None,
        "elapsed_s": None,
    }
    if rec["request_kind"] == "json":
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if isinstance(body, dict):
            rec["model"] = _setting(body.get("model"), _IDENT)
            rec["n"] = _count(body.get("n"))
            rec["size"] = _setting(body.get("size"), _SIZE)
            rec["quality"] = _setting(body.get("quality"), _WORD)
            rec["stream"] = body.get("stream") if isinstance(body.get("stream"), bool) else None
            if isinstance(body.get("prompt"), str):
                rec["prompt_chars"] = len(body["prompt"])
            if isinstance(body.get("images"), list):
                rec["images_in"] = len(body["images"])
            elif isinstance(body.get("image"), (str, dict)):
                rec["images_in"] = 1
    elif rec["request_kind"] == "multipart":
        boundary = _boundary(content_type)
        rec["model"] = _setting(_form_value(raw, b"model", boundary), _IDENT)
        rec["n"] = _count(_form_value(raw, b"n", boundary))
        rec["size"] = _setting(_form_value(raw, b"size", boundary), _SIZE)
        rec["quality"] = _setting(_form_value(raw, b"quality", boundary), _WORD)
        stream = _form_value(raw, b"stream", boundary)
        rec["stream"] = (stream.strip() == b"true") if stream is not None else None
        prompt = _form_value(raw, b"prompt", boundary)
        rec["prompt_chars"] = len(prompt.decode("utf-8", "replace")) if prompt is not None else None
        rec["images_in"] = (raw.count(b"--" + boundary + b'\r\nContent-Disposition: form-data; name="image')
                            if boundary else 0) or None
    return rec


def _usage(obj):
    """The token counts of an upstream `usage` object, and nothing else: whole numbers
    under identifier-shaped names, one level of nesting."""
    if not isinstance(obj, dict):
        return None
    out = {}
    for key, value in obj.items():
        if not isinstance(key, str) or not _NAME.fullmatch(key):
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
        elif isinstance(value, dict):
            inner = {k: v for k, v in value.items()
                     if isinstance(k, str) and _NAME.fullmatch(k)
                     and isinstance(v, int) and not isinstance(v, bool)}
            if inner:
                out[key] = inner
    return out or None


def _usage_in_body(body):
    """`usage` from a JSON answer, found without parsing the images: the body may hold tens of
    megabytes of base64, and the object is a few hundred bytes after its key."""
    at = body.rfind(b'"usage"')
    if at < 0:
        return None
    tail = body[at + len(b'"usage"'):at + len(b'"usage"') + 4096].decode("utf-8", "replace").lstrip()
    if not tail.startswith(":"):
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(tail[1:].lstrip())
    except ValueError:
        return None
    return _usage(obj)


def _error_identifiers(body):
    """`code` and `type` of the cliproxy's error object, when they are identifiers. Never its
    message, which can quote the prompt."""
    try:
        err = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return None
    if not isinstance(err, dict):
        return None
    return {k: err[k] for k in ("code", "type")
            if isinstance(err.get(k), str) and _NAME.fullmatch(err[k])} or None


class _Once:
    """Writes the run record once, however the request ends."""

    def __init__(self, rec, started):
        self.rec, self.started, self.done = rec, started, False

    def __call__(self):
        if not self.done:
            self.done = True
            self.rec["elapsed_s"] = round(time.monotonic() - self.started, 3)
            log_run(self.rec)


class _UpstreamBroke(Exception):
    """The cliproxy's connection failed after it had begun to answer."""


def serve(h, path, raw, rec):
    """Forward one image request and answer the caller. `h` is the request handler; `rec`
    comes from `new_record` and is written to the run log before the caller sees an answer
    (a stream: when it ends), the order the text route keeps. `elapsed_s` includes the wait
    for a slot and for the pacing gap. If the run log cannot be written the caller gets a
    500 and the image is lost, as a text run is: the evidence comes first."""
    headers = {"Authorization": "Bearer " + config.API_KEY,
               "Content-Type": h.headers.get("Content-Type") or "application/json"}
    if h.headers.get("Accept"):
        headers["Accept"] = h.headers["Accept"]
    req = urllib.request.Request(config.UPSTREAM + path, data=raw, headers=headers, method="POST")
    log = _Once(rec, time.monotonic())
    try:
        with image_slot():
            engine.pace()
            _answer(h, req, rec, log)
    finally:
        log()


def _answer(h, req, rec, log):
    try:
        resp = _OPENER.open(req, timeout=config.IMAGE_TIMEOUT)
    except urllib.error.HTTPError as e:
        resp = e       # a refusal is an answer: its status and body go back as they are
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        rec["error"] = type(getattr(e, "reason", None) or e).__name__
        log()
        h._error(502, "the image service did not answer; the relay's log has the detail")
        print(f"image request to the upstream failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return
    with resp:
        status = getattr(resp, "status", None) or resp.code
        rec["status"] = status
        ctype = resp.headers.get("Content-Type") or ""
        if 300 <= status < 400:
            rec["error"] = "redirect"
            log()
            h._error(502, "the image service sent a redirect; the relay does not follow one")
        elif status < 400 and ctype.lower().startswith("text/event-stream"):
            _stream(h, resp, rec, log)
        else:
            _buffered(h, resp, status, ctype, rec, log)


def _buffered(h, resp, status, ctype, rec, log):
    """Any answer that is not a stream (a result, or a refusal): read whole (up to a cap),
    recorded, then sent. Read whole so that a connection that breaks halfway is a 502 and not
    a body that stops short. `status` in the record is the cliproxy's; when `error` is set the
    caller was answered 502 instead."""
    limit = config.MAX_IMAGE_RESPONSE_BYTES
    try:
        body = resp.read(limit + 1)
        # `read(n)` returns what arrived when the connection closes early; only a read with no
        # size raises. A declared length that was not met is the same break, so it is raised.
        declared = resp.headers.get("Content-Length", "")
        if declared.isdigit() and len(body) <= limit and len(body) != int(declared):
            raise http.client.IncompleteRead(body, int(declared) - len(body))
    except (OSError, http.client.HTTPException) as e:
        rec["error"] = type(e).__name__
        log()
        h._error(502, "the image service's answer broke off; the relay's log has the detail")
        return
    if len(body) > limit:
        rec["error"] = "response_too_large"
        log()
        h._error(502, f"the image service's answer is over {limit} bytes; the relay does not carry it")
        return
    rec["response_bytes"] = len(body)
    if status < 400:
        rec["usage"] = _usage_in_body(body)
        rec["usage_source"] = "provider" if rec["usage"] else None
        rec["images_out"] = body.count(b'"b64_json"')
    else:
        rec["upstream_error"] = _error_identifiers(body)
    log()
    h.send_response(status)
    h.send_header("Content-Type", ctype or "application/json")
    h.send_header("Content-Length", str(len(body)))
    for name in _ANSWER_HEADERS:
        if resp.headers.get(name):
            h.send_header(name, resp.headers[name])
    h.end_headers()
    h.wfile.write(body)       # a caller that has gone raises here; the handler lets it go


def _lines(resp, wfile, rec):
    """The cliproxy's stream, line by line, passed to the caller as it arrives."""
    while True:
        try:
            line = resp.readline()
        except (OSError, http.client.HTTPException) as e:
            raise _UpstreamBroke(type(e).__name__) from e
        if not line:
            return
        wfile.write(line)           # a gone caller raises here, and ends the stream
        rec["response_bytes"] += len(line)
        yield line


def _stream(h, resp, rec, log):
    """A stream (`stream: true`): sent as it arrives, ended by the connection's close. The
    record is written when it ends; the last event carries the usage."""
    h.close_connection = True
    images = 0
    try:
        h.send_response(rec["status"])
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Connection", "close")
        h.end_headers()
        h._response_started = True
        for event, data in iter_sse_frames(_lines(resp, h.wfile, rec)):
            rec["events"] += 1
            if event and event.endswith(".completed"):
                images += 1
            if data and '"usage"' in data:
                try:
                    found = _usage(json.loads(data).get("usage"))
                except (ValueError, AttributeError):
                    found = None
                if found:
                    rec["usage"], rec["usage_source"] = found, "provider"
    except OSError:
        # A write to the caller failed (it left, or stopped reading and the socket timed out).
        # The upstream's own failures are `_UpstreamBroke`, raised by `_lines`.
        rec["client_gone"] = True
    except _UpstreamBroke as e:
        # The headers are out, so the only honest signal left is an error event: a stream
        # that stops without one reads as a short success.
        rec["error"] = str(e)
        try:
            sse_send(h.wfile, "error", error_payload(
                "the image service's stream broke off; the relay's log has the detail"))
        except Exception:  # noqa: BLE001 - nobody left to tell
            pass
    rec["images_out"] = images          # `*.completed` events: one per finished image
