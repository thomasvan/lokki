#!/usr/bin/env python3
"""The midstream-abort relay for the Codex Responses API.

**A cut removes the upstream's usage event, not its charge.** The provider
finalizes no response, so it reports no usage — and it still bills the cached prefix
of the request it had already read when the socket dropped. A cut turn is
therefore cheaper than a direct one and never free, and how much cheaper depends
on where the cut can land: the same Codex command cost the 5h meter +3 through
this relay (3 dispatches, all cut) against +9 sent direct (6 dispatches,
finalized), and the relayed arm also stopped before the file was written. See
*The cut removes the usage event, not the charge* in docs/relay/cost-of-a-cut.md.

The relay is an ordinary client of the provider: it sends requests with the operator's own
credential and closes a read it no longer needs. Its purpose is to make long turns cost
less. It does not alter what the provider records or meters; README.md says what it changes
in a request and what it does not do.

This module is the entry point and a re-export surface. The implementation is
split under `lokki_core/`:

    core.py     paths, run log, input flattening, SSE writing, shared errors
    config.py   config.yaml plus environment overrides, resolved settings
    renamer.py  frame identity (sequence/item/output renumbering), body building
    sse.py      server-sent-events framing per spec
    engine.py   one upstream request per segment, and the segment run loop
    handler.py  request guards, the listeners, main()

Importing this module gives the same names as before the split, which is what
the offline and live test suites use.

Sits between the client and CLIProxyAPI (which relays to the ChatGPT Codex
backend).

Exposure rules (see WORKSPACE_PROTOCOL.md): the relay only listens on addresses
named in its config file and refuses to start without an auth token, because it
injects the operator's upstream credential into every request. Shipping config
binds loopback plus this machine's Tailscale address, the same posture
docker-compose.yml uses for cliproxy — never a wildcard. Adding a public
address is a Human decision, not a code default.

Configuration is read from core/config.yaml (see config.example.yaml)
with environment variables overriding it, so no secret has to be passed on the
command line and none is stored in the repository.

Modes:

  control      - clean pass-through; the real response.completed + usage arrives.
  marker       - injects marker/appendix instructions; aborts the upstream socket
                 the moment the marker is seen, then closes the turn with the
                 terminal frame the client needs, its token counts estimated from
                 the text the client was shown.
  after-deltas - no injection; aborts the upstream socket after N text deltas.
  segmented    - marker+appendix injection, optional __pad9f3 tool-schema padding,
                 a holdback buffer so the marker never reaches the client, a
                 per-segment char budget that aborts mid-generation and continues
                 transparently (tail carried over), renumbered SSE frames, and a
                 terminal frame synthesized for the client.

The char budget bounds the *text* the relay delivers. It says nothing about an
item payload — a generated image is a base64 field of ~2M characters on one item —
so `payload_cap` bounds that separately: an item carrying a payload above the cap
is delivered with the payload replaced by a 1x1 pixel placeholder, and the run
record counts what was withheld. The client stores what it was sent, so a capped
payload is also what it does not send back on every later turn. The cap withholds
content; it does not cut the run.

What that volume is worth was measured, and then corrected, on 2026-10-05: aborted
dispatches are cheap and roughly flat (3,952,741 characters over seven dispatches
moved the meter by 0), so capping 84% of a run's characters is worth ~1-2 points
rather than the ~36 an earlier reading of the same run attributed to them. No cost
model for a real *session* survives the data: a pair of pelican runs that moved the
meter +12 and +1 was never a controlled experiment (run A sent ~3.3x the
`astra`/`xhigh` volume; run C's bulk was `luna` at `low`; the +12 bracket also
contains the turn before it), so `imagegen_cut` is justified by the artifact it
prevents, not by a points claim: it ends a run at
the frame that *names* an image item, before the `in_progress` -> `generating` ->
60-140s -> `completed` -> 2M-character `result` exists. That is a capability cut, so
it is a knob, and false is the setting for a client whose images matter. See
docs/relay/relay-8371.md.

Every run appends a JSON line to the run log, which is evidence and never a source
file: it defaults to `$XDG_STATE_HOME/cliproxy/runs.jsonl` (see `runs_log:` in
config.example.yaml), and a path inside this repository is refused, at startup and
again at the write. The committed `core/runs.jsonl` is the historical
seed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Re-exported names below are bound once, at import. They exist so the test
# suites and any older caller keep working; they are *snapshots*, not live
# readings. To read or change configuration use `config` (or `server.config`):
# assigning to a scalar here does not reach the code that reads it, which
# the test_offline.py ENSURES row "the shims scalars are snapshots, not live readings" pins.
from lokki_core import config, core, engine, handler, renamer, sse  # noqa: E402,F401
from lokki_core.config import (  # noqa: E402,F401
    API_KEY,
    CONFIG_PATH,
    LISTEN,
    RELAY_TOKEN,
    UPSTREAM,
    ConfigError,
    apply_settings,
    ensure_settings,
    load_settings,
    parse_listen,
    _DEFAULT_LISTEN,
    _DEFAULT_UPSTREAM,
)
from lokki_core.core import (  # noqa: E402,F401
    ResponseError,
    _BufSink,
    extract_prompt,
    log_run,
    read_runs,
    sse_send,
)
from lokki_core.engine import State, run_upstream, stream_segment  # noqa: E402,F401
from lokki_core.handler import Handler, main, start_listeners, _ReusableServer  # noqa: E402,F401
from lokki_core.renamer import (  # noqa: E402,F401
    APPENDIX,
    DUMMY_TOOL,
    HOLD_CHARS,
    INJECTED_INSTRUCTIONS,
    PAD_KEY,
    PAD_PROP,
    MARKER,
    TAIL_CHARS,
    Renamer,
    build_body,
    inject_pad,
    synth_completed,
)
from lokki_core.sse import iter_sse_frames  # noqa: E402,F401
# Deliberately after the renamer import: `renamer.TAIL_CHARS` is the *default*
# (1500) while the running value is the resolved setting (W1.6). The name here
# must mean the one the engine uses, or a reader of this shim gets the wrong
# number.
from lokki_core.config import DRAIN, TAIL_CHARS, TOKENS  # noqa: E402,F401

__all__ = [
    "APPENDIX", "API_KEY", "CONFIG_PATH", "ConfigError", "DUMMY_TOOL", "HOLD_CHARS",
    "INJECTED_INSTRUCTIONS", "LISTEN", "PAD_KEY", "PAD_PROP",
    "RELAY_TOKEN", "Renamer", "ResponseError", "MARKER", "State",
    "DRAIN", "TAIL_CHARS", "TOKENS", "UPSTREAM", "Handler", "apply_settings",
    "build_body",
    "ensure_settings", "extract_prompt", "inject_pad", "iter_sse_frames",
    "load_settings", "log_run", "main", "parse_listen", "read_runs",
    "run_upstream", "sse_send", "start_listeners", "stream_segment",
    "synth_completed",
]

if __name__ == "__main__":
    sys.exit(main())
