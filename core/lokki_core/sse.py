"""Server-sent-events framing.

The Responses API sends one JSON document per frame, but the SSE spec allows a
frame's body to arrive as several `data:` lines and terminates a frame with a
blank line. Parsing one JSON document per line silently drops those frames.
"""

__all__ = ["iter_sse_frames"]


def iter_sse_frames(lines):
    """Yield (event_name, data_payload) for each complete SSE frame.

    Per the SSE spec a frame ends at a blank line, `data:` may be repeated and
    the payload is the lines joined with newlines, and a leading colon is a
    comment. Parsing one JSON document per line silently dropped any event the
    upstream split across several `data:` lines.

    `data_payload` is None for a frame that carried only an `event:` field.
    """
    event_name = None
    data_lines = []
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        line = raw.rstrip("\r\n")
        if line == "":
            if event_name is not None or data_lines:
                yield event_name, ("\n".join(data_lines) if data_lines else None)
            event_name, data_lines = None, []
            continue
        if line.startswith(":"):
            continue  # comment / keepalive
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        # other fields (id:, retry:) are not used by this engine
    if event_name is not None or data_lines:
        yield event_name, ("\n".join(data_lines) if data_lines else None)
