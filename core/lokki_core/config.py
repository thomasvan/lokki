"""Configuration for the relay.

Settings are resolved from `config.yaml` and then overridden by environment
variables, the same shape cliproxy uses: a local file holds the secrets, nothing
sensitive has to appear on the command line, and nothing sensitive is committed.
The token has no default — the relay refuses to start without one.
"""

import json
import os
import sys
import time
import urllib.parse

try:
    import yaml
except ImportError:  # config file is optional; env vars alone are enough
    yaml = None

from . import core, migrate
from .core import (
    CONFIG_PATH,
    DEFAULT_RUNS_LOG,
    ResponseError,
    _text,
    _DEFAULT_LISTEN,
    refuse_inside_repo,
)
from . import schema
from .schema import _DEFAULT_UPSTREAM

# Request-shape defaults. They live here rather than as literals inside the
# handler so the run record's config fingerprint can name them: two records taken
# with different segment budgets are not comparable. Every `BUILTIN_*` value is the
# schema's default (`schema.py` is the one place a default is stated); the names
# stay because the handler and the tests read them.
#
# The three cut budgets are built in, and an operator may name them in the config
# file or the environment (`seg_budget`, `total_cap`, `max_segments`;
# `RELAY_SEG_BUDGET` and so on). `DEFAULT_*` below is what the relay is running
# with: the built-in value until `apply_settings` replaces it, and again after a
# reload that no longer names it. A built-in change is a code change; a named one is
# the operator's decision, and it is in `/version` and in every run record's knobs.
BUILTIN_SEG_BUDGET = schema.get("seg_budget").default
BUILTIN_TOTAL_CAP = schema.get("total_cap").default
BUILTIN_MAX_SEGMENTS = schema.get("max_segments").default
DEFAULT_SEG_BUDGET = BUILTIN_SEG_BUDGET
DEFAULT_TOTAL_CAP = BUILTIN_TOTAL_CAP
DEFAULT_MAX_SEGMENTS = BUILTIN_MAX_SEGMENTS
# Whether a text answer that a segment budget continues stays ONE message item for
# the client. Off, each continuation opens its own item and a long answer arrives as
# one message per segment (the behaviour before 2026-10-07). It is a setting because
# it changes the shape of the stream a strict client sees, so an operator whose
# client mishandles the merged stream can return to the old shape without a deploy:
# `merge_text: false`, or `RELAY_MERGE_TEXT=0`.
BUILTIN_MERGE_TEXT = schema.get("merge_text").default
DEFAULT_MERGE_TEXT = BUILTIN_MERGE_TEXT
# Whether the relay sends a `session-id` header of its own on every upstream request: a
# constant for each client, derived from the client's name. Off by default, and a
# decision, not a tuning knob: of Codex's per-session headers the provider's prompt cache
# followed that one when it was measured, and the relay sends none of the client's own (it
# forwards `session_id` with an underscore, which Codex does not send), so a new session's
# first request does not read what an earlier one wrote. With it, one client's
# sessions share a cache entry for as long as it lasts, and look to the provider like
# one session. `session_pin: true`, or `RELAY_SESSION_PIN=1`.
BUILTIN_SESSION_PIN = schema.get("session_pin").default
DEFAULT_SESSION_PIN = BUILTIN_SESSION_PIN
# Whether the model is asked to write a tool call as text, which the relay turns
# into the call and ends the request at, so that no request of a turn that calls a tool finalizes
# the call (a cut request is still charged for the prefix it had read, docs/relay/cost-of-a-cut.md).
# On unless a file or the environment turns it off, and off
# means the relay behaves exactly as before. `text_calls: false`, or `RELAY_TEXT_CALLS=0`.
BUILTIN_TEXT_CALLS = schema.get("text_calls").default
DEFAULT_TEXT_CALLS = BUILTIN_TEXT_CALLS
# Whether a `text_calls` request still carries the relay's padding tool and asks for the filler section after the
# marker. On is what the relay always did: the tool is the abort window for an answer that has no marker. Off is the
# lighter request: no tool, the marker alone, and the model has less to think about before it answers (docs/relay/
# latency.md). Only a clear yes or no is accepted, because off removes that window. `text_calls_pad: false`, or
# `RELAY_TEXT_CALLS_PAD=0`; it does nothing unless `text_calls` is on.
BUILTIN_TEXT_CALLS_PAD = schema.get("text_calls_pad").default
DEFAULT_TEXT_CALLS_PAD = BUILTIN_TEXT_CALLS_PAD
# The largest payload field the relay will *deliver* inside one output item.
# `seg_budget` and `total_cap` count answer text; a base64 image result counts
# toward neither, and a generated image is 1.9-2.1M characters of one. Without the
# cap the client stores that item and sends it back on every later turn: 18.2M of
# one run's 22.2M upstream characters were two such results. What that volume is
# *worth* was corrected on 2026-10-05 — an aborted dispatch is charged ~0.21 points
# whatever it carries, so removing 84% of the characters saved ~1-2 points, not the
# ~36 an earlier reading of the same run attributed to them. The cap is still right
# — it bounds what crosses the wire and what the client keeps in its transcript —
# but its reason is not the meter. 0 disables it.
#
# An operator may name it in the config file or the environment (`payload_cap`,
# `RELAY_PAYLOAD_CAP`); `DEFAULT_PAYLOAD_CAP` is what the relay is running with, the
# built-in value until `apply_settings` replaces it and again after a reload that no
# longer names it.
BUILTIN_PAYLOAD_CAP = schema.get("payload_cap").default
DEFAULT_PAYLOAD_CAP = BUILTIN_PAYLOAD_CAP
# Whether a run ends as soon as upstream names an `image_generation_call` item —
# before the image exists. What it buys is not a measured points saving: two runs of
# one prompt, half an hour apart, each with one generated image, moved the meter +12
# and +1, so a single session cannot be priced (see docs/relay/relay-8371.md). It
# buys the artifact: without the cut the provider produces, and the relay then caps,
# a ~2M-character base64 result, and the run takes ~5 minutes against 31 seconds.
# The cost is capability — a client that asked for an image gets an aborted turn —
# which is why it is a knob and false is the setting for a client whose images
# matter. On an agentic command the cost reaches past the image: the turn ends at
# the frame that names the item, the relay synthesizes a completion, and the client
# stops. A Codex run cut this way cost the 5h meter +3 against +9 for the same run
# sent direct, and left the file it was asked for unwritten
# (docs/relay/cost-of-a-cut.md), so a command whose artifact has to land belongs
# on false.
#
# Named in the config file or the environment as `imagegen_cut` / `RELAY_IMAGEGEN_CUT`,
# with `payload_cap: 0`, it is the setting that lets an image item through as the
# provider sends it (docs/relay/images.md, "Images in a Responses session"). Only a
# clear true or false is read: this is the knob whose off is the expensive state, so
# a typo is refused and does not turn it off.
BUILTIN_IMAGEGEN_CUT = schema.get("imagegen_cut").default
DEFAULT_IMAGEGEN_CUT = BUILTIN_IMAGEGEN_CUT

# Serving limits — the hostile-client mitigations (plan W3.4-W3.6). Each is a
# property a test can measure, rather than a claim that service "cannot degrade".
LISTEN_BACKLOG = 128           # stdlib default is 5; under load that refuses clients
MAX_CLIENT_CONNECTIONS = 64    # concurrent client connections; past it, 503
CLIENT_TIMEOUT = 30            # per-connection socket timeout, so an idle client
                               # cannot hold a thread forever (slowloris)
MAX_BODY_BYTES = 8 * 1024 * 1024   # body cap, answered 413 before being buffered
# The image routes (lokki_core/images.py) carry pictures, so their caps are their own: an edit
# sends the image it edits (as base64 in JSON or as a multipart file), and an answer
# holds each generated image as base64 (a 1024x1024 PNG measured 2.4 MB, 3.2 million
# characters). Both are bounds on memory per request, answered before anything is held.
MAX_IMAGE_BODY_BYTES = 32 * 1024 * 1024
MAX_IMAGE_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_CONCURRENT_IMAGES = 2      # image calls in flight at once (their own slots, not the text route's)
IMAGE_TIMEOUT = 600            # seconds the cliproxy may take to answer, or to send its next bytes
                               # (one 1024x1024 image took 36 s; a high-quality one takes longer)
RUNS_MAX_BYTES = 8 * 1024 * 1024   # W4.4: rotate runs.jsonl at this size, keeping
                                   # the chain continuous across the seam
RUNS_KEEP = 5                      # rotated files kept beside it
DEBUG_MAX_BYTES = 32 * 1024 * 1024  # rotate the diagnostic trace at this size. It
                                    # is the one file here that grows with every
                                    # frame: an investigation left switched on had
                                    # 261 MB beside the run log when this was set.
DEBUG_KEEP = 3                      # rotated trace files kept beside it
# W3.7: SIGTERM lets in-flight streams finish for this long, then exits anyway.
# Bounded on purpose: an unbounded graceful shutdown hangs a deploy on one stuck
# client. `restart-relay.sh` waits longer than this before it escalates to SIGKILL.
DRAIN_SECONDS = schema.get("drain_seconds").default
DEFAULT_TAIL_CHARS = schema.get("tail_chars").default

__all__ = [
    "CONFIG_PATH", "RUNS_LOG", "ResponseError", "ConfigError",
    "ALLOW_CLIENT_TUNING", "ALLOW_PUBLIC_BIND", "API_KEY", "CLIENT_TIMEOUT", "DEFAULT_CLIENT",
    "DEFAULT_MAX_SEGMENTS", "DEFAULT_TAIL_CHARS", "CAPTURE_DIR", "DRAIN", "DEBUG_LOG", "REJECT_PREVIOUS_RESPONSE_ID", "TAIL_CHARS",
    "DEFAULT_SEG_BUDGET", "DEFAULT_TOTAL_CAP", "LISTEN", "LISTEN_BACKLOG",
    "BUILTIN_SEG_BUDGET", "BUILTIN_TOTAL_CAP", "BUILTIN_MAX_SEGMENTS",
    "BUILTIN_MERGE_TEXT", "DEFAULT_MERGE_TEXT", "BUILTIN_SESSION_PIN", "DEFAULT_SESSION_PIN",
    "BUILTIN_TEXT_CALLS", "DEFAULT_TEXT_CALLS", "BUILTIN_TEXT_CALLS_PAD", "DEFAULT_TEXT_CALLS_PAD",
    "BUILTIN_PAYLOAD_CAP", "DEFAULT_PAYLOAD_CAP", "BUILTIN_IMAGEGEN_CUT", "DEFAULT_IMAGEGEN_CUT",
    "MAX_BODY_BYTES", "MAX_CLIENT_CONNECTIONS", "MAX_CONCURRENT", "PACING",
    "MAX_IMAGE_BODY_BYTES", "MAX_IMAGE_RESPONSE_BYTES", "MAX_CONCURRENT_IMAGES", "IMAGE_TIMEOUT",
    "DEBUG_KEEP", "DEBUG_MAX_BYTES",
    "RUNS_KEEP", "RUNS_MAX_BYTES",
    "RELAY_TOKEN", "RETRIES", "TOKENS", "UPSTREAM", "ROUTING", "LOW_QUOTA", "CLIPROXY",
    "apply_settings", "ensure_settings", "load_settings", "read_config", "resolve_settings", "parse_listen",
    "reload_settings",
]

class ConfigError(Exception):
    """Raised when the relay cannot start from the given configuration."""


def __getattr__(name):
    """`config.RUNS_LOG` is the *running* path, not the import-time one.

    It used to be a copy of `core.RUNS_LOG` taken at import, which is a second
    representation of one fact the moment the serving process can be pointed
    somewhere else: `/api/chain` and the operator would name one file while the
    relay appended to another. Slice 3 lost a commit to exactly this shape — a
    token role that was a bool where it was produced and a string where it was
    compared, so every operator was a client and no test noticed. One
    representation, asked for rather than copied."""
    if name == "RUNS_LOG":
        return core.RUNS_LOG
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def parse_listen(value, default=_DEFAULT_LISTEN):
    """Normalise the `listen` setting to a tuple of host:port strings.

    One "host:port" or a list of them; a bare port means loopback. This is what
    lets the relay answer on both 127.0.0.1 and the Tailscale address, the way
    docker-compose.yml publishes cliproxy."""
    if value is None:
        return tuple(default)
    entries = [value] if isinstance(value, str) else list(value)
    out = []
    for entry in entries:
        entry = str(entry).strip()
        if not entry:
            continue
        if entry.isdigit():          # bare port -> loopback
            entry = f"127.0.0.1:{entry}"
        host, _, port = entry.rpartition(":")
        if not port.isdigit():
            host, port = "", entry     # "8371" or a malformed tail
        if not port.isdigit():
            raise ConfigError(f"listen entry {entry!r} is not host:port")
        out.append(f"{host}:{int(port)}")
    if not out:
        raise ConfigError("listen is empty")
    return tuple(out)


def _count(value, name):
    """A whole number from YAML or the environment, as `int(value)` alone would not read it:
    not a boolean (`int(True)` is 1), not a fraction (`int(1.5)` is 1), and infinity or
    NaN (`.inf` in YAML) is a `ConfigError` and not the `OverflowError` that would escape a
    reload. The caller checks the range."""
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a whole number")
    if isinstance(value, float):
        if not value.is_integer():          # also False for inf and nan
            raise ConfigError(f"{name} must be a whole number")
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a whole number") from None


def _flag(value, default=False):
    """A YAML boolean or an environment string, without `bool("false") is True`.

    Anything unrecognised is treated as false rather than as "set", so a typo in
    a security flag fails closed.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return _text(value).lower() in ("1", "true", "yes", "on")


def _strict_flag(value, name):
    """A flag whose off is the costly state: only a clear yes or no, and a typo is an error, not a quiet off."""
    if isinstance(value, bool):
        return value
    word = _text(value).lower()
    if word in ("1", "true", "yes", "on"):
        return True
    if word in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false")


def _bounded(value, key, why=""):
    """`value` inside the schema's range for `key`, else a `ConfigError` naming the key;
    `why` is added to the message where the bound has a reason the operator should read."""
    f = schema.get(key)
    if f.minimum is not None and value < f.minimum:
        raise ConfigError(f"{key} must be at least {f.minimum}" + (f": {why}" if why else ""))
    if f.maximum is not None and value > f.maximum:
        raise ConfigError(f"{key} must be at most {f.maximum}" + (f": {why}" if why else ""))
    return value


def _choice(value, key):
    """One of the schema's words for `key`; unset is its default, and an unknown word is
    an error, not a fallback (spec 3.3)."""
    f = schema.get(key)
    word = _text(value) or f.default
    if word not in f.choices:
        raise ConfigError(f"{key} must be one of " + ", ".join(f.choices))
    return word


def _str_list(value, key):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{key} must be a list of strings")
    return list(value)


def _upstream_url(value, key):
    """`http://` or `https://`, a host and an optional port, and nothing else; empty
    is unset. The value is not echoed: it is the place a password would be."""
    value = _text(value).rstrip("/")
    if not value:
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:              # an unclosed bracket, a port that is not a number
        parts, port = urllib.parse.urlsplit(""), 0
    if (parts.scheme.lower() not in ("http", "https") or not parts.hostname or port == 0
            or "@" in parts.netloc or parts.path or parts.query or parts.fragment
            or "?" in value or "#" in value or any(c.isspace() for c in value)):
        raise ConfigError(f"{key} must be http:// or https://, a host and an optional port, "
                          "with no path, query or credentials")
    return value


def _entry(section, key):
    """A section key's value, or the schema's default when it is missing or null: a
    null section key reads as unset, as a null top-level key does."""
    value = section.get(key.split(".", 1)[1])
    return schema.get(key).default if value is None else value


def _section(raw, name):
    """A nested mapping from the file, or {}. One `get` on `raw`, so the recording
    test sees the top-level key once."""
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _sanitize_secret(value):
    """Secrets reach an HTTP header (upstream Authorization) or a comparison;
    CR/LF/NUL in one is header smuggling, not a password. Reject it outright."""
    value = _text(value)
    if any(ch in value for ch in ("\r", "\n", "\0")):
        raise ConfigError("a configured secret contains CR/LF/NUL")
    return value


# The name a token gets when it arrived the old way (`auth.token`, `RELAY_TOKEN`).
# One client, no identity: honest about being unnamed rather than inventing one.
DEFAULT_CLIENT = "default"


def _token_entries(auth, env):
    """Resolve `auth.tokens` (or the older single `auth.token`) to a tuple of
    `(name, token)`.

    W0: one shared token meant no attribution and no revocation that did not
    mean restarting every client, so "no second client is onboarded until this
    lands". Two names on one token is the
    same defect wearing a list, so it is refused rather than accepted.
    """
    entries = []
    raw_env_tokens = _text(env.get("RELAY_TOKENS"))
    if raw_env_tokens:
        try:
            parsed = json.loads(raw_env_tokens)
        except ValueError as e:
            raise ConfigError(f"RELAY_TOKENS is not valid JSON: {e}")
        if not isinstance(parsed, list):
            raise ConfigError(
                "RELAY_TOKENS must be a JSON list of {name, token, operator?}")
        entries = parsed
    else:
        configured = auth.get("tokens")
        if configured is not None:
            if not isinstance(configured, list):
                raise ConfigError(
                    "auth.tokens must be a list of {name, token} entries")
            entries = configured

    resolved = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"auth.tokens[{i}] must be a mapping with 'name' and 'token'")
        name = _text(entry.get("name"))
        token = _sanitize_secret(entry.get("token"))
        if not name:
            raise ConfigError(f"auth.tokens[{i}] has no 'name'")
        if not token:
            raise ConfigError(f"auth.tokens[{i}] ({name}) has no 'token'")
        if any(ch.isspace() for ch in name):
            raise ConfigError(f"auth.tokens[{i}] name {name!r} contains whitespace")
        # A named client is a *client* unless it says otherwise. The M7 policy was
        # keyed on the path, so any client token could reach the operator's
        # `/api/run` and set the relay's own knobs from there; knob rights follow
        # the identity now.
        #
        # The role is the *string* the handler and `/version` compare against, and
        # it is normalised here rather than at each reader. It was a bool for one
        # commit — `True` never equals `"operator"`, so every real operator was
        # silently a client, and only the tests noticed nothing because they
        # stubbed the string. One representation, tested from both ends.
        resolved.append((name, token,
                         "operator" if _flag(entry.get("operator", False))
                         else "client"))

    if not resolved:
        legacy = (_sanitize_secret(env.get("RELAY_TOKEN"))
                  or _sanitize_secret(auth.get("token")))
        if legacy:
            # The legacy single token is the operator's own secret, held before
            # there was any notion of a client. Demoting it would lock the
            # operator out of their own path on upgrade.
            resolved = [(DEFAULT_CLIENT, legacy, "operator")]

    seen_names = {}
    seen_tokens = {}
    for name, token, _role in resolved:
        if name in seen_names:
            raise ConfigError(f"two tokens are both named {name!r}")
        seen_names[name] = token
        if token in seen_tokens:
            raise ConfigError(
                f"clients {seen_tokens[token]!r} and {name!r} share one token; "
                "that is one identity, so attribution and revocation are both "
                "lost — give each client its own token")
        seen_tokens[token] = name
    return tuple(resolved)


def _pick(*values, default):
    """Return the first value that was actually set, else `default`.

    An `or` chain cannot express "the operator explicitly asked for zero": both
    `pacing_seconds: 0` and `RELAY_PACING=0` are falsy, so they silently fell
    through to the default and made pacing impossible to disable. Only None and
    a blank string mean "not set" here."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return default


def load_settings(config_path=None, env=None):
    """Resolve settings from config.yaml, then environment overrides.

    Returns a dict with keys upstream, api_key, listen, token.
    Raises ConfigError with a specific reason instead of starting half
    configured. `config_path` is used as given (an unusable explicit path is an
    error); the default path is optional."""
    env = os.environ if env is None else env
    explicit = config_path is not None
    path = config_path if explicit else env.get("RELAY_CONFIG", CONFIG_PATH)
    raw = {}
    if explicit and path and not os.path.exists(path):
        raise ConfigError(f"config file {path!r} does not exist")
    if path and os.path.exists(path):
        if yaml is None:
            raise ConfigError(f"{path} exists but PyYAML is not installed; "
                              "install pyyaml or drop the config file")
        raw = read_config(path)[1]
        migrate.migrate(raw)
    return resolve_settings(raw, env, path=path)


def read_config(path):
    """The text of the config file at `path` and its top-level mapping, as UTF-8.
    Raises ConfigError when it cannot be read, is not YAML, or is not a mapping;
    the settings write path reads the file it is about to rewrite through here."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        loaded = yaml.safe_load(text)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}")
    except Exception as e:  # not UTF-8, or a YAML parse failure
        raise ConfigError(f"{path} is not valid YAML: {e}")
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return text, loaded


def resolve_settings(raw, env, path=None):
    """The settings a parsed config mapping and an environment resolve to.

    Every default and bound is the schema's (`schema.py`); this function reads the
    keys and applies the rules that join them. `raw` is the file's top-level mapping
    and is only read with `get`, so a recording mapping can list the keys it reads.
    Raises ConfigError naming the key."""
    S = schema.get
    auth = raw.get("auth") or {}
    if not isinstance(auth, dict):
        raise ConfigError("auth must be a mapping (auth: {token: ...})")

    try:
        pacing = float(_pick(env.get("RELAY_PACING"), raw.get("pacing_seconds"),
                             default=S("pacing_seconds").default))
    except (TypeError, ValueError):
        raise ConfigError("pacing_seconds must be a number")
    _bounded(pacing, "pacing_seconds")
    try:
        max_concurrent = int(_pick(env.get("RELAY_MAX_CONCURRENT"),
                                   raw.get("max_concurrent_upstream"),
                                   default=S("max_concurrent_upstream").default))
    except (TypeError, ValueError):
        raise ConfigError("max_concurrent_upstream must be an integer")
    _bounded(max_concurrent, "max_concurrent_upstream")
    # Coerced, never refused: a value under 1 is read as 1.
    retries = _pick(env.get("RELAY_RETRIES"), raw.get("retries"),
                    default=S("retries").default)
    try:
        retries = max(1, int(retries))
    except (TypeError, ValueError):
        raise ConfigError(f"retries must be an integer, got {retries!r}")

    # W1.6: the continuation tail was a module constant, so fidelity could not be
    # tuned without editing code — and the tradeoff is real, since the tail is
    # what a continuation carries of the answer so far. Its value now rides in
    # the fingerprint so two records taken with different tails are comparable.
    try:
        tail_chars = int(_pick(env.get("RELAY_TAIL_CHARS"), raw.get("tail_chars"),
                               default=DEFAULT_TAIL_CHARS))
    except (TypeError, ValueError):
        raise ConfigError("tail_chars must be an integer")
    _bounded(tail_chars, "tail_chars")

    # The cut budgets, named only if the operator names them. At least 1, never 0:
    # for these a zero means "no bound", which is the direction that leaks the
    # answer this relay exists to cut, so `resolve_knobs` already reads a request's
    # zero as "use the default" and a zero here is refused rather than obeyed.
    budgets = {}
    for name, builtin in (("seg_budget", BUILTIN_SEG_BUDGET),
                          ("total_cap", BUILTIN_TOTAL_CAP),
                          ("max_segments", BUILTIN_MAX_SEGMENTS)):
        try:
            value = int(_pick(env.get("RELAY_" + name.upper()), raw.get(name),
                              default=builtin))
        except (TypeError, ValueError):
            raise ConfigError(f"{name} must be an integer")
        budgets[name] = _bounded(value, name, why="0 would mean no bound, "
                                 "which leaks the answer the relay exists to cut")
    if budgets["total_cap"] < budgets["seg_budget"]:
        raise ConfigError(
            f"total_cap ({budgets['total_cap']}) cannot be below seg_budget "
            f"({budgets['seg_budget']}): the cap would end every run before one "
            "segment could fill")
    # Not a number: a flag, read like the other flags, so a typo falls to false
    # (the old stream shape) rather than to an unintended on.
    merge_text = _flag(_pick(env.get("RELAY_MERGE_TEXT"), raw.get("merge_text"),
                             default=BUILTIN_MERGE_TEXT), default=BUILTIN_MERGE_TEXT)
    # A flag whose default is off and whose typo is off: only a clear yes turns it on.
    session_pin = _flag(_pick(env.get("RELAY_SESSION_PIN"), raw.get("session_pin"),
                              default=BUILTIN_SESSION_PIN), default=BUILTIN_SESSION_PIN)
    text_calls = _flag(_pick(env.get("RELAY_TEXT_CALLS"), raw.get("text_calls"),
                             default=BUILTIN_TEXT_CALLS), default=BUILTIN_TEXT_CALLS)
    text_calls_pad = _strict_flag(_pick(env.get("RELAY_TEXT_CALLS_PAD"), raw.get("text_calls_pad"),
                                        default=BUILTIN_TEXT_CALLS_PAD), "text_calls_pad")
    # The two image knobs. `payload_cap` is a count of characters where 0 is a setting (no
    # cap), unlike the budgets above; anything that is not a whole number of at least 0 is
    # refused (a fraction, infinity, a yes/no, a negative). `imagegen_cut` is a flag whose
    # default is on and whose off is the costly state, so only a clear yes or no is
    # accepted (true/false, 1/0, yes/no, on/off), and a typo is an error and not a quiet "off".
    payload_cap = _count(_pick(env.get("RELAY_PAYLOAD_CAP"), raw.get("payload_cap"),
                               default=BUILTIN_PAYLOAD_CAP), "payload_cap")
    _bounded(payload_cap, "payload_cap", why="0 turns the cap off")
    imagegen_cut = _strict_flag(_pick(env.get("RELAY_IMAGEGEN_CUT"), raw.get("imagegen_cut"),
                                      default=BUILTIN_IMAGEGEN_CUT), "imagegen_cut")

    # W3.7: how long SIGTERM lets in-flight streams finish. A resolved setting so
    # a test can shrink it, and so an operator can lengthen it without editing
    # code — the alternative was a module constant nobody could see.
    try:
        drain = int(_pick(env.get("RELAY_DRAIN_SECONDS"), raw.get("drain_seconds"),
                          default=DRAIN_SECONDS))
    except (TypeError, ValueError):
        raise ConfigError("drain_seconds must be an integer")
    _bounded(drain, "drain_seconds")

    # S4.1. Where the evidence goes. `RELAY_RUNS_LOG` first, then `runs_log:`, else
    # the XDG state directory — never the checkout. The relay appends here while it
    # serves, so an in-repo path makes the repository permanently dirty with live
    # evidence and lets a `checkout`/`stash`/`clean` drop records behind the running
    # process. Refused rather than warned about, and refused for an explicit path
    # *or* an environment one: a guard that only covers the config file is a guard
    # the next deployment walks around.
    #
    # The default is resolved from `env`, not from the import-time constant, so the
    # `env` argument means the same thing for every key here — and so the default
    # itself is validated: with `XDG_STATE_HOME` pointing into the checkout, startup
    # refuses rather than starting a relay whose every run would fail at the write.
    runs_log_raw = _text(env.get("RELAY_RUNS_LOG"))
    runs_log_source = "env RELAY_RUNS_LOG"
    if not runs_log_raw:
        runs_log_raw = _text(raw.get("runs_log"))
        runs_log_source = "config runs_log"
    if not runs_log_raw:
        runs_log_raw = core.default_runs_log(env)
        runs_log_source = "default"
    runs_log = refuse_inside_repo(
        runs_log_raw, "runs_log",
        "evidence must not be written into a git working tree, where a checkout, "
        "a stash or a `git clean` can drop it while the relay is still serving. "
        f"The default is {DEFAULT_RUNS_LOG}.")

    # S3.4. Where a captured client *body* may be written, for the real-client
    # round trip the plan still owes (W1.1's acceptance and W1.5's header list).
    # Off unless set, and refused if it points inside this repository: a captured
    # body is the client's content, and client bodies are barred from committed
    # evidence. A capture is a transient measurement, so
    # the guard is structural rather than a note in a doc. The check is shared with
    # the run log above; `realpath`, not `abspath`, because a link *outside* the
    # repository pointing *inside* it was accepted by the first version of it.
    capture_dir = _text(raw.get("capture_dir")) or _text(env.get("RELAY_CAPTURE_DIR"))
    if capture_dir:
        capture_dir = refuse_inside_repo(
            capture_dir, "capture_dir",
            "captured client bodies must never be committable. Point it somewhere "
            "like ~/.local/state/cliproxy/captures.")

    # The diagnostic trace. Off unless named. Same guard and same reason as
    # `capture_dir` above, one step further: it writes the frames in both
    # directions and the bodies the relay sent upstream, so it is client content
    # by local law and must not be committable. It is not a second evidence file —
    # nothing reads it, and the relay must keep working with it off.
    # Environment first, like `runs_log` and unlike `capture_dir`: the caller that
    # most needs a trace is one investigating *this* deployment, and it should not
    # have to edit the operator's config file — nor leave a trace switched on
    # there after the investigation ends.
    debug_log = _text(env.get("RELAY_DEBUG_LOG")) or _text(raw.get("debug_log"))
    if debug_log:
        debug_log = refuse_inside_repo(
            debug_log, "debug_log",
            "a diagnostic trace carries client content and must never be "
            "committable. Point it somewhere like "
            "~/.local/state/cliproxy/debug.jsonl.")

    # W1.4. Off is the *dangerous* state, not a safe one: with the field accepted
    # and dropped, a client that keeps its history server-side sends only the new
    # turn and gets a confidently wrong answer from a model with no context. On
    # is loud and immediate. It is a setting only so an operator can back out
    # during an incident with a SIGHUP rather than a redeploy.
    reject_previous = _flag(_pick(env.get("RELAY_REJECT_PREVIOUS_RESPONSE_ID"),
                                  raw.get("reject_previous_response_id"),
                                  default=S("reject_previous_response_id").default))
    token = _sanitize_secret(env.get("RELAY_TOKEN")) or _sanitize_secret(auth.get("token"))
    tokens = _token_entries(auth, env)
    api_key = (_sanitize_secret(env.get("CLI_PROXY_API_KEY"))
               or _sanitize_secret(raw.get("cli_proxy_api_key")))
    listen = (env.get("RELAY_LISTEN") if _text(env.get("RELAY_LISTEN"))
              else raw.get("listen"))

    # The mode and Auto's rule (`quota/poller.py` and `routing.py` read them).
    routing = _choice(raw.get("routing"), "routing")
    lq = _section(raw, "low_quota")
    low_quota = {
        "threshold_percent": _bounded(_count(_entry(lq, "low_quota.threshold_percent"),
                                             "low_quota.threshold_percent"), "low_quota.threshold_percent"),
        "rule": _choice(_entry(lq, "low_quota.rule"), "low_quota.rule"),
        "accounts": _str_list(_entry(lq, "low_quota.accounts"), "low_quota.accounts"),
        "models": _str_list(_entry(lq, "low_quota.models"), "low_quota.models"),
        "poll_seconds": _bounded(_count(_entry(lq, "low_quota.poll_seconds"),
                                        "low_quota.poll_seconds"), "low_quota.poll_seconds"),
    }
    cp = _section(raw, "cliproxy")
    cliproxy = {
        "port": _bounded(_count(_entry(cp, "cliproxy.port"), "cliproxy.port"), "cliproxy.port"),
        "url": _upstream_url(_entry(cp, "cliproxy.url"), "cliproxy.url"),
        "version": _text(_entry(cp, "cliproxy.version")),
        "panel": _strict_flag(_entry(cp, "cliproxy.panel"), "cliproxy.panel"),
        # Stored expanded, so a reader opens the path that was checked here.
        "management_secret_file": os.path.expanduser(_text(_entry(cp, "cliproxy.management_secret_file"))),
    }
    # `cliproxy.url` and `cliproxy.port` are one fact: the upstream is a named URL when
    # there is one (the environment, the old `upstream` key, then `cliproxy.url`), else
    # the local CLIProxyAPI on `cliproxy.port`. Which of the two it came from decides
    # whether a reload may move it (`apply_settings`).
    named_upstream = _text(env.get("CLIPROXY_URL")) or _text(raw.get("upstream")) or cliproxy["url"]
    upstream = (named_upstream or schema.local_upstream(cliproxy["port"])).rstrip("/")
    upstream_source = "named" if named_upstream else "port"
    # A key no schema entry knows is kept and named, not refused: a hand-written file
    # must still load, and `/api/status` lists what was ignored.
    known_top = {f.key.split(".")[0] for f in schema.FIELDS}
    unknown_keys = sorted(k for k in raw if k not in known_top)
    for name in ("low_quota", "cliproxy"):
        known = {f.key.split(".", 1)[1] for f in schema.FIELDS if f.key.startswith(name + ".")}
        unknown_keys += sorted(f"{name}.{k}" for k in _section(raw, name) if k not in known)

    return {
        **budgets,
        "merge_text": merge_text,
        "session_pin": session_pin,
        "text_calls": text_calls,
        "text_calls_pad": text_calls_pad,
        "payload_cap": payload_cap,
        "imagegen_cut": imagegen_cut,
        "tail_chars": tail_chars,
        "drain": drain,
        "reject_previous_response_id": reject_previous,
        "capture_dir": capture_dir,
        "debug_log": debug_log,
        "runs_log": runs_log,
        "runs_log_source": runs_log_source,
        "pacing": pacing,
        "max_concurrent": max_concurrent,
        "retries": retries,
        "token": token,
        "tokens": tokens,
        "api_key": api_key,
        "listen": parse_listen(listen, default=S("listen").default),
        "upstream": upstream,
        "upstream_source": upstream_source,
        "routing": routing,
        "low_quota": low_quota,
        "cliproxy": cliproxy,
        "unknown_keys": unknown_keys,
        # Binding anything wider than loopback + Tailscale is a Human decision
        # recorded in WORKSPACE_PROTOCOL.md, so the code refuses it by default
        # rather than only warning about it.
        "allow_public_bind": _flag(
            _pick(env.get("RELAY_ALLOW_PUBLIC_BIND"), raw.get("allow_public_bind"),
                  default=S("allow_public_bind").default)),
        # Review M7. A production endpoint's cut policy is the operator's, not the
        # caller's: `mode: control` finalizes every turn (billed in full, where a
        # cut turn bills only the cached prefix), and `pad_tools: false` removes
        # the abort surface. Defaults to False because this relay IS the
        # client-facing endpoint since the cutover; the operator's own path
        # `/api/run` keeps the knobs regardless, and `RELAY_ALLOW_CLIENT_TUNING=1`
        # (or `allow_client_tuning: true`) re-opens them for a caller explicitly.
        "allow_client_tuning": _flag(
            _pick(env.get("RELAY_ALLOW_CLIENT_TUNING"),
                  raw.get("allow_client_tuning"), default=S("allow_client_tuning").default)),
        "config_path": path if path and os.path.exists(path) else None,
    }


# Called with no arguments at the end of every apply, start and reload alike; a hook
# must not block.
ON_APPLY = []


def apply_settings(settings, reload=False):
    """Push resolved settings into the module globals the handler reads.

    `reload` is true only from `reload_settings`: the import-time apply and
    `ensure_settings` are starts. It decides one thing, the upstream below."""
    global UPSTREAM, API_KEY, LISTEN, RELAY_TOKEN, TOKENS, ROUTING, LOW_QUOTA, CLIPROXY, STARTED_CLIPROXY_PORT, \
        RETRIES, PACING, MAX_CONCURRENT, ALLOW_PUBLIC_BIND, ALLOW_CLIENT_TUNING, \
        CAPTURE_DIR, DEBUG_LOG, TAIL_CHARS, DRAIN, REJECT_PREVIOUS_RESPONSE_ID, \
        DEFAULT_SEG_BUDGET, DEFAULT_TOTAL_CAP, DEFAULT_MAX_SEGMENTS, DEFAULT_MERGE_TEXT, \
        DEFAULT_SESSION_PIN, DEFAULT_TEXT_CALLS, DEFAULT_TEXT_CALLS_PAD, DEFAULT_PAYLOAD_CAP, DEFAULT_IMAGEGEN_CUT
    # Per request, from here on: a run reads these when it starts, so a reload never
    # changes a run in flight. A key the new file no longer names returns to the
    # built-in value, so removing it and reloading is a way back.
    DEFAULT_SEG_BUDGET = int(settings.get("seg_budget", BUILTIN_SEG_BUDGET))
    DEFAULT_TOTAL_CAP = int(settings.get("total_cap", BUILTIN_TOTAL_CAP))
    DEFAULT_MAX_SEGMENTS = int(settings.get("max_segments", BUILTIN_MAX_SEGMENTS))
    DEFAULT_MERGE_TEXT = bool(settings.get("merge_text", BUILTIN_MERGE_TEXT))
    DEFAULT_SESSION_PIN = bool(settings.get("session_pin", BUILTIN_SESSION_PIN))
    DEFAULT_TEXT_CALLS = bool(settings.get("text_calls", BUILTIN_TEXT_CALLS))
    DEFAULT_TEXT_CALLS_PAD = bool(settings.get("text_calls_pad", BUILTIN_TEXT_CALLS_PAD))
    DEFAULT_PAYLOAD_CAP = int(settings.get("payload_cap", BUILTIN_PAYLOAD_CAP))
    DEFAULT_IMAGEGEN_CUT = bool(settings.get("imagegen_cut", BUILTIN_IMAGEGEN_CUT))
    # An upstream derived from `cliproxy.port` is applied only at a start. The port is
    # startup-only (the shell restarts CLIProxyAPI on its new port), so a reload that
    # moved the running upstream would point it at a port nothing listens on yet. A
    # named upstream is applied by reload, as it always was.
    if not (reload and settings.get("upstream_source") == "port"):
        UPSTREAM = settings["upstream"]
    ROUTING = settings.get("routing", schema.get("routing").default)
    LOW_QUOTA = dict(settings.get("low_quota") or _DEFAULT_SETTINGS["low_quota"])
    CLIPROXY = dict(settings.get("cliproxy") or _DEFAULT_SETTINGS["cliproxy"])
    # The port the shell started CLIProxyAPI on: it reads the same file just before it
    # starts the core, and a CLIProxyAPI restart restarts the core.
    if not reload:
        STARTED_CLIPROXY_PORT = CLIPROXY["port"]
    API_KEY = settings["api_key"]
    LISTEN = settings["listen"]
    RELAY_TOKEN = settings["token"]
    # One attribute holding the whole immutable list, so a SIGHUP reload swaps
    # identities in a single assignment: no request can ever authorise against a
    # half-updated set. The other settings are individually atomic and their
    # cross-field tearing does not decide who may spend quota.
    TOKENS = tuple(settings.get("tokens") or ())
    RETRIES = settings.get("retries", schema.get("retries").default)
    TAIL_CHARS = int(settings.get("tail_chars", DEFAULT_TAIL_CHARS))
    DRAIN = int(settings.get("drain", DRAIN_SECONDS))
    REJECT_PREVIOUS_RESPONSE_ID = bool(
        settings.get("reject_previous_response_id", schema.get("reject_previous_response_id").default))
    CAPTURE_DIR = settings.get("capture_dir") or ""
    # A startup setting like `runs_log`, and for a related reason: a trace that
    # moved mid-stream would split one run's frames across two files. It is read
    # once in `main()`, never on SIGHUP.
    DEBUG_LOG = settings.get("debug_log") or ""
    PACING = settings.get("pacing", schema.get("pacing_seconds").default)
    MAX_CONCURRENT = settings.get("max_concurrent", schema.get("max_concurrent_upstream").default)
    ALLOW_PUBLIC_BIND = bool(settings.get("allow_public_bind", schema.get("allow_public_bind").default))
    ALLOW_CLIENT_TUNING = bool(settings.get("allow_client_tuning", schema.get("allow_client_tuning").default))
    # `runs_log` is deliberately *not* applied here. `apply_settings` runs again on
    # every SIGHUP, and moving a serving process's run log mid-stream would split
    # one chain across two files — every record before the reload in one, after it
    # in another — while each file still looks continuous from the inside. It is a
    # startup setting: `handler.main()` calls `core.set_runs_log` once, and only
    # when the config or the environment actually named a path. A reload that
    # changes it says so instead (`reload_settings`).

    for hook in list(ON_APPLY):
        try:
            hook()
        except Exception as e:  # noqa: BLE001 - a hook must not take the apply down
            print(f"settings hook {hook!r} failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return settings

def ensure_settings(force=False, config_path=None, env=None):
    """Resolve config and push it into the module globals. main() calls this so
    a configuration problem is reported as a message, not an import traceback.
    Re-resolves when CONFIG_PATH changes, so an explicit --config takes effect
    and tests can point at their own file. The globals are always re-applied, so
    a handler can never read a half-updated configuration.

    `env` is passed through to load_settings; leave it None in production so the
    process environment keeps overriding the file, and pass `{}` in a test that
    must not depend on the shell it runs in.

    A configuration error that was found when this module was imported is raised
    here rather than at import: `main()` catches `ConfigError` and prints
    "refusing to start: …" with exit 2, and an import statement cannot be caught by
    the code that is being imported. Without this, `RELAY_RUNS_LOG=<a path in the
    checkout>` printed a traceback instead of a refusal — found while verifying the
    deployed build, not by a test, which is why there is one now."""
    global _settings, _settings_resolved_for, _config_error
    key = CONFIG_PATH if config_path is None else config_path
    if force or _settings_resolved_for != key:
        _settings = load_settings(config_path=config_path, env=env)
        _settings_resolved_for = key
        _config_error = None
    if _config_error:
        # Raised, not applied: the lenient settings exist so that importing this
        # module cannot fail, and a process about to *act* on them must not proceed
        # with a configuration that was refused.
        raise ConfigError(_config_error)
    return apply_settings(_settings)


def _default_log(message):
    sys.stderr.write("%s config: %s\n" % (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message))
    sys.stderr.flush()


def reload_settings(config_path=None, env=None, log=None):
    """Re-resolve the configuration in place, for SIGHUP.

    W0 wants a token added without dropping the process. The failure direction
    matters more than the success one: a reload that cannot be parsed must not
    take the endpoint down, and must not silently leave the operator believing
    their edit took effect. So the running configuration stays in force and the
    refusal is said out loud, including which token names are now live.
    """
    global _settings, _settings_resolved_for
    say = log or _default_log
    key = CONFIG_PATH if config_path is None else config_path
    try:
        settings = load_settings(config_path=config_path, env=env)
    except ConfigError as e:
        say(f"reload refused, keeping the running configuration: {e}")
        return None
    if not settings.get("tokens"):
        # Deliberately stricter than startup: `load_settings` resolving an empty
        # deployment is legitimate (the process then refuses to start), but a
        # *reload* that empties the token list would lock every live client out
        # of a serving process. That is never what an operator meant.
        say("reload refused, keeping the running configuration: the new "
            "configuration has no client tokens at all, which would revoke "
            "every client at once")
        return None
    _settings = settings
    _settings_resolved_for = key
    applied = apply_settings(settings, reload=True)
    if (settings.get("runs_log_source", "default") != "default"
            and settings.get("runs_log") != core.RUNS_LOG):
        # Said out loud rather than obeyed, and only when the config or the
        # environment actually *named* a path — a resolved default that differs
        # from this process's log is not an edit anyone made. Silently continuing
        # to write the old file would make the edit look applied while the evidence
        # went somewhere else.
        say(f"note: runs_log {settings['runs_log']!r} takes effect at the next "
            f"start; this process is still writing {core.RUNS_LOG}. A reload must "
            "not move a serving process's evidence, or the chain continues in a "
            "second file with the records before the reload in the first.")
    if settings["upstream"] != UPSTREAM:
        # The hold in `apply_settings`: the new configuration derives its upstream from
        # `cliproxy.port`, which is applied at start. Said out loud for the same reason
        # as the run log above: the edit must not look applied while requests still go
        # to the running upstream.
        say(f"note: upstream {settings['upstream']!r} (from cliproxy.port) takes effect "
            f"at the next start; this process is still sending to {UPSTREAM}. The port "
            "is applied at start, when CLIProxyAPI is restarted on it.")
    names = [entry[0] for entry in applied.get("tokens") or ()]
    operators = [entry[0] for entry in applied.get("tokens") or ()
                 if len(entry) > 2 and entry[2] == "operator"]
    say(f"reloaded: {len(names)} client token(s): {', '.join(names) or 'none'}"
        f" (operators: {', '.join(operators) or 'none'})")
    return applied

# --- resolved settings (module globals the engine and handler read) ----------
# One definition of the defaults, used both for the pre-resolution globals and as
# the fallback when resolution refuses. It was briefly two copies — the drift hazard
# this file's own token-role comment is about — and the second copy existed only to
# stop an import from raising.
# The defaults are the schema's; the keys below are the ones it does not state (the
# auth entries resolve to `token`/`tokens`, and `listen` to a tuple).
_DEFAULT_SETTINGS = dict(
    schema.default_settings(),
    token="", tokens=(), api_key="", listen=_DEFAULT_LISTEN, config_path=None,
    runs_log_source="default", upstream_source="port", unknown_keys=[],
)
_settings = dict(_DEFAULT_SETTINGS)
UPSTREAM = _settings["upstream"]
API_KEY = ""
LISTEN = _settings["listen"]
RELAY_TOKEN = ""
TOKENS = ()
RETRIES = _settings["retries"]
TAIL_CHARS = DEFAULT_TAIL_CHARS
DRAIN = DRAIN_SECONDS
REJECT_PREVIOUS_RESPONSE_ID = _settings["reject_previous_response_id"]
CAPTURE_DIR = ""
DEBUG_LOG = ""
PACING = _settings["pacing"]
MAX_CONCURRENT = _settings["max_concurrent"]
ALLOW_PUBLIC_BIND = _settings["allow_public_bind"]
ALLOW_CLIENT_TUNING = _settings["allow_client_tuning"]
ROUTING = _settings["routing"]
LOW_QUOTA = dict(_settings["low_quota"])
CLIPROXY = dict(_settings["cliproxy"])
STARTED_CLIPROXY_PORT = CLIPROXY["port"]

# Resolve once at import so handlers and tests have values before main() runs. A
# refusal here is *remembered* rather than raised: importing a module cannot be caught
# by the module, so raising would print a traceback where the operator needs a
# sentence. `ensure_settings` raises it at the point a process is about to act.
_config_error = None
try:
    _settings = load_settings()
except ConfigError as e:
    _config_error = str(e)
    _settings = dict(_DEFAULT_SETTINGS)
apply_settings(_settings)
_settings_resolved_for = CONFIG_PATH
