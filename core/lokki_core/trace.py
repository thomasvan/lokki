"""An opt-in diagnostic trace of the relay, in the relay.

The run record is the *evidence* file: counts, identifiers, timings, and nothing
the client or the provider wrote. That shape is right for evidence and wrong for
diagnosis — three separate investigations of this relay needed a hand-built proxy
between it and cliproxy before a single rejected request body could be read, and
one of those investigations ended in a port far from the one clients use.

This module is that proxy, built in.

* **Off unless `debug_log:` names a path** (or `RELAY_DEBUG_LOG` does). With no path
  every function here returns immediately, so a caller never has to branch.
* **Bounded.** It rotates at `DEBUG_MAX_BYTES`, keeping `DEBUG_KEEP` files, so a
  trace left on is capped at that size times the count rather than growing until
  someone notices.
* **Refused inside the repository**, by the same guard `runs_log` and
  `capture_dir` use. What it writes is exactly what local law bars from committed
  files: the frames in both directions, the request bodies the relay sent
  upstream, and the provider's error bodies. Treat it the way `capture_dir` is
  treated — a diagnostic that exists while it is being used, 0600, deleted when
  the investigation ends, never committed.
* **Never read back by anything.** The chain verifier and every test
  read `runs.jsonl`; none of them may depend on a file that is off by default.
* **Never raises into a request.** A trace that can break the thing it is
  observing is worse than no trace: every write is best-effort, and a failure is
  counted (`errors`) rather than propagated.
"""

import json
import os
import threading
import time

__all__ = ["Trace", "enable", "get", "disable", "start_run",
           "record_up", "record_down", "record_skip", "record_request",
           "record_provider_error", "record_note", "current_run"]

# One record is capped so a pathological frame cannot fill a disk in a single
# line. Truncation is marked in the record rather than silently applied.
MAX_RECORD_CHARS = 200_000
# What a string value is clipped to when a record is over the cap. A base64 image
# result is the field this exists for; the shape of the record is what a reader
# needs, and the payload is what it does not.
CLIP_VALUE_CHARS = MAX_RECORD_CHARS // 8

_local = threading.local()
_current = None
_next_run = 0
_lock = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _clip(value, limit, where, cut):
    """`value` with every string longer than `limit` clipped, collecting paths.

    Cutting the *serialised* line instead is what this replaced: the cut lands
    wherever it lands, so it can end inside a string, inside a list, or inside
    one of several nested objects, and the one `}` the old code appended closed
    only the innermost — leaving a line no reader can parse. A diagnostic that
    cannot be read back is worse than one that says what it dropped."""
    if isinstance(value, str):
        if len(value) > limit:
            cut.append(f"{where or 'value'}: {len(value)} chars")
            return value[:limit]
        return value
    if isinstance(value, dict):
        return {k: _clip(v, limit, f"{where}.{k}" if where else str(k), cut)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit, f"{where}[{i}]", cut)
                for i, v in enumerate(value)]
    return value


def _clip_line(record):
    """`record` as one JSON line of at most `MAX_RECORD_CHARS`, always parseable."""
    cut = []
    clipped = _clip(record, CLIP_VALUE_CHARS, "", cut)
    if cut:
        clipped["truncated"] = cut
    try:
        line = json.dumps(clipped, ensure_ascii=False, default=repr)
    except Exception:  # noqa: BLE001 - unrepresentable data still gets a line
        line = json.dumps({"ts": record.get("ts"), "dir": record.get("dir"),
                           "unserialisable": True})
    if len(line) > MAX_RECORD_CHARS:
        # Many large fields rather than one: keep what identifies the record and
        # the names of what it carried, and say the values are gone. The names go
        # if they are themselves why the line is long — a record can carry a
        # hundred thousand keys — because the bound is the promise here and the
        # names are a courtesy. `truncated` alone is the floor: it parses, and it
        # is under the cap whatever the record held.
        fallback = {"ts": record.get("ts"), "dir": record.get("dir"),
                    "event": record.get("event"), "run": record.get("run"),
                    "keys": sorted(record),
                    "truncated": [f"whole record: {len(line)} chars"]}
        line = json.dumps(fallback)
        if len(line) > MAX_RECORD_CHARS:
            del fallback["keys"]
            fallback["n_keys"] = len(record)
            line = json.dumps(fallback)
        if len(line) > MAX_RECORD_CHARS:
            line = json.dumps({"truncated": True})
    return line


class Trace:
    """One diagnostic file, written by one process, appended line by line.

    `max_bytes` bounds it: the trace is the one file here that grows with every
    frame, and an investigation that is forgotten must not be able to fill the
    disk. `0` disables rotation, which is what a caller that wants one whole
    capture wants."""

    _CHECK_BYTES = 1_000_000        # the largest gap between size checks

    def __init__(self, path, max_bytes=0, keep=3):
        self.path = path
        self.records = 0
        self.errors = 0
        self.max_bytes = max_bytes
        self.keep = max(1, keep)
        # Check often enough that the overshoot stays a fraction of the cap: a
        # fixed interval would let a small cap be exceeded many times over before
        # the first look.
        self._interval = (min(self._CHECK_BYTES, max(1024, max_bytes // 8))
                          if max_bytes > 0 else 0)
        self._lock = threading.Lock()
        self._fh = None
        self._written = 0           # bytes written since this process opened it
        self._next_check = 0

    # --- lifecycle -----------------------------------------------------------

    def open(self):
        """Create or append the file, 0600. Raises, so a bad path fails at start.

        The path itself is validated by the caller (it must be outside the
        checkout); what can still fail here is the filesystem, and an operator
        who asked for a trace should hear about it before the relay starts
        serving rather than discover an empty file afterwards.
        """
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._fh = os.fdopen(fd, "a", encoding="utf-8")
        self._written = 0
        self._next_check = 0
        return self

    def close(self):
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:  # noqa: BLE001 - shutdown must not raise either
                    pass
                self._fh = None

    # --- writing -------------------------------------------------------------

    def write(self, record):
        """Append one record. Best-effort: a trace must not break the relay."""
        record.setdefault("ts", _now())
        record.setdefault("pid", os.getpid())
        run = current_run()
        if run is not None:
            record.setdefault("run", run)
        try:
            line = json.dumps(record, ensure_ascii=False, default=repr)
        except Exception:  # noqa: BLE001 - unrepresentable data still gets a line
            line = json.dumps({"ts": record.get("ts"), "dir": record.get("dir"),
                               "unserialisable": True})
        if len(line) > MAX_RECORD_CHARS:
            line = _clip_line(record)
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
                self.records += 1
                self._written += len(line) + 1
                self._rotate_if_full()
            except Exception:  # noqa: BLE001 - a full disk is not a request error
                self.errors += 1

    def _rotate_if_full(self):
        """Keep the diagnostic bounded. Called with `self._lock` held.

        The check is driven by bytes written since open rather than by a `stat`
        per record, so a file that is already large is not measured per line. The
        overshoot between checks is one record, itself capped at
        `MAX_RECORD_CHARS`, plus `self._interval`."""
        if self.max_bytes <= 0 or self._written < self._next_check:
            return
        self._next_check = self._written + self._interval
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        self._rotate()

    def _rotate(self):
        """`<path>` -> `<path>.1`, shifting the older ones up, and reopen.

        Named like the run log's rotation so a reader who knows one knows this
        one: `<path>.N … <path>.1, <path>`, oldest first."""
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise either
                pass
            self._fh = None
        for i in range(self.keep - 1, 0, -1):
            src, dst = f"{self.path}.{i}", f"{self.path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(self.path, f"{self.path}.1")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._fh = os.fdopen(fd, "a", encoding="utf-8")

    def frame(self, direction, event, data, note=None):
        """One SSE frame, in one direction, with the fields an operator greps."""
        record = {"dir": direction, "event": event}
        if isinstance(data, dict):
            for key in ("type", "sequence_number", "output_index", "content_index",
                        "item_id"):
                if key in data:
                    record[key] = data[key]
            item = data.get("item")
            if isinstance(item, dict):
                record["item"] = {k: item.get(k)
                                  for k in ("id", "type", "name", "status", "call_id")}
        if note:
            record["note"] = note
        record["data"] = data
        self.write(record)

    def stats(self):
        return {"trace": self.path, "trace_records": self.records,
                "trace_errors": self.errors}


def enable(path, max_bytes=0, keep=3):
    """Turn the trace on for this process. Called once, from `main()`."""
    global _current
    trace = Trace(path, max_bytes=max_bytes, keep=keep).open()
    with _lock:
        _current = trace
    return trace


def get():
    return _current


def disable():
    global _current
    with _lock:
        trace, _current = _current, None
    if trace is not None:
        trace.close()


def start_run(ident):
    """Give this thread's run a process-unique integer id, so lines can be grouped.

    A run id is what turns a 300-line trace into an answer, so it is allocated
    process-wide and only *stored* per thread: one run is served by one thread,
    and several runs may share a thread on a keep-alive connection.
    """
    global _next_run
    with _lock:
        _next_run += 1
        n = _next_run
    _local.run = n
    record_note("run_start", ident=ident)
    return n


def current_run():
    return getattr(_local, "run", None)


# --- the call sites' whole surface ------------------------------------------
# Each is a no-op when the trace is off, which is why no caller branches on it.

def record_up(event, data, note=None):
    if _current is not None:
        _current.frame("up", event, data, note)


def record_down(event, data):
    if _current is not None:
        _current.frame("down", event, data)


def record_skip(event, data, why):
    """A frame the relay deliberately did not write to the client."""
    if _current is not None:
        _current.frame("skipped", event, data, note=why)


def record_request(segment, body_bytes):
    """The body the relay sent upstream. This is client content; see the module
    docstring for why it is allowed here and nowhere else."""
    if _current is None:
        return
    try:
        body = json.loads(body_bytes)
    except Exception:  # noqa: BLE001
        body = {"__unparsed_bytes__": len(body_bytes)}
    _current.write({"dir": "request", "segment": segment, "bytes": len(body_bytes),
                    "body": body})


def record_provider_error(status, body):
    if _current is None:
        return
    record = {"dir": "provider_error", "status": status}
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    record["body"] = body
    _current.write(record)


def record_note(note, **fields):
    if _current is not None:
        record = {"dir": "note", "note": note}
        record.update(fields)
        _current.write(record)
