"""The abort engine: one upstream request per segment, plus the run loop.

`stream_segment` streams one upstream request and returns how it ended plus the
tail to carry into a continuation. `run_upstream` drives the segment loop, emits
the synthetic terminal frame and logs the run record.
"""

import json
import re
import threading
import socket
import time
import urllib.request
from contextlib import contextmanager

from . import config, trace
from .core import (ClientGone, ResponseError, error_object, error_payload,
                   is_client_gone, log_run, prompt_size, sse_send,
                   summarise_usage)
from .renamer import (
    MARKER,
    HOLD_CHARS,
    DUMMY_TOOL,
    Renamer,
    build_body,
    pads_with_dummy,
    synth_completed,
)
from .sse import iter_sse_frames
from .stream_view import StreamView
from .textcalls import TEXT_CALL_MAX_CHARS, call_frames, find_end, find_open

__all__ = ["State", "run_upstream", "stream_segment", "pace", "set_retry_hook",
           "upstream_slot"]

# The provider's structured error body, kept for evidence as identifiers only.
# A `code` or `type` is copied into the run record — a file that must not carry
# client content — so both must be identifier-shaped before they are read. The
# regex is a second gate; the control is that these come only from the provider's
# `error.code` / `error.type` fields and never from its free-text message.
_ERROR_IDENT = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MAX_ERROR_BODY = 64 * 1024     # never buffer an unbounded error body
_MAX_ERROR_MESSAGE = 500        # what the client is told, which is its own request

# --- upstream tolerance ------------------------------------------------------
# The Codex backend answers a burst with 503 service_unavailable_error
# (`server_is_overloaded`). Two controls keep the relay inside its tolerance:
# a minimum gap between calls, and a cap on how many streams are open at once.
# A request that has produced no output yet is also safe to retry; once a frame
# has reached the client it is not, because a second attempt would deliver the
# answer twice.
RETRY_STATUSES = (429, 502, 503)
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 8.0

# The provider's terminal frames other than `response.completed`. Each one means
# the provider *finalized* the response, so a run that reaches one was not cut.
# `response.incomplete` (a `max_output_tokens` limit, a content filter) is
# finalized like a completion and carries the provider's own usage; treating it
# as an unknown frame let the stream end without `response.completed`, and the
# record called a finalized turn a truncation, `usage-discarded`.
PROVIDER_TERMINALS = ("response.incomplete", "response.failed")

# What a client is told when the relay ends an answer at its length limit (`cap`, or
# the last segment a run may have). Measured against Codex 0.160.1 on the test clone,
# 2026-10-07: a response that ends `response.incomplete` is read as a stream error and
# retried (5 reconnects, 9 relay runs for one command, then failure), so the response
# stays `completed`. The message is closed `incomplete`, which Codex accepts, and the
# text says so, because nothing else a person reading it would see does.
TRUNCATION_NOTICE = "\n\n[relay: this answer was cut at its length limit ({chars} characters)]"

# How far past `seg_budget` a segment may run to reach a place to cut at. A budget cut
# lands after the last whitespace the relay is holding, so the continuation starts a
# new word and not the middle of one (9 of 15 answers had a word cut or a
# space lost at the seam). Text with no whitespace to cut at (CJK, base64) is cut where
# it is once this many characters past the budget have arrived.
SEAM_LOOKAHEAD = 200

# W1.5: the relay built its own four headers and forwarded none of the client's,
# so whatever the upstream keys off — a beta opt-in, the originator, the session
# id, which account to bill — was silently dropped. This is an allow-list, and it
# deliberately excludes Authorization: that header carries the *relay's* upstream
# credential, and a client must never be able to substitute its own. The set is
# the plan's named list and is NOT yet derived from a capture of a real client,
# which the plan still owes and records as owed.
FORWARDED_HEADERS = (
    "openai-beta",
    "originator",
    "session_id",
    "chatgpt-account-id",
)

_pace_lock = threading.Lock()
_last_call_at = 0.0

_slots = threading.BoundedSemaphore(2)
_slots_for = 2
_slots_lock = threading.Lock()

_retry_hook = None


def set_retry_hook(fn):
    """Test seam: called as fn(attempt, exc, delay) before each retry wait."""
    global _retry_hook
    _retry_hook = fn


def pace(min_interval=None):
    """Block until `min_interval` (default config.PACING) has passed since the
    last upstream call in this process. The gap is serialised on purpose: the
    point is to stop clients from arriving at the provider as one burst."""
    global _last_call_at
    if min_interval is None:
        min_interval = config.PACING
    if min_interval <= 0:
        return
    with _pace_lock:
        wait = _last_call_at + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


@contextmanager
def upstream_slot():
    """Hold one of config.MAX_CONCURRENT upstream slots for a whole segment, so
    extra clients queue instead of opening more simultaneous streams than the
    provider tolerates."""
    global _slots, _slots_for
    with _slots_lock:
        if _slots_for != config.MAX_CONCURRENT:
            _slots = threading.BoundedSemaphore(max(1, config.MAX_CONCURRENT))
            _slots_for = config.MAX_CONCURRENT
        # The semaphore this segment acquires is the one it must give back. A
        # SIGHUP that changes `max_concurrent_upstream` swaps the global while
        # segments are still holding the old one; releasing whatever the global
        # names *then* raised `Semaphore released too many times` out of an
        # in-flight run (losing its record), or handed the new semaphore a slot
        # it never lent.
        slots = _slots
    slots.acquire()
    try:
        yield
    finally:
        slots.release()


def _retry_after(exc):
    """Seconds the provider asked us to wait, or None.

    cliproxy answers `auth_unavailable: no auth available` with 503 plus a
    `Retry-After` header once every account is cooling down; honouring it beats
    guessing with a fixed backoff."""
    headers = getattr(exc, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None  # an HTTP-date Retry-After is not worth parsing here


def _retry_delay(attempt, exc=None):
    served = _retry_after(exc) if exc is not None else None
    if served is not None:
        return min(RETRY_MAX_DELAY, served)
    return min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** (attempt - 1)))


def _marker_prefix_len(text):
    """How many trailing characters of `text` could be the start of `MARKER`.

    The longest proper prefix of the marker that `text` ends with, else 0. A
    budget cut flushes the hold-back window whole; a marker prefix at its end
    (`...TASK_COMP`) would reach the client, and the rest of the marker would
    arrive in the next segment, where the scan never sees the two halves
    together — so no marker cut fires and the appendix streams to the client.
    """
    for n in range(min(len(MARKER) - 1, len(text)), 0, -1):
        if text.endswith(MARKER[:n]):
            return n
    return 0


def open_upstream(req, rec, retries):
    """urlopen with bounded backoff on a pre-output overload response.

    Returns the response, or re-raises the last error once the budget is spent.
    Only statuses that mean "no work was done, try again" are retried."""
    last = None
    for attempt in range(1, max(1, retries) + 1):
        pace()
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            rec["attempts"] = attempt
            return resp
        except urllib.error.HTTPError as e:
            last = e
            rec["last_status"] = getattr(e, "code", None)
            if e.code not in RETRY_STATUSES or attempt >= retries:
                rec["attempts"] = attempt
                raise
            delay = _retry_delay(attempt, e)
            e.close()   # the retry is the last use; a raised one stays open for its caller
            rec["retries"] = attempt
            if _retry_hook is not None:
                _retry_hook(attempt, e, delay)   # the hook stands in for the wait
            else:
                time.sleep(delay)
        except urllib.error.URLError as e:
            last = e
            if attempt >= retries:
                rec["attempts"] = attempt
                raise
            delay = _retry_delay(attempt)
            rec["retries"] = attempt
            if _retry_hook is not None:
                _retry_hook(attempt, e, delay)
            else:
                time.sleep(delay)
    rec["attempts"] = max(1, retries)
    raise last


def http_error_detail(exc, rec):
    """Keep what the provider said, split by who may see it.

    A provider-side rejection used to reach the run record as
    `HTTPError: HTTP Error 400: Bad Request` and nothing else, which is how a
    resumed Codex turn failed six times without anyone being able to see that the
    provider had rejected an item id the relay had rewritten.

    Two destinations, because they are governed by different rules:

    * the **run record** gets `status`, and `code`/`type` only when they are
      identifier-shaped. It is an evidence file, and local law keeps content out
      of it; a code is an identifier, a message is not.
    * the **client** gets the provider's message, truncated and stripped of
      control characters. It is the client's own request the provider is
      complaining about, and a diagnostic it cannot see is a diagnostic it cannot
      act on.

    Returns `(record_text, client_message)`; the caller decides where the second
    goes, and it must not be into `rec`.
    """
    status = getattr(exc, "code", None)
    body = b""
    try:
        body = exc.read(_MAX_ERROR_BODY) or b""
    except Exception:  # noqa: BLE001 - an unreadable body is simply not evidence
        body = b""
    # The trace is the one place this body may be read back in full: it is
    # provider text about a client's request, and the run record keeps identifiers
    # only. Reading it here is what stops the next such defect needing a proxy.
    trace.record_provider_error(status, body)

    info = {"status": status}
    message = None
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        doc = None
    err = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(err, dict):
        for key in ("code", "type"):
            value = err.get(key)
            if isinstance(value, str) and _ERROR_IDENT.match(value):
                info[key] = value
        raw = err.get("message")
        if isinstance(raw, str) and raw.strip():
            message = "".join(c for c in raw if c >= " " or c == "\t")
            message = message.strip()[:_MAX_ERROR_MESSAGE]
    rec["upstream_error"] = info

    named = info.get("code") or info.get("type")
    text = f"{type(exc).__name__} {status}"
    if named:
        text += f": {named}"
    return text, message


def stream_segment(body, seg, state, cfg, rec):
    """Stream one upstream request. Returns (cut_reason, tail_text).

    Ends the run: 'marker' | 'after-deltas' | 'terminal' | 'cap' | 'error' |
    'toolcall' | 'textcall' | 'itemdone' | 'imagegen' | 'client_gone'.
    Continues into another segment: 'budget' | 'toolpad' | 'itemdone-empty' |
    'eof'."""

    headers = {
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    for name, value in (cfg.get("headers") or {}).items():
        # Never override a header the relay owns: `Authorization` above all.
        if name not in headers:
            headers[name] = value
    if cfg.get("session_pin_id"):
        # `session_pin`: the provider's prompt cache is keyed on this header, and the
        # relay's own requests carry none of the client's. A constant per client.
        headers["session-id"] = cfg["session_pin_id"]

    # The estimate basis for synthetic usage, and the only number the relay has
    # for input: the provider never reports usage before `response.completed`,
    # because the relay never reaches it.
    body_bytes = json.dumps(body).encode()
    # `upstream_input_chars` is the basis of the synthetic input-token estimate:
    # the size of the request the answer was actually produced from, which is the
    # *last* segment's. The running total is a different fact — what the account
    # was asked to read across the whole run — so it is recorded separately
    # rather than being read out of a field that means something else.
    rec["upstream_input_chars"] = len(body_bytes)
    rec["upstream_input_chars_total"] = (rec.get("upstream_input_chars_total", 0)
                                         + len(body_bytes))
    trace.record_request(seg, body_bytes)

    req = urllib.request.Request(
        config.UPSTREAM + "/v1/responses",
        data=body_bytes,
        headers=headers,
        method="POST",
    )

    resp = None
    seg_chars = 0
    arg_chars = 0              # chars streamed into tool-call arguments
    pending_text = ""          # text received but not yet flushed downstream
    # A possible marker prefix the previous segment's budget or toolpad cut held
    # back (`cut_flush`). It
    # joins `pending_text` at this segment's first text delta, so the scan sees it
    # with its continuation and it goes out on that delta's item. Never earlier:
    # the item it came from is closed by now, and a flush from any other arm (a
    # tool call, an empty completion) would attach it to the wrong item or to
    # none. A segment with no text hands it on to the next one, where the
    # marker's other half may still be; what no segment used is counted when the
    # run ends (`carry_dropped_chars`).
    carried, state.carry = state.carry, ""
    # No invented id and no invented index: an item id is the provider's, and
    # inventing one is what broke the next turn. Both are filled from the item the
    # client was actually shown, before the first delta that needs them.
    last_item_id = None
    last_index = None
    abort_now = None
    # Every tool call seen this segment, by item id: True when it is the relay's
    # own padding tool (the abort window), False when it is the client's (which
    # must be delivered whole). A *map*, not a single slot: with
    # `parallel_tool_calls` two calls interleave, and a single slot let a pad's
    # resumed arguments be read as a client's — which meant no cut fired and the
    # provider finalized `usage`. That is a billing regression in exactly the
    # thing this relay exists for.
    tool_kinds = {}
    # `text_calls`: the opening line of a call the model is writing as text (None when it is
    # not writing one), the tool it names, and whether this segment has stopped reading calls
    # because what looked like one was not (it is delivered as the text it is).
    call_head = None
    call_name = None
    call_off = False
    call_checked = 0       # how much of the call read so far has been looked at for its end

    def give_back():
        """What looked like a call is text after all: the opening line goes back in front of
        what was read after it, to be delivered or scanned like any other text."""
        nonlocal pending_text, call_head, call_off
        if call_head is not None:
            pending_text = call_head + pending_text
            call_head = None
            rec["text_call_fallbacks"] += 1
        call_off = True

    def flush(force=False, upto=None):
        """Emit pending text as renumbered delta frames."""
        nonlocal pending_text, last_item_id, last_index
        if force and call_head is not None:
            # Whatever forces text out while a call is being read (the item closing, the
            # response ending) means the closing line is not coming.
            give_back()
        limit = len(pending_text) if force else max(0, len(pending_text) - HOLD_CHARS)
        if upto is not None:
            limit = min(limit, upto)
        if limit <= 0:
            return
        # The protection against showing the client a fragment of the abort
        # marker is the `HOLD_CHARS` window above, not anything here: a marker can
        # only complete if its fragment sits at the *end* of `pending_text`, and a
        # fragment at the end is always inside the last `HOLD_CHARS` characters —
        # so it is never part of the chunk this function emits. A suffix scan used
        # to run here and could only ever fire on a chunk whose tail happened to
        # be a marker prefix with `HOLD_CHARS` characters after it, which is a
        # fragment that cannot complete. It cost real characters for that:
        #   * it compared one-character prefixes, so any answer ending in `A`
        #     (APPENDIX_9F3) or `T` (TASK_COMPLETE_9F3) lost that character from
        #     the deltas the client kept while the forwarded `output_text.done`
        #     carried the full text, and
        #   * it broke on the *shortest* match, so `TASK_COMPLET` reached the
        #     client as `TASK_COMPLE` — it dropped a character *and* delivered the
        #     fragment it existed to withhold.
        # Deleting it removes both. The window's own guarantee is asserted by the
        # `RUNS` row "a marker split across two deltas never reaches the client" in
        # `test_engine.py`.
        chunk = pending_text[:limit]
        if limit <= 0:
            return
        pending_text = pending_text[limit:]
        if last_item_id is None or last_index is None:
            shown_id, shown_index = state.last_shown()
            if last_item_id is None:
                last_item_id = shown_id
            if last_index is None:
                last_index = shown_index
        frame = state.make_delta(chunk, last_index, last_item_id)
        state.send("response.output_text.delta", frame)
        rec["delivered_chars"] += len(chunk)

    def seam_boundary():
        """The index just after the last whitespace in the text not yet delivered,
        or 0 when there is none."""
        for i in range(len(pending_text) - 1, -1, -1):
            if pending_text[i].isspace():
                return i + 1
        return 0

    def budget_cut():
        """End this segment at its budget.

        The last segment a run may have ends where `cut_flush` ends it: nothing
        follows to finish a word. Any other ends after its last whitespace. What
        came after that is a partial word the model writes again, so it is neither
        delivered nor left in the tail the continuation is given, and no marker
        prefix is carried (the model writes its own)."""
        nonlocal pending_text
        cut = seam_boundary() if seg + 1 < cfg.get("max_segments", 1) else 0
        if not cut:
            cut_flush()
            return
        flush(force=True, upto=cut)
        dropped = len(pending_text)
        if dropped:
            state.accumulated = state.accumulated[:len(state.accumulated) - dropped]
        pending_text = ""
        state.carry = ""

    def cut_flush():
        """Flush for a cut that another segment continues past (`budget`,
        `toolpad`): everything except a trailing marker prefix, which is carried
        into the next segment's scan. Only when one follows — `run_upstream` runs
        a single segment outside `segmented` whatever `max_segments` says — since
        on the last segment the run ends here, and holding back a trailing `T`
        would just lose a character the client should have."""
        keep = 0
        if cfg["mode"] == "segmented" and seg + 1 < cfg.get("max_segments", 1):
            keep = _marker_prefix_len(pending_text)
        flush(force=True, upto=len(pending_text) - keep)
        state.carry = pending_text

    def text_call_step():
        """`text_calls`: read a tool call the model wrote as text out of `pending_text`.

        Returns `(handled, result)`. Not handled: the text goes through the usual scan. Handled
        with no result: a call is being read, none of it is delivered and nothing else is
        decided until its closing line arrives. Handled with a result: the call was built and
        handed to the client, and the request ends here, inside the message it came in."""
        nonlocal pending_text, call_head, call_name, call_checked, call_off
        if call_head is None:
            found = find_open(pending_text)
            if found is None:
                return False, None
            start, end, call_name = found
            if MARKER in pending_text[:start]:
                # The answer already ended: what follows is the appendix, not a call.
                call_off = True
                return False, None
            # The note before the call is delivered. `flush` takes it and nothing more, so
            # what is left starts at the opening line.
            flush(force=True, upto=start)
            call_head = pending_text[:end - start]
            pending_text = pending_text[end - start:]
            call_checked = 0
        # Only what arrived since the last look can hold the closing line or the marker (a
        # little before it, for one split across two deltas), so a long call is not scanned
        # whole again on every delta.
        look_from = max(0, call_checked - len(MARKER))
        done = find_end(pending_text, look_from)
        if done is not None:
            text, _ = done
            name, call_head = call_name, None
            # Nothing after the closing line is read, delivered or counted.
            pending_text = ""
            state.send_text_call(name, text)
            rec["text_call"] = name
            rec["text_call_chars"] = len(text)
            rec["delivered_chars"] += len(text)
            return True, ("textcall", state.accumulated[-config.TAIL_CHARS:])
        if pending_text.find(MARKER, look_from) >= 0 or len(pending_text) > TEXT_CALL_MAX_CHARS:
            # The answer's end, or a call too long to be one, before any closing line.
            give_back()
            return False, None
        call_checked = len(pending_text)
        return True, None

    def dispatch(etype, obj, event_name):
        """Handle one decoded SSE event; returns the (cut, tail) pair when the
        segment must end, else None."""
        nonlocal arg_chars, last_item_id, last_index, pending_text, abort_now, seg_chars
        nonlocal carried

        if etype == "response.created":
            # The client's stream has to open with this, and a strict one refuses
            # to finalize without it. It used to be consumed for the response id
            # and never forwarded, so the first frame a client saw was
            # `response.in_progress`.
            rid = (obj.get("response") or {}).get("id")
            if rid and not state.resp_id:
                state.resp_id = rid
            state.emit(event_name, obj, seg)
            return None

        if etype == "response.output_text.delta":
            rec["deltas"] += 1
            if "item_id" in obj:
                # The provider's, verbatim, except that a continuation merged into the
                # message the client already has goes out on that message's id.
                last_item_id = state.client_item_id(obj["item_id"])
            if "output_index" in obj:
                last_index = state.canonical_index(obj["output_index"])
            delta = obj.get("delta", "")
            if carried:
                pending_text, carried = carried + pending_text, ""
            pending_text += delta
            state.accumulated += delta
            seg_chars += len(delta)

            if cfg.get("text_calls") and not call_off:
                handled, result = text_call_step()
                if handled:
                    return result

            # marker scan inside the held-back buffer only — the marker
            # is shorter than HOLD_CHARS so it can never be flushed early
            pos = pending_text.find(MARKER)
            if cfg["mode"] in ("marker", "segmented") and pos >= 0:
                flush(force=True, upto=pos)
                abort_now = "marker"
            elif (
                cfg["mode"] == "after-deltas"
                and cfg["abort_after"]
                and rec["deltas"] >= cfg["abort_after"]
            ):
                flush(force=True)
                abort_now = "after-deltas"
            elif (
                cfg["mode"] == "segmented"
                and cfg["seg_budget"]
                and seg_chars >= cfg["seg_budget"]
                and (seg + 1 >= cfg.get("max_segments", 1) or seam_boundary()
                     or seg_chars >= cfg["seg_budget"] + SEAM_LOOKAHEAD)
            ):
                budget_cut()
                abort_now = "budget"
            elif (
                cfg["mode"] == "segmented"
                and cfg.get("total_cap")
                and rec["delivered_chars"] + len(pending_text) - HOLD_CHARS
                    >= cfg["total_cap"]
            ):
                # hard cap: last-segment safety net — truncate mid-text
                # rather than wait for a natural terminal that bills
                flush(force=True)
                abort_now = "cap"
            else:
                flush()

            if abort_now:
                return abort_now, state.accumulated[-config.TAIL_CHARS:]
            return None

        if etype == "response.output_item.added":
            item = obj.get("item") or {}
            if state.merge_rec is not None and state.alias:
                # Another item opens while the continuation's message, merged into the
                # held one, has not been closed by the provider. A provider closes an item
                # before it opens the next; none has been seen to do otherwise. Merging
                # cannot follow it: the client cannot hold two open items, the rest of that
                # message would have no item to go to, and text the relay did not scan could
                # carry the marker that ends the run. So the run ends here, with an error
                # the client is told, and no more text is read. What was received is
                # delivered, except a trailing marker prefix of two characters or more: the
                # relay's own protocol text is not sent to the client.
                keep = _marker_prefix_len(pending_text)
                keep = keep if keep >= 2 else 0
                flush(force=True, upto=len(pending_text) - keep)
                if pending_text:
                    state.accumulated = state.accumulated[:len(state.accumulated) - len(pending_text)]
                    pending_text = ""
                rec["error"] = ("the provider opened an item while a continuation's message "
                                "was still open (merge_text: false returns the old shape)")
                return "error", state.accumulated[-config.TAIL_CHARS:]
            if item.get("type") == "image_generation_call" and cfg.get("imagegen_cut"):
                # The image does not exist yet — this frame only names it. The
                # provider then sends `in_progress` -> `generating` -> 60-140s ->
                # `completed` -> the item's `done` with a ~2M-character base64
                # `result`. Ending the run here means the provider never produces
                # that artifact and the client never receives it. What it is worth
                # in points is NOT established: two runs of one prompt, half an
                # hour apart, each with one generated image, moved the meter +12
                # and +1 (docs/relay/relay-8371.md), so the cut is justified by the
                # artifact and by the ten-fold shorter run, not by a saving.
                #
                # The frame is emitted first: the client is shown the item, and
                # `close_stream` then closes it as `incomplete` — the same shape a
                # cut client tool call gets, not an item that never ends. Whatever
                # text the model produced before choosing the image is already
                # delivered and stays delivered — hence the flush, which every
                # ending arm here does and which this one silently skipped until a
                # test caught the 1500 held-back characters going nowhere.
                flush(force=True)
                state.emit(event_name, obj, seg)
                return "imagegen", state.accumulated[-config.TAIL_CHARS:]
            if item.get("type") in ("function_call", "custom_tool_call"):
                # Which call is this? The relay's own padding tool is the abort
                # window; anything else is the client's, and the client's call is
                # what makes its tool use possible. Deciding once, at the frame
                # that names the call, is what lets the cut below tell them apart
                # without a second home for the same fact.
                # A pad only when the relay actually injected one *and* the name
                # matches. Keying on the name alone made a client that defines a
                # tool called `discarded_placeholder` lose its own call to the
                # abort window.
                is_pad = (bool(cfg.get("injected_pad"))
                          and item.get("name") == DUMMY_TOOL["name"])
                tool_kinds[item.get("id")] = is_pad
                if is_pad:
                    arg_chars = 0
            state.emit(event_name, obj, seg)
            return None

        if etype in (
            "response.function_call_arguments.delta",
            "response.custom_tool_call_input.delta",
        ):
            state.emit(event_name, obj, seg)
            kind = tool_kinds.get(obj.get("item_id"))
            if kind is False:
                # A client's tool call: let its arguments finish. The abort lands
                # when the call is complete and the client has something it can
                # actually run. Cutting here at 50 characters was what truncated
                # every real call and made a tool-using client impossible.
                return None
            if kind is None:
                # Arguments for a call whose `added` frame was never seen. Counted
                # toward the cut on purpose: the abort is the product, and a cut
                # that fires too eagerly costs one truncated call, where a cut
                # that never fires costs a finalized `usage`. The count is in the
                # record so the operator can see it happen.
                rec["untracked_tool_deltas"] += 1
            arg_chars += len(obj.get("delta", ""))
            # 'toolpad' cut: the mandatory __pad9f3 arg keeps the model
            # generating inside the tool call -> safe abort window
            if arg_chars >= cfg.get("pad_abort_chars", 50):
                cut_flush()
                abort_now = "toolpad"
                return abort_now, state.accumulated[-config.TAIL_CHARS:]
            return None

        if etype in PROVIDER_TERMINALS:
            # The provider finalized the response without completing it. This is
            # a terminal frame like `response.completed` and is read like one:
            # the held-back text goes out first, the usage it carries is the
            # provider's own, and the stream ends here rather than at the EOF
            # after it — which used to be recorded as a truncation, with a second
            # error frame sent after the client's terminal.
            flush(force=True)
            provider_resp = obj.get("response") or {}
            usage = summarise_usage(provider_resp.get("usage"))
            rec["provider_terminal"] = etype
            # Usage is kept whatever came with it: it is the provider's count.
            # `real_completed` follows the frame, not the usage — an incomplete
            # response is the provider's finalized answer; a failed one is not,
            # and whether it was charged is a meter question this record does
            # not answer.
            rec["usage"] = usage
            if etype == "response.incomplete":
                rec["real_completed"] = True
            # Upstream normally closes its items before its terminal frame;
            # whatever it left open is closed before the terminal, not after it. A
            # message held open for a merge is one of those, and is closed `completed`
            # like every item the relay closes here: the provider's own terminal
            # frame, which follows, is what tells the client the response was not.
            state.close_stream()
            out = state.renumber(obj, seg)
            if out is not None:
                resp = out.get("response")
                if isinstance(resp, dict):
                    resp["output"] = state.view.output()
                state.send(event_name or "message", out)
            if etype == "response.failed":
                err = provider_resp.get("error")
                code = err.get("code") if isinstance(err, dict) else None
                rec["error"] = "upstream response.failed"
                if isinstance(code, str) and _ERROR_IDENT.match(code):
                    rec["error"] += f": {code}"
                return "error", state.accumulated[-config.TAIL_CHARS:]
            return "terminal", state.accumulated[-config.TAIL_CHARS:]

        if etype == "response.completed":
            flush(force=True)
            if state.merge_rec is not None:
                # Upstream closed nothing of the continuation's message before its
                # terminal frame, so the held message is closed here, once, with the
                # whole text, before the completed response lists it.
                state.close_stream()
            rec["real_completed"] = True
            rec["provider_terminal"] = etype
            rec["usage"] = summarise_usage((obj.get("response") or {}).get("usage"))
            out = state.renumber(obj, seg)
            if out is not None:
                # One rule for both terminals: the completed response describes the
                # output the client was actually shown. Upstream's own `output`
                # covers only the segment that just ended, so in a multi-segment
                # run it would omit the items earlier segments delivered.
                resp = out.get("response")
                if isinstance(resp, dict):
                    resp["output"] = state.view.output()
                state.send(event_name or "message", out)
            return "terminal", state.accumulated[-config.TAIL_CHARS:]

        if etype in ("response.output_text.done", "response.content_part.done",
                     "response.output_item.done"):
            # Upstream is closing something. Any text this relay is still holding
            # back for that item has to reach the client BEFORE the frame that
            # closes it. Otherwise the client gets a delta after its item's
            # `done` — a frame a strict consumer drops (Codex says
            # "OutputTextDelta without active item") — and the `done` text
            # disagrees with the deltas the client actually kept, so the answer
            # arrives attached to nothing.
            #
            # Flushing is safe here: the marker scan runs on every delta, so a
            # text whose item upstream has already finished cannot still be
            # hiding the marker.
            flush(force=True)
            state.emit(event_name, obj, seg)
            if etype != "response.output_item.done":
                return None
            item = obj.get("item") or {}
            if tool_kinds.get(item.get("id")) is False:
                # The client's call is complete and delivered. Abort now, before
                # the provider finalizes the response the call belongs to: the
                # client gets a call it can run, and `usage` still never
                # finalizes.
                abort_now = "toolcall"
                return abort_now, state.accumulated[-config.TAIL_CHARS:]
            if (item.get("type") == "message" and cfg.get("owns_tool_list")
                    and cfg.get("structured_output")):
                # A text item has just closed on a request that asked for a
                # **structured output** — the one shape where the reply
                # cannot carry the marker and cannot be a call to the padding
                # tool: Codex generates its task titles and rollout summaries with
                # `text.format: {type: json_schema, strict: true}`, and a marker
                # line or a tool call instead of the object would violate the
                # schema the caller asked for. Measured before this cut existed:
                # 24 of 201 runs in one day reached `response.completed` with a
                # 2-763 character JSON answer and were billed, and in the trace
                # structured-output runs bill at 8 of 12 against 3 of 110 for
                # prose (docs/relay/cost-of-a-cut.md, "The leak, and what closed it";
                # the rule is in docs/relay/relay-8371.md, "The `itemdone` cut").
                #
                # The gate is the structured-output flag and NOT "the request
                # offered no tool", which is what it was first. That version was a
                # generalization past the evidence, and the trace falsifies it:
                # Codex sends its tools inside the `input` items rather than in
                # the top-level `tools`, so `injected_pad` is true for a whole
                # Codex turn — and **22 runs in the trace opened a tool call only
                # after a message item had closed** (a preamble message, then the
                # call). Ending the run at that close would have delivered the
                # preamble as the answer and never the call. See
                # `docs/relay/relay-8371.md`, "The `itemdone` cut".
                #
                # Two cases, told apart by whether this segment produced any text:
                #
                # * text delivered: the client has a complete item, so the run
                #   ends here (`itemdone`). Nothing the client was shown is taken
                #   back, and the provider's `response.completed` is never read.
                # * no text: the item is empty (the model answered with reasoning
                #   only and closed), so the run continues to another segment
                #   rather than ending the turn with nothing (`itemdone-empty`).
                #   Ending here would turn a retryable empty answer into a
                #   definitely empty one.
                #
                # Gated on `structured_output` *and* `owns_tool_list` (`injected_pad` unless `text_calls_pad` is off, when the
                # relay writes no tool but still owns the list). The first is
                # what makes the marker and the padding call impossible; the second
                # keeps the cut off `control` mode and off any request whose own
                # top-level tool list the relay did not write into — the padding
                # name is only the relay's to reserve when the relay injected it.
                # See the paragraph above for why "the caller offered no tool" is
                # NOT the gate on its own.
                if seg_chars > 0:
                    abort_now = "itemdone"
                    return abort_now, state.accumulated[-config.TAIL_CHARS:]
                abort_now = "itemdone-empty"
                return abort_now, state.accumulated[-config.TAIL_CHARS:]
            return None

        state.emit(event_name, obj, seg)
        return None

    def handle_frame(frame_name, payload):
        """Decode one complete SSE frame and hand it to dispatch."""
        if payload is None:
            trace.record_up(frame_name or "keepalive", None)
            return None
        rec["events"] += 1
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as e:
            trace.record_up(frame_name or "message", {"unparseable": payload[:2000]})
            # A frame we cannot parse is a truncated or corrupted stream, not a
            # no-op. Treating it as one let a half-written frame sail through and
            # the run end as a clean, short, successful answer — the silent
            # failure this relay must not have. Every `data:` line on this lane is
            # JSON, so an unparseable one is a fault rather than a keepalive.
            rec["error"] = f"unparseable SSE frame: {e}"
            return "error", state.accumulated[-config.TAIL_CHARS:]
        trace.record_up(frame_name or "message", obj)
        etype = obj.get("type", frame_name or "")
        return dispatch(etype, obj, frame_name)

    with upstream_slot():
        try:
            resp = open_upstream(req, rec, cfg.get("retries", 3))
            rec["upstream_status"] = resp.status
            for frame_name, payload in iter_sse_frames(resp):
                result = handle_frame(frame_name, payload)
                if result is not None:
                    return result

            # The upstream closed with no terminal frame and no cut of ours:
            # that is a truncation, and faking a completion over it is exactly the
            # silent failure this relay must not have. (On a marker abort the
            # held-back text IS the marker/appendix, so it is never flushed.)
            if not abort_now:
                rec["error"] = "upstream closed without response.completed"
                return "error", state.accumulated[-config.TAIL_CHARS:]
            return abort_now, state.accumulated[-config.TAIL_CHARS:]

        except ClientGone as e:
            # Raised only by State.send, so this is definitely the client. An
            # upstream failure reaching here would now be recorded as one, which
            # is the point: the type alone cannot tell them apart, the site can.
            rec["error"] = None
            rec["client_gone"] = str(e)
            return "client_gone", state.accumulated[-config.TAIL_CHARS:]
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError):
                # The provider said why; keep it. `rec["error"]` is the
                # record-safe form, `state.client_error` the diagnostic the
                # client gets.
                try:
                    rec["error"], state.client_error = http_error_detail(e, rec)
                finally:
                    e.close()
            else:
                rec["error"] = f"{type(e).__name__}: {e}"
            return "error", state.accumulated[-config.TAIL_CHARS:]
        finally:
            if carried:
                # Unused: this segment produced no text. `pending_text` only grows
                # on a text delta, which would have consumed the carry, so any
                # `cut_flush` this segment made (a `toolpad` cut) left
                # `state.carry` empty, and nothing is overwritten.
                state.carry = carried
            if resp is not None:
                # mid-read close => TCP abort upstream (FIN/RST before
                # response.completed => usage never finalized)
                try:
                    resp.fp.close()
                except Exception:
                    pass
                try:
                    resp.close()
                except Exception:
                    pass

    # unreachable
    return abort_now or "eof", state.accumulated[-config.TAIL_CHARS:]


class State:
    def __init__(self, wfile, hidden_tools=(), payload_cap=0):
        self.wfile = wfile
        self.renamer = Renamer()
        self.accumulated = ""
        # What the client has actually been shown. Every write to the client goes
        # through `send`, which observes it here, so this is the only place that
        # can say what is still open — and the only source for the closing frames
        # and for `response.completed.response.output`.
        self.view = StreamView(seq=self.renamer.seq_bump,
                               hidden_tool_names=hidden_tools,
                               payload_cap=payload_cap)
        # The provider's own diagnostic for the client. Deliberately not on `rec`:
        # it is free text and the run record is an evidence file.
        self.client_error = None
        # A possible marker prefix a budget cut held back, handed to the next
        # segment's scan (see `_marker_prefix_len`).
        self.carry = ""
        # `merge_text`: the message a budget cut left open on purpose, so that the
        # next segment's text goes on it instead of opening a second message. `alias`
        # maps that segment's provider item id to the client's; `merged_segments`
        # counts the continuations that went onto an open message.
        self.merge_rec = None
        self.alias = {}
        self.hidden = set()          # the continuation's own reasoning items, withheld
        self.merged_segments = 0

    def send(self, event, data):
        """Write one frame to the client, classifying a failure by *site*.

        This is the discriminator the segment loop needs: the upstream read and
        the client write sit in the same `try`, and both raise
        `ConnectionResetError`. Classifying by exception type alone made an
        upstream reset look like an abandoned request — no error frame, no
        terminal frame, and a record that said `client_gone`.

        The payload cap is applied here, before the write: what goes on the wire
        and what the view stores as the item's `done` have to be the same object,
        or `response.completed.response.output` would hand the client back the
        very payload this frame withheld.
        """
        data = self.view.cap(data)
        try:
            sse_send(self.wfile, event, data)
        except Exception as e:  # noqa: BLE001 - re-raised as the classified type
            if is_client_gone(e):
                raise ClientGone(f"{type(e).__name__}: {e}") from e
            raise
        trace.record_down(event, data)
        self.view.observe(event, data)

    def emit(self, event_name, obj, seg):
        """Renumber one upstream frame and route it to the client.

        The view decides what is forwarded: a call to the relay's own padding
        tool is not, because the client never offered that tool. Asked before the
        renumbering, so a withheld frame takes no sequence number with it.
        """
        if self.view.withhold(obj):
            trace.record_skip(event_name or "message", obj,
                              "a call to the relay's own padding tool")
            return
        if self.merge_rec is not None and self._merge_route(event_name, obj, seg):
            return
        out = self.renamer.renumber(obj, seg)
        if out is None:
            # The renamer drops a repeat of a once-only frame. The trace has to
            # say so: it recorded the frame as read from upstream, and a reader
            # who sees no `down` for it must be able to tell "withheld" from
            # "never arrived".
            trace.record_skip(event_name or "message", obj,
                              "a repeat of a once-only frame the client has")
            return
        self.send(event_name or "message", out)

    def hold_open_message(self):
        """Leave the message a budget cut ended open, for the next segment to go on.

        True only when exactly one item is open and it is a message that has text:
        the shape of a text answer cut at its budget. Anything else is closed by
        the caller as before. The frames that close it are not lost, only later:
        `_finish_merge` or the run's final `close_stream` writes them, once, with
        the text of every segment."""
        open_items = [r for r in self.view.items if r.done is None and not r.upstream_done]
        if len(open_items) != 1 or open_items[0].kind != "message" or not open_items[0].parts:
            return False
        self.merge_rec = open_items[0]
        self.alias = {}
        self.hidden = set()
        return True

    def client_item_id(self, item_id):
        """The id the client knows this upstream item by."""
        return self.alias.get(item_id, item_id)

    def _finish_merge(self):
        """Close the held message with everything it now holds, and stop merging."""
        self.merge_rec = None
        self.alias = {}
        self.hidden = set()
        for event, data in self.view.close_frames():
            self.send(event, data)

    def _merge_route(self, event_name, obj, seg):
        """Route one upstream frame while a message is held open. True: consumed.

        The continuation's message is not shown as an item of its own: its opening
        frames are withheld, its deltas go out on the held item (`client_item_id`),
        and its closing frames are withheld and replaced by the held item's own,
        written once with the whole text. A frame that is anything else — a
        reasoning item, a client's tool call, a second message — ends the merge
        first, so the held message is closed before the new item opens: a client
        holding two open items cannot finalize either."""
        etype = obj.get("type")
        rec = self.merge_rec
        if etype == "response.output_item.added":
            item = obj.get("item") or {}
            iid = item.get("id") or obj.get("item_id")
            if item.get("type") == "message" and iid and not self.alias:
                self.alias[iid] = rec.item_id
                # The deltas the engine writes itself take their index from here.
                self.renamer.seg = seg
                self.renamer.item_map[(seg, obj.get("output_index", 0))] = rec.index
                self.merged_segments += 1
                trace.record_skip(event_name or "message", obj,
                                  "a continuation's message, merged into the open one")
                return True
            if item.get("type") == "reasoning" and iid and not self.alias:
                # The continuation's own reasoning, ahead of its text (a provider
                # that reasons before every answer sends one each segment). It is the
                # relay's second request thinking about "go on from here", not part
                # of the answer, and a reasoning item shown between two halves of one
                # message is what would end the merge. Withheld, with its frames.
                self.hidden.add(iid)
                trace.record_skip(event_name or "message", obj,
                                  "a continuation's reasoning, withheld while merging")
                return True
            self._finish_merge()
            return False
        iid = obj.get("item_id") or (obj.get("item") or {}).get("id")
        if iid in self.hidden:
            trace.record_skip(event_name or "message", obj,
                              "a frame of a withheld continuation reasoning item")
            return True
        if iid in self.alias:
            trace.record_skip(event_name or "message", obj,
                              "a frame of a merged continuation's message")
            if etype == "response.output_item.done":
                self._finish_merge()
            return True
        return False

    def send_text_call(self, name, text):
        """Hand the client the tool call the model wrote as text, as the provider would have
        sent it. Whatever message is open is closed first (a client holding two open items
        cannot finalize either), then the call's frames go out numbered after the frames
        already sent. The view records them like any other, so the terminal frame lists the
        call in its `output`."""
        if not self.close_stream():
            raise ClientGone("the client left while the relay closed the stream")
        index = self.renamer.next_index
        self.renamer.next_index += 1
        for event, data in call_frames(name, text, index, self.renamer.seq_bump):
            self.send(event, data)

    def append_notice(self, text):
        """One more delta on the message still open, if there is exactly one: the
        relay's own words to the client, written after everything the model said."""
        open_msgs = [r for r in self.view.items
                     if r.done is None and not r.upstream_done and r.kind == "message"]
        if len(open_msgs) != 1:
            return
        rec = open_msgs[0]
        try:
            self.send("response.output_text.delta",
                      self.renamer.make_delta(text, rec.index, rec.item_id))
        except ClientGone:
            # The `close_stream` that follows finds the same closed socket and says so;
            # this write is not worth an exception out of the run.
            pass

    def close_stream(self, message_status=None):
        """Finish every item the client still has open. False if it has gone."""
        self.merge_rec = None
        self.alias = {}
        self.hidden = set()
        try:
            for event, data in self.view.close_frames(message_status):
                self.send(event, data)
            return True
        except ClientGone:
            return False

    def last_shown(self):
        """(item_id, output_index) of the most recent item the client was shown.

        The replacement for the invented `item_0` default: a delta that arrives
        without an item id is attributed to the item the client is looking at, and
        when there is none the frame carries no id rather than a made-up one.
        """
        for rec in reversed(self.view.items):
            return rec.item_id, rec.index
        return None, None

    @property
    def resp_id(self):
        return self.renamer.resp_id

    @resp_id.setter
    def resp_id(self, v):
        self.renamer.resp_id = v

    def __getattr__(self, name):
        return getattr(self.renamer, name)


# Run-record fields that describe the operator's deployment rather than this
# client's run: the serving revision and configuration, the cut policy, the trace
# file's path, the route the run took and why (`reason`, and pass-through's
# `reserialized`), and the evidence chain's position and hashes (`seq` also
# counts every other client's runs). They stay in the record
# and on the operator's `/api/runs`; the `relay.summary` frame a client receives
# leaves them out.
OPERATOR_ONLY_FIELDS = frozenset((
    "revision", "config_fingerprint", "knobs",
    "trace", "trace_records", "trace_errors",
    "seq", "prev_hash", "hash",
    "route", "reason", "reserialized",
))


def client_summary(rec):
    """The run record as the client's `relay.summary` frame carries it."""
    return {k: v for k, v in rec.items() if k not in OPERATOR_ONLY_FIELDS}


def run_upstream(cfg, wfile):
    """Run the configured mode and return the run record (also logged)."""
    mode = cfg["mode"]
    model = cfg["model"]
    # The client's own `input` when it sent one, so its structure survives to
    # upstream. `prompt` is the flattened text the `/api/run` path uses.
    inp = cfg["input"] if cfg.get("input") is not None else cfg["prompt"]
    pad_tools = cfg.get("pad_tools") and mode in ("marker", "segmented")
    tools = cfg.get("tools")

    rec = {
        "route": "relay",
        "mode": mode,
        "model": model,
        # W0: which configured token authorised this request. The name, never the
        # token — and the handler refuses an unknown token before we get here.
        "client": cfg.get("client"),
        # W1.4: a fact, never the value. Recording it is how the operator finds
        # out which source keeps its history server-side; refusing it (by default)
        # is what stops that source getting a confidently wrong answer.
        "previous_response_id_seen": bool(cfg.get("previous_response_id_seen")),
        # W1.3/W1.5: names only. The *values* of a forwarded parameter or header
        # are client body content and must never reach a committed evidence file
        # (W4.6); these three lists are how an operator sees what was carried
        # without any of it being committed.
        "params_forwarded": sorted(cfg.get("extra") or ()),
        "dropped_params": list(cfg.get("dropped_params") or ()),
        "headers_forwarded": sorted(cfg.get("headers") or ()),
        "pad_tools": bool(pad_tools),
        "tools_forwarded": len(tools) if isinstance(tools, list) else 0,
        "prompt_chars": prompt_size(inp),
        "delivered_chars": 0,
        "deltas": 0,
        "events": 0,
        "segs": 0,
        "cuts": [],
        "attempts": 0,      # upstream open attempts in the last segment
        "retries": 0,       # overload retries spent in the last segment
        "tail_chars": [],   # continuation tail length actually sent per segment
        # Per-segment evidence: how much answer text each segment delivered. The
        # totals were already here; the split is what tells an operator whether a
        # finalized run was a continuation that had nothing to cut, or a segment
        # whose own budget the cut should have caught.
        "seg_chars": [],
        "cut": None,
        "real_completed": False,
        # Which of the provider's own terminal frames ended the run, if one did:
        # `response.completed`, `response.incomplete` or `response.failed`. None
        # means the relay cut the stream (or it broke) before the provider
        # finalized anything.
        "provider_terminal": None,
        # Characters of a carried marker prefix that no later segment in the run
        # had text to attach to, so the client never received them (see
        # `stream_segment`).
        "carry_dropped_chars": 0,
        "usage": None,
        # W4: which kind of number `usage` is. `provider` means the provider
        # reported it; `synthetic-estimate` means the relay derived it from
        # character counts because it aborted before the provider reported
        # anything. Naming that here is what keeps an estimate from being read
        # later as a measurement.
        "usage_source": None,
        "upstream_input_chars": 0,   # basis of the synthetic input estimate
        # What the payload cap withheld from the client: how many payload fields
        # were replaced, and the characters that did not go out with them. Both
        # are counts, and `state.view.report()` below is what fills them in; they
        # are seeded here so a record that never reached the view still carries
        # the key rather than looking like a relay without a cap.
        "payload_capped": 0,
        "payload_chars_dropped": 0,
        # Arguments for a tool call whose `added` frame never arrived. Non-zero
        # means the relay guessed, and guessed toward cutting (see dispatch).
        "untracked_tool_deltas": 0,
        # `text_calls`: the tool of the call the relay built from the model's text and the
        # characters of its input (None and 0 when it built none), and how many times what
        # looked like a call was delivered as text instead. Names and counts, never content.
        "text_call": None,
        "text_call_chars": 0,
        "text_call_fallbacks": 0,
        "resp_id": None,
        "error": None,
        "upstream_error": None,      # identifiers only; never the provider's text
        "client_gone": None,   # set when the client hung up; not an upstream fault
    }

    # Reading calls out of the text needs the text-shaped cuts: in `control` and
    # `after-deltas`, or with `pad_tools` off (a request that removes the abort surface), the
    # model is not asked for the format, so there is nothing to read.
    text_calls = (bool(cfg.get("text_calls")) and mode in ("marker", "segmented")
                  and bool(pad_tools))
    # `text_calls_pad: false`: the same, without the relay's padding tool and the filler
    # section (the marker is the only abort window left). Only with `text_calls`.
    lite = text_calls and not cfg.get("text_calls_pad", True)
    # The relay's own padding tool is in play only when the caller sent no tool
    # of its own, so only then can its name be treated as reserved. A client that
    # does define a tool by that name keeps it: nothing is withheld from it.
    owns_tool_list = bool(pad_tools and pads_with_dummy(tools))
    pad_injected = bool(owns_tool_list and not lite)
    hidden = (DUMMY_TOOL["name"],) if pad_injected else ()
    # `dispatch` decides whether a tool call is the abort window from this, not
    # from the name alone: a client that defines a tool of the same name offered
    # it, and its call is not the relay's to cut.
    #
    # `structured_output` is the client's own `text.format` request, read from the
    # forwarded parameters: a JSON schema or JSON object format is the shape in
    # which the marker and the padding tool would both break what the caller
    # asked for, so it is the only shape the `itemdone` cut is allowed to end
    # (see dispatch).
    _text = (cfg.get("extra") or {}).get("text")
    _fmt = (_text or {}).get("format") if isinstance(_text, dict) else None
    structured = (isinstance(_fmt, dict)
                  and _fmt.get("type") in ("json_schema", "json_object"))
    cfg = dict(cfg, injected_pad=pad_injected, owns_tool_list=owns_tool_list,
               structured_output=structured, text_calls=text_calls)
    # What the run record says about the padding tool is what the request carried.
    rec["pad_tools"] = bool(pad_tools and not lite)
    trace.start_run({"mode": mode, "model": model, "client": cfg.get("client"),
                     "stream": cfg.get("stream", True),
                     "pads_with_dummy": pads_with_dummy(tools)})
    # Absent means the operator's configured cap, not "off": off is the dangerous
    # state here, the same way it is for `reject_previous_response_id` — the cap is
    # what stops a client being handed a payload its own next turn is billed for.
    # The policy default lives here rather than in `StreamView`, which is the
    # mechanism and takes whatever it is given.
    payload_cap = cfg.get("payload_cap")
    if payload_cap is None:
        payload_cap = config.DEFAULT_PAYLOAD_CAP
    # Same reading for the image cut: absent is the operator's policy, not "off".
    # Off buys a client its images back — the provider produces (and the relay then
    # caps) a ~2M-character artifact, and the run takes minutes rather than seconds
    # — which is a decision to make explicitly (the knob, or `DEFAULT_IMAGEGEN_CUT`).
    if cfg.get("imagegen_cut") is None:
        cfg = dict(cfg, imagegen_cut=config.DEFAULT_IMAGEGEN_CUT)
    # Absent is the operator's policy here too, not "off": an old caller's config
    # that never heard of the knob must not silently lose the merged stream.
    if cfg.get("merge_text") is None:
        cfg = dict(cfg, merge_text=config.DEFAULT_MERGE_TEXT)
    # The cut policy this run actually ran with. `config_fingerprint` names the
    # configured defaults, and an operator (or a caller, with tuning open) can
    # override every one of these per request — so without them here a run with
    # a 300-character budget reads as a run with the default 1500.
    rec["knobs"] = {
        "seg_budget": cfg.get("seg_budget"),
        "total_cap": cfg.get("total_cap"),
        "max_segments": cfg.get("max_segments"),
        "abort_after": cfg.get("abort_after"),
        "payload_cap": payload_cap,
        "imagegen_cut": bool(cfg.get("imagegen_cut")),
        "merge_text": bool(cfg.get("merge_text")),
        "text_calls": text_calls,
        "text_calls_pad": bool(cfg.get("text_calls_pad", True)),
        # Whether the requests carried the relay's own `session-id`, never the id.
        "session_pin": bool(cfg.get("session_pin_id")),
        "retries": cfg.get("retries"),
    }
    state = State(wfile, hidden_tools=hidden, payload_cap=payload_cap)
    t0 = time.time()
    tail = ""
    max_segs = cfg.get("max_segments", 1) if mode == "segmented" else 1

    for seg in range(max_segs):
        rec["segs"] = seg + 1
        # pad schema only on the first segment: after a toolpad cut the
        # continuation drops tools so the model goes straight to text.
        #
        # Measured, not assumed (2026-10-04): giving the continuation this window
        # too was tried against a replay of the exact request that leaks, and it
        # did not save the bill — the model called the padding tool *instead of*
        # answering in every offered segment, the run needed 3-4 segments instead
        # of 2, and two of three runs delivered the answer twice (80 characters
        # for a 40-character title). The last segment is tool-free either way, so
        # the short answer still ended on its own and still billed. Rejected.
        body = build_body(mode, inp, model, tail, pad_tools and seg == 0,
                          tools=tools, extra=cfg.get("extra"), text_calls=text_calls, lite=lite)
        if seg > 0:
            # The continuation carries only the last TAIL_CHARS of the
            # transcript. Using the whole accumulated text here replayed the
            # entire answer on every segment, so continuation input grew without
            # bound; record what was actually sent.
            rec["tail_chars"].append(len(tail))
        delivered_before = rec["delivered_chars"]
        how, tail = stream_segment(body, seg, state, cfg, rec)
        rec["seg_chars"].append(rec["delivered_chars"] - delivered_before)
        rec["cuts"].append(how)
        if how in ("marker", "after-deltas", "terminal", "cap", "error",
                   "toolcall", "textcall", "client_gone", "itemdone", "imagegen"):
            rec["cut"] = how
            break
        # 'budget' / 'toolpad' / 'itemdone-empty' / 'eof' -> seamless continuation
        # segment. Close what this segment left open *before* the next one opens
        # anything: a client holding two open items cannot finalize either, and the
        # same provider item id can now reappear at a new index, which only reads
        # correctly if the previous one was closed first.
        #
        # One exception, `merge_text`: a text answer cut at its budget stays open, and
        # the next segment's text goes on the same message (`State._merge_route`
        # closes it the moment anything else opens). The client then holds one open
        # item, not two, and the answer is one message however many segments it took.
        if seg + 1 >= max_segs:
            # No segment follows, so nothing needs the item closed *now*; the run's
            # last `close_stream` closes it once, with the status and the notice a run
            # ended on its length limit gets (closing here made it `completed` first).
            # What this gives up: a client that leaves during that final close is no
            # longer recorded `client_gone` when the last segment ended in any way the
            # loop does not itself end the run on (`budget`, `toolpad`, `itemdone-empty`,
            # `eof`), because the record is written before the final close, as it is
            # for every other way a run ends.
            break
        if how == "budget" and cfg.get("merge_text") and state.hold_open_message():
            continue
        if not state.close_stream():
            rec["cuts"].append("client_gone")
            rec["cut"] = "client_gone"
            rec["client_gone"] = "the client left while the relay closed the stream"
            break
    if rec["cut"] is None:
        rec["cut"] = rec["cuts"][-1] if rec["cuts"] else None
    # The answer was ended by the relay's length limit, not by the model or the marker:
    # `cap`, or a budget cut on the last segment a run may have. The client is told.
    rec["truncated"] = rec["cut"] in ("cap", "budget")
    # A carried marker prefix that no later segment had text for: the client
    # never received it.
    rec["carry_dropped_chars"] = len(state.carry)

    rec["resp_id"] = state.resp_id
    rec["elapsed_s"] = round(time.time() - t0, 3)
    # `usage-discarded` says only that no terminal frame arrived, so the provider reported no
    # usage for the run. It does not say the requests were not charged
    # (docs/relay/cost-of-a-cut.md); `billed` says a terminal frame, and the usage, arrived.
    rec["verdict"] = "billed" if rec["real_completed"] else "usage-discarded"
    # Which kind of number `usage` is, and only where a completion carries one:
    # an errored or abandoned run sends no response.completed, so labelling its
    # (absent) usage as an estimate would describe something that never existed.
    if rec["provider_terminal"] and rec["usage"] is not None:
        rec["usage_source"] = "provider"
    elif (rec["real_completed"] or rec["error"] or rec["client_gone"]
          or rec["cut"] == "error" or rec["provider_terminal"]):
        # A provider terminal without usage is not an estimate either: no
        # synthetic frame is sent over the provider's own, and "provider" would
        # name a usage object that does not exist.
        rec["usage_source"] = None
    else:
        rec["usage_source"] = "synthetic-estimate"
    rec.update(state.view.report())
    rec["merged_segments"] = state.merged_segments
    # `trace_errors` rides in the run record so a trace that has stopped writing
    # is visible where the operator already looks — the evidence file — instead
    # of only in a counter nothing reads. A path and two integers, no content.
    if trace.get() is not None:
        rec.update(trace.get().stats())

    # Evidence before the client-facing tail of the stream. A client that hangs up
    # mid-answer used to cost the run record, because the emissions below raise on
    # a closed socket and log_run had not run yet.
    log_run(rec)

    # The client-visible order is unchanged: synthetic completion, then the error
    # if there was one, then the summary. A client that has gone gets nothing:
    # reaching for a dead socket is what used to turn a disconnect into a
    # recorded upstream error.
    def emit_terminal(event, data):
        """The terminal frames go out here rather than through `State.send`, so
        the trace has to be told about them explicitly — and these are the frames
        an investigation is usually about.

        The payload cap is asked here too. `synth_completed` is built from
        `state.view.output()`, which already holds capped items, so on today's
        paths this is a guard rather than a second pass — and it has to stay a
        guard: the run record is written *before* these frames, so a cap applied
        for the first time here would not be counted in it.
        """
        data = state.view.cap(data)
        trace.record_down(event, data)
        sse_send(wfile, event, data)

    try:
        if rec.get("client_gone"):
            pass
        else:
            # Close whatever the last segment left open, on every terminal path:
            # a completed or an errored response still must not leave the client
            # holding an item that never ends.
            if rec["truncated"]:
                # The notice is the relay's own words: it is in what the client holds and
                # not in `delivered_chars`, the usage estimate or the tail, which count
                # what the model wrote.
                state.append_notice(TRUNCATION_NOTICE.format(chars=rec["delivered_chars"]))
            state.close_stream(message_status="incomplete" if rec["truncated"] else None)
            if (not rec["real_completed"] and rec["cut"] != "error"
                    and not rec["provider_terminal"]):
                # the terminal frame the client needs: the provider sent none
                emit_terminal(
                    "response.completed",
                    synth_completed(state.resp_id, model, rec["delivered_chars"],
                                    est_in_chars=rec.get("upstream_input_chars", 0),
                                    seq=state.renamer.seq_bump(),
                                    output=state.view.output()),
                )
        if rec["error"]:
            # A standard `error` event in the OpenAI shape, because `relay.error`
            # below is an event no SDK recognises: without this a mid-stream
            # failure reached the client as unrecognised frames and a closed
            # stream, with no status (headers are already out) and no terminal
            # frame (the synthetic one is deliberately skipped on this path).
            #
            # The client gets the provider's own diagnostic when there is one —
            # the complaint is about the request this client sent — while the run
            # record keeps only the identifier-shaped form of it.
            #
            # Not after the provider's own `response.failed`: that is the
            # client's terminal frame already, and a second failure after it is
            # a frame past the end of the stream.
            if not rec["provider_terminal"]:
                emit_terminal("error", error_payload(
                    state.client_error or rec["error"],
                    code="upstream_stream_error", seq=state.renamer.seq_bump()))
            emit_terminal("relay.error", {"error": rec["error"]})
        emit_terminal("relay.summary", client_summary(rec))
        trace.record_note("run_end", cut=rec.get("cut"), error=rec.get("error"),
                          verdict=rec.get("verdict"),
                          usage_source=rec.get("usage_source"),
                          segs=rec.get("segs"), delivered_chars=rec.get("delivered_chars"))
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
            TimeoutError):
        # The connection family a client write raises, and deliberately not
        # `socket.error` — that is `OSError`, which would also swallow a failure
        # that has nothing to do with the client (the same trap this module
        # documents for the segment loop).
        pass
    return rec
