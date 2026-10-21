"""The client's view of the stream: what was written, and how to close it.

The relay aborts upstream before `response.completed`, so upstream never closes
the items and content parts it opened. A strict client — Codex CLI is one —
cannot commit an assistant message whose item/content lifecycle never completes,
and it drops the answer silently when that happens. This module holds exactly
what was written to the client and can therefore say what is still missing.

It is built from **delivered** frames and never from what upstream produced. The
engine withholds `HOLD_CHARS` of trailing text while it scans for the marker,
and on a marker cut the marker text and the whole appendix stay undelivered; text the
client never received must not appear in the frames that close the response, and
must not appear in `response.completed.response.output` either.

Two kinds of open item get different treatment, because they are not the same
thing:

* a call to the relay's **own** padding tool exists only because the relay
  injected it — the client never offered that tool — so its frames are held back
  and never shown;
* a call to any **client** tool is forwarded as the client sent it, and if the
  relay cuts it mid-arguments the close asks for `status: "incomplete"` rather
  than either leaving it open for ever or presenting half a JSON argument string
  as a finished call.

A third rule is about **size, not lifecycle**. `seg_budget` and `total_cap` count
answer *text*; an image result is a base64 field on its own item and counts toward
neither, so a 1.9-2.1M-character one used to be delivered whole. The client then
stores it and sends it back on every later turn: 18.2M of one run's 22.2M upstream
characters were two such results, and the client's own rollout of a later session
was 4.6MB against 0.42MB with the cap. What that volume is *worth* was corrected on
2026-10-05 — an aborted dispatch is charged ~0.21 points whatever it carries, so
removing 84% of a run's characters is worth ~1-2 points, not the ~36 an earlier
reading of the same run attributed to them. The cap still earns its place: it
bounds what crosses the wire and what the client keeps. So an item carrying a
payload field above `payload_cap` is delivered with that field replaced by a 1x1
pixel placeholder (`CAPPED_IMAGE_B64`). The item's lifecycle is left *complete* on
purpose: an item the client cannot commit makes it drop the turn's answer, which is
the failure this module exists to prevent. The truth lives in the run record
(`payload_capped`, `payload_chars_dropped`) and in the item's untouched
`revised_prompt`, not in a status invented for the client.
"""

__all__ = ["StreamView", "cap_frame", "CAPPED_IMAGE_B64", "PAYLOAD_FIELDS"]

FUNCTION_TOOL = "function_call"
CUSTOM_TOOL = "custom_tool_call"
TOOL_KINDS = (FUNCTION_TOOL, CUSTOM_TOOL)
MESSAGE = "message"

# Which field of which item type is a *payload* — content the client may keep but
# that costs the account every time the client sends it back. Deliberately narrow:
# `custom_tool_call_output.output` is also large and also re-sent, but a client's
# tool result is referenced by the call that produced it, and dropping one is the
# bug that made the relay rebuild-and-flatten path uninhabitable (see
# `renamer.build_body`). A generated image references nothing.
PAYLOAD_FIELDS = {"image_generation_call": ("result",)}

# A 1x1 fully transparent PNG, base64, 68 bytes / 92 characters. Verified by the
# offline suite (chunk CRCs, 1x1 IHDR, IDAT decompresses), because a client that
# decodes the field would otherwise be the first to find out it was malformed.
CAPPED_IMAGE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNgAAIAAAUAAen6"
    "3NgAAAAASUVORK5CYII="
)

# The per-kind event names and item field that carry a tool call's arguments.
_TOOL_EVENTS = {
    FUNCTION_TOOL: ("response.function_call_arguments.delta",
                    "response.function_call_arguments.done", "arguments"),
    CUSTOM_TOOL: ("response.custom_tool_call_input.delta",
                  "response.custom_tool_call_input.done", "input"),
}


def cap_frame(data, cap):
    """One outgoing frame, with any oversized payload field replaced.

    Returns `(frame, dropped_chars, payloads)`. `data` is returned unchanged — the
    same object, no copy — unless something was actually capped, so the per-delta
    cost on a text stream is one dict lookup per frame. The capped frame is a
    shallow copy with a copied item: upstream's own object is what the trace
    records as received, and it must not be edited under it.
    """
    if not cap or cap < 1 or not isinstance(data, dict):
        return data, 0, 0

    item = data.get("item")
    if isinstance(item, dict):
        capped, dropped, payloads = _cap_item(item, cap)
        if payloads:
            frame = dict(data)
            frame["item"] = capped
            return frame, dropped, payloads

    if data.get("type") == "response.completed":
        resp = data.get("response")
        out = resp.get("output") if isinstance(resp, dict) else None
        if isinstance(out, list):
            dropped = payloads = 0
            capped_out = []
            for obj in out:
                if isinstance(obj, dict):
                    new, n, k = _cap_item(obj, cap)
                else:
                    new, n, k = obj, 0, 0
                capped_out.append(new)
                dropped += n
                payloads += k
            if payloads:
                frame = dict(data)
                frame["response"] = dict(resp, output=capped_out)
                return frame, dropped, payloads

    return data, 0, 0


def _cap_item(item, cap):
    """`(item, dropped_chars, payloads)` with the whitelisted fields capped.

    `dropped_chars` is what did not go out; `payloads` is how many fields were
    withheld, which is what the run record counts — a frame can carry more than
    one item (the completed aggregate does).
    """
    fields = PAYLOAD_FIELDS.get(item.get("type"))
    if not fields:
        return item, 0, 0
    dropped = payloads = 0
    capped = None
    for name in fields:
        value = item.get(name)
        if isinstance(value, str) and len(value) > cap:
            if capped is None:
                capped = dict(item)
            dropped += len(value) - len(CAPPED_IMAGE_B64)
            payloads += 1
            capped[name] = CAPPED_IMAGE_B64
    return (capped if capped is not None else item), dropped, payloads


class _Item:
    """One item the client has been shown."""

    __slots__ = ("index", "item", "item_id", "kind", "parts", "args",
                 "args_done", "upstream_done", "done")

    def __init__(self, index, item, item_id, kind):
        self.index = index
        self.item = item            # the object last forwarded for this item
        self.item_id = item_id
        self.kind = kind
        self.parts = {}             # content_index -> {part, text, *_done}
        self.args = ""              # accumulated tool arguments, delivered only
        self.args_done = False      # upstream already sent the arguments' end
        self.upstream_done = False  # upstream closed it itself
        self.done = None            # the item object the client ends up with


class StreamView:
    """What the client has been shown, and the frames that finish it.

    `seq` is a zero-argument callable returning the next `sequence_number`, so
    every frame this class produces continues the numbering the engine already
    put on the stream.
    """

    def __init__(self, seq, hidden_tool_names=(), payload_cap=0):
        self._seq = seq
        self._hidden = frozenset(hidden_tool_names)
        self._payload_cap = int(payload_cap or 0)
        self.payload_capped = 0           # payload fields replaced with the
        self.payload_chars_dropped = 0    # placeholder, and the characters that
                                          # did not go out with them
        self.items = []
        self._by_id = {}
        self._hidden_ids = set()  # ids of the relay's own calls, withheld
        self.hidden_calls = 0     # the relay's own pad calls, never shown
        self.cut_calls = 0        # client tool calls closed as incomplete

    # --- routing -------------------------------------------------------------

    def cap(self, data):
        """The frame as the client will get it, with an oversized payload capped.

        Asked in `State.send` **before** the write and before `observe`, so the
        capped object is both what goes out on the wire and what this view stores
        as the item's `done` — which is what makes every aggregate built from the
        view (`response.completed.response.output`, synthetic or upstream's) carry
        the capped version without a second pass.

        The counters are per **delivery**, not per item: upstream that repeated a
        payload in two frames would have sent it twice, so withholding it in both
        is two payloads' worth of bytes that did not go out, which is the number
        the record is for. On the shape this was built against, `result` arrives
        once, in the item's `done` frame.
        """
        frame, dropped, payloads = cap_frame(data, self._payload_cap)
        if payloads:
            self.payload_capped += payloads
            self.payload_chars_dropped += dropped
        return frame

    def withhold(self, data):
        """True when this upstream frame must not reach the client.

        Withholds the relay's own padding call: the client never offered
        `discarded_placeholder`, and a call to a tool the client does not have is
        an item it has no way to act on. The frames are not buffered for
        later delivery — a call that is cut is never shown at all.

        This is asked **before** the frame is renumbered, so a withheld frame
        consumes no `sequence_number` and no `output_index`: the numbers a client
        sees stay contiguous instead of jumping over frames that were never sent.
        """
        etype = data.get("type")
        item_id = data.get("item_id")

        if etype == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") in TOOL_KINDS and item.get("name") in self._hidden:
                self._hidden_ids.add(item.get("id") or item_id)
                self.hidden_calls += 1
                return True
            return False

        if etype == "response.output_item.done":
            item = data.get("item") or {}
            iid = item.get("id") or item_id
            if iid in self._hidden_ids:
                # The relay's own call finished. Still its tool, not the
                # client's, and already counted when it was withheld: stay hidden.
                self._hidden_ids.discard(iid)
                return True

        return item_id is not None and item_id in self._hidden_ids

    # --- observation ---------------------------------------------------------

    def observe(self, event, data):
        """Record a frame that was written to the client."""
        etype = data.get("type")

        if etype == "response.output_item.added":
            item = data.get("item") or {}
            item_id = item.get("id") or data.get("item_id")
            rec = _Item(data.get("output_index", 0), item, item_id,
                        item.get("type"))
            self.items.append(rec)
            if item_id:
                self._by_id[item_id] = rec
            return

        if etype == "response.output_item.done":
            item = data.get("item") or {}
            rec = self._by_id.get(item.get("id") or data.get("item_id"))
            if rec is not None:
                rec.upstream_done = True
                rec.item = item
                # Upstream's close frame normally carries the item's own content
                # and is what the client ends up with. It is not allowed to *erase*
                # the answer: a thin close frame (no `content`) would otherwise
                # reach the client inside the terminal frame as a completed
                # assistant message with no text in it, while the deltas it is
                # meant to describe were already delivered. The client's own view
                # is the deltas, so they are the fallback.
                if rec.kind == MESSAGE and not item.get("content") and rec.parts:
                    merged = dict(item)
                    merged["content"] = [obj for _, _, obj in self._content(rec)]
                    rec.done = merged
                else:
                    rec.done = item
            return

        if etype == "response.content_part.added":
            rec = self._by_id.get(data.get("item_id"))
            if rec is not None:
                rec.parts[data.get("content_index", 0)] = {
                    "part": dict(data.get("part") or {}), "text": "",
                    "text_done": False, "part_done": False}
            return

        if etype == "response.output_text.delta":
            rec = self._by_id.get(data.get("item_id"))
            if rec is None and data.get("item_id") is None and self.items:
                # Upstream omitted the id. The last item shown is the only honest
                # attribution, and it keeps `output()` equal to the deltas.
                rec = self.items[-1]
            if rec is not None:
                part = rec.parts.setdefault(
                    data.get("content_index", 0),
                    {"part": {"type": "output_text", "annotations": []},
                     "text": "", "text_done": False, "part_done": False})
                part["text"] += data.get("delta", "")
            return

        if etype == "response.output_text.done":
            rec = self._by_id.get(data.get("item_id"))
            if rec is not None:
                part = rec.parts.setdefault(
                    data.get("content_index", 0),
                    {"part": {"type": "output_text", "annotations": []},
                     "text": "", "text_done": False, "part_done": False})
                part["text"] = data.get("text", part["text"])
                part["text_done"] = True
            return

        if etype == "response.content_part.done":
            rec = self._by_id.get(data.get("item_id"))
            if rec is not None:
                part = rec.parts.get(data.get("content_index", 0))
                if part is not None:
                    part["part_done"] = True
                    if "part" in data:
                        part["part"] = dict(data["part"])
            return

        for kind, (delta_t, done_t, field) in _TOOL_EVENTS.items():
            if etype == delta_t:
                rec = self._by_id.get(data.get("item_id"))
                if rec is not None:
                    rec.args += data.get("delta", "")
                return
            if etype == done_t:
                rec = self._by_id.get(data.get("item_id"))
                if rec is not None:
                    rec.args = data.get(field, rec.args)
                    rec.args_done = True
                return

    # --- closing -------------------------------------------------------------

    def close_frames(self, message_status=None):
        """The frames that finish every item still open, in emission order.

        `message_status` is the status an open message is closed with (default
        `completed`)."""
        frames = []
        for rec in self.items:
            if rec.done is not None or rec.upstream_done:
                continue
            if rec.kind in TOOL_KINDS:
                frames.extend(self._close_tool(rec))
            else:
                frames.extend(self._close_message(rec, message_status))
        return frames

    def _content(self, rec):
        """This message's parts as the delivered frames left them.

        Returns `(content_index, part_state, object)` — the object is what the
        client ends up with, the state is what the closing frames still owe.
        """
        out = []
        for ci in sorted(rec.parts):
            part = rec.parts[ci]
            obj = dict(part["part"])
            obj.setdefault("type", "output_text")
            obj["text"] = part["text"]
            out.append((ci, part, obj))
        return out

    def _close_message(self, rec, status=None):
        frames = []
        content = []
        for ci, part, obj in self._content(rec):
            # Two events, two flags. Upstream can send the text's end and be cut
            # before the part's; conflating them left the part open on the wire,
            # which is the same malformed-stream class as a second `done`.
            if not part["text_done"]:
                frames.append(("response.output_text.done", {
                    "type": "response.output_text.done",
                    "content_index": ci, "item_id": rec.item_id,
                    "output_index": rec.index, "text": part["text"],
                    "sequence_number": self._seq()}))
                part["text_done"] = True
            if not part["part_done"]:
                frames.append(("response.content_part.done", {
                    "type": "response.content_part.done",
                    "content_index": ci, "item_id": rec.item_id,
                    "output_index": rec.index, "part": obj,
                    "sequence_number": self._seq()}))
                part["part_done"] = True
            content.append(obj)
        item = dict(rec.item)
        if rec.kind == MESSAGE:
            item["status"] = status or "completed"
            item["content"] = content
        elif item.get("status") == "in_progress":
            # The relay is closing an item upstream never finished. Echoing
            # `in_progress` inside a `done` frame is a frame that contradicts
            # itself; `incomplete` is what actually happened.
            item["status"] = "incomplete"
        # Any other kind — a reasoning item above all — is closed as upstream
        # last described it. Stamping `content` and `completed` on a reasoning
        # item would claim a shape and a status it never had, and would leave its
        # own open part (a reasoning summary) unclosed while saying otherwise.
        frames.append(("response.output_item.done", {
            "type": "response.output_item.done", "output_index": rec.index,
            "item": item, "sequence_number": self._seq()}))
        rec.done = item
        return frames

    def _close_tool(self, rec):
        """Close a client tool call the relay cut. `incomplete` is the truth: the
        arguments below are what was delivered, and they are not a finished call."""
        delta_t, done_t, field = _TOOL_EVENTS[rec.kind]
        frames = []
        if not rec.args_done:
            # Upstream may have ended the arguments and then been cut before it
            # closed the item. A second `*.done` for one call is the kind of
            # malformed stream this module exists to remove, so the flag is
            # whether *upstream* ended them, not whether the item is closed.
            frames.append((done_t, {
                "type": done_t, "item_id": rec.item_id,
                "output_index": rec.index, field: rec.args,
                "sequence_number": self._seq()}))
        item = dict(rec.item)
        item["status"] = "incomplete"
        item[field] = rec.args
        frames.append(("response.output_item.done", {
            "type": "response.output_item.done", "output_index": rec.index,
            "item": item, "sequence_number": self._seq()}))
        rec.done = item
        self.cut_calls += 1
        return frames

    # --- the completed response ---------------------------------------------

    def output(self):
        """`response.completed.response.output`: the items the client was shown.

        Every item is closed by the time this is read, so each one carries the
        object the client ended up with — upstream's own for an item upstream
        completed, the relay's reconstruction for one it had to close.
        """
        out = []
        for rec in self.items:
            out.append(dict(rec.done if rec.done is not None else rec.item))
        return out

    def report(self):
        """Counts for the run record. No content, only what happened."""
        return {"hidden_pad_calls": self.hidden_calls,
                "cut_tool_calls": self.cut_calls,
                "items": len(self.items),
                # How much answer the relay withheld for size, in the same shape
                # as `delivered_chars`: a count, never the content. A run with a
                # non-zero `payload_chars_dropped` is a run whose client was sent
                # a placeholder — the operator reads it here, not in the stream.
                "payload_capped": self.payload_capped,
                "payload_chars_dropped": self.payload_chars_dropped}
