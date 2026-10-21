"""Tool calls written as text (`text_calls`, on unless a file or the environment turns it off).

A tool call is a finalized item, so a request that ends in one is billed; a request cut
inside an unfinished message cost little beyond the prefix it had read when it was measured.
With `text_calls` the model is asked to write the call it would make as text,

    <<<CALL exec>>>
    ...the input of the call...
    <<<END>>>

and the relay, which reads the text as it streams, builds the call the client would have
received, hands it over and ends the request at `<<<END>>>`, inside the message.

This module is the reading half and has no I/O: it finds the opening line and the closing
line in the text the engine holds back, and builds the frames of the call. The engine decides
what to do with them (`engine.stream_segment`, `State.send_text_call`).

What the first version does not do, on purpose: only `exec` is read. Its call is a custom tool
call (raw input, no arguments object). A function call needs the `namespace` of the tool it
calls, which the relay would have to read out of the client's tool list; the model's calls to any
other tool pass through as native calls, billed as before.
"""

import re
import secrets

__all__ = [
    "TEXT_CALL_TOOLS", "TEXT_CALL_OPEN", "TEXT_CALL_END", "TEXT_CALL_MAX_CHARS",
    "find_open", "find_end", "call_frames",
]

# The tools the model may call as text. Names the client defines; the relay does not check
# that the client offered one (a call to a tool the client does not have comes back as the
# client's own tool error, as any bad call does).
TEXT_CALL_TOOLS = ("exec",)

_NAMES = "|".join(re.escape(n) for n in TEXT_CALL_TOOLS)
# The opening line: at the start of the text or after a newline, and complete (its own newline
# is there), so a line split across two deltas is not read until the rest has arrived. The
# line ending before it (\n or \r\n) is part of the match: it ends the note the person reads, and is not shown.
TEXT_CALL_OPEN = re.compile(r"(?:\A|\r?\n)<<<CALL (?P<name>" + _NAMES + r")>>>[ \t]*\r?\n")
# The closing line, after the input. Complete as soon as `<<<END>>>` is there: what follows is
# not read, and the engine ends the request here. The FIRST such line ends the call, so an input
# that has a line of its own reading `<<<END>>>` (a heredoc written that way) is cut short there.
TEXT_CALL_END = re.compile(r"(?:\A|\r?\n)<<<END>>>")

# A call longer than this is not read as a call: the engine delivers it as text, as before. A
# file written by one `exec` call is some kilobytes; this is two orders above.
TEXT_CALL_MAX_CHARS = 250_000


def find_open(text):
    """The first opening line in `text`: `(start, end, name)`, or None.

    `text[:start]` is the note that comes before the call (without the newline that ends it) and
    `text[end:]` is the beginning of the input."""
    m = TEXT_CALL_OPEN.search(text)
    if m is None:
        return None
    return m.start(), m.end(), m.group("name")


def find_end(text, start=0):
    """The closing line in `text` (the input read so far, after the opening line):
    `(input, end)` where `input` is everything before the newline that precedes it, or None.
    Only from index `start` on, for a caller that has already looked at what is before it.
    """
    m = TEXT_CALL_END.search(text, start)
    if m is None:
        return None
    return text[:m.start()], m.end()


def _new_id(prefix):
    return prefix + secrets.token_hex(24)


def call_frames(name, text, index, seq):
    """The frames of a custom tool call carrying `text`, in the order and with the fields the
    provider sends them (`added`, one input delta, the input's end,
    `done`). `index` is the item's `output_index`; `seq` returns the next `sequence_number`.

    Returns a list of `(event, data)`.
    """
    item_id = _new_id("ctc_")
    item = {"id": item_id, "type": "custom_tool_call", "status": "in_progress",
            "call_id": _new_id("call_"), "name": name, "input": ""}
    finished = dict(item, status="completed", input=text)
    return [
        ("response.output_item.added", {
            "type": "response.output_item.added", "output_index": index, "item": item,
            "sequence_number": seq()}),
        ("response.custom_tool_call_input.delta", {
            "type": "response.custom_tool_call_input.delta", "item_id": item_id,
            "output_index": index, "delta": text, "sequence_number": seq()}),
        ("response.custom_tool_call_input.done", {
            "type": "response.custom_tool_call_input.done", "item_id": item_id,
            "output_index": index, "input": text, "sequence_number": seq()}),
        ("response.output_item.done", {
            "type": "response.output_item.done", "output_index": index, "item": finished,
            "sequence_number": seq()}),
    ]
