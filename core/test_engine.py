#!/usr/bin/env python3
"""Offline tests for the stream engine and the parts it is built from: SSE framing,
the renamer, the request body, the client's view of the stream, the payload cap, the
trace, and `engine.run_upstream` itself.

These use a fake upstream response, so they spend no quota and need no cliproxy.
They exist because the segmented-continuation engine is the riskiest code in the
relay and the live suite can only assert its end-to-end shape.

One table per entry point: each row is a behaviour. A row names what it binds as paths
into views of what the call produced, each with the assertion it makes (`Has`, `Lt`,
...); a bare value is compared for equality, a bool or None with `is`. A row of `RUNS`
is one run and carries everything it produces together: the record returned and the
one written, the requests sent, the frames the client got and the trace. The frames a
client sees for each shape of stream are held by goldens, `test_engine.<shape>.golden`,
which are read and never written by a test.

Run:  python3 core/test_engine.py
"""

import base64
import binascii
import contextlib
import copy
import functools
import inspect
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import urllib.error  # noqa: E402

from lokki_core import config, core, engine, renamer, stream_view, trace  # noqa: E402
from lokki_core.sse import iter_sse_frames  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = renamer.MARKER
APPENDIX = renamer.APPENDIX
HOLD_CHARS = renamer.HOLD_CHARS
TAIL_CHARS = renamer.TAIL_CHARS
SEAM_LOOKAHEAD = engine.SEAM_LOOKAHEAD
# The forwarded `text` parameter that makes `itemdone` available at all: a strict
# structured output is the one shape the marker and the padding tool cannot
# reach. Codex CLI sends it for its task-title and rollout-summary calls.
STRUCTURED = {"text": {"verbosity": "low",
                       "format": {"type": "json_schema", "strict": True}}}


def sse(event, obj):
    """One complete SSE frame as its individual lines (the engine consumes
    lines, and `data:` may legitimately be split across several of them)."""
    payload = obj if isinstance(obj, str) else json.dumps(obj)
    return [f"event: {event}\n", f"data: {payload}\n", "\n"]


class FakeResponse:
    """Minimal urlopen() result: line iteration plus the close() the engine's
    finally block calls to simulate the TCP abort."""

    def __init__(self, lines, status=200):
        self._lines = [ln.encode() for ln in lines]
        self.status = status

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


class FakeSink:
    """wfile-shaped sink that records the bytes the engine writes."""

    def __init__(self):
        self.data = bytearray()

    def write(self, b):
        self.data += b

    def flush(self):
        pass

    def text(self):
        return self.data.decode("utf-8", "replace")

    def deltas(self):
        """The text of the `response.output_text.delta` frames only.

        Not `text()`: every stream also carries `relay.summary`, whose payload
        embeds the revision sha, so a substring assertion over the whole stream
        depends on that sha: one that happens to contain "d3" fails
        `assertNotIn("d3", ...)`.
        """
        out = []
        for name, payload in iter_sse_frames(self.text().splitlines(keepends=True)):
            if name == "response.output_text.delta":
                out.append(json.loads(payload).get("delta") or "")
        return "".join(out)


def make_cfg(mode="segmented", **over):
    cfg = {"mode": mode, "model": "test-model", "prompt": "P",
           "abort_after": 0, "seg_budget": 0, "total_cap": 0,
           "max_segments": 1, "pad_tools": False, "pad_abort_chars": 50}
    cfg.update(over)
    return cfg


class _EngineHarness(unittest.TestCase):
    def setUp(self):
        self._orig_urlopen = engine.urllib.request.urlopen
        self.addCleanup(setattr, config, "PACING", config.PACING)
        config.PACING = 0          # engine tests must not wait on the real floor
        # These tests drive the cut mechanics with budgets of a few characters and
        # text without spaces, so the budget cut must land where the budget is, not
        # after a word boundary it may look 200 characters further for. The seam
        # rule has its own rows, which set it explicitly.
        self._orig_lookahead = engine.SEAM_LOOKAHEAD
        engine.SEAM_LOOKAHEAD = 0
        self._orig_runs_log = core.RUNS_LOG
        # run_upstream logs every run; these are fake runs, so keep them out of
        # the real evidence file.
        fd, self._tmp_runs = tempfile.mkstemp(suffix=".jsonl",
                                              prefix="relay-test-runs-")
        os.close(fd)
        core.RUNS_LOG = self._tmp_runs
        self.sent_bodies = []
        self.sent_headers = []

    def tearDown(self):
        engine.urllib.request.urlopen = self._orig_urlopen
        engine.SEAM_LOOKAHEAD = self._orig_lookahead
        core.RUNS_LOG = self._orig_runs_log
        try:
            os.unlink(self._tmp_runs)
        except OSError:
            pass

    def install(self, responses):
        """Serve one FakeResponse per upstream call and capture request bodies."""
        state = {"i": 0}

        def fake_urlopen(req, timeout=None):
            self.sent_bodies.append(json.loads(req.data.decode()))
            self.sent_headers.append({k.lower(): v for k, v in req.header_items()})
            i = min(state["i"], len(responses) - 1)
            state["i"] += 1
            return responses[i]

        engine.urllib.request.urlopen = fake_urlopen


class _ResetAfterWrites(FakeSink):
    """A client that hangs up after N writes — the segment-boundary case."""

    def __init__(self, writes_before_reset):
        super().__init__()
        self.writes = 0
        self.limit = writes_before_reset

    def write(self, b):
        self.writes += 1
        if self.writes > self.limit:
            raise ConnectionResetError("client went away")
        super().write(b)


class _DeadSink(FakeSink):
    """A client that has hung up before the first write."""

    def write(self, b):
        raise BrokenPipeError("client gone")


class _GoneOnNotice(FakeSink):
    """A client that hangs up as the relay writes its notice."""

    def write(self, b):
        if b"[relay:" in bytes(b):
            raise ConnectionResetError("the client went away")
        super().write(b)


def message_frames(item_id="msg_1", output_index=0, delta=None):
    """A message item opened, with an output_text part and optional text."""
    lines = sse("response.output_item.added",
                {"type": "response.output_item.added", "output_index": output_index,
                 "item": {"id": item_id, "type": "message",
                          "status": "in_progress", "role": "assistant",
                          "content": []}})
    lines += sse("response.content_part.added",
                 {"type": "response.content_part.added", "output_index": output_index,
                  "item_id": item_id, "content_index": 0,
                  "part": {"type": "output_text", "annotations": [], "text": ""}})
    if delta is not None:
        lines += sse("response.output_text.delta",
                     {"type": "response.output_text.delta", "output_index": output_index,
                      "item_id": item_id, "content_index": 0, "delta": delta})
    return lines


def _client_view(sink):
    """What a strict Responses client makes of the stream, or an AssertionError.

    The rules are the ones the relay's module docstrings name: one item open at a
    time, a delta only on the open item and at its index, a `done` only for an open
    item, the item's own text equal to the deltas it was sent, and `completed` only
    when nothing is open. Returns {item_id: text} in order, plus the completed
    response's output."""
    objs = [json.loads(p) for _, p in
            iter_sse_frames(sink.text().splitlines(keepends=True)) if p]
    open_id, index_of, text = None, {}, {}
    last_seq = -1
    output = None
    for o in objs:
        t = o.get("type", "")
        if "sequence_number" in o:
            assert o["sequence_number"] > last_seq, "sequence_number went backwards"
            last_seq = o["sequence_number"]
        if t == "response.output_item.added":
            assert open_id is None, f"item {o['item']['id']} opened while {open_id} is open"
            open_id = o["item"]["id"]
            index_of[open_id] = o["output_index"]
            text[open_id] = ""
        elif t == "response.output_text.delta":
            assert open_id is not None, "OutputTextDelta without active item"
            assert o.get("item_id") in (None, open_id), \
                f"a delta for {o.get('item_id')} while {open_id} is open"
            assert o.get("output_index") in (None, index_of[open_id])
            text[open_id] += o["delta"]
        elif t == "response.output_text.done":
            assert o["item_id"] == open_id
            assert o["text"] == text[open_id], "output_text.done disagrees with its deltas"
        elif t == "response.content_part.done":
            assert o["item_id"] == open_id
            assert o["part"]["text"] == text[open_id]
        elif t == "response.output_item.done":
            assert o["item"]["id"] == open_id, "done for an item that is not open"
            if o["item"].get("type") == "message":
                parts = o["item"].get("content") or []
                assert "".join(p.get("text", "") for p in parts) == text[open_id], \
                    "item.done disagrees with its deltas"
            open_id = None
        elif t == "response.completed":
            assert open_id is None, f"completed with {open_id} still open"
            output = o["response"]["output"]
    return text, output


def _done_frames(item, text):
    """The frames with which a provider closes a message it has finished."""
    return (sse("response.output_text.done",
                {"type": "response.output_text.done", "output_index": 0,
                 "item_id": item, "content_index": 0, "text": text})
            + sse("response.output_item.done",
                  {"type": "response.output_item.done", "output_index": 0,
                   "item": {"id": item, "type": "message", "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}]}}))


def _text_frames(item, text, step=7):
    frames = message_frames(item_id=item, output_index=0)
    for i in range(0, len(text), step):
        frames += sse("response.output_text.delta",
                      {"type": "response.output_text.delta", "output_index": 0,
                       "item_id": item, "content_index": 0, "delta": text[i:i + step]})
    return frames


# --- the checks a row names -------------------------------------------------------

class _Missing:
    """What a path names where there is nothing: equal to no value, and false."""

    def __bool__(self):
        return False

    def __repr__(self):
        return "<missing>"


MISSING = _Missing()


class Op:
    """An assertion on the value a path names. `a & b` makes both."""

    def __init__(self, kind, *args):
        self.kind, self.args = kind, args

    def __and__(self, other):
        return Op("all", self, other)

    def __repr__(self):
        return f"{self.kind}{self.args!r}"


class Ref:
    """The value at another path, passed through `then`, where a constant would stand."""

    def __init__(self, path, then=None):
        self.path, self.then = path, then


def Has(*needles): return Op("has", *needles)              # each is in it
def Lacks(*needles): return Op("lacks", *needles)          # none is in it
def Eq(want): return Op("assertEqual", want)
def Ne(want): return Op("assertNotEqual", want)
def Lt(n): return Op("assertLess", n)
def Le(n): return Op("assertLessEqual", n)
def Gt(n): return Op("assertGreater", n)
def Ge(n): return Op("assertGreaterEqual", n)
def IsA(kind): return Op("assertIsInstance", kind)
def Truthy(): return Op("assertTrue")
def Falsy(): return Op("assertFalse")
def NotNone(): return Op("not_none")
def Len(want): return Op("len", want)                      # a value or an Op
def Each(op): return Op("each", op)
def Stripped(want): return Op("stripped", want)            # its trailing space removed
def Starts(s): return Op("starts", s)
def Ends(s): return Op("ends", s)
def PrefixOf(s): return Op("prefix_of", s)                 # `s` starts with it
def Head(items): return Op("head", items)                  # it starts with these items
def Before(a, b): return Op("before", a, b)                # `a` occurs in it before `b`
def Count(x, n): return Op("count", x, n)
def Increasing(): return Op("increasing")                  # strictly
def Golden(shape): return Op("golden", shape)              # <GOLDEN>.<shape>.golden


class _Checks:
    """Asserts a row's `expect` against views of what the call produced.

    A path is a view's name, with an argument in parentheses where the view takes one,
    then keys and list indices; `*` maps the rest over a list. A view nobody defines
    fails the row: a key nobody reads asserts nothing. `Golden(shape)` reads
    `<GOLDEN>.<shape>.golden` beside this file."""

    GOLDEN = "test_engine"

    def unread(self, keys, known):
        self.assertEqual(set(keys) - set(known), set(), "a key nobody reads asserts nothing")

    def check(self, views, expect):
        for path, want in expect.items():
            with self.subTest(expect=path):
                self.holds(self.at(views, path), want, views)

    def at(self, views, path):
        name, arg, rest = re.fullmatch(r"(\w+)(?:\((.*?)\))?(.*)", path).groups()
        self.assertTrue(name in views, f"{path}: no view is called {name!r}")
        self.assertTrue(not rest or rest.startswith("."), path)
        return self.walk(views[name](arg), rest.split(".")[1:])

    def walk(self, value, keys):
        for i, key in enumerate(keys):
            if key == "*" and isinstance(value, (list, tuple)):
                return [self.walk(v, keys[i + 1:]) for v in value]
            if isinstance(value, dict):
                value = value.get(key, MISSING)
            elif isinstance(value, (list, tuple, str)):
                try:
                    value = value[int(key)]
                except (ValueError, IndexError):
                    value = MISSING
            else:
                value = getattr(value, key, MISSING)
        return value

    def resolve(self, value, views):
        if isinstance(value, Ref):
            got = self.at(views, value.path)
            return value.then(got) if value.then else got
        return value

    def holds(self, got, want, views):
        want = self.resolve(want, views)
        if not isinstance(want, Op):
            want = Op("assertIs" if want is None or isinstance(want, bool) else "assertEqual", want)
        kind, args = want.kind, [self.resolve(a, views) for a in want.args]
        if got is MISSING and kind not in ("all", "assertEqual", "assertNotEqual", "assertIs", "not_none"):
            self.fail(f"nothing is there to make {want!r} of")
        if kind == "all":
            for op in args:
                self.holds(got, op, views)
        elif kind == "each":
            for item in got:
                self.holds(item, args[0], views)
        elif kind == "len":
            self.holds(len(got), args[0], views)
        elif kind == "stripped":
            self.holds(got.rstrip(), args[0], views)
        elif kind == "has":
            for needle in args:
                self.assertIn(needle, got)
        elif kind == "lacks":
            for needle in args:
                self.assertNotIn(needle, got)
        elif kind == "starts":
            self.assertTrue(got.startswith(args[0]), got)
        elif kind == "ends":
            self.assertTrue(got.endswith(args[0]), got)
        elif kind == "prefix_of":
            self.assertTrue(args[0].startswith(got), got)
        elif kind == "head":
            self.assertEqual(got[:len(args[0])], args[0])
        elif kind == "before":
            self.assertLess(got.index(args[0]), got.index(args[1]))
        elif kind == "count":
            self.assertEqual(got.count(args[0]), args[1])
        elif kind == "increasing":
            self.assertEqual(got, sorted(set(got)))
        elif kind == "not_none":
            self.assertIsNotNone(None if got is MISSING else got)
        elif kind == "golden":
            with open(os.path.join(HERE, f"{self.GOLDEN}.{args[0]}.golden"), encoding="utf-8") as f:
                self.assertEqual(got, f.read())
        else:
            getattr(self, kind)(got, *args)


# --- sse.iter_sse_frames ----------------------------------------------------------

def _split_data_frame():
    full = json.dumps({"type": "response.output_text.delta", "delta": "MULTI",
                       "output_index": 0, "item_id": "i0"})
    mid = len(full) // 2
    return ["event: response.output_text.delta\n", f"data: {full[:mid]}\n",
            f"data: {full[mid:]}\n", "\n"]


# The parser follows the SSE spec, not one JSON per line.
# (lines, the frames read from them: (event name, the payload parsed) in order)
FRAMES = (
    # a payload split across several `data:` lines is joined and parsed
    (["event: response.output_text.delta\n", 'data: {"type": "response.output_text.delta",\n',
      'data: "delta": "hello"}\n', "\n"],
     [("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hello"})]),
    (sse("x", {"type": "x", "n": 1}), [("x", {"type": "x", "n": 1})]),
    ([": keepalive\n", "\n"] + sse("x", {"type": "x"}), [("x", {"type": "x"})]),     # a comment
    (["event: ping\n", "\n"], [("ping", None)]),
    # a stream cut right after the payload still delivers its last frame
    (['data: {"type": "response.completed"}\n'], [(None, {"type": "response.completed"})]),
    (sse("a", {"type": "a"}) + sse("b", {"type": "b"}), [("a", {"type": "a"}), ("b", {"type": "b"})]),
)


class FrameTests(unittest.TestCase):
    def test_the_frames_read_from_the_lines(self):
        for lines, want in FRAMES:
            with self.subTest(lines=lines):
                self.assertEqual([(name, json.loads(p) if p else p) for name, p in iter_sse_frames(lines)],
                                 want)


# --- renamer.Renamer and renamer.synth_completed -----------------------------------

def _text_delta(item, text="x"):
    return {"type": "response.output_text.delta", "delta": text, "output_index": 0, "item_id": item}


# What the renumbering does across segments: `calls` are (frame, segment) in order;
# `out` is what each returned, `renamer` the renamer after them, `seqs` the sequence numbers
# returned and then the synthetic completion's slot.
RENAMES = (
    # The item id is the provider's and is forwarded verbatim, so one id arrives at
    # two different canonical output_indices: the client does not see one seamless
    # item across segments. What makes this safe is the close-at-boundary rule in
    # engine.run_upstream: an item is finished before the next segment opens anything,
    # so the client never holds two open items claiming the same id. Inventing an id
    # instead is what the provider rejects on the next turn.
    dict(name="item identity is scoped per segment",
         calls=[(_text_delta("i0", "a"), 0), (_text_delta("i0", "b"), 1)],
         expect={"out.0.item_id": "i0", "out.1.item_id": "i0",
                 "out.0.output_index": Ne(Ref("out.1.output_index")),
                 "distinct(out.*.item_id)": Len(1), "distinct(out.*.output_index)": Len(2)}),
    # A reasoning item's `encrypted_content` is verified against the id it is replayed
    # under, so an invented id makes the next turn a 400.
    dict(name="an item id is never invented",
         calls=[({"type": "response.output_item.added", "output_index": 0,
                  "item": {"id": "rs_abc123", "type": "reasoning"}}, 0), (_text_delta("msg_xyz"), 0)],
         expect={"out.0.item.id": "rs_abc123", "out.1.item_id": "msg_xyz"}),
    dict(name="the same segment reuses the canonical item",
         calls=[(_text_delta("i0", "a"), 0), (_text_delta("i0", "b"), 0)],
         expect={"out.0.item_id": Ref("out.1.item_id"), "out.0.output_index": Ref("out.1.output_index")}),
    dict(name="the item map is segment scoped: one canonical item per segment",
         calls=[({"type": "x", "output_index": 0}, 0), ({"type": "x", "output_index": 0}, 1)],
         expect={"renamer.next_index": 2}),
    dict(name="response.created is sent once",
         calls=[({"type": "response.created", "response": {"id": "r1"}}, 0),
                ({"type": "response.created", "response": {"id": "r2"}}, 1)],
         expect={"out.0": NotNone(), "out.1": None, "renamer.resp_id": "r1"}),
    dict(name="sequence numbers are strictly increasing, the synthetic completions slot included",
         calls=[({"type": "response.created", "response": {"id": "resp_1"}}, 0)]
         + [(_text_delta("i0"), 0) for _ in range(3)],
         expect={"seqs": Increasing()}),
)

# `renamer.synth_completed("resp_1", "m", 40, **kw)`: the completion the relay writes itself.
SYNTHS = (
    dict(name="the synthetic completion takes the sequence number it is given", kw=dict(seq=9),
         expect={"frame.sequence_number": 9, "frame.response.synthetic": Truthy()}),
    dict(name="without one its sequence number is minus one", expect={"frame.sequence_number": -1}),
)


class RenamerTests(_Checks, unittest.TestCase):
    def test_the_renumbering_across_segments(self):
        for row in RENAMES:
            with self.subTest(row=row["name"]):
                self.unread(row, ("name", "calls", "expect"))
                r = renamer.Renamer()
                out = [r.renumber(frame, seg) for frame, seg in copy.deepcopy(row["calls"])]
                views = {"out": lambda _: out, "renamer": lambda _: r,
                         "seqs": lambda _: [o["sequence_number"] for o in out] + [r.seq_bump()]}
                views["distinct"] = lambda path: set(self.at(views, path))
                self.check(views, row["expect"])

    def test_the_synthetic_completion(self):
        for row in SYNTHS:
            with self.subTest(row=row["name"]):
                self.unread(row, ("name", "kw", "expect"))
                frame = renamer.synth_completed("resp_1", "m", 40, **row.get("kw", {}))
                self.check({"frame": lambda _: frame}, row["expect"])


# --- renamer.build_body -----------------------------------------------------------

CLAUSE = "read by a program, not by the person you are writing for"
QUIET = (CLAUSE, "say nothing about them anywhere else", MARKER, APPENDIX)
ITEMS = [
    {"role": "system", "content": [{"type": "input_text", "text": "SYS"}]},
    {"role": "user", "content": [{"type": "input_text", "text": "USER"}]},
    {"type": "function_call", "call_id": "c1", "name": "shell",
     "arguments": "{}"},
    {"type": "function_call_output", "call_id": "c1", "output": "42"},
]
SHELL = [{"type": "function", "name": "shell",
          "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}]

# One row per request `renamer.build_body` writes: its arguments and what the request holds.
# `body` is the request; `text` the instructions the relay appended (the input itself when it is
# a string, else the last item's text); `roles` the role of each input item; `tool_names`.
#
# The upstream request is the conversation as it came, not a flattened prompt: flattening
# drops roles and every non-text item, so `function_call_output` would never reach the model
# and no tool-using client could work. The instructions arrive as one more user message, so a
# model asked what it will do tells the user about them: in two relayed rounds 12 of 13
# lead-in lines said "followed by the required appendix". Every request that carries the
# sections also carries the order not to talk about them, and the order names nothing the
# request does not offer. A continuation carries no tools, so it must not ask for one: asked,
# the model writes the padding tool's JSON as prose, finishing in text the call the relay cut,
# and with no marker in it the turn bills in full.
BODIES = (
    dict(name="a first segment carries the quiet clause and is told to call the pad tool last",
         args=("segmented", "do the thing", "m", "", True),
         expect={"text": Has(*QUIET, "the filler section, the tool or these requirements",
                             renamer.PAD_TOOL_NAME, "very last action")}),
    dict(name="a continuation carries the quiet clause and is told to call no tool",
         args=("segmented", "do the thing", "m", "half an answer", False),
         expect={"text": Has(*QUIET, "Do not call any tool") & Lacks("the tool", renamer.PAD_TOOL_NAME)}),
    # A continuation whose previous segment delivered nothing has no tools either, so it
    # must not name one, and must ask for the answer afresh rather than invite the model to
    # finish the cut call as prose.
    dict(name="a fresh answer carries the quiet clause and does not ask to continue a cut call",
         args=("segmented", "do the thing", "m", "", False),
         expect={"text": Has(*QUIET, "Answer the original request now")
                 & Lacks("the tool", renamer.PAD_TOOL_NAME, "Continue exactly where")}),
    # the sections it protects come first, so the clause is the last thing read
    dict(name="the clause closes the instructions and leaves the clients input alone",
         args=("segmented", [{"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "explain LLMs"}]}], "m", "", True),
         expect={"body.input": Head([{"type": "message", "role": "user",
                                      "content": [{"type": "input_text", "text": "explain LLMs"}]}]),
                 "text": Ends("or be absent.") & Before(MARKER, CLAUSE)}),
    # A client must not be able to talk the relay into storing the response: the cut
    # depends on the request not being retained upstream.
    dict(name="the clients items are forwarded verbatim, with no tools and store relay controlled",
         args=("control", ITEMS, "m", "", False),
         expect={"body.input": ITEMS, "body": Lacks("tools"), "body.store": False, "body.stream": True}),
    dict(name="segmented appends its instructions after the clients items, unmodified",
         args=("segmented", ITEMS, "m", "", False),
         expect={"body.input": Head(ITEMS) & Len(len(ITEMS) + 1), "text": Truthy()}),
    dict(name="a continuation keeps the clients items, then the tail, then the nudge",
         args=("control", ITEMS, "m", "TAILTEXT", False),
         expect={"body.input": Head(ITEMS), "roles": ["system", "user", None, None, "assistant", "user"],
                 "body.input.4.content.0.text": "TAILTEXT"}),
    dict(name="a string input is a string", args=("segmented", "hello", "m", "", False),
         expect={"body.input": IsA(str)}),
    # Wrapped into a message list, segment 2 would be a different *kind* of request from
    # segment 1: semantically equal, but a strict upstream or a client-side log comparing
    # shapes sees the difference, and only this relay knows why.
    dict(name="a string input stays a string across a continuation",
         args=("segmented", "hello", "m", "the answer so far", False),
         expect={"body.input": IsA(str) & Has("the answer so far", "Continue exactly where")}),
    dict(name="a list input keeps the assistant tail as an item",
         args=("segmented", [{"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "hi"}]}], "m", "tail text", False),
         expect={"body.input": IsA(list), "roles": ["user", "assistant", "user"],
                 "body.input.1.content.0.text": "tail text"}),
    # Strict mode and additionalProperties are constrained decoding imposed on a schema this
    # relay did not write. Forcing them on a client's tool can break that client at the model
    # level, so they stay on our own dummy tool only.
    dict(name="the clients tools get the pad property, not the dummy and not strict mode",
         args=("segmented", ITEMS, "m", "", True), kw=dict(tools=SHELL),
         expect={"tool_names": ["shell"], "body.tools.0.parameters.properties": Has(renamer.PAD_KEY),
                 "body.tools.0.parameters.required": Has(renamer.PAD_KEY), "body.tools.0": Lacks("strict"),
                 "body.tools.0.parameters": Lacks("additionalProperties")}),
    dict(name="the dummy tool is only a fallback", args=("segmented", ITEMS, "m", "", True),
         kw=dict(tools=None), expect={"tool_names": [renamer.DUMMY_TOOL["name"]]}),
    dict(name="with padding off the tools are forwarded untouched", args=("segmented", ITEMS, "m", "", False),
         kw=dict(tools=[{"type": "function", "name": "shell", "parameters": {"type": "object", "properties": {}}}]),
         expect={"body.tools": [{"type": "function", "name": "shell",
                                 "parameters": {"type": "object", "properties": {}}}]}),
)

# One row per call of `renamer.inject_pad`: `arg` is the tools it was given, after the call.
PADS = (
    # A shallow copy (`[dict(t) for t in tools]`) would leave the nested parameters,
    # properties and required as the caller's own objects, and injecting the pad would
    # edit the client's request in place.
    dict(name="injecting the pad does not change the callers tools",
         tools=[dict(SHELL[0], parameters=dict(SHELL[0]["parameters"], required=["cmd"]))],
         expect={"arg": [dict(SHELL[0], parameters=dict(SHELL[0]["parameters"], required=["cmd"]))]}),
    dict(name="the dummy tool is strict because it is ours", tools=None,
         expect={"out.0.strict": Truthy(), "out.0.parameters.additionalProperties": Falsy(),
                 "out.0.parameters.required": Has(renamer.PAD_KEY)}),
)


class BodyTests(_Checks, unittest.TestCase):
    def test_the_request_body(self):
        for row in BODIES:
            with self.subTest(row=row["name"]):
                self.unread(row, ("name", "args", "kw", "expect"))
                body = renamer.build_body(*copy.deepcopy(row["args"]), **copy.deepcopy(row.get("kw", {})))
                inp = body["input"]
                self.check({
                    "body": lambda _: body,
                    "text": lambda _: inp if isinstance(inp, str) else inp[-1]["content"][0]["text"],
                    "roles": lambda _: [i.get("role") for i in inp],
                    "tool_names": lambda _: [t.get("name") for t in body["tools"]],
                }, row["expect"])

    def test_the_padded_tools(self):
        for row in PADS:
            with self.subTest(row=row["name"]):
                self.unread(row, ("name", "tools", "expect"))
                arg = copy.deepcopy(row["tools"])
                out = renamer.inject_pad(arg)
                self.check({"arg": lambda _: arg, "out": lambda _: out}, row["expect"])


# --- core.summarise_usage ---------------------------------------------------------

# The upstream `usage` object is copied into a COMMITTED file, and
# `attribution.items[*].content[*]` is upstream-controlled free text: what is kept is a
# whitelist of counts. (the usage upstream sent, what is kept)
USAGES = (
    ({"input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
      "input_tokens_details": {"cached_tokens": 0, "junk": "x"},
      "output_tokens_details": {"reasoning_tokens": 9},
      "attribution": {"items": {"a": {"output_tokens": 2, "content": [1]}},
                      "request_fields": {"tools": {"input_tokens": 296}}}},
     {"kept.total_tokens": 3, "kept.input_tokens_details": {"cached_tokens": 0},
      "kept.output_tokens_details": {"reasoning_tokens": 9},
      "kept.attribution": {"items": {"a": {"output_tokens": 2}}}, "kept": Lacks("request_fields")}),
    (None, {"kept": None}),                 # a missing usage object stays None
    ("nonsense", {"kept": None}),
)


class UsageTests(_Checks, unittest.TestCase):
    def test_the_usage_kept(self):
        for usage, expect in USAGES:
            with self.subTest(usage=usage):
                kept = core.summarise_usage(copy.deepcopy(usage))
                self.check({"kept": lambda _: kept}, expect)


# --- stream_view.StreamView and stream_view.cap_frame -----------------------------

def _added(item, index=0):
    return {"type": "response.output_item.added", "output_index": index, "item": item}


IMAGE_ITEM = {"id": "ig1", "type": "image_generation_call", "status": "completed"}

# What the client was shown, and the frames that finish it. Built from delivered frames
# only: the engine holds `HOLD_CHARS` of text back while it scans for the marker, so text
# that was never written must not reach the closing frames or the completed response's
# `output`. A row's view is given `withhold` frames to ask about, `observe` frames, then
# `cap` capped and observed as its own type; `items` is what it tracks then. It is closed
# `closes` times: `close` and `again` are the frames of the first and the second close.
VIEWS = (
    dict(name="a message is closed in the order a client expects, and only once",
         observe=[("response.output_item.added", _added({"id": "m1", "type": "message", "status": "in_progress"})),
                  ("response.content_part.added",
                   {"type": "response.content_part.added", "output_index": 0, "item_id": "m1",
                    "content_index": 0, "part": {"type": "output_text", "annotations": []}}),
                  ("response.output_text.delta",
                   {"type": "response.output_text.delta", "output_index": 0, "item_id": "m1",
                    "content_index": 0, "delta": "hello"})],
         closes=2,
         expect={"close_names": ["response.output_text.done", "response.content_part.done",
                                 "response.output_item.done"],
                 "close.0.1.text": "hello", "close.2.1.item.status": "completed",
                 "close.2.1.item.content.0.text": "hello",
                 "again": []}),                       # closing twice would duplicate an item
    # the held-back text is never observed, because it is never written
    dict(name="text that was never written is not in the output",
         observe=[("response.output_item.added", _added({"id": "m1", "type": "message"})),
                  ("response.output_text.delta",
                   {"type": "response.output_text.delta", "item_id": "m1", "content_index": 0,
                    "delta": "the answer"})],
         closes=1, expect={"output.0.content.0.text": "the answer"}),
    dict(name="the relays own call is withheld and counted",
         withhold=[_added({"id": "fc1", "type": "function_call", "name": "pad_tool"}),
                   {"type": "response.function_call_arguments.delta", "item_id": "fc1", "delta": "{}"},
                   _added({"id": "fc2", "type": "function_call", "name": "exec"}, 1)],
         closes=1,
         expect={"withheld": [True, True, False], "items": [], "close": [], "view.hidden_calls": 1,
                 "output": []}),
    dict(name="a client call cut mid arguments is closed incomplete",
         observe=[("response.output_item.added",
                   _added({"id": "fc2", "type": "function_call", "name": "exec", "call_id": "c1",
                           "status": "in_progress"}, 1)),
                  ("response.function_call_arguments.delta",
                   {"type": "response.function_call_arguments.delta", "item_id": "fc2", "output_index": 1,
                    "delta": '{"cmd": "ls'})],
         closes=1,
         expect={"close_names": ["response.function_call_arguments.done", "response.output_item.done"],
                 "close.0.1.arguments": '{"cmd": "ls', "close.1.1.item.status": "incomplete",
                 "view.cut_calls": 1, "output.0.status": "incomplete"}),
    # the view stores the capped object, which is what makes `output()` safe to embed in a
    # terminal frame
    dict(name="a view reports what it withheld", view=dict(payload_cap=8),
         observe=[("response.output_item.added", _added(dict(IMAGE_ITEM)))],
         cap={"type": "response.output_item.done", "output_index": 0, "item": dict(IMAGE_ITEM, result="C" * 100)},
         expect={"capped.item.result": Len(len(stream_view.CAPPED_IMAGE_B64)),
                 "output.0.result": stream_view.CAPPED_IMAGE_B64, "report.payload_capped": 1,
                 "report.payload_chars_dropped": 100 - len(stream_view.CAPPED_IMAGE_B64)}),
)

HUGE_B = "B" * 5000
# `cap_frame` at the unit level, where the shapes it has to survive are cheaper to state
# than to drive through the engine. Capped at 1024; `frame` is the frame given, after the call.
CAPS = (
    dict(name="the completed aggregate is capped without an item frame, per payload",
         frame={"type": "response.completed",
                "response": {"id": "r1", "output": [
                    {"id": "ig1", "type": "image_generation_call", "result": HUGE_B},
                    {"id": "ig2", "type": "image_generation_call", "result": HUGE_B + "B"},
                    {"id": "m1", "type": "message", "content": [{"type": "output_text", "text": "hi"}]}]}},
         expect={"payloads": 2, "dropped": 2 * len(HUGE_B) + 1 - 2 * len(stream_view.CAPPED_IMAGE_B64),
                 "out.response.output.0.result": stream_view.CAPPED_IMAGE_B64,
                 "out.response.output.1.result": stream_view.CAPPED_IMAGE_B64,
                 "out.response.output.2.content.0.text": "hi",
                 # the trace records what upstream sent, and it is read after this runs
                 "frame.response.output.0.result": HUGE_B}),
    # 60k text deltas a run go through this: a copy per frame would be a cost with no benefit
    *(dict(name=f"a frame with nothing to cap is the same object: {frame!r:.60}", frame=frame,
           expect={"same": True, "counts": (0, 0)})
      for frame in ({"type": "response.output_text.delta", "delta": "x" * 10000},
                    _added({"id": "m1", "type": "message"}),
                    {"type": "response.output_item.done",
                     "item": {"id": "ig1", "type": "image_generation_call", "result": "small"}})),
    *(dict(name=f"a frame that is not a dict is not a reason to raise: {frame!r}", frame=frame,
           expect={"counts": (0, 0), "same": True})
      for frame in (None, "message", 17, [])),
)


class StreamViewTests(_Checks, unittest.TestCase):
    def test_what_the_client_was_shown(self):
        for row in VIEWS:
            with self.subTest(row=row["name"]):
                self.unread(row, ("name", "view", "withhold", "observe", "cap", "closes", "expect"))
                seq = iter(range(1000))
                v = stream_view.StreamView(seq=lambda: next(seq),
                                           **row.get("view", dict(hidden_tool_names=("pad_tool",))))
                withheld = [v.withhold(frame) for frame in copy.deepcopy(row.get("withhold", []))]
                for event, data in copy.deepcopy(row.get("observe", [])):
                    v.observe(event, data)
                capped = None
                if "cap" in row:
                    capped = v.cap(copy.deepcopy(row["cap"]))
                    v.observe(capped["type"], capped)
                items = list(v.items)
                closed = [v.close_frames() for _ in range(row.get("closes", 0))] + [MISSING, MISSING]
                self.check({
                    "withheld": lambda _: withheld, "items": lambda _: items, "capped": lambda _: capped,
                    "close": lambda _: closed[0], "again": lambda _: closed[1],
                    "close_names": lambda _: [name for name, _ in closed[0]],
                    "view": lambda _: v, "output": lambda _: v.output(), "report": lambda _: v.report(),
                }, row["expect"])

    def test_the_cap_of_a_frame(self):
        for row in CAPS:
            with self.subTest(row=row["name"]):
                self.unread(row, ("name", "frame", "expect"))
                frame = copy.deepcopy(row["frame"])
                out, dropped, payloads = stream_view.cap_frame(frame, 1024)
                self.check({"out": lambda _: out, "dropped": lambda _: dropped, "payloads": lambda _: payloads,
                            "counts": lambda _: (dropped, payloads), "frame": lambda _: frame,
                            "same": lambda _: out is frame}, row["expect"])

    def test_the_placeholder_is_a_valid_one_pixel_png(self):
        """A client that decodes the field must not be the first to find out the
        placeholder is malformed. Verified structurally: chunk CRCs, a 1x1 IHDR,
        and an IDAT that decompresses to one filter byte plus one RGBA pixel."""
        png = base64.b64decode(stream_view.CAPPED_IMAGE_B64)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        chunks = {}
        pos = 8
        while pos < len(png):
            length = struct.unpack(">I", png[pos:pos + 4])[0]
            kind = png[pos + 4:pos + 8]
            body = png[pos + 8:pos + 8 + length]
            crc = struct.unpack(">I", png[pos + 8 + length:pos + 12 + length])[0]
            self.assertEqual(crc, binascii.crc32(kind + body) & 0xffffffff,
                             f"{kind!r} chunk CRC")
            chunks[kind] = body
            pos += 12 + length
        self.assertEqual(list(chunks), [b"IHDR", b"IDAT", b"IEND"])
        width, height, depth, colour = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
        self.assertEqual((width, height, depth, colour), (1, 1, 8, 6))
        self.assertEqual(zlib.decompress(chunks[b"IDAT"]), b"\x00\x00\x00\x00\x00")
        self.assertLess(len(stream_view.CAPPED_IMAGE_B64), 128)


# --- trace.Trace ------------------------------------------------------------------

# One row per trace file written: `made` the arguments of the `Trace`, `writes` the records.
# `files` are the names in its directory, `sizes` theirs, `line` the first line, `parsed` it read.
TRACES = (
    # Bounded, like the run log. The trace is the only file here that grows with every
    # frame, so a trace left switched on has to stop at a size instead of being discovered
    # at 261 MB. The size is the cap plus one check interval.
    dict(name="the trace rotates at its cap and keeps the named count", made=dict(max_bytes=4096, keep=2),
         writes=[{"dir": "note", "note": "x" * 60, "i": i} for i in range(600)],
         expect={"files": Has("debug.jsonl", "debug.jsonl.1", "debug.jsonl.2") & Lacks("debug.jsonl.3"),
                 "sizes": Each(Lt(65536))}),
    # `max_bytes=0` is the caller asking for one whole capture
    dict(name="a trace with no cap is never rotated", made=dict(max_bytes=0),
         writes=[{"dir": "note", "note": "y" * 60, "i": i} for i in range(300)],
         expect={"files": ["debug.jsonl"]}),
    # The case the cap exists for: one frame carrying a base64 image result. Cutting the
    # serialised line instead lands the cut wherever it lands, inside a nested object, so
    # one appended `}` closes only the innermost one and leaves the rest unterminated. A
    # trace line no reader can parse is worse than one that says what it dropped.
    dict(name="an oversized record is clipped to a line that parses",
         writes=[{"dir": "up", "event": "response.output_item.done",
                  "data": {"type": "response.output_item.done",
                           "item": {"id": "ig_1", "type": "image_generation_call", "result": "A" * 400_000}},
                  "note": "the frame that carries the image"}],
         expect={"line": Len(Le(trace.MAX_RECORD_CHARS)), "parsed.dir": "up",
                 "parsed.note": "the frame that carries the image", "parsed.data.item.id": "ig_1",
                 "parsed.data.item.result": Len(trace.CLIP_VALUE_CHARS),
                 "parsed.truncated": ["data.item.result: 400000 chars"]}),
    # No single field to blame, and clipping them all is still too long: the record keeps
    # its identity and the names of what it carried, and says the values are gone.
    dict(name="a record with many large fields still parses",
         writes=[{"dir": "down", "event": "response.completed", "run": 7,
                  "fields": {f"f{i}": "B" * 100_000 for i in range(12)}}],
         expect={"line": Len(Le(trace.MAX_RECORD_CHARS)), "parsed.dir": "down",
                 "parsed.event": "response.completed", "parsed.run": 7, "parsed.keys": Has("fields"),
                 "parsed.truncated.0": Starts("whole record: ")}),
    # four large fields clip to well under the cap: the values shortened, and which ones said
    dict(name="clipping keeps a record that fits after the clip",
         writes=[{"dir": "up", "event": "response.output_item.done",
                  "fields": {f"f{i}": "B" * 100_000 for i in range(4)}}],
         expect={"parsed.fields.f0": Len(trace.CLIP_VALUE_CHARS), "parsed.fields.f1": Len(trace.CLIP_VALUE_CHARS),
                 "parsed.fields.f2": Len(trace.CLIP_VALUE_CHARS), "parsed.fields.f3": Len(trace.CLIP_VALUE_CHARS),
                 "parsed.truncated": Len(4) & Each(Has("100000 chars"))}),
    # A record can be long because of its *keys*, which clipping the values does not touch:
    # the names are dropped for a count, and a record long even without them falls back to
    # `truncated` alone. The count is the 100 written plus the three `write()` stamps.
    dict(name="a record whose keys are huge is still bounded",
         writes=[{("K" * 5000) + str(i): "v" for i in range(100)}],
         expect={"line": Len(Le(trace.MAX_RECORD_CHARS)), "parsed": Lacks("keys"), "parsed.n_keys": 103}),
)


class TraceFileTests(_Checks, unittest.TestCase):
    def test_the_trace_file(self):
        trace.start_run({})        # every record is stamped with the thread's run, as in a run
        for row in TRACES:
            with self.subTest(row=row["name"]):
                self.unread(row, ("name", "made", "writes", "expect"))
                folder = tempfile.mkdtemp(prefix="trace-file-")
                self.addCleanup(shutil.rmtree, folder, True)
                path = os.path.join(folder, "debug.jsonl")
                made = trace.Trace(path, **row.get("made", {})).open()
                try:
                    for record in copy.deepcopy(row["writes"]):
                        made.write(record)
                finally:
                    made.close()
                with open(path, encoding="utf-8") as f:
                    line = f.read().splitlines()[0]
                names = sorted(os.listdir(folder))
                self.check({
                    "files": lambda _: names, "line": lambda _: line, "parsed": lambda _: json.loads(line),
                    "sizes": lambda _: [os.path.getsize(os.path.join(folder, n)) for n in names],
                }, row["expect"])


# --- engine.pace, engine.upstream_slot, engine.client_summary ----------------------

class EngineSeamTests(unittest.TestCase):
    def test_pacing_waits_between_calls(self):
        with mock.patch.object(config, "PACING", 0.05):
            engine.pace()
            t0 = time.monotonic()
            engine.pace()
            self.assertGreaterEqual(time.monotonic() - t0, 0.04)

    def test_a_slot_taken_before_a_resize_is_returned_to_its_own_semaphore(self):
        """A SIGHUP that changes `max_concurrent_upstream` swaps the semaphore while
        segments hold the old one. Each must give back the one it took, on a grow and
        on a shrink."""
        for before, after in ((2, 3), (3, 1)):
            with self.subTest(before=before, after=after), \
                    mock.patch.object(config, "MAX_CONCURRENT", before):
                with engine.upstream_slot():
                    pass                       # settle the global on `before` slots
                holding, release, errors = threading.Event(), threading.Event(), []

                def hold():
                    try:
                        with engine.upstream_slot():
                            holding.set()
                            release.wait(5)
                    except Exception as e:     # noqa: BLE001 - the assertion below
                        errors.append(e)

                t = threading.Thread(target=hold)
                t.start()
                self.assertTrue(holding.wait(5))
                config.MAX_CONCURRENT = after  # what a reload does
                with engine.upstream_slot():   # the next segment swaps the semaphore
                    pass
                release.set()
                t.join(5)
                self.assertEqual(errors, [], "an in-flight segment failed to release")

    def test_the_summary_leaves_out_a_pass_through_records_operator_fields(self):
        # on a record that carries them: the engine's never does, so `RUNS` cannot see them dropped
        for rec, field in (({"reason": "routing: direct"}, "reason"), ({"reserialized": True}, "reserialized")):
            with self.subTest(field=field):
                self.assertNotIn(field, engine.client_summary(rec))


# --- engine.run_upstream ----------------------------------------------------------

BIG = 10 ** 9
UPSTREAM = "http://upstream/v1/responses"
NOTICE = "\n\n[relay:"
CUT_NOTICE = "[relay: this answer was cut"
GO = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}]
ANSWER = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
          "nu xi omicron pi rho sigma tau upsilon phi chi psi omega")


class Http:
    """An HTTP error the provider answers with, built for each run and closed after it."""

    def __init__(self, code, msg="boom", headers=None, body=None):
        self.code, self.msg, self.headers, self.body = code, msg, headers, body

    def build(self):
        return urllib.error.HTTPError(UPSTREAM, self.code, self.msg, self.headers or {},
                                      None if self.body is None else io.BytesIO(self.body))


class Breaks:
    """A response that sends `lines` and then fails with `error` (an `Http`, or an
    exception class and its message)."""

    def __init__(self, lines, error, *args):
        self.lines, self.error, self.args = lines, error, args


class _Breaking(FakeResponse):
    def __init__(self, lines, error):
        super().__init__(lines)
        self.error = error

    def __iter__(self):
        yield from self._lines
        raise self.error


class Continuing:
    """A provider that goes on from exactly the text the client holds, as a good
    continuation does: request k serves what remains of `answer`, as the message item
    `m{k+1}` (after a reasoning item `rs{k+1}` with `reasoning`), and the marker and
    appendix after it. With `then`, every request after the first serves `then(k, rest)`."""

    def __init__(self, answer, step=7, then=None, reasoning=False):
        self.answer, self.step, self.then, self.reasoning = answer, step, then, reasoning

    def serve(self, k, sink):
        held = sink.deltas()
        assert self.answer.startswith(held.rstrip("\n")) or held == "", \
            "the client's text is not a prefix of the answer"
        rest = self.answer[len(held):]
        if self.then is not None and k > 0:
            return self.then(k, rest)
        lead = _reasoning(f"rs{k + 1}") if self.reasoning else []
        return lead + _text_frames(f"m{k + 1}", rest + "\n" + MARKER + "\n" + APPENDIX, self.step)


class _Unwritable:
    """A trace file on a full disk."""

    def write(self, _):
        raise OSError("no space left on device")

    def flush(self):
        raise OSError("no space left on device")


def delta(text, item="i0", index=0):
    """A text delta; with `item=None`, one that names no item."""
    obj = {"type": "response.output_text.delta", "delta": text}
    if item is not None:
        obj.update(output_index=index, item_id=item)
    return sse("response.output_text.delta", obj)


def completed(rid="r1", **response):
    return sse("response.completed", {"type": "response.completed",
                                      "response": dict({"id": rid, "usage": {}}, **response)})


def opened(item, index=0):
    return sse("response.output_item.added", {"type": "response.output_item.added",
                                              "output_index": index, "item": item})


def closed(item, index=0):
    return sse("response.output_item.done", {"type": "response.output_item.done",
                                             "output_index": index, "item": item})


def args_delta(item_id, text, index=0):
    return sse("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
                                                          "output_index": index, "item_id": item_id,
                                                          "delta": text})


def args_done(item_id, arguments, index=0):
    return sse("response.function_call_arguments.done", {"type": "response.function_call_arguments.done",
                                                         "output_index": index, "item_id": item_id,
                                                         "arguments": arguments})


def input_delta(item_id, text, index=0):
    return sse("response.custom_tool_call_input.delta", {"type": "response.custom_tool_call_input.delta",
                                                         "output_index": index, "item_id": item_id,
                                                         "delta": text})


def input_done(item_id, text, index=0):
    return sse("response.custom_tool_call_input.done", {"type": "response.custom_tool_call_input.done",
                                                        "output_index": index, "item_id": item_id,
                                                        "input": text})


def text_done(item_id, text, index=0):
    return sse("response.output_text.done", {"type": "response.output_text.done", "output_index": index,
                                             "item_id": item_id, "content_index": 0, "text": text})


def _reasoning(item):
    """A reasoning item the way a provider that reasons before answering sends it."""
    item_obj = {"id": item, "type": "reasoning", "summary": []}
    return (opened(item_obj)
            + sse("response.reasoning_summary_part.added", {
                "type": "response.reasoning_summary_part.added", "item_id": item,
                "output_index": 0, "summary_index": 0,
                "part": {"type": "summary_text", "text": ""}})
            + closed(item_obj))


def _image_done(result, index=1):
    """An image item the way the payload cap meets it: `result` in the item's `done` frame."""
    item = {"id": "ig1", "type": "image_generation_call", "status": "completed", "revised_prompt": "a pelican"}
    return (opened(dict(item), index)
            + sse("response.image_generation_call.in_progress",
                  {"type": "response.image_generation_call.in_progress", "item_id": "ig1", "output_index": index})
            + closed(dict(item, result=result), index))


IMAGE_PAYLOAD = "A" * 200000


def _image(payload=IMAGE_PAYLOAD, index=1):
    """The provider's whole, slow, fixed image sequence: `added` -> `in_progress` ->
    `generating` -> `completed` -> `done` with the ~2M-character base64 `result`."""
    item = {"id": "ig1", "type": "image_generation_call", "status": "in_progress", "revised_prompt": "a pelican"}
    steps = [sse(f"response.image_generation_call.{step}",
                 {"type": f"response.image_generation_call.{step}", "item_id": "ig1", "output_index": index})
             for step in ("in_progress", "generating", "completed")]
    return opened(dict(item), index) + sum(steps, []) + closed(dict(item, status="completed", result=payload), index)


def _tool_call(name, item_id, call_id, chunks, done=True):
    """A function call to `name` whose arguments arrive as `chunks`, ended and closed when `done`."""
    lines = opened({"id": item_id, "type": "function_call", "name": name, "call_id": call_id,
                    "status": "in_progress", "arguments": ""})
    for chunk in chunks:
        lines += args_delta(item_id, chunk)
    if done:
        text = "".join(chunks)
        lines += args_done(item_id, text) + closed({"id": item_id, "type": "function_call", "name": name,
                                                    "call_id": call_id, "status": "completed",
                                                    "arguments": text})
    return lines


PAD_CHUNKS = ('{"summary": "a short summary of the work", ', '"__pad9f3": "implementation notes and edge cases ',
              'that the relay asks for so the model keeps ', 'generating inside the tool call"}')
OWN_CALL = '{"summary": "the client\'s own tool, not the relay\'s"}'


def _preamble_then_call(preamble, call_input):
    """A message that closes, then a client call: how a Codex turn writes a preamble."""
    call = {"id": "ctc1", "type": "custom_tool_call", "name": "exec", "call_id": "c1"}
    return (message_frames(item_id="msg_1", output_index=0, delta=preamble)
            + closed({"id": "msg_1", "type": "message", "status": "completed"})
            + opened(dict(call, status="in_progress"), 1)
            + input_done("ctc1", call_input, 1)
            + closed(dict(call, status="completed", input=call_input), 1)
            + completed(usage={"input_tokens": 9}))


def _closed_message(text, usage):
    return (message_frames(item_id="msg_1", output_index=0, delta=text)
            + closed({"id": "msg_1", "type": "message", "status": "completed"})
            + completed(usage=usage))


def _terminal(terminal):
    """A short answer the provider ends with its own terminal frame `terminal`."""
    return (sse("response.created", {"type": "response.created", "response": {"id": "resp_1"}})
            + opened({"id": "msg_1", "type": "message", "role": "assistant", "content": []})
            + sse("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "msg_1",
                                                 "output_index": 0, "delta": "short answer"})
            + sse(terminal["type"], terminal))


_FN_RUN = {"id": "fc1", "type": "function_call", "name": "run", "call_id": "c1", "arguments": ""}
_CALL_RUN = opened(_FN_RUN) + args_delta("fc1", "{}") + closed(dict(_FN_RUN, arguments="{}", status="completed"))


def _then_finished(k, rest):
    frames = _text_frames(f"m{k + 1}", rest) + _done_frames(f"m{k + 1}", rest)
    return frames + (completed("r") if len(rest) <= 30 else [])


def _then_a_second_item(k, rest):
    return (_text_frames(f"m{k + 1}", rest[:20]) + _done_frames(f"m{k + 1}", rest[:20])
            + _reasoning(f"rs{k + 1}")
            + _text_frames(f"m{k + 1}b", rest[20:] + "\n" + MARKER + "\n" + APPENDIX))


def _then_a_call(k, rest):
    return _CALL_RUN


def _then_a_call_without_arguments(k, rest):
    return opened(_FN_RUN) + closed(dict(_FN_RUN, arguments="{}", status="completed"))


def _then_reasoning_inside(k, rest):
    first, second = rest[:20], rest[20:]
    return (_text_frames(f"m{k + 1}", first) + _reasoning(f"rs{k + 1}")
            + _text_frames(f"m{k + 1}", second + "\n" + MARKER + "\n" + APPENDIX)
            + _done_frames(f"m{k + 1}", rest))


def _then_incomplete(k, rest):
    return (_text_frames(f"m{k + 1}", rest[:15])
            + sse("response.incomplete", {
                "type": "response.incomplete",
                "response": {"id": "r", "status": "incomplete", "usage": {},
                             "incomplete_details": {"reason": "max_output_tokens"}}}))


def _then_interleaved(k, rest):
    return (_text_frames(f"m{k + 1}", rest[:10]) + _reasoning(f"rs{k + 1}")
            + _text_frames(f"m{k + 1}", rest[10:] + "\n" + MARKER + "\n" + APPENDIX)
            + _done_frames(f"m{k + 1}", rest) + completed("r"))


def _then_marker_prefix(k, rest):
    return _text_frames(f"m{k + 1}", rest[:12] + "TASK_COMP") + _reasoning(f"rs{k + 1}") + completed("r")


def _then_capital_t(k, rest):
    return _text_frames(f"m{k + 1}", rest[:12] + " GPT") + _reasoning(f"rs{k + 1}") + completed("r")


PLAIN = dict(seg_budget=BIG, total_cap=BIG, pad_tools=False)
RETRYING = dict(mode="control", model="test", retries=3)
CAPPED = dict(mode="after-deltas", abort_after=0, imagegen_cut=False, payload_cap=1024)
SHOWN = dict(mode="after-deltas", abort_after=1)
PADDED = dict(mode="segmented", pad_tools=True)
CONT = dict(seg_budget=30, max_segments=6, total_cap=BIG, input=GO)
SEAM = {"engine.SEAM_LOOKAHEAD": SEAM_LOOKAHEAD}        # the real rule, not the hard cut
CAPPED_B64 = stream_view.CAPPED_IMAGE_B64
HUGE = "A" * 100000            # stands in for a ~2MB base64 result
SECRET = "CLIENT-SECRET-PAYLOAD-9F3"
LEAK = "TOOL-ARGUMENTS-LEAK-9F3"
CALL = 'const r = await tools.exec_command({cmd:"printf hello > hello.txt"})'
PINNED = "11111111-2222-5333-8444-555555555555"
REASONING = {"id": "rs1", "type": "reasoning", "summary": [], "content": [], "encrypted_content": "gAAAA"}
OPERATOR_FIELDS = ("revision", "config_fingerprint", "knobs", "trace", "trace_records", "trace_errors",
                   "seq", "prev_hash", "hash", "route")

# One row per behaviour of `engine.run_upstream`: what the relay does with the stream a provider
# sends, what it sends the provider, and what it records. A row's options:
#   provider  what each upstream request is answered with, in order (the last one repeats): a
#             list of lines, an `Http` error, a `Breaks` response; or one `Continuing` provider
#   cfg       the run's configuration (`make_cfg`), only keys the engine reads
#   sink      the client, made for the run (FakeSink);  patch  module globals for the run
#   trace     "on", or "broken" (its file cannot be written): the trace is enabled for the run
# and `expect` paths into the views of what the run produced (`RunTests.views`).
RUNS = (
    # A continuation carries only the last TAIL_CHARS, not the whole transcript replayed
    # again on every segment, and the record carries what was sent per continuation.
    dict(name="the continuation is sliced by the configured tail, not a module constant",
         patch={"config.TAIL_CHARS": 10}, provider=(delta("A" * 5000) + args_delta("i1", "x" * 80, 1), completed()),
         cfg=dict(PLAIN, max_segments=2), expect={"rec.tail_chars": [10]}),
    # An explicit conversation-shaped `input`, which is what a real client sends: a string
    # input keeps its shape across a continuation, so a string here would test the string path
    # instead of the item replay.
    dict(name="the tail is bounded and the text before it is absent",
         provider=(delta("A" * (TAIL_CHARS + 400)) + args_delta("i1", "x" * 80, 1), completed()),
         cfg=dict(PLAIN, max_segments=2,
                  input=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "P"}]}]),
         expect={"rec.cuts": ["toolpad", "terminal"], "sent": Len(2), "tails.0": "A" * TAIL_CHARS,
                 "json(sent.1.input)": Lacks("A" * (TAIL_CHARS + 1)), "rec.tail_chars": [TAIL_CHARS]}),
    dict(name="the first request has no assistant replay", provider=(completed(),), cfg=dict(mode="control"),
         expect={"sent.0.input": IsA(str)}),
    dict(name="each continuations tail is recorded and bounded by the text delivered so far",
         provider=(delta("B" * 40) + args_delta("i1", "x" * 80, 1), delta("C" * 40) + args_delta("i1", "x" * 80, 1),
                   completed()),
         cfg=dict(max_segments=3, seg_budget=BIG, total_cap=BIG),
         expect={"rec.cuts": ["toolpad", "toolpad", "terminal"], "rec.tail_chars": Len(2) & Each(Le(TAIL_CHARS))}),
    # A forced flush must not withhold a suffix, because nothing more is coming: an answer
    # whose last character starts the marker or the appendix keeps it, and a fixture that
    # answers "A" * 40 records 40 characters delivered, not 39.
    dict(name="a final A is delivered", provider=(delta("USA") + completed(),), cfg=PLAIN, expect={"deltas": "USA"}),
    dict(name="a final T is delivered", provider=(delta("FORGOT") + completed(),), cfg=PLAIN,
         expect={"deltas": "FORGOT"}),
    # What actually protects the marker is the HOLD_CHARS window: a marker can only complete if
    # its fragment sits at the end of the held text, which is inside the window. The stream
    # does not end at once (the terminal flush would deliver the text whatever the code did).
    dict(name="a marker split across two deltas never reaches the client",
         provider=(delta("z" * (HOLD_CHARS + 50) + "TASK_COMPLET") + delta("E_9F3") + completed(),), cfg=PLAIN,
         expect={"rec.cuts": ["marker"], "deltas": Lacks("TASK_COMPL")}),
    # the SSE framing and the tail together
    dict(name="a delta split across two data lines reaches the client",
         provider=(_split_data_frame() + completed(),), cfg=dict(mode="control"), expect={"stream": Has("MULTI")}),
    # The relay's own abort paths, driven against a fake upstream.
    dict(name="after-deltas cuts on exactly the nth delta",
         provider=([ln for i in range(6) for ln in delta(f"d{i}")],), cfg=dict(mode="after-deltas", abort_after=3),
         expect={"rec.cuts": ["after-deltas"], "rec.cut": "after-deltas", "rec.deltas": 3,
                 "rec.real_completed": False, "deltas": Has("d0") & Lacks("d3")}),
    dict(name="after-deltas without a threshold does not cut",
         provider=([ln for i in range(3) for ln in delta(f"d{i}")] + completed(),),
         cfg=dict(mode="after-deltas", abort_after=0),
         expect={"rec.cuts": ["terminal"], "rec.real_completed": True}),
    # A trigger, not a hard ceiling: the abort flushes the held-back buffer with the pending
    # delta, so delivery overshoots by up to HOLD_CHARS plus one delta.
    dict(name="the cap stops a long stream well before its end",
         provider=([ln for _ in range(20) for ln in delta("A" * 100)],), cfg=dict(seg_budget=BIG, total_cap=500),
         expect={"rec.cut": "cap", "rec.real_completed": False, "rec.deltas": Lt(20),
                 "rec.delivered_chars": Lt(2000) & Le(500 + HOLD_CHARS + 100)}),
    dict(name="the cap does not fire below its threshold",
         provider=([ln for _ in range(2) for ln in delta("A" * 50)] + completed(),),
         cfg=dict(seg_budget=BIG, total_cap=5000), expect={"rec.cuts": ["terminal"], "rec.real_completed": True}),
    # A 503/502/429 before any output is retried with backoff; once anything has been
    # forwarded the request is not retried, because a second attempt would deliver the
    # answer twice.
    dict(name="a 503 is retried and then succeeds", provider=(Http(503), completed("r")), cfg=RETRYING,
         expect={"sent": Len(2), "rec.attempts": 2, "rec.retries": 1, "rec.real_completed": True}),
    dict(name="a client error is not retried", provider=(Http(400),), cfg=RETRYING,
         expect={"sent": Len(1), "rec.attempts": 1, "rec.retries": Falsy(), "rec.cut": "error"}),
    # N attempts wait N-1 times: the last failure is surfaced, not waited out.
    dict(name="the retries stop at their budget", provider=(Http(503),), cfg=dict(RETRYING, retries=3),
         expect={"sent": Len(3), "rec.attempts": 3, "rec.retries": 2, "rec.last_status": 503, "delays": Len(2),
                 "rec.cut": "error", "rec.error": Has("503")}),
    dict(name="the backoff doubles from its base", provider=(Http(503),), cfg=dict(RETRYING, retries=5),
         patch={"engine.RETRY_BASE_DELAY": 0.25}, expect={"delays": [0.25, 0.5, 1.0, 2.0]}),
    # The delta is consumed, not discarded, and no second connection replays it; the failure
    # is reported to the client rather than hidden.
    dict(name="nothing is retried after output has been forwarded",
         provider=(Breaks(delta("partial"), Http(503, "overloaded")),), cfg=RETRYING,
         expect={"rec.deltas": 1, "sent": Len(1), "rec.attempts": 1, "rec.cut": "error",
                 "stream": Has("relay.error", "503")}),
    # cliproxy answers `auth_unavailable` with 503 + Retry-After: the relay waits what it asked
    dict(name="retry-after is honoured",
         provider=(Http(503, "auth_unavailable", {"Retry-After": "1"}), completed("r")), cfg=RETRYING,
         expect={"delays": [1.0], "rec.attempts": 2, "rec.real_completed": True}),
    dict(name="retry-after is capped", provider=(Http(503, "x", {"Retry-After": "9999"}), []), cfg=RETRYING,
         expect={"delays": [engine.RETRY_MAX_DELAY]}),
    # A disconnect is labelled a disconnect wherever it lands, including between segments,
    # before any write has gone through `State.send`. Enough writes for the first segment's
    # deltas, then the reset lands as the continuation starts.
    dict(name="a reset before the first write of a continuation is client_gone",
         provider=([ln for _ in range(4) for ln in delta("x" * 60)] + delta("tail"),),
         cfg=dict(seg_budget=1, total_cap=BIG, max_segments=3, pad_tools=False),
         sink=functools.partial(_ResetAfterWrites, 3),
         expect={"rec.segs": 2, "rec.cut": "client_gone", "rec.error": None, "rec.client_gone": Truthy()}),
    # A failure after the first byte reaches the client as an error it can recognise, not only
    # as `relay.error` (an event no SDK reads); the synthetic terminal frame is skipped on
    # this path, so the error frame is what gives the client a status. The top-level `type`
    # is the field a generated SDK dispatches on, and it sits inside a numbered stream.
    dict(name="a midstream failure emits an OpenAI error event",
         provider=(Breaks(["event: response.output_text.delta\n",
                           'data: {"type":"response.output_text.delta","delta":"hi"}\n', "\n"],
                          RuntimeError, "upstream died mid-stream"),),
         cfg=dict(mode="control"),
         expect={"rec.cut": "error", "named(error)": Len(1), "named(error).0.type": "error",
                 "named(error).0.error.type": "server_error", "named(error).0.error.message": Truthy(),
                 "named(error).0": Has("sequence_number"), "names": Has("relay.error"),
                 "transcript": Golden("error")}),
    # More than HOLD_CHARS, so a numbered delta reaches the client before the failure.
    dict(name="the error frame keeps the streams sequence monotonic",
         provider=(Breaks(delta("x" * (HOLD_CHARS + 10), None), RuntimeError, "upstream died mid-stream"),),
         cfg=dict(mode="control"),
         expect={"seqs(response.output_text.delta,error)": Len(Ge(2)) & Lacks(None) & Increasing()}),
    dict(name="no terminal frame is faked on the error path",
         provider=(Breaks([], RuntimeError, "upstream died"),), cfg=dict(mode="control"),
         expect={"names": Lacks("response.completed")}),
    # The client going away is not a relay fault, and the record is written before the
    # client-facing tail of the stream. The delta exceeds HOLD_CHARS, so a write is made.
    dict(name="a client that hangs up is not an upstream error and still leaves a record",
         provider=(delta("x" * 400, None),), cfg=dict(mode="control"), sink=_DeadSink,
         expect={"rec.cut": "client_gone", "rec.error": None, "rec.client_gone": Starts("BrokenPipeError"),
                 "written": Truthy()}),
    # A record names the code and configuration that produced it, and never carries the
    # client's body.
    dict(name="every record names the revision and the config",
         provider=(sse("response.completed", {"type": "response.completed", "response": {"id": "r1"}}),),
         cfg=dict(mode="control"),
         expect={"written.-1.revision": Truthy(),
                 "written.-1.config_fingerprint": Has("pacing", "max_concurrent", "retries", "seg_budget",
                                                      "total_cap", "max_segments", "tail_chars")}),
    dict(name="a record never contains the clients body",
         provider=(sse("response.completed", {"type": "response.completed", "response": {"id": "r1"}}),),
         cfg=dict(mode="control", input=[{"role": "user", "content": [{"type": "input_text", "text": SECRET}]}]),
         expect={"log": Lacks(SECRET), "written.-1.prompt_chars": len(SECRET)}),
    # The segment loop reads upstream and writes to the client inside one `try`, and a reset on
    # either raises the same exception type: classifying by type would make every upstream reset
    # look like an abandoned request, with no error frame and no terminal frame.
    dict(name="an upstream read reset is an error the client is told, not a disconnect",
         provider=(Breaks([], ConnectionResetError, "upstream reset"),), cfg=dict(mode="control"),
         expect={"rec.cut": "error", "rec.client_gone": None, "rec.error": Has("ConnectionResetError"),
                 "names": Has("error")}),
    # An upstream that ends with no terminal frame and no cut of ours is a truncation; faking
    # completion over it is the exact silent failure this relay must not have.
    dict(name="eof without a terminal frame is an error",
         provider=(delta("partial answer", None),), cfg=dict(mode="control"),
         expect={"rec.cut": "error", "rec.error": Has("without response.completed"),
                 "names": Lacks("response.completed")}),
    # `json.loads` failing must not leave `obj = {}`: a frame cut mid-`data:` line would become
    # a no-op and the run would end as a clean short success.
    dict(name="a half written frame is an error, not a no-op",
         provider=(delta("good", None) + ["event: response.output_text.delta\n", 'data: {"type": "resp\n', "\n"],),
         cfg=dict(mode="control"),
         expect={"rec.cut": "error", "rec.error": Has("unparseable SSE frame"), "names": Lacks("response.completed")}),
    dict(name="a real terminal frame is still a completion",
         provider=(completed(usage={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}),),
         cfg=dict(mode="control"),
         expect={"rec.cut": "terminal", "rec.real_completed": True, "rec.verdict": "billed"}),
    # A negative test over the request body cannot catch text arriving through `usage`.
    dict(name="free text inside usage never reaches the record",
         provider=(completed(usage={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12,
                                    "attribution": {"items": {"msg_1": {
                                        "input_tokens": 5, "output_tokens": 7,
                                        "content": [{"text": LEAK, "input_tokens": 5}]}}}}),),
         cfg=dict(mode="control"),
         expect={"log": Lacks(LEAK), "written.-1.usage.total_tokens": 12,
                 "written.-1.usage.attribution.items.msg_1.input_tokens": 5,
                 "written.-1.usage.attribution.items.msg_1": Lacks("content")}),
    # The payload cap: what the relay delivers stops at `payload_cap`. The per-segment budget
    # counts answer text, and an image result is one base64 field of about two million
    # characters, which counts toward no budget, would go out whole, and the client would
    # store it and send it back on every later turn. What the removed volume is worth is not
    # established (docs/relay/relay-8371.md): the cap is justified by what crosses the wire and
    # what the client keeps. Both delivery sites hold the placeholder: the item's `done` frame,
    # and `response.completed.response.output`, which is built from the view.
    dict(name="an oversized result is capped on both delivery sites when the relay cuts",
         provider=(_image_done(HUGE) + delta("d0"),), cfg=dict(CAPPED, abort_after=1),
         expect={"rec.cut": "after-deltas", "rec.real_completed": False,
                 "stream": Lacks(HUGE) & Count(CAPPED_B64, 2), "rec.payload_capped": 1,
                 "rec.payload_chars_dropped": len(HUGE) - len(CAPPED_B64)}),
    # Not only the returned record: `view.report()` is merged before `log_run`, so an operator
    # reading the evidence file sees what was withheld.
    dict(name="an oversized result is capped on upstreams own completion, and the log counts it",
         provider=(_image_done(HUGE) + delta("d0") + completed(output=[]),), cfg=CAPPED,
         expect={"rec.real_completed": True, "stream": Lacks(HUGE) & Count(CAPPED_B64, 2),
                 "rec.payload_capped": 1, "written": Truthy(), "written.-1.payload_capped": 1,
                 "written.-1.payload_chars_dropped": len(HUGE) - len(CAPPED_B64)}),
    dict(name="a result under the cap is delivered untouched",
         provider=(_image_done("iVBORw0KGgo=") + delta("d0") + completed(output=[]),), cfg=CAPPED,
         expect={"stream": Has("iVBORw0KGgo="), "rec.payload_capped": 0, "rec.payload_chars_dropped": 0}),
    # `run_upstream` is called with a hand-built cfg by anything that is not the handler, and
    # off is the dangerous state, so absent resolves to the operator's default (65,536).
    dict(name="an absent cap means the configured policy, not off",
         provider=(_image_done(HUGE) + delta("d0") + completed(output=[]),),
         cfg=dict(mode="after-deltas", abort_after=0, imagegen_cut=False),
         expect={"cfg": Lacks("payload_cap"), "config.DEFAULT_PAYLOAD_CAP": Lt(len(HUGE)),
                 "stream": Lacks(HUGE), "rec.payload_capped": 1}),
    dict(name="a cap of zero is off",
         provider=(_image_done(HUGE) + delta("d0") + completed(output=[]),), cfg=dict(CAPPED, payload_cap=0),
         expect={"stream": Has(HUGE), "rec.payload_capped": 0, "rec.payload_chars_dropped": 0}),
    # A client's tool output is large and re-sent too, but it is referenced by the call that
    # produced it: rewriting one would break the conversation, so the cap is a whitelist.
    dict(name="a client tool result is not capped",
         provider=(opened({"id": "ct1", "type": "custom_tool_call_output", "call_id": "c1", "output": ""}, 1)
                   + closed({"id": "ct1", "type": "custom_tool_call_output", "call_id": "c1",
                             "output": "Z" * 200000}, 1)
                   + delta("d0") + completed(output=[]),),
         cfg=CAPPED, expect={"stream": Has("Z" * 200000), "rec.payload_capped": 0}),
    # The cap withholds content; it is not a cut. Ending the turn would cost the client the
    # answer the run was producing.
    dict(name="capping does not end the run",
         provider=(_image_done(HUGE) + delta("the answer") + completed(output=[]),),
         cfg=dict(seg_budget=BIG, total_cap=BIG, imagegen_cut=False, payload_cap=1024),
         expect={"rec.real_completed": True, "deltas": "the answer", "rec.payload_capped": 1}),
    # A frame is not a way around the cap, and the counters are per delivery: a payload
    # upstream sent twice is withheld twice.
    dict(name="a payload in the added frame is capped too",
         provider=(opened({"id": "ig1", "type": "image_generation_call", "status": "in_progress",
                           "result": HUGE}, 1)
                   + closed({"id": "ig1", "type": "image_generation_call", "status": "completed",
                             "result": HUGE}, 1)
                   + delta("d0") + completed(output=[]),),
         cfg=CAPPED,
         expect={"stream": Lacks(HUGE), "rec.payload_capped": 2,
                 "rec.payload_chars_dropped": 2 * (len(HUGE) - len(CAPPED_B64))}),
    # The image cut ends the run at the frame that names an image item. Without it the
    # provider produces a ~2M-character base64 `result` the relay then caps, and the turn takes
    # 60-140 seconds per image against 31 for the whole run with the cut. It does not claim a
    # points saving: two runs of one prompt moved the meter +12 and +1.
    dict(name="the run ends at the frame that names the image",
         provider=(delta("working on it") + _image() + delta("never delivered"),),
         cfg=dict(mode="after-deltas", abort_after=0),
         expect={"rec.cut": "imagegen", "rec.cuts": ["imagegen"], "rec.real_completed": False,
                 "deltas": "working on it"}),
    # The run ends four frames before the payload arrives, so the client never stores it. A cut
    # run leaves no item open: the image item is closed incomplete, as a cut client call is.
    # Absent, the knob is `DEFAULT_IMAGEGEN_CUT`: the expensive state is chosen explicitly.
    dict(name="the image payload never reaches the client, whose item is closed and terminated",
         provider=(_image(),), cfg=dict(mode="after-deltas", abort_after=0),
         expect={"stream": Lacks(IMAGE_PAYLOAD), "named(response.completed).0": NotNone(),
                 "named(response.completed).0.response.output": Len(1),
                 "named(response.completed).0.response.output.0.type": "image_generation_call",
                 "named(response.completed).0.response.output.0.status": "incomplete",
                 "named(response.completed).0.response.output.0": Lacks("result"),
                 "config.DEFAULT_IMAGEGEN_CUT": Truthy(), "cfg": Lacks("imagegen_cut"), "rec.cut": "imagegen",
                 "transcript": Golden("image")}),
    dict(name="the knob turns the image cut off",
         provider=(_image("small-payload") + completed(output=[]),),
         cfg=dict(mode="after-deltas", abort_after=0, imagegen_cut=False),
         expect={"rec.cuts": Lacks("imagegen"), "rec.cut": "terminal", "stream": Has("small-payload")}),
    # the image arm sits at the frame type the tool-call bookkeeping uses
    dict(name="a client tool call is not an image",
         provider=(opened({"id": "fc1", "type": "function_call", "name": "exec", "call_id": "c1",
                           "status": "in_progress"})
                   + args_done("fc1", "{}")
                   + closed({"id": "fc1", "type": "function_call", "name": "exec", "call_id": "c1",
                             "status": "completed", "arguments": "{}"}),),
         cfg=dict(seg_budget=BIG, total_cap=BIG),
         expect={"rec.cut": "toolcall", "rec.cuts": Lacks("imagegen")}),
    # The opt-in trace at the engine boundary: off unless asked for, one run's frames grouped
    # under one id, the terminal frames included (they are what an investigation is usually
    # about), and it can never break the run it is observing.
    dict(name="a run is traced and grouped under one id", trace="on",
         provider=(message_frames(delta="x" * 40),), cfg=SHOWN,
         expect={"distinct(trace.*.dir)": {"note", "request", "up", "down"},
                 "distinct(trace.*.run)": Len(1), "trace.*.run": Each(NotNone()),
                 "traced(down).*.event": Has("response.output_text.delta", "response.completed"),
                 "traced(* response.completed).0.data.response.synthetic": Truthy(),
                 "traced(* response.completed).0.data.response": Has("output")}),
    dict(name="a withheld frame is recorded as skipped, not as sent", trace="on",
         provider=(opened({"id": "pad1", "type": "function_call", "name": renamer.DUMMY_TOOL["name"],
                           "call_id": "c1", "status": "in_progress", "arguments": ""})
                   + args_delta("pad1", "y" * 60),),
         cfg=dict(PADDED),
         expect={"traced(skipped)": Truthy(), "traced(skipped).0.note": Has("padding tool"),
                 "traced(down response.output_item.added).*.item.id": Lacks("pad1")}),
    # A frame the renamer drops is recorded too: a reader can tell "withheld" from "never arrived".
    dict(name="a dropped repeat is recorded as skipped, not as absent", trace="on",
         provider=(sse("response.created", {"type": "response.created", "response": {"id": "r1"}})
                   + sse("response.created", {"type": "response.created", "response": {"id": "r2"}})
                   + message_frames(delta="hi" + MARKER),),
         cfg=dict(mode="marker"),
         expect={"traced(up response.created)": Len(2), "traced(down response.created)": Len(1),
                 "traced(skipped response.created)": Len(1)}),
    # A trace that has stopped writing is an incident, visible where the operator already
    # looks; and a trace that can break the thing it observes is worse than no trace.
    dict(name="a broken trace does not break the run, and the record says how it is doing", trace="broken",
         provider=(message_frames(delta="x" * 40),), cfg=SHOWN,
         expect={"rec.trace": Ref("trace_path"), "rec.trace_errors": Ge(1), "rec.cut": "after-deltas",
                 "tracer.errors": Ge(1)}),
    dict(name="the trace is off unless it is enabled", provider=(message_frames(delta="x" * 40),), cfg=SHOWN,
         expect={"tracer": None, "trace_files": []}),
    # The frame sequence a strict Responses client needs to finalize.
    dict(name="response.created is forwarded exactly once",
         provider=(sse("response.created", {"type": "response.created", "response": {"id": "r1"}})
                   + sse("response.in_progress", {"type": "response.in_progress", "response": {"id": "r1"}})
                   + completed(),),
         cfg=dict(mode="control"),
         expect={"types.0": "response.created", "count(response.created)": 1,
                 "count(response.in_progress)": 1}),
    # after-deltas cuts on the first delta, so the message is open when the relay synthesizes
    # the terminal frame: the exact shape Codex could not finalize. The abort still happened.
    dict(name="a cut message is closed and the completed output matches the deltas",
         provider=(message_frames(delta="x" * 40),), cfg=SHOWN,
         expect={"count(response.output_text.done)": 1, "count(response.content_part.done)": 1,
                 "count(response.output_item.done)": 1, "count(response.completed)": 1,
                 "text_of(response.output_text.delta)": "x" * 40,
                 "of(response.output_text.done).0.text": "x" * 40,
                 "completed.output.0.content.0.text": "x" * 40,
                 "rec.real_completed": False, "rec.cut": "after-deltas"}),
    dict(name="held back text never reaches the client or the output",
         provider=(message_frames(delta="the answer" + MARKER + APPENDIX + "notes"),), cfg=dict(mode="marker"),
         expect={"completed.output.0.content.0.text": "the answer", "json(objs)": Lacks(MARKER, APPENDIX),
                 "transcript": Golden("marker")}),
    # pad_tools is what makes the relay inject its own tool; only then is its name one the
    # relay may withhold
    dict(name="the relays pad call is never shown",
         provider=(_tool_call(renamer.DUMMY_TOOL["name"], "pad1", "c-pad1", PAD_CHUNKS, done=False),),
         cfg=dict(PADDED),
         expect={"rec.cut": "toolpad", "rec.hidden_pad_calls": 1, "stream": Lacks("function_call"),
                 "completed.output": []}),
    # The relay's own padding call is the abort window; a client's call is not: cut at 50
    # argument characters, every real `exec` a Codex session makes is truncated. The abort
    # lands when the call is complete: the client gets a call it can run, and `usage` never
    # finalizes because the provider's `response.completed` is never read.
    dict(name="a clients tool call is delivered whole and the abort lands after it",
         provider=(opened({"id": "ctc1", "type": "custom_tool_call", "name": "exec", "call_id": "c1",
                           "status": "in_progress", "input": ""})
                   + [ln for i in range(0, len(CALL), 20) for ln in input_delta("ctc1", CALL[i:i + 20])]
                   + input_done("ctc1", CALL)
                   + closed({"id": "ctc1", "type": "custom_tool_call", "name": "exec", "call_id": "c1",
                             "status": "completed", "input": CALL})
                   + completed(usage={"input_tokens": 9}),),
         cfg=dict(PADDED, max_segments=6),
         expect={"rec.cut": "toolcall", "rec.segs": 1, "rec.real_completed": False, "rec.cut_tool_calls": 0,
                 "of(response.output_item.done).0.item.status": "completed",
                 "text_of(response.custom_tool_call_input.delta)": CALL, "completed.output.0.input": CALL,
                 "transcript": Golden("tool")}),
    # The leak the `itemdone` cut closes. A strict structured output constrains the model to
    # one JSON object, so it cannot write the marker line, and calling the padding tool would
    # violate the schema: the model closes the text item and the next frame is
    # `response.completed`, which the relay must not read: reading it bills the run.
    dict(name="a text item that closes without the marker cuts instead of billing",
         provider=(message_frames(item_id="msg_1", output_index=0, delta='{"title":"Generate pelican SVG"}')
                   + text_done("msg_1", '{"title":"Generate pelican SVG"}')
                   + closed({"id": "msg_1", "type": "message", "status": "completed"})
                   + completed(usage={"input_tokens": 9, "output_tokens": 8}),),
         cfg=dict(PADDED, max_segments=6, extra=STRUCTURED),
         expect={"rec.cut": "itemdone", "rec.segs": 1, "rec.real_completed": False, "rec.usage": None,
                 "deltas": '{"title":"Generate pelican SVG"}', "completed.synthetic": Truthy(),
                 "completed.output.0.content.0.text": '{"title":"Generate pelican SVG"}'}),
    # The model answers with reasoning and closes an empty item: ending the turn would make an
    # empty answer final; continuing gives it the fresh-answer segment it needs.
    dict(name="an empty text item continues the run instead of ending it",
         provider=(message_frames(item_id="msg_1", output_index=0)
                   + closed({"id": "msg_1", "type": "message", "status": "completed"})
                   + completed(usage={"input_tokens": 9}),
                   message_frames(item_id="msg_2", output_index=0, delta="the answer" + MARKER)),
         cfg=dict(PADDED, max_segments=6, extra=STRUCTURED),
         expect={"rec.cuts": ["itemdone-empty", "marker"], "rec.segs": 2, "rec.real_completed": False,
                 "json(sent.1)": Has("interrupted before any answer text")}),
    # With a tool of the client's own the model may emit a short preamble and then the call:
    # the itemdone cut is gated on the request having offered no tool of its own.
    dict(name="a request with the clients own tool keeps the toolcall cut",
         provider=(_preamble_then_call("Let me check that file.", "ls"),),
         cfg=dict(PADDED, max_segments=6, extra=STRUCTURED,
                  tools=[{"type": "function", "name": "exec", "parameters": {"type": "object", "properties": {}}}]),
         expect={"rec.cut": "toolcall", "rec.segs": 1, "rec.real_completed": False,
                 "stream": Has("custom_tool_call")}),
    # `control` is the do-nothing baseline the billing measurements are read against.
    dict(name="control never takes the itemdone cut",
         provider=(_closed_message("a short answer", {"input_tokens": 9, "output_tokens": 4}),),
         cfg=dict(mode="control"), expect={"rec.cut": "terminal", "rec.real_completed": True}),
    # The gate is the structured-output flag. Codex sends its tools inside the `input` items,
    # so the pad is injected for a whole Codex turn, and a Codex turn routinely writes a
    # one-line preamble, closes it, and then calls: 22 runs in the trace do exactly that.
    # Their turn ends where it always did, on the client's own call.
    dict(name="a prose request keeps the call that follows a preamble",
         provider=(_preamble_then_call("I'll create the file now.", "printf hello > hello.txt"),),
         cfg=dict(PADDED, max_segments=6),
         expect={"rec.cut": "toolcall", "rec.segs": 1, "rec.real_completed": False,
                 "stream": Has("custom_tool_call")}),
    # the residual the cut does not claim
    dict(name="a prose request that closes a text item is not cut either",
         provider=(_closed_message("A short answer with no marker in it.", {"input_tokens": 9, "output_tokens": 8}),),
         cfg=dict(PADDED, max_segments=6), expect={"rec.cut": "terminal", "rec.real_completed": True}),
    # `parallel_tool_calls` can interleave two calls. The open call is tracked by item id, or a
    # pad's resumed arguments are read as a client's, no cut fires and the provider finalizes
    # `usage`.
    dict(name="an interleaved pad call is still the abort window",
         provider=(opened({"id": "pad1", "type": "function_call", "name": renamer.DUMMY_TOOL["name"],
                           "call_id": "c0", "status": "in_progress"})
                   + args_delta("pad1", '{"summary"')
                   + opened({"id": "ctc1", "type": "custom_tool_call", "name": "exec", "call_id": "c1",
                             "status": "in_progress"}, 1)
                   + [ln for _ in range(6) for ln in args_delta("pad1", "x" * 20)]
                   + completed(usage={"input_tokens": 9}),),
         cfg=dict(PADDED), expect={"rec.cut": "toolpad", "rec.real_completed": False}),
    # A cut that fires too eagerly costs one truncated call; one that never fires costs a
    # finalized `usage`. The guess is visible in the record.
    dict(name="arguments with no added frame are counted toward the cut and reported",
         provider=(args_delta("ghost", "y" * 60) + completed(usage={"input_tokens": 9}),), cfg=dict(PADDED),
         expect={"rec.cut": "toolpad", "rec.real_completed": False, "rec.untracked_tool_deltas": Ge(1)}),
    # upstream ended the arguments and was cut before it closed the item
    dict(name="the arguments are ended once when upstream already ended them",
         provider=(opened({"id": "fc4", "type": "function_call", "name": "exec", "call_id": "c4",
                           "status": "in_progress", "arguments": ""})
                   + args_delta("fc4", '{"cmd": "ls"}') + args_done("fc4", '{"cmd": "ls"}')
                   + message_frames(item_id="m1", output_index=1, delta="hi" + MARKER),),
         cfg=dict(mode="marker"), expect={"count(response.function_call_arguments.done)": 1}),
    # The relay withholds HOLD_CHARS of text while it scans for the marker, but upstream's
    # closing frames are not withheld: without a flush the client would get the item's `done`
    # first, and a strict consumer drops a delta whose item is closed ("OutputTextDelta
    # without active item").
    dict(name="held back text precedes the frame that closes its item",
         provider=(message_frames(item_id="m1", output_index=0, delta="A" * 200) + text_done("m1", "A" * 200)
                   + sse("response.content_part.done",
                         {"type": "response.content_part.done", "output_index": 0, "item_id": "m1",
                          "content_index": 0, "part": {"type": "output_text", "text": "A" * 200}})
                   + closed({"id": "m1", "type": "message", "role": "assistant", "status": "completed",
                             "content": [{"type": "output_text", "text": "A" * 200}]})
                   + completed(usage={"input_tokens": 3}),),
         cfg=dict(mode="control"),
         expect={"count(response.output_text.delta)": Truthy(),
                 "last(response.output_text.delta)": Lt(Ref("first(response.output_item.done)")),
                 "of(response.output_text.done).0.text": Ref("text_of(response.output_text.delta)"),
                 "completed.output.0.content.0.text": Ref("text_of(response.output_text.delta)")}),
    # `output_text.done` and `content_part.done` are two events: upstream can send the first
    # and be cut before the second.
    dict(name="a part is closed even when upstream only ended its text",
         provider=(opened({"id": "m9", "type": "message", "role": "assistant", "status": "in_progress",
                           "content": []})
                   + sse("response.content_part.added",
                         {"type": "response.content_part.added", "output_index": 0, "item_id": "m9",
                          "content_index": 0, "part": {"type": "output_text", "annotations": []}})
                   + sse("response.output_text.delta",
                         {"type": "response.output_text.delta", "output_index": 0, "item_id": "m9",
                          "content_index": 0, "delta": "answer"})
                   + text_done("m9", "answer")
                   + message_frames(item_id="m2", output_index=1, delta="next" + MARKER),),
         cfg=dict(mode="marker"),
         expect={"count(response.output_text.done)": 2, "count(response.content_part.done)": 2}),
    dict(name="an item upstream never finished is not reported in progress",
         provider=(opened({"id": "rs2", "type": "reasoning", "status": "in_progress", "summary": [],
                           "encrypted_content": "gAAAA"})
                   + message_frames(item_id="m1", output_index=1, delta="hi" + MARKER),),
         cfg=dict(mode="marker"), expect={"item_done(rs2).0.item.status": "incomplete"}),
    # A reasoning item has no `content` and no `completed` status of its own.
    dict(name="a reasoning item is closed as upstream described it",
         provider=(opened(REASONING) + message_frames(item_id="m1", output_index=1, delta="hi" + MARKER),),
         cfg=dict(mode="marker"),
         expect={"item_done(rs1)": Len(1), "item_done(rs1).0.item": REASONING,
                 "item_done(rs1).0.item.status": Ne("completed")}),
    # The pad tool is the relay's only when the relay added it.
    dict(name="a client tool the relay did not inject is never withheld",
         provider=(opened({"id": "fc5", "type": "function_call", "name": renamer.DUMMY_TOOL["name"],
                           "call_id": "c5", "status": "in_progress", "arguments": ""})
                   + args_delta("fc5", '{"a": 1}')
                   + closed({"id": "fc5", "type": "function_call", "name": renamer.DUMMY_TOOL["name"],
                             "call_id": "c5", "status": "completed", "arguments": '{"a": 1}'})
                   + completed(usage={"input_tokens": 3}),),
         cfg=dict(PADDED, tools=[{"type": "function", "name": renamer.DUMMY_TOOL["name"],
                                  "parameters": {"type": "object"}}]),
         expect={"rec.hidden_pad_calls": 0, "stream": Has("function_call")}),
    # the same provider item id can arrive at a new index in the next segment, which only
    # reads correctly if the previous item was closed first
    dict(name="an item is closed before the next segment opens one",
         provider=(message_frames(item_id="m1", output_index=0, delta="A" * 20),
                   message_frames(item_id="m2", output_index=0, delta="B" * 20 + MARKER)),
         cfg=dict(seg_budget=10, max_segments=2, merge_text=False),
         expect={"rec.cuts": ["budget", "marker"],
                 "first(response.output_item.done)": Lt(Ref("last(response.output_item.added)")),
                 "count(response.output_item.done)": 2, "count(response.output_item.added)": 2}),
    # Keyed on the name alone, the cut would take the call of a client that defines
    # `discarded_placeholder` as the abort window. Its own tool means no dummy is injected.
    dict(name="a client tool named like the pad is still the clients",
         provider=(_tool_call(renamer.DUMMY_TOOL["name"], "fc1", "c1",
                              [OWN_CALL[i:i + 20] for i in range(0, len(OWN_CALL), 20)]),),
         cfg=dict(PADDED, tools=[{"type": "function", "name": renamer.DUMMY_TOOL["name"],
                                  "parameters": {"type": "object"}}]),
         expect={"rec.hidden_pad_calls": 0, "rec.cut": "toolcall",
                 "of(response.output_item.done).0.item.arguments": OWN_CALL}),
    dict(name="a tool call that completes is forwarded whole",
         provider=(opened({"id": "fc3", "type": "function_call", "name": "exec", "call_id": "c3",
                           "status": "in_progress", "arguments": ""})
                   + args_delta("fc3", '{"cmd": "ls"}') + args_done("fc3", '{"cmd": "ls"}')
                   + closed({"id": "fc3", "type": "function_call", "name": "exec", "call_id": "c3",
                             "status": "completed", "arguments": '{"cmd": "ls"}'})
                   + completed(usage={"input_tokens": 3}),),
         cfg=dict(mode="control"),
         expect={"rec.cut_tool_calls": 0, "completed.output.0.status": "completed",
                 "completed.output.0.arguments": '{"cmd": "ls"}'}),
    # A synthetic completion's usage is an estimate and says so: the relay does not know the
    # cached or reasoning splits, so it does not claim them.
    dict(name="the synthetic usage is an estimate with no unmeasured details",
         provider=(message_frames(delta="y" * 40),), cfg=SHOWN,
         expect={"completed.synthetic": Truthy(),
                 "completed.usage.input_tokens": Eq(Ref("rec.upstream_input_chars", lambda n: n // 4)) & Gt(0),
                 "completed.usage.total_tokens": Ref("completed.usage",
                                                     lambda u: u["input_tokens"] + u["output_tokens"]),
                 "completed.usage": Lacks("input_tokens_details", "output_tokens_details"),
                 "rec.usage_source": "synthetic-estimate", "rec.upstream_input_chars": Gt(0)}),
    dict(name="a real completion reports the provider numbers as provider numbers",
         provider=(completed(usage={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}),),
         cfg=dict(mode="control"),
         expect={"rec.usage_source": "provider", "rec.usage.input_tokens": 5, "completed.usage.input_tokens": 5}),
    # The provider's reason survives, split by audience: the record keeps identifiers, the
    # client, whose request it is, gets the message. No response.completed reaches the client
    # on this path, so there is no usage to label.
    dict(name="the code is kept as evidence and the message goes to the client",
         provider=(Http(400, "Bad Request", body=json.dumps({"error": {
             "code": "thinking_signature_invalid", "type": "invalid_request_error",
             "message": "the encrypted content did not match, and here is the client's prompt echoed back"}}).encode()),),
         cfg=dict(),
         expect={"rec.cut": "error",
                 "rec.upstream_error": {"status": 400, "code": "thinking_signature_invalid",
                                        "type": "invalid_request_error"},
                 "rec.usage_source": None, "rec.error": Has("thinking_signature_invalid"),
                 "json(rec)": Lacks("echoed back"), "of(error)": Len(1), "of(error).0.message": Has("echoed back")}),
    *(dict(name=f"a code that is not identifier shaped is not recorded: {hostile!r:.20}",
           provider=(Http(400, "Bad Request", body=json.dumps({"error": {"code": hostile, "message": "m"}}).encode()),),
           cfg=dict(), expect={"rec.upstream_error": Lacks("code"), "rec.upstream_error.status": 400})
      for hostile in ("../../etc/passwd", "has spaces", "x" * 65, "")),
    # `response.incomplete` and `response.failed` are the provider's terminal frames too: read
    # as unknown frames, a billed turn would be recorded `usage-discarded`, the held-back text
    # would never reach the client, and an `error` frame would follow the client's terminal.
    dict(name="an incomplete response is recorded as billed",
         provider=(_terminal({"type": "response.incomplete",
                              "response": {"id": "resp_1", "status": "incomplete",
                                           "incomplete_details": {"reason": "max_output_tokens"},
                                           "usage": {"input_tokens": 900, "output_tokens": 16,
                                                     "total_tokens": 916}}}),),
         cfg=dict(mode="control"),
         expect={"rec.cut": "terminal", "rec.provider_terminal": "response.incomplete",
                 "rec.real_completed": True, "rec.verdict": "billed", "rec.usage_source": "provider",
                 "rec.usage.output_tokens": 16, "rec.error": None, "deltas": "short answer",
                 "names": Lacks("error", "response.completed"),
                 "after(response.incomplete)": ["relay.summary"], "transcript": Golden("terminal")}),
    dict(name="an incomplete response without usage claims no provider usage",
         provider=(_terminal({"type": "response.incomplete", "response": {"id": "resp_1", "status": "incomplete"}}),),
         cfg=dict(mode="control"),
         expect={"rec.real_completed": True, "rec.usage": None, "rec.usage_source": None}),
    dict(name="a failed response keeps its usage without claiming completion",
         provider=(_terminal({"type": "response.failed",
                              "response": {"id": "resp_1", "status": "failed",
                                           "usage": {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5}}}),),
         cfg=dict(mode="control"),
         expect={"rec.real_completed": False, "rec.verdict": "usage-discarded", "rec.usage.input_tokens": 5,
                 "rec.usage_source": "provider"}),
    dict(name="a failed response ends the run once",
         provider=(_terminal({"type": "response.failed",
                              "response": {"id": "resp_1", "status": "failed",
                                           "error": {"code": "server_error", "message": "boom"}}}),),
         cfg=dict(mode="control"),
         expect={"rec.cut": "error", "rec.provider_terminal": "response.failed",
                 "rec.error": "upstream response.failed: server_error", "rec.real_completed": False,
                 "rec.usage_source": None, "names": Lacks("error"),
                 "after(response.failed)": ["relay.error", "relay.summary"]}),
    # A budget cut flushes the hold-back window except a marker prefix at its end: flushed, it
    # would reach the client, its other half in the next segment would never be matched, and
    # the appendix would stream.
    dict(name="a marker split by a budget cut is still caught",
         provider=(delta("A" * 20 + MARKER[:9]), delta(MARKER[9:] + "\n" + APPENDIX + " notes", "i1")),
         cfg=dict(seg_budget=25, max_segments=2, total_cap=BIG),
         expect={"rec.cuts": ["budget", "marker"], "deltas": "A" * 20}),
    # the carried `T` belongs to an item that is already closed
    dict(name="a carry never lands on a tool call",
         provider=(delta("A" * 20 + "T", "msg_0"), _CALL_RUN), cfg=dict(seg_budget=10, max_segments=2, total_cap=BIG),
         expect={"rec.cuts": ["budget", "toolcall"], "named(response.output_text.delta).*.item_id": ["msg_0"],
                 "rec.carry_dropped_chars": 1}),
    # if a textless segment dropped it, a marker whose second half arrives two segments later
    # would never be matched
    dict(name="a textless segment hands the carry on",
         provider=(delta("A" * 20 + MARKER[:9]), args_delta("untracked", "x" * 60),
                   delta(MARKER[9:] + "\n" + APPENDIX + " notes", "i2")),
         cfg=dict(seg_budget=25, max_segments=3, total_cap=BIG),
         expect={"rec.cuts": ["budget", "toolpad", "marker"], "deltas": "A" * 20, "rec.carry_dropped_chars": 0}),
    dict(name="a carry with no item to join is dropped and counted, never sent with no id",
         provider=(delta("A" * 20 + "T", "msg_0"), completed("r")), cfg=dict(seg_budget=10, max_segments=2, total_cap=BIG),
         expect={"named(response.output_text.delta).*.item_id": Each(Truthy()), "rec.carry_dropped_chars": 1,
                 "rec.delivered_chars": 20}),
    # holding back is for the next segment's scan; on the last one a held `T` would be lost
    dict(name="a trailing prefix is delivered when no segment follows",
         provider=(delta("A" * 20 + "T"),), cfg=dict(seg_budget=10, max_segments=1, total_cap=BIG),
         expect={"rec.cuts": ["budget"], "deltas": "A" * 20 + "T"}),
    # The `toolpad` cut is the other cut a segment continues past, and it carries the window
    # into the next segment as the budget cut does.
    dict(name="a marker split by a toolpad cut is still caught",
         provider=(delta("A" * 20 + MARKER[:9]) + args_delta("pad", "x" * 60, 1),
                   delta(MARKER[9:] + "\n" + APPENDIX + " notes", "i1")),
         cfg=dict(seg_budget=BIG, max_segments=2, total_cap=BIG),
         expect={"rec.cuts": ["toolpad", "marker"], "deltas": "A" * 20}),
    # `marker` mode runs one segment whatever `max_segments` says
    dict(name="outside segmented mode nothing is held back",
         provider=(delta("A" * 20 + "T") + args_delta("pad", "x" * 60, 1),), cfg=dict(mode="marker", max_segments=6),
         expect={"rec.cuts": ["toolpad"], "deltas": "A" * 20 + "T", "rec.carry_dropped_chars": 0}),
    # `relay.summary` is the client's copy of its run record. The deployment's revision,
    # configuration, cut policy, trace path and chain position are the operator's and stay on
    # `/api/runs`. The fields are a literal list, not the constant: one dropped from the
    # constant turns this red instead of quietly dropping its own assertion.
    dict(name="the summary leaves the operator fields out", provider=(completed("r"),), cfg=dict(),
         expect={"named(relay.summary).0": Lacks(*OPERATOR_FIELDS),
                 **{f"named(relay.summary).0.{field}": Ref(f"rec.{field}")
                    for field in ("cut", "cuts", "segs", "verdict", "provider_terminal")},
                 "rec": Has("revision", "config_fingerprint", "knobs", "seq", "hash", "route")}),
    # The record names the cut policy the run actually ran with; the config fingerprint names
    # only the configured defaults.
    dict(name="the per request knobs are recorded", provider=(completed("r"),),
         cfg=dict(seg_budget=300, total_cap=900, max_segments=3, payload_cap=0, imagegen_cut=False, retries=2),
         expect={"rec.knobs": {"seg_budget": 300, "total_cap": 900, "max_segments": 3, "abort_after": 0,
                               "payload_cap": 0, "imagegen_cut": False, "merge_text": True, "text_calls": False,
                               "text_calls_pad": True, "session_pin": False, "retries": 2}}),
    # the flag, never the id, is what the record keeps
    *(dict(name=f"the pin is a flag in the record and a header on every segment: pinned {bool(pinned)}",
           provider=(completed("r"),), cfg=dict(session_pin_id=pinned or None, headers={"session-id": "from-the-caller"}),
           expect={"rec.knobs.session_pin": bool(pinned), "headers.0.session-id": pinned or "from-the-caller",
                   "json(rec)": Lacks(pinned or "no id")})
      for pinned in ("", PINNED)),
    # A budget cut lands after the last whitespace, so the continuation starts a new word: 9 of
    # 15 relayed answers had a word cut or a space lost. 4-character deltas, so the budget (30)
    # is crossed in the middle of a word. What the client holds when the first cut is made is
    # exactly the tail the continuation is given.
    dict(name="every cut lands after a space and the continuation is given the text the client has",
         patch=SEAM, provider=Continuing(ANSWER, step=4), cfg=dict(CONT, merge_text=False),
         expect={"rec.cut": "marker", "sent": Len(Ge(3)), "deltas": Stripped(ANSWER) & Starts(Ref("tails.0")),
                 "tails": Each(Ends(" ") & PrefixOf(ANSWER)), "tails.0": Len(Lt(len(ANSWER))),
                 "rec.delivered_chars": Ref("deltas", len)}),
    # nothing to cut at: the budget plus the lookahead, then where it stands
    dict(name="text without whitespace is cut once the lookahead has passed",
         patch=SEAM, provider=Continuing("A" * 700, step=50), cfg=dict(CONT, seg_budget=60, merge_text=False),
         expect={"rec.cuts.0": "budget", "rec.seg_chars.0": Ge(60 + SEAM_LOOKAHEAD) & Le(60 + SEAM_LOOKAHEAD + 50),
                 "deltas": Stripped("A" * 700)}),
    # no continuation follows, so no partial word is dropped; the run's own notice follows it
    dict(name="the last segment a run may have keeps everything it received",
         patch=SEAM, provider=Continuing(ANSWER, step=4), cfg=dict(CONT, max_segments=1, merge_text=False),
         expect={"rec.cuts": ["budget"], "said": Len(Ge(30)) & PrefixOf(ANSWER), "rec.delivered_chars": Ref("said", len)}),
    # `merge_text`: a text answer cut at its budget is one message for the client, and the
    # text is the same merged or not. A run the model finished is neither marked nor noticed.
    dict(name="a continued answer is one message on the first items id",
         provider=Continuing(ANSWER), cfg=CONT,
         expect={"rec.cut": "marker", "sent": Len(Ge(3)), "rec.merged_segments": Ref("sent", lambda s: len(s) - 1),
                 "client_ids": ["m1"], "client_text.m1": Stripped(ANSWER) & Lacks("[relay:"),
                 "client_output": Len(1), "client_output.0.id": "m1",
                 "client_output.0.content.0.text": Stripped(ANSWER), "client_output.0.status": "completed",
                 "stream": Lacks('"item_id": "m2"'), "deltas": Stripped(ANSWER), "rec.truncated": False,
                 "transcript": Golden("merged")}),
    dict(name="merge off, it is one message per segment", provider=Continuing(ANSWER), cfg=dict(CONT, merge_text=False),
         expect={"rec.merged_segments": 0, "client_ids": Ref("sent", lambda s: [f"m{k + 1}" for k in range(len(s))])}),
    dict(name="a continuation the provider finishes closes the message once with all the text",
         provider=Continuing(ANSWER, then=_then_finished), cfg=CONT,
         expect={"client_ids": ["m1"], "client_text.m1": ANSWER, "client_output.0.content.0.text": ANSWER,
                 "stream": Count('"type": "response.output_item.done"', 1)}),
    # the first segment's reasoning is the answer's own and goes out; the later segments' never does
    dict(name="a continuations own reasoning is withheld and the text still merges",
         provider=Continuing(ANSWER, reasoning=True), cfg=CONT,
         expect={"sent": Len(Ge(3)), "rec.merged_segments": Ref("sent", lambda s: len(s) - 1),
                 "client_ids": ["rs1", "m1"], "client_text.m1": Stripped(ANSWER), "stream": Lacks('"rs2"'),
                 "client_output.*.id": ["rs1", "m1"]}),
    dict(name="a second item after the continuations finished message is its own message",
         provider=Continuing(ANSWER, then=_then_a_second_item), cfg=CONT,
         expect={"client_ids.0": "m1", "client_ids.1": "rs2", "message_text": Stripped(ANSWER)}),
    dict(name="a client tool call after a held message closes the message first",
         provider=Continuing(ANSWER, then=_then_a_call), cfg=CONT,
         expect={"rec.cuts": ["budget", "toolcall"], "client_output.*.id": ["m1", "fc1"]}),
    # A run the relay ends on its length limit says so: the message closes `incomplete`, the
    # text ends with the relay's notice, and the response stays `completed`:
    # Codex retries a `response.incomplete` five times and fails.
    dict(name="a run ended on the cap closes the merged message, marked and saying so",
         provider=Continuing(ANSWER * 4), cfg=dict(CONT, total_cap=70, max_segments=20),
         expect={"rec.cut": "cap", "rec.truncated": True, "client_ids": ["m1"],
                 "client_output.0.content.0.text": Ref("client_text.m1"),
                 "client_text.m1": Has(NOTICE, "[relay: this answer was cut at its length limit (")
                 & Stripped(Ends("characters)]")),
                 "said": Stripped(PrefixOf(ANSWER * 4)), "client_output.0.status": "incomplete",
                 "of(response.completed)": Len(1), "of(response.completed).0.response.status": "completed",
                 "of(response.incomplete)": Falsy(), "transcript": Golden("truncated")}),
    dict(name="a run ended on the segment limit closes the merged message, marked too",
         provider=Continuing(ANSWER * 4), cfg=dict(CONT, max_segments=3),
         expect={"rec.cut": "budget", "rec.segs": 3, "client_ids": ["m1"], "rec.truncated": True,
                 "client_text.m1": Has(CUT_NOTICE), "client_output.0.status": "incomplete"}),
    dict(name="the client going away while a message is held open is client_gone",
         provider=Continuing(ANSWER), cfg=dict(CONT, max_segments=4), sink=functools.partial(_ResetAfterWrites, 7),
         expect={"rec.cut": "client_gone", "rec.error": None}),
    dict(name="without merging, the last message carries the notice",
         provider=Continuing(ANSWER * 4), cfg=dict(CONT, total_cap=70, max_segments=20, merge_text=False),
         expect={"client_texts.-1": Has(CUT_NOTICE), "client_texts_but_last": Lacks("[relay:"),
                 "client_output.-1.status": "incomplete", "client_output.0.status": "completed"}),
    dict(name="a run that ends on a tool call is not truncated",
         provider=Continuing(ANSWER, then=_then_a_call_without_arguments), cfg=CONT,
         expect={"rec.cut": "toolcall", "rec.truncated": False, "stream": Lacks("[relay:")}),
    dict(name="without merging, a budget cut on the last segment still gets the notice",
         provider=Continuing(ANSWER * 4), cfg=dict(CONT, max_segments=3, merge_text=False),
         expect={"rec.cut": "budget", "rec.truncated": True, "client_texts.-1": Has(CUT_NOTICE),
                 "client_output.*.status": ["completed", "completed", "incomplete"]}),
    dict(name="a client that leaves during the notice is a disconnect, not an exception",
         provider=Continuing(ANSWER * 4), cfg=dict(CONT, max_segments=20, total_cap=70), sink=_GoneOnNotice,
         expect={"rec.cut": "cap", "rec.truncated": True, "stream": Lacks("[relay:")}),
    # The word-boundary cut and the merge together: covering them only apart (the seam
    # unmerged, the merge with a hard cut) would miss their interaction.
    *(dict(name=f"a merged answer cut mid word by the budget comes out whole: {step}-character deltas",
           patch=SEAM, provider=Continuing(ANSWER, step=step, reasoning=True), cfg=CONT,
           expect={"rec.cut": "marker", "rec.merged_segments": Ref("sent", lambda s: len(s) - 1),
                   "client_text.m1": Stripped(ANSWER), "deltas": Stripped(ANSWER)})
      for step in (1, 3, 4, 7)),
    # Orderings of items across a merged continuation. A provider closes an item before it
    # opens the next, so the first is not expected; the relay must still not corrupt the
    # stream. The merge cannot follow it, so the run ends with an error the client is told;
    # what the continuation wrote before the other item opened is delivered in order, and
    # nothing after it is read.
    dict(name="a reasoning item that opens while the continuations message is open ends the run",
         provider=Continuing(ANSWER, then=_then_reasoning_inside), cfg=CONT,
         expect={"strict": True, "rec.cut": "error",
                 "rec.error": Has("opened an item while a continuation's message"),
                 "deltas": Stripped(PrefixOf(ANSWER)) & Len(Gt(30)), "stream": Has('"type": "error"')}),
    # the held message is closed before the provider's terminal, not after it
    dict(name="the provider ending the response incomplete while a message is held",
         provider=Continuing(ANSWER, then=_then_incomplete), cfg=CONT,
         expect={"types": Has("response.incomplete"),
                 "last(response.output_item.done)": Lt(Ref("first(response.incomplete)")),
                 "seqs": Increasing()}),
    # An item that opens while the continuation's message is open: the relay must not lose
    # text silently, hand the provider a finalized turn, or send a malformed stream.
    dict(name="an interleaved item ends the run with an error before the provider can finalize it",
         provider=Continuing(ANSWER, then=_then_interleaved), cfg=CONT,
         expect={"strict": True, "rec.cut": "error", "rec.real_completed": False, "rec.provider_terminal": None,
                 "client_ids": ["m1"], "before_summary": Lacks('"m2"')}),
    dict(name="a marker prefix of two characters or more is not sent by an interleaved item",
         provider=Continuing(ANSWER, then=_then_marker_prefix), cfg=CONT,
         expect={"strict": True, "rec.cut": "error", "deltas": Lacks("TASK_COMP")}),
    dict(name="a lone capital T is text and is delivered by an interleaved item",
         provider=Continuing(ANSWER, then=_then_capital_t), cfg=CONT,
         expect={"strict": True, "rec.cut": "error", "deltas": Ends(" GPT")}),
)

# The keys a row's `cfg` may hold: the ones `engine` reads.
ENGINE_CFG = frozenset(re.findall(r'cfg(?:\.get\(|\[)"(\w+)"', inspect.getsource(engine)))
PATCHABLE = {"config": config, "engine": engine}


def transcript(sink):
    """The frames the client sees, one line each. The synthetic usage follows the size of
    the request, and `relay.summary` carries the record and its timings: the golden holds
    where each stands, not its value."""
    out = []
    for event, payload in iter_sse_frames(sink.text().splitlines(keepends=True)):
        obj = json.loads(payload) if payload else payload
        if event == "relay.summary":
            obj = "<record>"
        elif event == "response.completed" and obj["response"].get("synthetic"):
            obj["response"]["usage"] = "<estimate>"
        out.append(f"{event} {json.dumps(obj, sort_keys=True)}\n")
    return "".join(out)


class _Run:
    """What one run produced, each part read once and only when a row asks for it."""

    def __init__(self, **parts):
        self.__dict__.update(parts)

    @functools.cached_property
    def frames(self):
        return list(iter_sse_frames(self.sink.text().splitlines(keepends=True)))

    @functools.cached_property
    def objs(self):
        return [json.loads(p) for _, p in self.frames if p]

    @functools.cached_property
    def types(self):
        return [o.get("type") for o in self.objs]

    @functools.cached_property
    def client(self):
        return _client_view(self.sink)

    @functools.cached_property
    def written(self):
        return [json.loads(line) for line in self.log.splitlines() if line.strip()]

    @functools.cached_property
    def log(self):
        with open(core.RUNS_LOG, encoding="utf-8") as f:
            return f.read()

    @functools.cached_property
    def trace(self):
        with open(self.trace_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


def _at(items, wanted):
    """Index of the first and of the last item equal to `wanted`, or MISSING."""
    found = [i for i, x in enumerate(items) if x == wanted]
    return (found[0], found[-1]) if found else (MISSING, MISSING)


class RunTests(_Checks, _EngineHarness):
    ROW = {"name", "provider", "cfg", "sink", "patch", "trace", "expect"}

    def test_what_the_relay_does_with_the_stream_and_what_it_sends(self):
        for row in RUNS:
            with self.subTest(row=row["name"]):
                self.run_row(row)

    def run_row(self, row):
        self.unread(row, self.ROW)
        self.unread(row["cfg"], ENGINE_CFG)
        with contextlib.ExitStack() as stack:
            scratch = tempfile.mkdtemp(prefix="engine-run-")
            stack.callback(shutil.rmtree, scratch, True)
            stack.enter_context(mock.patch.object(core, "RUNS_LOG", os.path.join(scratch, "runs.jsonl")))
            for name, value in row.get("patch", {}).items():
                module, attr = name.split(".")
                self.assertTrue(hasattr(PATCHABLE[module], attr), f"{name} is not a global")
                stack.enter_context(mock.patch.object(PATCHABLE[module], attr, value))
            delays = []
            engine.set_retry_hook(lambda attempt, exc, delay: delays.append(delay))
            stack.callback(engine.set_retry_hook, None)
            os.mkdir(os.path.join(scratch, "trace"))
            trace_path = os.path.join(scratch, "trace", "debug.jsonl")
            if row.get("trace"):
                self.assertIn(row["trace"], ("on", "broken"))
                stack.callback(trace.disable)
                made = trace.enable(trace_path)
                if row["trace"] == "broken":
                    made._fh.close()
                    made._fh = _Unwritable()
            tracer = trace.get()
            sink = row.get("sink", FakeSink)()
            errors = []
            stack.callback(lambda: [e.close() for e in errors])
            self.sent_bodies.clear()
            self.sent_headers.clear()

            def respond(recipe):
                if isinstance(recipe, Http):
                    errors.append(recipe.build())
                    raise errors[-1]
                if isinstance(recipe, Breaks):
                    if isinstance(recipe.error, Http):
                        errors.append(recipe.error.build())
                        return _Breaking(recipe.lines, errors[-1])
                    return _Breaking(recipe.lines, recipe.error(*recipe.args))
                return FakeResponse(recipe)

            def urlopen(req, timeout=None):
                self.sent_bodies.append(json.loads(req.data.decode()))
                self.sent_headers.append({k.lower(): v for k, v in req.header_items()})
                k, provider = len(self.sent_bodies) - 1, row["provider"]
                if isinstance(provider, Continuing):
                    return FakeResponse(provider.serve(k, sink))
                return respond(provider[min(k, len(provider) - 1)])

            stack.enter_context(mock.patch.object(engine.urllib.request, "urlopen", urlopen))
            cfg = make_cfg(**row["cfg"])
            rec = engine.run_upstream(cfg, sink)
            trace.disable()
            run = _Run(rec=rec, sink=sink, cfg=cfg, bodies=list(self.sent_bodies),
                       headers=list(self.sent_headers), delays=delays, tracer=tracer, trace_path=trace_path)
            self.check(self.views(run), row["expect"])

    def views(self, r):
        names = lambda: [name for name, _ in r.frames]
        client_text = lambda: r.client[0]
        views = {
            "rec": lambda _: r.rec,
            "written": lambda _: r.written,           # every record in the run log, parsed
            "log": lambda _: r.log,                   # the run log as text
            "cfg": lambda _: r.cfg,
            "config": lambda _: config,
            "sent": lambda _: r.bodies,
            "headers": lambda _: r.headers,
            # the assistant text each continuation request was given
            "tails": lambda _: [item["content"][0]["text"] for body in r.bodies[1:] for item in body["input"]
                                if isinstance(item, dict) and item.get("role") == "assistant"],
            "delays": lambda _: r.delays,             # the waits before each retry
            "stream": lambda _: r.sink.text(),
            "before_summary": lambda _: r.sink.text().split("relay.summary")[0],
            "deltas": lambda _: r.sink.deltas(),
            "said": lambda _: r.sink.deltas().split(NOTICE)[0],              # what the model said
            "transcript": lambda _: transcript(r.sink),
            "objs": lambda _: r.objs,
            "names": lambda _: names(),
            "types": lambda _: r.types,
            "named": lambda name: [json.loads(p) for n, p in r.frames if n == name and p],
            "of": lambda kind: [o for o in r.objs if o.get("type") == kind],
            "first": lambda kind: _at(r.types, kind)[0],
            "last": lambda kind: _at(r.types, kind)[1],
            "count": lambda kind: r.types.count(kind),
            "after": lambda name: names()[names().index(name) + 1:],
            "seqs": lambda which: ([o["sequence_number"] for o in r.objs if "sequence_number" in o] if which is None
                                   else [json.loads(p).get("sequence_number") for n, p in r.frames
                                         if n in which.split(",")]),
            "completed": lambda _: [o["response"] for o in r.objs if o.get("type") == "response.completed"][0],
            "text_of": lambda kind: "".join(o.get("delta", "") for o in r.objs if o.get("type") == kind),
            "item_done": lambda item_id: [o for o in r.objs if o.get("type") == "response.output_item.done"
                                          and o["item"]["id"] == item_id],
            # what a strict client makes of the stream (`_client_view`), which asserts it accepts it
            "strict": lambda _: bool(r.client),
            "client_text": lambda _: client_text(),
            "client_ids": lambda _: list(client_text()),
            "client_texts": lambda _: list(client_text().values()),
            "client_texts_but_last": lambda _: "".join(list(client_text().values())[:-1]),
            "message_text": lambda _: "".join(v for k, v in client_text().items() if k.startswith("m")),
            "client_output": lambda _: r.client[1],
            "trace": lambda _: r.trace,
            # trace records by direction, and by event after a space; `*` is any
            "traced": lambda which: [x for x in r.trace if which.split(" ")[0] in ("*", x["dir"])
                                     and (" " not in which or x.get("event") == which.split(" ")[1])],
            "tracer": lambda _: r.tracer,             # the trace enabled when the run began
            "trace_path": lambda _: r.trace_path,
            "trace_files": lambda _: os.listdir(os.path.dirname(r.trace_path)),
        }
        views["json"] = lambda path: json.dumps(self.at(views, path))
        views["distinct"] = lambda path: set(self.at(views, path))
        return views


if __name__ == "__main__":
    unittest.main(verbosity=2)
