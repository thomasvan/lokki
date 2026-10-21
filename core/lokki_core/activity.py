"""What the run records hold over a window, for `GET /api/activity`
(docs/app/api.md, "Activity", is the body's contract).

The copy is the only work done under `core._runs_lock`, the lock every `log_run`
appends and rotates under: bytes from the end of the live run log and then its
rotated files, newest first, until the oldest whole line copied is older than the
window or the copy holds `config.RUNS_MAX_BYTES`. While the lock is held only that
line's `ts` is read, by a byte search; the records are parsed and counted after it
is released, since parsing 8 MiB would hold back every append for as long."""
import collections
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from . import config, core, routing

WINDOWS = {"24h": (86400, 3600), "7d": (604800, 21600)}    # span, bucket step, in seconds
CHUNK = 65536
CACHE_SECONDS = 10
RECENT = 50
TOP = 8
ERROR_CHARS = 200
_clock = time.monotonic
# `log_run` writes `json.dumps(rec)` with the default separators, so every record it
# writes holds these bytes before its timestamp; a quote inside a string value is
# escaped, so a value cannot form them. A line without them is not old enough to stop.
_TS = re.compile(rb'"ts": "([^"\\]{1,40})"')
_RELAY_CLASSES = ("finalized", "cut", "client_gone", "error")
_ROUTES = ("relay", "passthrough", "images")
_cache = {}             # window -> (live file size, _clock() at the read, the body without events)
_cache_lock = threading.Lock()


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _epoch(text):
    """A record's `ts`, as `log_run` stamps it (UTC, whole seconds), in epoch seconds;
    None when it is not that."""
    if not isinstance(text, str):
        return None
    try:
        when = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return int(when.replace(tzinfo=timezone.utc).timestamp())


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _name(value):
    return value if isinstance(value, str) and value else "unknown"


def outcome(record):
    """The class a record counts under, which is also its recent row's `outcome`;
    None for a route this version does not write. A record without `route` is a
    relay record, as `core.log_run` counts it."""
    route = record.get("route") or "relay"
    cut = record.get("cut")
    failed = cut == "error" or bool(record.get("error"))
    if route == "relay":
        verdict = record.get("verdict")
        if verdict == "billed" or (verdict is None and record.get("real_completed")):
            return "finalized"
        if failed:
            return "error"
        return "client_gone" if cut == "client_gone" else "cut"
    if route == "passthrough":
        if failed:
            return "error"
        return "client_gone" if cut == "client_gone" else "passed"
    if route == "images":
        status = record.get("status")
        return "error" if record.get("error") or (_number(status) and status >= 400) else "delivered"
    return None


def percentile(values, p):
    """The nearest-rank `p`-th percentile (0 < p <= 100): the value at 1-based rank
    ceil(p·n/100) of the sorted values, so always a value that occurred; None for none."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[-(-p * len(ordered) // 100) - 1]


def _older_than(chunks, at_zero, start):
    """Whether the first whole line of the copy (`chunks`, newest bytes first) has a
    `ts` before `start`. A chunk with no line break holds no new line start, so its
    line was looked at with the chunk before: at most one `_epoch` call per chunk."""
    head = chunks[-1]
    if at_zero:
        begin = 0
    else:
        begin = head.find(b"\n") + 1
        if not begin:
            return False
    line, later = head[begin:], len(chunks) - 2
    while b"\n" not in line and later >= 0:
        line += chunks[later]
        later -= 1
    found = _TS.search(line.split(b"\n", 1)[0])
    if not found:
        return False
    ts = _epoch(found.group(1).decode("utf-8", "replace"))
    return ts is not None and ts < start


def _copy(start):
    """Under the run log's lock: each file's copied bytes, newest file first, as
    (chunks oldest first, whether the copy starts at the file's byte 0); the live
    file's size; and whether the copy stopped at the byte cap with more left to read."""
    cap = config.RUNS_MAX_BYTES
    copies, copied = [], 0
    with core._runs_lock:
        size = core._size(core.RUNS_LOG)
        paths = [core.RUNS_LOG] + list(reversed(core.rotated_runs(core.RUNS_LOG)))
        for n, path in enumerate(paths):
            try:
                f = open(path, "rb")
            except OSError:
                continue
            with f:
                pos = os.fstat(f.fileno()).st_size
                chunks, reached = [], False
                while pos > 0 and copied < cap:
                    step = min(CHUNK, pos, cap - copied)
                    pos -= step
                    f.seek(pos)
                    chunks.append(f.read(step))
                    copied += step
                    if _older_than(chunks, pos == 0, start):
                        reached = True
                        break
            copies.append((chunks[::-1], pos == 0))
            if reached:
                return copies, size, False
            if copied >= cap:
                return copies, size, pos > 0 or n + 1 < len(paths)
    return copies, size, False


def _parse(copies):
    """The JSON objects copied, oldest first, and the count of lines that were not one.
    A copy that starts inside a file drops its first line, which the cut made."""
    objects, skipped = [], 0
    for chunks, at_zero in reversed(copies):
        lines = b"".join(chunks).split(b"\n")
        for raw in lines if at_zero else lines[1:]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, RecursionError):     # RecursionError: a line nested too deep
                obj = None
            if isinstance(obj, dict):
                objects.append(obj)
            else:
                skipped += 1
    return objects, skipped


def _top(counter, key):
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    rows = [{key: name, "runs": runs} for name, runs in ranked[:TOP]]
    if len(ranked) > TOP:
        rows.append({key: "other", "runs": sum(runs for _, runs in ranked[TOP:])})
    return rows


def _row(record, ts, route, kind):
    """A recent row: these fields of the record and no other."""
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    status = record.get("status")
    if route == "relay":
        refused = record.get("upstream_error")
        refused = refused.get("status") if isinstance(refused, dict) else None
        status = refused if refused is not None else record.get("upstream_status")
    error = record.get("error")
    if error is not None:
        error = str(error)[:ERROR_CHARS]
    return {"ts": ts, "route": route, "model": record.get("model"), "client": record.get("client"),
            "mode": record.get("mode"), "cut": record.get("cut"), "verdict": record.get("verdict"),
            "outcome": kind, "status": status, "elapsed_s": record.get("elapsed_s"),
            "delivered_chars": record.get("delivered_chars"), "segs": record.get("segs"),
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "reason": record.get("reason"), "error": error}


def _aggregate(window, now):
    span, step = WINDOWS[window]
    start = now - span
    copies, size, capped = _copy(start)
    objects, skipped = _parse(copies)

    buckets = [{"start": start + k * step, "relay": 0, "passthrough": 0, "images": 0, "errors": 0}
               for k in range(span // step)]
    totals = {"requests": 0, "relay": 0, "passthrough": 0, "images": 0, "errors": 0}
    relay = {"finalized": 0, "cut": 0, "client_gone": 0, "error": 0, "by_cut": {}}
    passthrough = {"by_reason": {}, "by_cut": {}}
    tokens = {"runs": 0, "input": 0, "output": 0, "cached": 0, "reasoning": 0}
    models, clients = collections.Counter(), collections.Counter()
    elapsed = {route: [] for route in _ROUTES}
    newest = collections.deque(maxlen=RECENT)
    oldest, records, undated, reached = None, 0, 0, False
    for record in objects:
        ts = _epoch(record.get("ts"))
        if ts is None:
            undated += 1
            continue
        oldest = ts if oldest is None else min(oldest, ts)
        if ts < start:
            reached = True
            continue
        if ts > now:
            continue
        records += 1
        kind = outcome(record)
        if kind is None:
            continue
        route = record.get("route") or "relay"
        bucket = buckets[min((ts - start) // step, len(buckets) - 1)]
        bucket[route] += 1
        totals[route] += 1
        totals["requests"] += 1
        if kind == "error":
            bucket["errors"] += 1
            totals["errors"] += 1
        if route == "relay":
            relay[kind] += 1
            if kind == "cut":
                cut = _name(record.get("cut"))
                relay["by_cut"][cut] = relay["by_cut"].get(cut, 0) + 1
        elif route == "passthrough":
            for field, table in (("reason", "by_reason"), ("cut", "by_cut")):
                value = _name(record.get(field))
                passthrough[table][value] = passthrough[table].get(value, 0) + 1
        usage = record.get("usage")
        if isinstance(usage, dict):
            tokens["runs"] += 1
            tokens["input"] += _count(usage.get("input_tokens"))
            tokens["output"] += _count(usage.get("output_tokens"))
            for total, details, field in (("cached", "input_tokens_details", "cached_tokens"),
                                          ("reasoning", "output_tokens_details", "reasoning_tokens")):
                nested = usage.get(details)
                tokens[total] += _count(nested.get(field) if isinstance(nested, dict) else usage.get(field))
        models[_name(record.get("model"))] += 1
        clients[_name(record.get("client"))] += 1
        if _number(record.get("elapsed_s")):
            elapsed[route].append(record["elapsed_s"])
        newest.append((record, ts, route, kind))

    def latency(values):
        return {"p50": percentile(values, 50), "p90": percentile(values, 90)}

    body = {
        "window": window, "now": now, "from": start,
        "coverage": {"oldest": oldest, "records": records, "undated": undated, "skipped": skipped,
                     "complete": not capped or reached},
        "buckets": buckets, "totals": totals, "relay": relay, "passthrough": passthrough,
        "tokens": tokens, "models": _top(models, "model"), "clients": _top(clients, "client"),
        "latency": dict({route: latency(elapsed[route]) for route in _ROUTES},
                        all=latency([v for route in _ROUTES for v in elapsed[route]])),
        "recent": [_row(*c) for c in reversed(newest)],
    }
    return body, size


def answer(window, now=None):
    """The whole `/api/activity` body for `window` (a `WINDOWS` key; KeyError for any
    other). The rest is cached `CACHE_SECONDS` per window and live file size, which a
    write or a rotation changes; `events` is read fresh every time."""
    WINDOWS[window]
    now = int(time.time()) if now is None else now
    size = core._size(core.RUNS_LOG)
    with _cache_lock:
        hit = _cache.get(window)
    if hit is not None and hit[0] == size and _clock() - hit[1] < CACHE_SECONDS:
        body = hit[2]
    else:
        at = _clock()
        body, size = _aggregate(window, now)
        with _cache_lock:
            _cache[window] = (size, at, body)
    return dict(body, events=routing.events())
