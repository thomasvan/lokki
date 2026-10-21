#!/usr/bin/env python3
"""Offline tests for `text_calls`: a tool call the model writes as text becomes the call.

A fake upstream, so they spend no quota. The feature is on unless a file or the environment
turns it off; these pin both halves: with it on, the text between `<<<CALL exec>>>` and
`<<<END>>>` reaches the client as the `exec` call the provider would have sent and the request
ends there; with it off, nothing in the relay changes.

One table per entry point: each row is a behaviour. The frames a client sees for a call are
held by one golden, `test_textcalls.golden`, which is read and never written by a test.

Run:  python3 core/test_textcalls.py
"""

import contextlib
import functools
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lokki_core import config, core, engine, handler, renamer, textcalls  # noqa: E402
from lokki_core.sse import iter_sse_frames  # noqa: E402
from test_engine import (  # noqa: E402
    STRUCTURED, FakeResponse, FakeSink, _EngineHarness, _ResetAfterWrites, _client_view, _text_frames,
    make_cfg, message_frames, sse)

MARKER = renamer.MARKER
APPENDIX = renamer.APPENDIX
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_textcalls.golden")

INPUT = [{"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "draw a pelican"}]}]
CODE = 'const svg = "<svg/>";\nawait tools.exec_command({cmd: "echo hi"});'
TOOLS = [{"type": "function", "name": "read", "parameters": {"type": "object"}}]
NOT_VIA_TOOL = "Do not call `exec` through the tool interface"


def call_text(code=CODE, note="I will write the file.", name="exec"):
    return f"{note}\n<<<CALL {name}>>>\n{code}\n<<<END>>>\n"


def completed():
    return sse("response.completed", {"type": "response.completed", "response": {
        "id": "r", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}})


def transcript(sink):
    """The frames the client sees, one line each. The ids the relay makes up are named, the
    usage estimate follows the instruction length, and `relay.summary` carries the revision
    and timings: none of them is part of the shape."""
    out = []
    for event, payload in iter_sse_frames(sink.text().splitlines(keepends=True)):
        if event == "relay.summary":
            continue
        obj = json.loads(re.sub(r"\b(ctc|call)_[0-9a-f]+", r"\1_ID", payload))
        if event == "response.completed":
            obj["response"]["usage"] = "<estimate>"
        out.append(f"{event} {json.dumps(obj, sort_keys=True)}\n")
    return "".join(out)


class ScannerTests(unittest.TestCase):
    # text -> (start of the opening line, start of the input, tool), or None
    OPEN = (
        # what follows the opening line is the input; what precedes it is the note
        ("<<<CALL exec>>>\nbody", (0, 16, "exec")),
        ("note\n<<<CALL exec>>>\nbody", (4, 21, "exec")),
        # a carriage return belongs to the line, not to the note or the input
        ("note\r\n<<<CALL exec>>>\r\nbody\r\n<<<END>>>\r\n", (4, 23, "exec")),
        ("<<<CALL exec>>>  \nx", (0, 18, "exec")),
        ("<<<CALL exec>>>\r\nx", (0, 17, "exec")),
        ("<<<CALL exec>>>", None),                  # its newline has not arrived
        ("<<<CALL exec", None),                     # nor has the rest
        ("see <<<CALL exec>>>\nbody", None),        # in the middle of a line
        ("<<<CALL shell>>>\nbody", None),           # a tool that is not read
        ("<<<CALL exec>>> extra\nbody", None),      # not the line it must be
        ("<<<CALL  exec>>>\nbody", None),
    )
    # (text, index to look from, what is found): the input without the closing line's newline,
    # or with the index the rest starts at, or None
    END = (
        ("a\nb\n<<<END>>>\nrest", 0, "a\nb"),
        ("<<<END>>>", 0, ""),
        ("a\n<<<END>>>", 0, "a"),
        ("body\r\n<<<END>>>\r\n", 0, "body"),
        # the first closing line ends the call even inside a heredoc
        ("cat <<'X'\n<<<END>>>\nX\n<<<END>>>", 0, "cat <<'X'"),
        # the input keeps its own blank lines and trailing spaces
        ("a\n\n  b  \n\n<<<END>>>", 0, "a\n\n  b  \n"),
        ("ab\n<<<END>>>", 2, ("ab", len("ab\n<<<END>>>"))),
        ("ab\n<<<END>>>", 3, None),                 # its newline is before the index
        ("a\n<<<END>>", 0, None),
        ("a <<<END>>>", 0, None),
        ("a\n<<<END", 0, None),
        ("a\n<<END>>>", 0, None),
    )

    def test_the_opening_line(self):
        for text, want in self.OPEN:
            with self.subTest(text=text):
                self.assertEqual(textcalls.find_open(text), want)

    def test_the_closing_line_ends_the_input_without_its_newline(self):
        for text, start, want in self.END:
            with self.subTest(text=text, start=start):
                found = textcalls.find_end(text, start)
                self.assertEqual(found if isinstance(want, tuple) or found is None else found[0], want)

    def test_the_frames_are_the_providers_call_in_the_providers_order(self):
        seq = iter(range(10, 99))
        out = textcalls.call_frames("exec", CODE, 3, lambda: next(seq))
        self.assertEqual([e for e, _ in out], [
            "response.output_item.added", "response.custom_tool_call_input.delta",
            "response.custom_tool_call_input.done", "response.output_item.done"])
        for event, data in out:
            self.assertEqual(data["type"], event)
            self.assertEqual(data["output_index"], 3)
        self.assertEqual([d["sequence_number"] for _, d in out], [10, 11, 12, 13])
        added, delta, done, item_done = (d for _, d in out)
        item = added["item"]
        self.assertTrue(item["id"].startswith("ctc_"))
        self.assertTrue(item["call_id"].startswith("call_"))
        self.assertEqual((item["type"], item["name"], item["input"], item["status"]),
                         ("custom_tool_call", "exec", "", "in_progress"))
        self.assertEqual(delta["item_id"], item["id"])
        self.assertEqual(delta["delta"], CODE)
        self.assertEqual(done["input"], CODE)
        self.assertEqual(item_done["item"], dict(item, input=CODE, status="completed"))
        # two calls do not share ids
        a = textcalls.call_frames("exec", "x", 0, lambda: 0)[0][1]["item"]
        b = textcalls.call_frames("exec", "x", 0, lambda: 0)[0][1]["item"]
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotEqual(a["call_id"], b["call_id"])


# What the fake provider streams: a function of the delta size, giving one list of lines per segment.
def says(text):
    return lambda step: [_text_frames("m1", text, step) + completed()]


def _message_done(text):
    return (sse("response.output_text.done", {
                "type": "response.output_text.done", "output_index": 0,
                "item_id": "m1", "content_index": 0, "text": text})
            + sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": 0,
                "item": {"id": "m1", "type": "message", "status": "completed", "role": "assistant",
                         "content": [{"type": "output_text", "text": text}]}}))


def _item_done(item_id, with_text_done, answer):
    lines = message_frames(item_id=item_id, output_index=0, delta=answer)
    if with_text_done:
        lines += sse("response.output_text.done", {"type": "response.output_text.done", "output_index": 0,
                                                    "item_id": item_id, "content_index": 0, "text": answer})
    lines += sse("response.output_item.done", {"type": "response.output_item.done", "output_index": 0,
                                                "item": {"id": item_id, "type": "message", "status": "completed"}})
    return lines + completed()


def _native_call(step):
    item = {"id": "fc1", "type": "custom_tool_call", "call_id": "call_1", "name": "exec",
            "status": "in_progress", "input": ""}
    return [sse("response.output_item.added", {
                "type": "response.output_item.added", "output_index": 0, "item": item})
            + sse("response.custom_tool_call_input.delta", {
                "type": "response.custom_tool_call_input.delta", "item_id": "fc1",
                "output_index": 0, "delta": "run()"})
            + sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": 0,
                "item": dict(item, status="completed", input="run()")})
            + completed()]


def _unclosed_then_done(step):
    # the item is finished by the provider, so the closing line is not coming
    body = "Note.\n<<<CALL exec>>>\nlet a = 1;"
    return [_text_frames("m1", body, 6) + _message_done(body) + completed()]


def _then_a_call(step):
    return [_text_frames("m1", "word " * 20, 7),
            _text_frames("m2", "<<<CALL exec>>>\nrun()\n<<<END>>>\n", 5)]


BASE = dict(input=INPUT, text_calls=True, pad_tools=True)      # the call format, with the padding tool
LITE = dict(BASE, text_calls_pad=False)                       # the call format, the marker alone
OFF = dict(input=INPUT, text_calls=False)
MANY = "".join(f"line {i}\n" for i in range(3000))
SQUARE = "x = 1;\n" * 400
STRUCTURED_CFG = dict(max_segments=6, extra=STRUCTURED)
A_MARKER = says("A\n" + MARKER)
SERVED = call_text() + MARKER + "\n" + APPENDIX + " notes"
CALL_VIEW = dict(text=["I will write the file.", ""], types=["message", "custom_tool_call"],
                 items={0: {"status": "completed"},
                        1: {"name": "exec", "input": CODE, "status": "completed"}})
CALL_REC = {"cut": "textcall", "verdict": "usage-discarded", "real_completed": False,
            "text_call": "exec", "text_call_chars": len(CODE), "text_call_fallbacks": 0}
NATIVE = dict(rec={"cut": "toolcall", "text_call": None}, items={0: {"input": "run()"}})
FORMAT_ASKED = ("<<<CALL exec>>>", "<<<END>>>", MARKER)

# One row per behaviour of `engine.run_upstream`, what the relay does with the stream a provider
# sends and what it sends the provider. `steps`: the delta sizes, so the opening and the closing
# line are split everywhere; `expect` names only what the row binds:
#   rec     fields of the run record (dotted for a nested one); a bool or None is compared with `is`
#   deltas  the text the client was sent, exactly;  deltas_has / deltas_lacks  substrings of it
#   text, types, items, all_completed  what a strict client makes of the stream (`items` by position)
#   golden  the file the client's frames must equal
#   sent    the first request: role / last_text / has / lacks (of the last input item), prefix (the
#           client's own items come first), no_keys, tool_names, tools_has, input_has / input_lacks
RUNS = (
    dict(name="a call written as text is handed over and ends the run", provider=says(SERVED),
         steps=(1, 2, 3, 5, 7, 11, 64, 10 ** 6), cfg=BASE,
         expect=dict(rec=CALL_REC, deltas="I will write the file.", **CALL_VIEW)),
    dict(name="the frames the client sees for a call", provider=says(SERVED), steps=(3,), cfg=BASE,
         expect=dict(golden=GOLDEN)),
    dict(name="a call with no note leaves an empty message the client can close",
         provider=says("<<<CALL exec>>>\n" + CODE + "\n<<<END>>>\n"), steps=(4,), cfg=BASE,
         expect=dict(rec={"cut": "textcall"}, text=["", ""], types=["message", "custom_tool_call"])),
    dict(name="nothing after the closing line is read",
         provider=says(call_text() + call_text(code="second", note="again") + MARKER), steps=(6,), cfg=BASE,
         expect=dict(types=["message", "custom_tool_call"], items={1: {"input": CODE}})),
    dict(name="the call input counts in the delivered characters", provider=says(call_text()), steps=(7,),
         cfg=BASE, expect=dict(rec={"delivered_chars": len("I will write the file.") + len(CODE)})),
    dict(name="a call of many small deltas is read and its split closing line found",
         provider=says(call_text(MANY)), steps=(1, 2, 3, 4), cfg=dict(BASE, total_cap=10 ** 6),
         expect=dict(rec={"cut": "textcall"}, items={1: {"input": MANY}})),
    dict(name="a long call is not cut by the segment budget", provider=says(call_text(SQUARE)), steps=(50,),
         cfg=dict(BASE, seg_budget=300, max_segments=3, total_cap=1000),
         expect=dict(rec={"cut": "textcall", "segs": 1}, items={1: {"input": SQUARE}})),
    dict(name="the tool the model names must be one that is read",
         provider=says(call_text(name="shell") + MARKER + "\n" + APPENDIX), steps=(5,), cfg=BASE,
         expect=dict(rec={"cut": "marker", "text_call": None}, deltas_has=["<<<CALL shell>>>"])),
    dict(name="a call written with windows line endings is built clean",
         provider=says("Note.\r\n<<<CALL exec>>>\r\nrun()\r\n<<<END>>>\r\n"), steps=(1, 3, 100), cfg=BASE,
         expect=dict(rec={"cut": "textcall"}, deltas="Note.", items={1: {"input": "run()"}})),
    dict(name="a line that reads like the closing line inside the input ends it",   # pinned, not wanted
         provider=says("<<<CALL exec>>>\ncat <<'X'\n<<<END>>>\nX\n<<<END>>>\n"), steps=(4,), cfg=BASE,
         expect=dict(rec={"cut": "textcall"}, items={1: {"input": "cat <<'X'"}})),
    dict(name="a marker before the opening line in the same delta is not a call",
         provider=says("A\n" + MARKER + "\n<<<CALL exec>>>\nrun()\n<<<END>>>\n"), steps=(10 ** 6,), cfg=BASE,
         expect=dict(rec={"cut": "marker", "text_call": None}, deltas_lacks=[MARKER, "<<<CALL"])),
    dict(name="without the padding surface the format is not asked and nothing is read",
         provider=says(call_text()), steps=(5,), cfg=dict(BASE, pad_tools=False),
         expect=dict(rec={"knobs.text_calls": False, "text_call": None}, sent=dict(input_lacks=["<<<CALL"]))),
    dict(name="a call in a continuation segment is built when the text is merged",
         provider=_then_a_call, steps=(None,), cfg=dict(BASE, seg_budget=30, max_segments=3,
                                                       total_cap=10 ** 6, merge_text=True),
         expect=dict(rec={"cut": "textcall", "segs": 2}, all_completed=True,
                     items={-1: {"type": "custom_tool_call", "input": "run()"}})),
    dict(name="a call in a continuation segment is built when the text is not merged",
         provider=_then_a_call, steps=(None,), cfg=dict(BASE, seg_budget=30, max_segments=3,
                                                       total_cap=10 ** 6, merge_text=False),
         expect=dict(rec={"cut": "textcall", "segs": 2}, all_completed=True,
                     items={-1: {"type": "custom_tool_call", "input": "run()"}})),
    dict(name="an answer with no call is cut at the marker as before",
         provider=says("The answer.\n" + MARKER + "\n" + APPENDIX + " notes"), steps=(5,), cfg=BASE,
         expect=dict(rec={"cut": "marker", "text_call": None}, deltas="The answer.\n")),
    dict(name="a call that reaches the marker unclosed is delivered as text",
         provider=says("Note.\n<<<CALL exec>>>\nlet a = 1;\n" + MARKER + "\n" + APPENDIX), steps=(1, 4, 9),
         cfg=BASE, expect=dict(rec={"cut": "marker", "text_call": None, "text_call_fallbacks": 1},
                               deltas="Note.\n<<<CALL exec>>>\nlet a = 1;\n", items={0: {"type": "message"}})),
    dict(name="a call whose message ends without a closing line is delivered as text",
         provider=_unclosed_then_done, steps=(None,), cfg=BASE,
         expect=dict(rec={"cut": "terminal", "text_call_fallbacks": 1},
                     deltas="Note.\n<<<CALL exec>>>\nlet a = 1;", strict=True)),
    dict(name="a call longer than the limit is delivered as text",
         provider=says("<<<CALL exec>>>\n" + "y" * 400 + "\n<<<END>>>\n" + MARKER), steps=(50,),
         cfg=dict(BASE, total_cap=10 ** 6), max_chars=200,
         expect=dict(rec={"text_call_fallbacks": 1, "text_call": None}, deltas_has=["y" * 400])),
    dict(name="a native call still passes through and ends the run when it is done",
         provider=_native_call, steps=(None,), cfg=BASE, expect=NATIVE),
    dict(name="off, the same text is just text", provider=says(call_text() + MARKER + "\n" + APPENDIX),
         steps=(5,), cfg=OFF,
         expect=dict(rec={"cut": "marker", "text_call": None, "text_call_fallbacks": 0,
                          "knobs.text_calls": False}, deltas=call_text())),
    dict(name="a cfg without the key is off", provider=says(call_text() + MARKER), steps=(5,),
         cfg=dict(input=INPUT), expect=dict(rec={"cut": "marker", "knobs.text_calls": False})),
    dict(name="the request is the plain one when off", provider=A_MARKER, steps=(5,),
         cfg=dict(input=INPUT, pad_tools=True),
         expect=dict(sent=dict(input_has=[renamer.INJECTED_INSTRUCTIONS.strip().splitlines()[0]],
                               input_lacks=["<<<CALL"]))),
    dict(name="the control mode is not asked for the format and does not read it", provider=says(call_text()),
         steps=(5,), cfg=dict(BASE, mode="control", abort_after=0),
         expect=dict(rec={"knobs.text_calls": False, "text_call": None})),
    dict(name="the after-deltas mode is not asked for the format and does not read it",
         provider=says(call_text()), steps=(5,), cfg=dict(BASE, mode="after-deltas", abort_after=0),
         expect=dict(rec={"knobs.text_calls": False, "text_call": None})),
    # A developer message, not a user one: measured, the model weighs the call format for much
    # longer from the user's side (renamer.py, the note at `_TEXT_CALL_HEAD`).
    dict(name="the first segment is asked for the call format", provider=A_MARKER, steps=(5,), cfg=BASE,
         expect=dict(rec={"pad_tools": True, "knobs.text_calls_pad": True},
                     sent=dict(role="developer", last_text=renamer.TEXT_CALL_INSTRUCTIONS.strip(),
                               has=FORMAT_ASKED + (APPENDIX,), prefix=True,
                               tool_names=[renamer.PAD_TOOL_NAME]))),
    dict(name="the lite request has no padding tool and asks for the marker alone", provider=A_MARKER,
         steps=(5,), cfg=LITE,
         expect=dict(rec={"pad_tools": False},
                     sent=dict(no_keys=["tools", "tool_choice"], role="developer", prefix=True,
                               last_text=renamer.TEXT_CALL_LITE_INSTRUCTIONS.strip(),
                               has=FORMAT_ASKED + (NOT_VIA_TOOL,),
                               lacks=[APPENDIX, renamer.PAD_TOOL_NAME, renamer.PAD_KEY, "filler section"]))),
    dict(name="lite does nothing without text_calls", provider=A_MARKER, steps=(5,),
         cfg=dict(OFF, text_calls_pad=False, pad_tools=True),
         expect=dict(rec={"knobs.text_calls": False}, sent=dict(tools_has=[renamer.PAD_TOOL_NAME]))),
    dict(name="lite: a call written as text is still handed over and ends the run", provider=says(SERVED),
         steps=(1, 5, 64), cfg=LITE,
         expect=dict(rec={"cut": "textcall", "real_completed": False, "text_call": "exec",
                          "text_call_chars": len(CODE), "knobs.text_calls_pad": False,
                          "knobs.text_calls": True},
                     types=["message", "custom_tool_call"], items={1: {"input": CODE}})),
    dict(name="lite: an answer is cut at the marker",
         provider=says("The answer.\n" + MARKER + "\n" + APPENDIX + " notes"), steps=(4,), cfg=LITE,
         expect=dict(rec={"cut": "marker", "real_completed": False}, deltas="The answer.\n")),
    # what is given up: with no padding tool there is no second abort window
    dict(name="lite: an answer with no marker reaches the providers end and is billed",
         provider=says("A short answer that forgets the marker."), steps=(4,), cfg=LITE,
         expect=dict(rec={"cut": "terminal", "real_completed": True})),
    dict(name="lite: a native call still passes through and ends the run when it is done",
         provider=_native_call, steps=(None,), cfg=LITE, expect=NATIVE),
    dict(name="lite: a closed structured answer is still cut instead of billed",
         provider=lambda step: [_item_done("msg_1", True, '{"title":"Generate pelican SVG"}')], steps=(None,),
         cfg=dict(LITE, **STRUCTURED_CFG),
         expect=dict(rec={"cut": "itemdone", "real_completed": False}, deltas='{"title":"Generate pelican SVG"}')),
    # the cut stays off a request whose own top-level tool list the relay did not write into
    dict(name="a structured answer with a client tool is billed with the padding", steps=(None,),
         provider=lambda step: [_item_done("msg_1", False, '{"title":"x"}')],
         cfg=dict(BASE, text_calls_pad=True, tools=TOOLS, **STRUCTURED_CFG),
         expect=dict(rec={"cut": "terminal", "real_completed": True})),
    dict(name="a structured answer with a client tool is billed without the padding", steps=(None,),
         provider=lambda step: [_item_done("msg_1", False, '{"title":"x"}')],
         cfg=dict(BASE, text_calls_pad=False, tools=TOOLS, **STRUCTURED_CFG),
         expect=dict(rec={"cut": "terminal", "real_completed": True})),
    dict(name="the same structured answer with the padding tool is cut", steps=(None,),
         provider=lambda step: [_item_done("msg_1", False, '{"title":"x"}')],
         cfg=dict(BASE, **STRUCTURED_CFG), expect=dict(rec={"cut": "itemdone"})),
)


class RunTests(_EngineHarness):
    def run_row(self, row, step):
        self.sent_bodies.clear()
        self.install([FakeResponse(lines) for lines in row["provider"](step)])
        sink = FakeSink()
        patch = (mock.patch.object(engine, "TEXT_CALL_MAX_CHARS", row["max_chars"])
                 if "max_chars" in row else contextlib.nullcontext())
        with patch:
            rec = engine.run_upstream(make_cfg(**row["cfg"]), sink)
        return rec, sink

    def same(self, got, want):
        if want is None or isinstance(want, bool):
            self.assertIs(got, want)
        else:
            self.assertEqual(got, want)

    def test_what_the_relay_does_with_the_stream_and_what_it_sends(self):
        for row in RUNS:
            for step in row["steps"]:
                with self.subTest(row=row["name"], step=step):
                    rec, sink = self.run_row(row, step)
                    self.check(row["expect"], rec, sink)

    EXPECTED = {"rec", "deltas", "deltas_has", "deltas_lacks", "golden", "text", "types", "items",
                "all_completed", "strict", "sent"}
    SENT = {"role", "last_text", "has", "lacks", "prefix", "no_keys", "tool_names", "tools_has",
            "input_has", "input_lacks"}

    def check(self, expect, rec, sink):
        self.assertEqual(set(expect) - self.EXPECTED, set())     # a key nobody reads asserts nothing
        for path, want in expect.get("rec", {}).items():
            self.same(functools.reduce(lambda d, k: d[k], path.split("."), rec), want)
        if "deltas" in expect:
            self.assertEqual(sink.deltas(), expect["deltas"])
        for needle in expect.get("deltas_has", ()):
            self.assertIn(needle, sink.deltas())
        for needle in expect.get("deltas_lacks", ()):
            self.assertNotIn(needle, sink.deltas())
        if "golden" in expect:
            with open(expect["golden"], encoding="utf-8") as f:
                self.assertEqual(transcript(sink), f.read())
        if {"text", "types", "items", "all_completed", "strict"} & set(expect):
            text, output = _client_view(sink)        # a strict client accepts the stream
            if "text" in expect:
                self.assertEqual(list(text.values()), expect["text"])
            if "types" in expect:
                self.assertEqual([o["type"] for o in output], expect["types"])
            for at, fields in expect.get("items", {}).items():
                for field, want in fields.items():
                    self.assertEqual(output[at][field], want)
            if expect.get("all_completed"):
                self.assertTrue(all(o["status"] == "completed" for o in output))
        if "sent" in expect:
            self.check_sent(expect["sent"], self.sent_bodies[0])

    def check_sent(self, want, body):
        self.assertEqual(set(want) - self.SENT, set())
        last = body["input"][-1]
        text = last["content"][0]["text"] if isinstance(last.get("content"), list) else None
        if "role" in want:
            self.assertEqual(last["role"], want["role"])
        if "last_text" in want:
            self.assertEqual(text, want["last_text"])
        for needle in want.get("has", ()):
            self.assertIn(needle, text)
        for needle in want.get("lacks", ()):
            self.assertNotIn(needle, text)
        if want.get("prefix"):
            self.assertEqual(body["input"][:-1], INPUT)
        for key in want.get("no_keys", ()):
            self.assertNotIn(key, body)
        if "tool_names" in want:
            self.assertEqual([t["name"] for t in body["tools"]], want["tool_names"])
        for needle in want.get("tools_has", ()):
            self.assertIn(needle, json.dumps(body["tools"]))
        for needle in want.get("input_has", ()):
            self.assertIn(needle, json.dumps(body["input"]))
        for needle in want.get("input_lacks", ()):
            self.assertNotIn(needle, json.dumps(body["input"]))

    def test_a_relay_with_no_setting_asks_for_text_calls(self):
        """A relay that was never told about `text_calls` asks for the call format, and the
        call the answer writes ends the request."""
        self.assertTrue(config.BUILTIN_TEXT_CALLS)
        self.assertTrue(config.DEFAULT_TEXT_CALLS)
        self.assertTrue(config._DEFAULT_SETTINGS["text_calls"])
        knobs = handler.resolve_knobs({}, tuning=False)
        self.sent_bodies.clear()
        self.install([FakeResponse(lines) for lines in says(SERVED)(5)])
        sink = FakeSink()
        rec = engine.run_upstream(make_cfg(input=INPUT, **knobs), sink)
        last = self.sent_bodies[0]["input"][-1]
        self.assertEqual(last["role"], "developer")
        self.assertEqual(last["content"][0]["text"], renamer.TEXT_CALL_INSTRUCTIONS.strip())
        self.assertEqual(rec["cut"], "textcall")
        self.assertEqual(rec["text_call"], "exec")

    def test_a_client_that_leaves_while_the_call_is_handed_over_is_recorded_as_gone(self):
        def run(sink):
            self.install([FakeResponse(_text_frames("m1", call_text(), 7) + completed())])
            return engine.run_upstream(make_cfg(**BASE), sink)

        full = FakeSink()
        run(full)
        for limit in range(1, full.text().count("event:")):     # the writes a whole run makes
            with self.subTest(writes_before_reset=limit):
                rec = run(_ResetAfterWrites(limit))
                self.assertIsNone(rec["error"])
                self.assertTrue(rec["client_gone"])
                self.assertEqual(rec["cut"], "client_gone")


# One row per request `renamer.build_body` writes: its arguments and what it must hold.
#   role, prefix   of the last input item, and the client's own items before it
#   input          the whole input;  no_call  the call format is not in it
#   same_as        arguments of a body the fields in `fields` must equal;  differs  a field that must not
#   tools          the tools field, exactly;  pad_key  whether the padding key is in the body
PLAIN = ("segmented", INPUT, "m", "", True)
BODIES = (
    dict(name="the plain request ends in a user message", args=PLAIN, expect=dict(role="user")),
    # Every other instruction the relay appends keeps the user role it was measured in.
    dict(name="the call format is the one developer message, after the client's own items", args=PLAIN,
         kw=dict(text_calls=True), expect=dict(role="developer", prefix=True)),
    dict(name="a continuation without tools asks for a fresh answer, whatever text_calls says",
         args=PLAIN[:4] + (False,), kw=dict(text_calls=True), expect=dict(role="user")),
    dict(name="a string input has no message to give a role to: the text is appended to it",
         args=("segmented", "ask", "m", "", True), kw=dict(text_calls=True),
         expect=dict(input="ask" + renamer.TEXT_CALL_INSTRUCTIONS)),
    dict(name="the padding tool and the callers tools are unchanged", args=PLAIN, kw=dict(tools=TOOLS, text_calls=True),
         expect=dict(same_as=(PLAIN, dict(tools=TOOLS)), fields=("tools", "tool_choice"), differs="input")),
    dict(name="a continuation does not ask again", args=PLAIN[:3] + ("tail so far", False), kw=dict(text_calls=True),
         expect=dict(no_call=True, same_as=(PLAIN[:3] + ("tail so far", False), {}), fields=("input",))),
    dict(name="lite forwards the callers own tools and does not pad", args=PLAIN,
         kw=dict(tools=TOOLS, text_calls=True, lite=True), expect=dict(tools=TOOLS, pad_key=False)),
    dict(name="without lite the callers tools are padded", args=PLAIN, kw=dict(tools=TOOLS, text_calls=True),
         expect=dict(pad_key=True)),
    dict(name="lite does nothing without text_calls", args=PLAIN, kw=dict(lite=True),
         expect=dict(same_as=(PLAIN, {}), fields=None)),
)


class BodyTests(unittest.TestCase):
    EXPECTED = {"role", "prefix", "input", "no_call", "same_as", "fields", "differs", "tools", "pad_key"}

    def test_the_request_body(self):
        for row in BODIES:
            with self.subTest(row=row["name"]):
                body = renamer.build_body(*row["args"], **row.get("kw", {}))
                want = row["expect"]
                self.assertEqual(set(want) - self.EXPECTED, set())     # a key nobody reads asserts nothing
                if "role" in want:
                    self.assertEqual(body["input"][-1]["role"], want["role"])
                if want.get("prefix"):
                    self.assertEqual(body["input"][:-1], INPUT)
                if "input" in want:
                    self.assertEqual(body["input"], want["input"])
                if want.get("no_call"):
                    self.assertNotIn("<<<CALL", json.dumps(body["input"]))
                if "same_as" in want:
                    args, kw = want["same_as"]
                    ref = renamer.build_body(*args, **kw)
                    if want["fields"] is None:
                        self.assertEqual(body, ref)
                    for field in want["fields"] or ():
                        self.assertEqual(body[field], ref[field])
                if "differs" in want:
                    ref = renamer.build_body(*want["same_as"][0], **want["same_as"][1])
                    self.assertNotEqual(body[want["differs"]], ref[want["differs"]])
                if "tools" in want:
                    self.assertEqual(body["tools"], want["tools"])
                if "pad_key" in want:
                    self.assertIs(renamer.PAD_KEY in json.dumps(body), want["pad_key"])


class InstructionTextTests(unittest.TestCase):
    def test_the_texts_the_relay_appends(self):
        head = renamer.TEXT_CALL_INSTRUCTIONS.split("B. Any other reply")[0]
        for needle in ("<<<CALL exec>>>", "<<<END>>>", NOT_VIA_TOOL):
            with self.subTest(head_names=needle):
                self.assertIn(needle, head)
        # the call format ends with the plain instructions word for word
        body = renamer.INJECTED_INSTRUCTIONS[len(renamer._SECTIONS_HEAD):]
        self.assertTrue(renamer.TEXT_CALL_INSTRUCTIONS.endswith(body))
        # the lite format has the same call paragraph and is the shorter
        self.assertTrue(renamer.TEXT_CALL_LITE_INSTRUCTIONS.startswith(head))
        self.assertLess(len(renamer.TEXT_CALL_LITE_INSTRUCTIONS), len(renamer.TEXT_CALL_INSTRUCTIONS))

    def test_the_instructions_without_text_calls_are_pinned(self):
        # length and digest of `INJECTED_INSTRUCTIONS`, the request of every client that has `text_calls` off: moving it
        # takes this edit as well as the wording
        text = renamer.INJECTED_INSTRUCTIONS
        self.assertEqual(len(text), 1059)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),
                         "64e4bca08cebdff91c300e61fe618c7677838ae3ce3762ac1c48c822ff1a7d63")


FILE_BASE = "auth:\n  token: 't'\ncli_proxy_api_key: 'k'\n"
ERR = config.ConfigError
# (setting, line in the file, environment, the setting that is read or the error)
LOADED = (
    ("text_calls", "", {}, True),
    ("text_calls", "text_calls: true\n", {}, True),
    ("text_calls", "", {"RELAY_TEXT_CALLS": "1"}, True),
    ("text_calls", "text_calls: true\n", {"RELAY_TEXT_CALLS": "0"}, False),
    ("text_calls", "text_calls: maybe\n", {}, False),
    ("text_calls_pad", "", {}, True),
    ("text_calls_pad", "text_calls_pad: false\n", {}, False),
    # the environment wins over the file, in both directions
    ("text_calls_pad", "", {"RELAY_TEXT_CALLS_PAD": "0"}, False),
    ("text_calls_pad", "text_calls_pad: false\n", {"RELAY_TEXT_CALLS_PAD": "1"}, True),
    *(("text_calls_pad", "", {"RELAY_TEXT_CALLS_PAD": word}, want)
      for word, want in (("no", False), ("off", False), ("false", False), ("yes", True), ("on", True))),
    # a typo is refused, not read as off
    ("text_calls_pad", "text_calls_pad: maybe\n", {}, ERR),
    ("text_calls_pad", "", {"RELAY_TEXT_CALLS_PAD": "of"}, ERR),
)
# (a setting from the file, the environment's, the flag read from them)
FLAGS = (
    ({}, {}, False), ({"text_calls": True}, {}, True), ({"text_calls": "yes"}, {}, True),
    ({"text_calls": "maybe"}, {}, False), ({}, {"RELAY_TEXT_CALLS": "1"}, True),
    ({"text_calls": True}, {"RELAY_TEXT_CALLS": "0"}, False),
)
# (setting, the relay's default, what the client sent, whether tuning is open, the knob that is used)
KNOBS = (
    ("text_calls", False, {"text_calls": True}, False, False),
    ("text_calls", False, {"text_calls": True}, True, True),
    ("text_calls", True, {}, False, True),
    ("text_calls", True, {"text_calls": False}, False, True),
    ("text_calls_pad", True, {"text_calls_pad": False}, False, True),
    ("text_calls_pad", True, {"text_calls_pad": False}, True, False),
    ("text_calls_pad", False, {}, False, False),
)
# (setting, the value apply_settings sets, then the default it clears back to)
APPLIED = (("text_calls", False, True), ("text_calls_pad", False, True))
# (setting, its value in a default relay or None to check only that it is named, the value flipped to)
NAMED = (("text_calls", None, True), ("text_calls_pad", True, False))
DEFAULTS = {"text_calls": "DEFAULT_TEXT_CALLS", "text_calls_pad": "DEFAULT_TEXT_CALLS_PAD"}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved = config.DEFAULT_TEXT_CALLS, config.DEFAULT_TEXT_CALLS_PAD
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def tearDown(self):
        config.DEFAULT_TEXT_CALLS, config.DEFAULT_TEXT_CALLS_PAD = self._saved

    def test_a_flag_is_read_from_the_file_and_the_environment(self):
        for raw, env, want in FLAGS:
            with self.subTest(raw=raw, env=env):
                got = config._flag(config._pick(env.get("RELAY_TEXT_CALLS"), raw.get("text_calls"),
                                                default=False), default=False)
                self.assertIs(got, want)

    def test_load_settings_reads_the_settings_from_the_file_and_the_environment(self):
        for n, (key, line, env, want) in enumerate(LOADED):
            with self.subTest(setting=key, file=line, env=env):
                path = os.path.join(self.dir, f"{n}.yaml")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(FILE_BASE + line)
                if want is ERR:
                    with self.assertRaises(ERR):
                        config.load_settings(config_path=path, env=env)
                else:
                    self.assertIs(config.load_settings(config_path=path, env=env)[key], want)

    def test_a_client_can_set_a_knob_only_where_tuning_is_open(self):
        for key, default, body, tuning, want in KNOBS:
            with self.subTest(setting=key, default=default, body=body, tuning=tuning):
                setattr(config, DEFAULTS[key], default)
                self.assertIs(handler.resolve_knobs(body, tuning=tuning)[key], want)

    def test_apply_settings_sets_and_clears_a_setting(self):
        for key, value, cleared in APPLIED:
            with self.subTest(setting=key):
                config.apply_settings(dict(config._DEFAULT_SETTINGS, **{key: value}))
                self.assertIs(bool(getattr(config, DEFAULTS[key])), value)
                config.apply_settings(dict(config._DEFAULT_SETTINGS))
                self.assertIs(bool(getattr(config, DEFAULTS[key])), cleared)

    def test_the_fingerprint_and_the_owned_params_name_a_setting(self):
        for key, base, flipped in NAMED:
            with self.subTest(setting=key):
                self.assertIn(key, handler.RELAY_OWNED_PARAMS)
                self.assertIn(key, core.config_fingerprint())
                if base is not None:
                    self.assertIs(bool(core.config_fingerprint()[key]), base)
                setattr(config, DEFAULTS[key], flipped)
                self.assertIs(bool(core.config_fingerprint()[key]), flipped)


if __name__ == "__main__":
    unittest.main()
