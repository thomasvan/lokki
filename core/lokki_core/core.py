"""Shared helpers: paths, the run log, input handling and SSE writing.

Nothing here reaches upstream or speaks HTTP; this is the layer both the engine
and the request handler depend on.

Input is handled two ways and the difference matters. `normalise_input` validates
a client's `input` and returns it **unchanged**, because the relay forwards it
verbatim so roles, tool calls and tool results survive. `extract_prompt` flattens
it to text, and is only for the `/api/run` path that takes a plain prompt.
Flattening a real client's input loses its roles and silently drops every
non-text item, including the tool results the client is sending back.
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid
from . import lock
try:
    import yaml
except ImportError:  # config file is optional; env vars alone are enough
    yaml = None

# Package lives one level below the data directory that holds config.yaml and
# runs.jsonl.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _text(value):
    """A setting's text, with "unset" and "blank" meaning the same thing.

    `config._text` is the same function for the config file; both resolutions have
    to agree on that, or a whitespace-only value takes one path in one place and
    another path in the other."""
    return str(value).strip() if value is not None else ""


def _repo_root(here=HERE, env=None):
    """The checkout this package lives in, or None.

    Evidence must not be written inside a checkout (`inside_repo`). A checkout is
    recognised by `.git` in the parent of `here`: a directory in a clone, a file
    in a git worktree. Only where there is none may `RELAY_REPO_ROOT` name the root
    (the image's test stage builds from `git archive` and sets it, so the suite's
    guard tests run there too). A PyInstaller bundle and the runtime image have
    neither, so there the guard has nothing to guard and is off."""
    env = os.environ if env is None else env
    root = os.path.dirname(here)
    if os.path.exists(os.path.join(root, ".git")):
        return root                      # a checkout: the variable cannot move or disarm the guard
    named = _text(env.get("RELAY_REPO_ROOT"))
    return os.path.abspath(named) if named else None


# The checkout this package lives in, or None. See `_repo_root`, `inside_repo`
# and `refuse_inside_repo`.
REPO_ROOT = _repo_root()


def default_runs_log(env=None):
    """Where evidence goes when nothing names a path: XDG state, not the checkout.

    The relay appends to this file while it serves, so a default inside the working
    tree would race every `git checkout`, `stash`, `clean` or `add`: a `checkout -f`
    drops records silently, and a test that forgot `RELAY_RUNS_LOG` writes a *false*
    record into a committed file, which is the one thing this module exists to
    prevent.

    Takes the environment to read rather than reaching for `os.environ`, so
    `config.load_settings(env=…)` resolves the default from the mapping it was
    handed. Reading `os.environ` here instead made `RELAY_RUNS_LOG` follow the `env`
    argument while `XDG_STATE_HOME` silently did not — the swe-2 reader's finding
    F3 in spirit, and untestable besides.

    The committed `runs.jsonl` stays as the historical seed it now is: read-only
    from the relay's point of view, and still the file the docs cite."""
    env = os.environ if env is None else env
    state = (_text(env.get("XDG_STATE_HOME"))
             or os.path.join(os.path.expanduser("~"), ".local", "state"))
    return os.path.join(state, "cliproxy", "runs.jsonl")


DEFAULT_RUNS_LOG = default_runs_log()
# Overridable (`RELAY_RUNS_LOG`) so a second instance keeps its own chain, and so
# tests never touch real evidence. `_text` because
# `RELAY_RUNS_LOG='   '` is truthy and would otherwise become a run log literally
# named three spaces: `load_settings` strips it and resolves the default, so the
# process would run with source "default" while `RUNS_LOG` was that string — the
# lock file `"   .lock"` in the cwd, every run refused as inside the checkout, and
# a heartbeat anchoring `"   "`. The two resolutions have to agree on what "unset"
# means.
RUNS_LOG = _text(os.environ.get("RELAY_RUNS_LOG")) or DEFAULT_RUNS_LOG


def inside_repo(path):
    """Is `path` inside this repository's working tree?

    `realpath`, not `abspath`: abspath normalises `.` and `..` but follows no
    symlinks, so a link outside the repository pointing inside it was accepted and
    the file landed somewhere `git add` can reach. The swe-2 reader found that by
    writing through exactly such a link, in the `capture_dir` guard this now
    shares."""
    if REPO_ROOT is None:
        return False
    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    root = os.path.realpath(REPO_ROOT)
    try:
        return os.path.commonpath([resolved, root]) == root
    except ValueError:      # different drives, or an empty path
        return False


def refuse_inside_repo(path, what, fix):
    """Return the resolved `path`, or refuse it for being in the checkout.

    One implementation for both callers — the run log and `capture_dir` — because
    the symlink hole above was in a copy of this logic, and a second copy is where
    the next one would hide."""
    from . import config     # local: config imports this module

    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    if inside_repo(resolved):
        raise config.ConfigError(
            f"{what} {str(path)!r} is inside the repository; {fix}")
    return resolved
CONFIG_PATH = os.environ.get("RELAY_CONFIG", os.path.join(HERE, "config.yaml"))
# Default listen posture: loopback only. Multi-address binds (for example loopback
# plus this machine's Tailscale address) come from config.yaml.
_DEFAULT_LISTEN = ("127.0.0.1:8371",)


class ResponseError(Exception):
    """A client fault the handler must answer with a real HTTP status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# The error `type` values OpenAI clients actually switch on. Anything else and a
# client cannot tell a relay fault from a protocol violation.
ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "server_error",
    503: "server_error",
}


def error_object(message, status=None, err_type=None, param=None, code=None):
    """The OpenAI-shaped error envelope: {message, type, param, code}."""
    if err_type is None:
        err_type = ERROR_TYPES.get(status, "server_error")
    return {"error": {"message": message, "type": err_type,
                      "param": param, "code": code}}


def error_payload(message, code=None, param=None, err_type="server_error", seq=None):
    """The SSE `error` event payload.

    The Responses API dispatches stream events on a top-level `type`, so a bare
    `{"error": {...}}` is dropped by a generated SDK even though the SSE event
    name is `error`. The nested envelope is kept alongside it for clients that
    read `data.error`.

    `seq` carries the same `sequence_number` every neighbouring frame does. It is
    optional because the handler's last-resort `_sse_error` fires when the engine
    never got to number anything; where a stream exists, the caller passes one.
    """
    payload = {"type": "error", "code": code, "param": param, "message": message,
               "error": {"message": message, "type": err_type, "param": param,
                         "code": code}}
    if seq is not None:
        payload["sequence_number"] = seq
    return payload


def is_client_gone(exc):
    """True for the connection-family errors a *write to the client* can raise.

    Trap worth naming: `socket.error` **is** `OSError`, and
    `urllib.error.URLError` — and therefore `HTTPError` — subclasses `OSError`,
    so a bare `except OSError` around the segment loop swallows upstream failures
    and relabels them as client disconnects.

    But the type alone is not enough either: the *same* `ConnectionResetError` is
    raised by a reset on the upstream read and by a failed client write, and they
    mean opposite things. That is why the engine classifies by **site**: every
    write to the client goes through `State.send`, which raises `ClientGone`. This
    helper is only used there. Do not use it to classify an arbitrary exception
    out of the segment loop.
    """
    return isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError, TimeoutError))


class ClientGone(Exception):
    """A write to the client failed.

    A distinct type on purpose: an upstream reset is an incident an operator has
    to see, and a client that hung up is not. Classifying by exception type alone
    cannot tell them apart, and getting it wrong makes every upstream failure look
    like an abandoned request.
    """


def summarise_usage(usage):
    """Whitelist what leaves the upstream `usage` object.

    This lands in a **committed** evidence file, and parts of the object are
    upstream-controlled text: `attribution.items[*].content[*]` is a free-form
    list that today happens to hold only token counts. Copying it whole means the
    client's prompt or tool arguments reach git the day the provider puts text
    there — and a negative test over the *request* body cannot catch that. The
    shape is whitelisted rather than trusted.
    """
    if not isinstance(usage, dict):
        return None

    def counts(d):
        if not isinstance(d, dict):
            return {}
        return {k: d[k] for k in ("input_tokens", "output_tokens", "total_tokens",
                                  "cached_tokens", "cache_write_tokens",
                                  "reasoning_tokens")
                if isinstance(d.get(k), int)}

    out = counts(usage)
    for key, fields in (("input_tokens_details",
                         ("cached_tokens", "cache_write_tokens")),
                        ("output_tokens_details", ("reasoning_tokens",))):
        details = usage.get(key)
        if isinstance(details, dict):
            kept = {k: details[k] for k in fields
                    if isinstance(details.get(k), int)}
            if kept:
                out[key] = kept

    attribution = usage.get("attribution")
    if isinstance(attribution, dict):
        items = attribution.get("items")
        if isinstance(items, dict):
            out["attribution"] = {"items": {
                item_id: counts(entry) for item_id, entry in items.items()
                if isinstance(entry, dict)}}
    return out


class _BufSink:
    """wfile-shaped buffer so run_upstream can write SSE into memory."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b

    def flush(self):
        pass


def _text_blocks(content):
    """Extract text from one message's content, which may be a string or a list
    of typed blocks. Unknown block shapes are a client fault, not a silent drop."""
    if content is None:
        return []
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        raise ResponseError(400, "input message content must be a string or a list")
    out = []
    for c in content:
        if not isinstance(c, dict):
            raise ResponseError(400, "input content blocks must be objects")
        t = c.get("text")
        if t is None:
            # Non-text blocks (images, files) carry no prompt text; skip them.
            continue
        if not isinstance(t, str):
            raise ResponseError(400, "input content block 'text' must be a string")
        out.append(t)
    return out


def extract_prompt(inp):
    """Flatten a Responses-API `input` to plain text for the `/api/run` path.

    Accepts a bare string or a list of {role, content} messages. **This loses
    structure**: only the text of each message survives, joined by newlines, and
    every non-text block is skipped. Roles are NOT preserved in the output — an
    earlier version of this docstring claimed they were, which was wrong. Do not
    use this on a real client's input; use `normalise_input`.
    """
    if isinstance(inp, str):
        return inp
    if not isinstance(inp, list):
        return None
    parts = []
    for msg in inp:
        if not isinstance(msg, dict):
            raise ResponseError(400, "each input item must be a message object")
        parts.extend(_text_blocks(msg.get("content")))
    return "\n".join(parts)


def normalise_input(inp):
    """Validate a Responses-API `input` and return it unchanged, for forwarding.

    The relay must not rebuild what the client sent. A message list keeps its
    roles, and items like `function_call` and `function_call_output` — which have
    no `content.text` at all — must reach upstream intact. Anything malformed is
    a 400 here rather than a silent drop later.
    """
    if isinstance(inp, str):
        if not inp.strip():
            raise ResponseError(400, "the 'input' string is empty")
        return inp
    if not isinstance(inp, list) or not inp:
        raise ResponseError(
            400, "provide a non-empty 'prompt' string or a Responses-API 'input' "
                 "(a string, or a list of message/typed items)")
    for i, item in enumerate(inp):
        if not isinstance(item, dict):
            raise ResponseError(400, f"input item {i} must be an object")
        # A message carries `role`; every other Responses item carries `type`.
        if item.get("role") is None and not isinstance(item.get("type"), str):
            raise ResponseError(
                400, f"input item {i} needs either a 'role' (message) or a "
                     f"'type' (typed item)")
        _check_content(item.get("content"), i)
    return inp


def _check_content(content, idx):
    """Light structural check on a message's content, without flattening it.

    Forwarding verbatim does not mean forwarding garbage: a malformed shape is a
    400 here rather than a mystery upstream. Non-text blocks are allowed — they
    are exactly what the old flattening threw away.
    """
    if content is None or isinstance(content, str):
        return
    if not isinstance(content, list):
        raise ResponseError(400, f"input item {idx} content must be a string or a list")
    for j, block in enumerate(content):
        if not isinstance(block, dict):
            raise ResponseError(400, f"input item {idx} content block {j} must be an object")
        if "text" in block and not isinstance(block["text"], str):
            raise ResponseError(
                400, f"input item {idx} content block {j} 'text' must be a string")


def prompt_size(inp):
    """Rough character size of the input, for the run record's `prompt_chars`.

    Deliberately best-effort and never a reason to reject a request: it exists so
    records stay comparable with the ones written before structural forwarding,
    not to validate anything. Content itself is never stored — see the client-data
    invariant in WORKSPACE_PROTOCOL.md.
    """
    if isinstance(inp, str):
        return len(inp)
    try:
        return len(extract_prompt(inp) or "")
    except ResponseError:
        return len(json.dumps(inp, ensure_ascii=False))

# Serialises appends and the /api/runs read so a reader never sees a
# half-written line.
_runs_lock = threading.Lock()

_revision_cache = None
# A revision is a commit id: 7 to 64 hex digits. Anything else named by the environment or the file is
# not taken for one (it would land in every run record and in `/version`).
_REVISION_RE = re.compile(r"[0-9a-f]{7,64}")


def _named_revision():
    """The revision a build wrote down, or "": `RELAY_REVISION`, then the file `REVISION` beside
    `server.py`. A container image has no checkout and no `git`, so this is where it says what it
    was built from (docs/relay/docker.md)."""
    value = _text(os.environ.get("RELAY_REVISION")).lower()
    if not _REVISION_RE.fullmatch(value):
        try:
            with open(os.path.join(HERE, "REVISION")) as f:
                value = f.read(80).strip().lower()
        except (OSError, ValueError):     # unreadable, or not text (UnicodeDecodeError)
            value = ""
    return value if _REVISION_RE.fullmatch(value) else ""


def revision():
    """The revision of the code that is actually serving, or "unknown".

    What the build wrote down (`_named_revision`) wins, then the checkout's own `HEAD`.
    Cached after the first call: a record is written per request, and shelling
    out to git on every one would be absurd. A record that cannot name its
    revision cannot be attributed to a tree, which the repository's proof rule
    requires.
    """
    global _revision_cache
    if _revision_cache is None:
        named = _named_revision()
        if named:
            _revision_cache = named
            return _revision_cache
        import subprocess
        try:
            _revision_cache = subprocess.check_output(
                ["git", "-C", HERE, "rev-parse", "HEAD"],
                text=True, stderr=subprocess.DEVNULL, timeout=5).strip() or "unknown"
        except Exception:  # noqa: BLE001 - a missing git is not a run failure
            _revision_cache = "unknown"
    return _revision_cache


def session_pin_id(client):
    """The `session-id` the relay sends for a client when `session_pin` is on: a UUID derived
    from the client's name, so a client's sessions share it, two clients never do, and it does
    not change from one run or one restart to the next. Not a secret and not the client's own
    id; the name comes from the relay's configuration, never from the request."""
    # the string is a namespace, not a path: changing it changes every client's pinned id
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "midstream-abort/session-pin/" + (client or "default")))


def config_fingerprint():
    """The serving configuration a record has to be read against.

    Two records from the same revision but different pacing or concurrency are
    not comparable, and without this there is no way to tell from the record.
    """
    from . import config  # local: config imports this module

    return {
        "pacing": config.PACING,
        "max_concurrent": config.MAX_CONCURRENT,
        "retries": config.RETRIES,
        "seg_budget": config.DEFAULT_SEG_BUDGET,
        "total_cap": config.DEFAULT_TOTAL_CAP,
        "max_segments": config.DEFAULT_MAX_SEGMENTS,
        "payload_cap": config.DEFAULT_PAYLOAD_CAP,
        "imagegen_cut": config.DEFAULT_IMAGEGEN_CUT,
        "merge_text": config.DEFAULT_MERGE_TEXT,
        "session_pin": config.DEFAULT_SESSION_PIN,
        "text_calls": config.DEFAULT_TEXT_CALLS,
        "text_calls_pad": config.DEFAULT_TEXT_CALLS_PAD,
        "tail_chars": config.TAIL_CHARS,
        # Who chose the cut policy for this record. A run's `mode` means the
        # opposite thing depending on this flag: operator policy, or the caller's
        # own request. Without it a record cannot be read at all.
        "allow_client_tuning": config.ALLOW_CLIENT_TUNING,
    }


# --- the evidence chain (W4.2-W4.4) -----------------------------------------
# A record is linked to the one before it, and carries a monotonic `seq`, so a
# deleted, inserted or edited record in the middle of the file is visible.
#
# What this is NOT, stated here because the plan over-claimed it once: an in-file
# chain cannot see truncation of the *tail*. Nothing inside the file remembers
# how long it was; that needs a head hash kept off this box, and this repository
# keeps none, so a rewrite of the whole file cannot be seen from here. A signed or
# append-only medium would be stronger and is a decision, not a default.
# A writer who can rewrite the whole file can also recompute the whole chain.
GENESIS_HASH = "0" * 64
# A cap on how much of the file a tail read may touch, and a counter of how many
# lines it scanned so a test can assert the bound (see read_runs).
_TAIL_MAX_BYTES = 4 * 1024 * 1024
_last_read_lines = 0
_chain_cache = {}          # RUNS_LOG path -> {"size", "seq", "hash"}


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def record_hash(rec):
    """The hash of one record: all of its other fields, canonically ordered."""
    body = {k: v for k, v in rec.items() if k != "hash"}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _last_link(path):
    """(seq, hash) of the newest parseable record in `path`, else (0, GENESIS).

    Unparseable tail lines are skipped rather than fatal: a half-written line is
    exactly what a crash leaves, and the next append must still be able to link
    to the last complete record."""
    seq, head = 0, GENESIS_HASH
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec.get("seq"), int) and isinstance(rec.get("hash"), str):
                    seq, head = rec["seq"], rec["hash"]
    except OSError:
        return 0, GENESIS_HASH
    return seq, head


def rotated_runs(path):
    """The rotated files beside `path`, oldest first: `<path>.3, .2, .1`."""
    found = []
    i = 1
    while os.path.exists(f"{path}.{i}"):
        found.append(f"{path}.{i}")
        i += 1
    return list(reversed(found))        # .3, .2, .1 -> oldest first


def chain_state(path=None):
    """The link the next record must build on, cached per path.

    Cached because re-reading the file per append is O(n^2); validated against the
    file's size, so an external truncation or replacement is noticed instead of
    being silently carried forward."""
    path = path or RUNS_LOG
    size = _size(path)
    entry = _chain_cache.get(path)
    if entry is not None and entry["size"] == size and size:
        return entry
    if size:
        seq, head = _last_link(path)
    else:
        seq, head = 0, GENESIS_HASH
        for older in reversed(rotated_runs(path)):
            seq, head = _last_link(older)
            if seq:
                break
    entry = {"size": size, "seq": seq, "hash": head}
    _chain_cache[path] = entry
    return entry


def _rotate_runs(limit, keep):
    """Move a full runs.jsonl aside, preserving the chain across the seam.

    The next record links to the last hash of the rotated file, so a verifier
    reading `<path>.N … <path>.1, <path>` in order sees one continuous chain."""
    if limit <= 0 or _size(RUNS_LOG) < limit:
        return False
    for i in range(keep - 1, 0, -1):
        src, dst = f"{RUNS_LOG}.{i}", f"{RUNS_LOG}.{i + 1}"
        if os.path.exists(src):
            os.replace(src, dst)
    os.replace(RUNS_LOG, f"{RUNS_LOG}.1")
    return True


def log_run(rec):
    from . import config  # local: config imports this module

    # The last line of defence for S4.1, and the one that does not depend on
    # anyone's configuration being right: a record that would land in the checkout
    # is refused here. Startup refuses such a configuration already, so this should
    # never fire in production — but the failure it prevents is a *false claim* in
    # committed evidence, and this module's whole purpose is that no such thing can
    # be written quietly. It surfaces as a loud 500 naming the reason rather than as
    # a silent write somewhere nobody will look.
    #
    # Two ways in, because a path is not the whole story. The path check catches a
    # target *named* inside the checkout, including through a symlink. The link
    # count catches a target whose path is outside it but whose **inode** is the one
    # inside it: a hard link, which no path inspection can see. Reproduced — with a
    # hard link to the committed seed at a path under $HOME, `log_run` appended 458
    # bytes to the file inside the checkout. Requiring `st_nlink == 1` for the
    # evidence file is cheap and exact: nothing legitimate hard-links it, and a
    # single file with two names is precisely the situation where a write reaches
    # the repository without ever naming it.
    if inside_repo(RUNS_LOG):
        raise RunLogUnsafe(
            f"refusing to write a run record into the repository ({RUNS_LOG}). "
            "Evidence must live outside the checkout: a checkout, stash or clean "
            "can drop it while the relay is serving, and a stray `git add` can "
            "commit it. Set runs_log: (or RELAY_RUNS_LOG) to a path such as "
            f"{DEFAULT_RUNS_LOG}.")
    links = _link_count(RUNS_LOG)
    if links > 1:
        raise RunLogUnsafe(
            f"refusing to write a run record to {RUNS_LOG}: the file has {links} "
            "hard links, so at least one other name for this inode exists and a "
            "write here reaches that name too — which is how a path outside the "
            "checkout can still be a file inside it. Give the evidence file exactly "
            "one name (check `ls -l` / `stat` on it, and on the committed "
            f"{os.path.join(HERE, 'runs.jsonl')}).")

    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Attribution rides on every record, not on a doc's promise.
    rec.setdefault("revision", revision())
    rec.setdefault("config_fingerprint", config_fingerprint())
    with _runs_lock:
        if _rotate_runs(config.RUNS_MAX_BYTES, config.RUNS_KEEP):
            # The file just changed under us; re-derive the link from the new one.
            _chain_cache.pop(RUNS_LOG, None)
        state = chain_state()
        rec["seq"] = state["seq"] + 1
        rec["prev_hash"] = state["hash"]
        rec["hash"] = record_hash(rec)
        # Flush and fsync before returning (W4.3): the record is a claim that a
        # run happened, and a claim that survives a crash as half a line is worse
        # than no claim. The next append skips an unparseable tail line and links
        # to the last complete one.
        with open(RUNS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        state["seq"] = rec["seq"]
        state["hash"] = rec["hash"]
        state["size"] = _size(RUNS_LOG)
    route = rec.get("route") or "relay"
    with _run_counts_lock:
        _run_counts[route] = _run_counts.get(route, 0) + 1


def verify_chain(paths=None):
    """Walk the evidence chain and report the first break, if any.

    Per record: the hash recomputes, `prev_hash` is the previous record's hash,
    and `seq` is exactly one more than the previous one.

    A chain whose *oldest surviving* record does not link to the genesis is
    reported as `head_missing` rather than as a break, and the walk continues from
    there. That is the honest reading: a rotated-away file and a deleted head look
    identical from inside the surviving files, so neither can be called
    tampering. Seeing it from outside is what an off-box anchor is for, and this
    repository keeps none.
    Every record after that first one is still checked strictly.

    The live files are walked under `_runs_lock`, the lock `log_run` appends and
    rotates under. Without it a rotation in the middle of the walk renamed the
    files out from under the list taken at its start — a record read twice or not
    at all — and `/api/chain` reported a break that was never in the evidence. The
    cost is that appends wait for the walk, which is bounded by the rotation limit
    times the files kept. Explicit `paths` are a reader's own files and are walked
    as given."""
    if paths:
        return _walk_chain(list(paths))
    with _runs_lock:
        return _walk_chain(rotated_runs(RUNS_LOG) + [RUNS_LOG])


def _walk_chain(paths):
    expected_seq, expected_prev = 0, GENESIS_HASH
    seen = 0
    unchained = 0
    breaks = []
    head = GENESIS_HASH
    head_missing = None
    first_seq = None
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    breaks.append({"file": path, "line": lineno,
                                   "why": "not JSON"})
                    continue
                if ("seq" not in rec) != ("hash" not in rec):
                    # Half a chain: a record with one field and not the other was
                    # not written by this code. Saying so beats guessing which
                    # half is the lie, and it keeps a future field named `hash`
                    # for another purpose from reading as tampering.
                    unchained += 1
                    breaks.append({"file": path, "line": lineno,
                                   "why": "half-chained record: seq and hash must "
                                          "be present together"})
                    continue
                if "seq" not in rec and "hash" not in rec:
                    # A record written before the chain existed. It cannot be
                    # verified, which is not the same as being wrong: calling it
                    # tampering would cry wolf on this repository's own history.
                    unchained += 1
                    continue
                where = {"file": path, "line": lineno, "seq": rec.get("seq")}
                if first_seq is None:
                    first_seq = rec.get("seq")
                    if rec.get("seq") != 1 or rec.get("prev_hash") != GENESIS_HASH:
                        head_missing = (
                            f"the oldest record present is seq {rec.get('seq')}, "
                            "not 1 (rotated away, or the head was deleted)")
                        expected_seq = (rec["seq"] - 1
                                        if isinstance(rec.get("seq"), int) else 0)
                        expected_prev = rec.get("prev_hash")
                if rec.get("seq") != expected_seq + 1:
                    breaks.append(dict(where, why=(
                        f"seq {rec.get('seq')!r} where {expected_seq + 1} was due"
                        " (a record was deleted, inserted, or the file was "
                        "replaced)")))
                if rec.get("prev_hash") != expected_prev:
                    breaks.append(dict(where, why=(
                        "prev_hash does not match the previous record's hash")))
                if rec.get("hash") != record_hash(rec):
                    breaks.append(dict(where, why="the record's hash does not "
                                                   "recompute: it was edited"))
                expected_seq = rec.get("seq") if isinstance(rec.get("seq"), int) \
                    else expected_seq + 1
                expected_prev = rec.get("hash") or expected_prev
                head = expected_prev
                seen += 1
    return {"ok": not breaks, "records": seen, "unchained": unchained,
            "head": head, "first_seq": first_seq, "head_missing": head_missing,
            "files": [p for p in paths if os.path.exists(p)],
            "breaks": breaks[:20]}


class RunLogBusy(Exception):
    """Another process already writes this run log."""


class RunLogUnsafe(Exception):
    """A run record was about to be written somewhere it does not belong.

    Named for the property rather than for one check: there are two ways to reach
    the repository with a write — a path that names it, and an inode that lives in
    it — and an exception named after the first would be a lie for the second."""


def _link_count(path):
    """How many names this inode has, or 0 when it does not exist yet."""
    try:
        return os.stat(path).st_nlink
    except OSError:
        return 0


_runs_lock_fd = None

_run_counts = {"relay": 0, "passthrough": 0, "images": 0}
_run_counts_lock = threading.Lock()


def run_counts():
    """Records written since this process started, per route. In memory only: the
    run log rotates and `read_runs` reads a tail, so neither is a count."""
    with _run_counts_lock:
        return dict(_run_counts)


def set_runs_log(path, for_writing=False):
    """Point this process at `path` for the rest of its life.

    A *startup* operation, called once by the serving entry point when the config
    or the environment named a path. It is deliberately not part of
    `config.apply_settings`: that runs again on every SIGHUP, and a reload that
    moved a serving process's run log mid-stream would split the chain across two
    files, with the records before the reload in one and after it in another. A
    reload says so instead of doing it (`config.reload_settings`).

    `for_writing` is the caller's intent, and only the serving entry point passes
    it: a *reader* (a debugger looking at the committed seed, a check script) may
    point at the repository legitimately, while a writer may
    not. `log_run` refuses an in-repo target either way, so the flag cannot be used
    to smuggle a write in. Only a writer creates the directory: a reader that
    `mkdir -p`s the path it was asked to read is a read-only tool with a side
    effect, and a mistyped `--runs-log` was creating directories rather than
    failing.

    The chain cache is cleared because it is keyed by path and holds a size and a
    head hash: carrying {size: n, seq: k} over to a *different* file is exactly the
    silent-corruption shape the cache's size check exists to catch."""
    if not _text(path):
        from . import config
        raise config.ConfigError("runs_log is empty")
    if for_writing:
        resolved = refuse_inside_repo(
            path, "runs_log",
            "evidence written while the relay serves must not land in a git "
            "working tree, where a checkout, stash or clean can drop it and a "
            "stray `git add` can commit it. Point it at something like "
            f"{DEFAULT_RUNS_LOG}.")
        directory = os.path.dirname(resolved)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
    else:
        resolved = os.path.realpath(os.path.abspath(os.path.expanduser(_text(path))))
    global RUNS_LOG
    RUNS_LOG = resolved
    _chain_cache.clear()
    return resolved


def acquire_runs_lock(path=None):
    """Refuse to be a second writer for one run log.

    `log_run` computes `seq` and `prev_hash` from the last line of the file, so two
    processes appending to one `runs.jsonl` interleave sequence numbers and break
    the chain that makes the records verifiable. The consequence was documented
    (a second instance gives itself its own log) but nothing
    enforced it; this does, at startup, because the damage is silent and the
    detection is afterwards.

    A lock *file* beside the log rather than the log itself: the log is rotated and
    read by other tools, and locking it would interfere with both. Kept open for
    the life of the process, which is what holds the lock."""
    global _runs_lock_fd
    path = path or RUNS_LOG
    lock_path = path + ".lock"
    directory = os.path.dirname(lock_path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    if not lock.acquire(fd):
        os.close(fd)
        raise RunLogBusy(
            f"another relay is already writing {path} (lock: {lock_path}). Two "
            "writers interleave seq/prev_hash and break the evidence chain. Give "
            "this instance its own RELAY_RUNS_LOG.") from None
    _runs_lock_fd = fd
    return lock_path


def release_runs_lock():
    """Give the run-log lock back. For tests, and for a process handing the log
    over deliberately — production holds it for life, which is what makes it a
    guard rather than a check."""
    global _runs_lock_fd
    if _runs_lock_fd is not None:
        lock.release(_runs_lock_fd)
        os.close(_runs_lock_fd)
        _runs_lock_fd = None


def read_runs(limit=50):
    """The tail of RUNS_LOG, read *from the end*.

    This used to parse the whole file on every call. `/api/runs` is a monitor
    endpoint on an append-only file, so the cost of looking at the last fifty
    records grew with all the history behind them — and with rotation at 8 MB that
    is a lot of history to read to answer a question about the end of it. Now it
    seeks to the end and reads backwards until it has the lines it needs, capped
    at `_TAIL_MAX_BYTES` so a file with no newlines cannot be read whole by
    accident.

    `_last_read_lines` exists so the property "bounded work" can be asserted
    rather than claimed: the result is the same either way, so only the work done
    distinguishes the two implementations."""
    global _last_read_lines
    runs = []
    if limit <= 0:
        return runs
    with _runs_lock:
        _last_read_lines = 0
        data = b""
        try:
            size = os.path.getsize(RUNS_LOG)
            with open(RUNS_LOG, "rb") as f:
                pos = size
                # One extra line: without it the first line of the chunk is
                # usually a partial record, and `limit` of them would be short.
                wanted = limit + 1
                while pos > 0 and data.count(b"\n") <= wanted \
                        and len(data) < _TAIL_MAX_BYTES:
                    step = min(65536, pos)
                    pos -= step
                    f.seek(pos)
                    data = f.read(step) + data
        except OSError:
            return runs
        for raw in data.split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line.decode("utf-8", "replace")))
            except (ValueError, UnicodeDecodeError):
                continue
        _last_read_lines = len(data.split(b"\n"))
    return runs[-limit:]


def sse_send(wfile, event, data):
    wfile.write(f"event: {event}\n".encode())
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    for line in payload.split("\n"):
        wfile.write(f"data: {line}\n".encode())
    wfile.write(b"\n")
    wfile.flush()
