"""Every setting the core reads: its key, type, default, range and one line of
meaning. The parser (`config.resolve_settings`) takes defaults and bounds from
here, `config.example.yaml` is generated from here, and the UI builds its forms
from `GET /api/schema`. Nothing else states a default."""
import copy
from dataclasses import dataclass, asdict

from . import core


@dataclass(frozen=True)
class Field:
    key: str                 # dotted for a nested key
    type: str                # int | float | bool | str | list | choice
    default: object
    meaning: str
    setting: str = ""        # the name in the resolved settings dict when it differs
    env: str = ""            # the environment variable that overrides it
    minimum: object = None
    maximum: object = None
    choices: tuple = ()
    startup_only: bool = False
    writable: bool = True
    secret: bool = False
    label: str = ""          # what the UI calls it
    group: str = ""          # one of GROUPS: where the UI puts it
    choice_labels: tuple = ()  # (choice, label) pairs in `choices` order


GROUPS = ("saving", "connection", "address", "cliproxy", "tuning.cutting", "tuning.tool_calls",
          "tuning.traffic", "tuning.quota_reading", "file", "hidden")


FIELDS = (
    Field("listen", "list", list(core._DEFAULT_LISTEN), "addresses the core listens on; every one is required (a bind failure refuses start); a wildcard needs allow_public_bind; the app writes one loopback address", env="RELAY_LISTEN", startup_only=True, label="Address", group="address"),
    Field("auth.tokens", "list", [], "client and operator tokens: [{name, token, operator}]", env="RELAY_TOKENS", writable=False, secret=True, label="Tokens", group="hidden"),
    Field("auth.token", "str", "", "the legacy single operator token", env="RELAY_TOKEN", writable=False, secret=True, label="Operator token", group="hidden"),
    Field("cli_proxy_api_key", "str", "", "the key CLIProxyAPI accepts from the core", setting="api_key", env="CLI_PROXY_API_KEY", writable=False, secret=True, label="CLIProxyAPI key", group="file"),
    Field("upstream", "str", "", "a server deployment's upstream URL, any path or credentials; the app writes cliproxy.url instead", env="CLIPROXY_URL", writable=False, label="Upstream URL (server deployments)", group="file"),
    Field("cliproxy.port", "int", 8317, "the local CLIProxyAPI the app runs; the upstream is http://127.0.0.1:<port> unless cliproxy.url is set", minimum=1, maximum=65535, startup_only=True, label="Port", group="cliproxy"),
    Field("cliproxy.url", "str", "", "a remote CLIProxyAPI instead of the local one; empty = derived from cliproxy.port", label="Use a remote CLIProxyAPI", group="cliproxy"),
    Field("cliproxy.panel", "bool", False, "serve CLIProxyAPI's management panel at /management.html on its port (it downloads the page from GitHub); a CLIProxyAPI restart applies it", startup_only=True, label="Web panel", group="cliproxy"),
    Field("cliproxy.version", "str", "", "the CLIProxyAPI version init pinned; empty on a server deployment (the management-route check is then skipped)", writable=False, label="Version", group="cliproxy"),
    Field("cliproxy.management_secret_file", "str", "", "path of the file holding CLIProxyAPI's management secret; never the secret", writable=False, label="Management secret file", group="file"),
    Field("routing", "choice", "relay", "relay: cut every request; auto: cut when the watched accounts are low; direct: pass every request through", choices=("relay", "auto", "direct"), label="Mode", group="saving", choice_labels=(("relay", "Relay"), ("auto", "Auto"), ("direct", "Direct"))),
    Field("low_quota.threshold_percent", "int", 30, "Auto cuts when an account has less than this share of its 5-hour window left", minimum=1, maximum=99, label="Cut below, % left", group="saving"),
    Field("low_quota.rule", "choice", "all", "with several watched accounts: all = cut when every one is low; any = when one is", choices=("all", "any"), label="Cut when", group="saving", choice_labels=(("all", "every watched account is low"), ("any", "any watched account is low"))),
    Field("low_quota.accounts", "list", [], "the accounts Auto watches, as CLIProxyAPI numbers them; empty = every enabled Codex account", label="Watched accounts", group="saving"),
    Field("low_quota.models", "list", [], "the models Auto cuts when the quota is low; empty = every model; any other model always passes through", label="Models cut when low", group="saving"),
    Field("low_quota.poll_seconds", "int", 60, "how often the accounts' quota is read, in every mode", minimum=30, maximum=600, label="Read every", group="tuning.quota_reading"),
    Field("seg_budget", "int", 1500, "characters of answer text one upstream request may produce", env="RELAY_SEG_BUDGET", minimum=1, label="Answer budget", group="tuning.cutting"),
    Field("total_cap", "int", 8000, "characters after which an answer is cut for good", env="RELAY_TOTAL_CAP", minimum=1, label="Hard cap", group="tuning.cutting"),
    Field("max_segments", "int", 6, "upstream requests one answer may take", env="RELAY_MAX_SEGMENTS", minimum=1, label="Requests per answer", group="tuning.cutting"),
    Field("tail_chars", "int", 1500, "characters of the answer so far a continuation is given", env="RELAY_TAIL_CHARS", minimum=1, label="Continuation context", group="tuning.cutting"),
    Field("merge_text", "bool", True, "a continued text answer is one message", env="RELAY_MERGE_TEXT", label="One message per answer", group="tuning.cutting"),
    Field("session_pin", "bool", False, "send a session id of Lokki's own, one per client", env="RELAY_SESSION_PIN", label="Pin a session id", group="tuning.traffic"),
    Field("text_calls", "bool", True, "the model writes its exec call as text, and Lokki makes the call and cuts there", env="RELAY_TEXT_CALLS", label="Text tool calls", group="tuning.tool_calls"),
    Field("text_calls_pad", "bool", True, "with text_calls: still carry the padding tool", env="RELAY_TEXT_CALLS_PAD", label="Keep the padding tool", group="tuning.tool_calls"),
    Field("imagegen_cut", "bool", True, "end the run when the provider names an image item", env="RELAY_IMAGEGEN_CUT", label="End on an image item", group="tuning.cutting"),
    Field("payload_cap", "int", 65536, "characters of a provider payload field delivered; 0 = no cap", env="RELAY_PAYLOAD_CAP", minimum=0, label="Payload cap", group="tuning.cutting"),
    Field("pacing_seconds", "float", 1.0, "minimum seconds between upstream calls; 0 disables", setting="pacing", env="RELAY_PACING", minimum=0, label="Pacing", group="tuning.traffic"),
    Field("max_concurrent_upstream", "int", 2, "upstream streams open at once", setting="max_concurrent", env="RELAY_MAX_CONCURRENT", minimum=1, label="Streams at once", group="tuning.traffic"),
    Field("retries", "int", 6, "attempts for a request that has produced no output; a value under 1 is read as 1", env="RELAY_RETRIES", label="Attempts", group="tuning.traffic"),
    Field("drain_seconds", "int", 15, "seconds a stop waits for in-flight streams", setting="drain", env="RELAY_DRAIN_SECONDS", minimum=0, label="Drain time", group="tuning.traffic"),
    Field("reject_previous_response_id", "bool", True, "refuse a request that chains onto a stored response", env="RELAY_REJECT_PREVIOUS_RESPONSE_ID", writable=False, label="Refuse chained responses", group="file"),
    Field("capture_dir", "str", "", "where a deliberate capture of a client body goes; outside the repository; the file wins over the environment here", env="RELAY_CAPTURE_DIR", writable=False, label="Capture directory", group="file"),
    Field("debug_log", "str", "", "the diagnostic trace file; outside the repository", env="RELAY_DEBUG_LOG", startup_only=True, writable=False, label="Debug log", group="file"),
    Field("runs_log", "str", "", "the evidence file; outside the repository; empty = <state dir>/cliproxy/runs.jsonl", env="RELAY_RUNS_LOG", startup_only=True, writable=False, label="Run log", group="file"),
    Field("allow_public_bind", "bool", False, "allow a wildcard listen address (the owner's decision)", env="RELAY_ALLOW_PUBLIC_BIND", startup_only=True, writable=False, label="Allow a public address", group="file"),
    Field("allow_client_tuning", "bool", False, "let a client set the relay's own knobs", env="RELAY_ALLOW_CLIENT_TUNING", writable=False, label="Let clients tune the relay", group="file"),
)

_BY_KEY = {f.key: f for f in FIELDS}


def local_upstream(port):
    """The upstream of the local CLIProxyAPI on `port`."""
    return f"http://127.0.0.1:{port}"


_DEFAULT_UPSTREAM = local_upstream(_BY_KEY["cliproxy.port"].default)


def get(key):
    return _BY_KEY[key]


def keys():
    return tuple(_BY_KEY)


def listing():
    rows = []
    for f in FIELDS:
        row = asdict(f)
        row["choices"] = list(f.choices)
        row["choice_labels"] = dict(f.choice_labels)
        if f.secret:
            row["default"] = "***" if f.default else f.default
        rows.append(row)
    return rows


def default_settings():
    """The resolved-settings names and their defaults, the shape `apply_settings`
    reads: flat for the top-level keys, a dict for `cliproxy` and `low_quota`.
    Copies, so a caller that changes a list in it does not change the schema."""
    out = {}
    for f in FIELDS:
        top, _, rest = f.key.partition(".")
        if top in ("cliproxy", "low_quota"):
            out.setdefault(top, {})[rest] = copy.deepcopy(f.default)
        elif top == "auth":
            continue
        else:
            out[f.setting or f.key] = copy.deepcopy(f.default)
    out["upstream"] = _DEFAULT_UPSTREAM
    out["runs_log"] = core.DEFAULT_RUNS_LOG
    return out


def example_yaml():
    """config.example.yaml: every key, its meaning and its default, grouped by the first segment."""
    out = ["# lokki core settings. Generated from lokki_core/schema.py: edit that, then",
           "# `python3 -m lokki_core schema --example > core/config.example.yaml`.",
           "# Environment variables named here win over the file, except for capture_dir,",
           "# where the file wins. A SIGHUP or POST /api/reload re-reads every key but",
           "# the startup-only ones.", ""]
    groups = {}
    for f in FIELDS:
        groups.setdefault(f.key.split(".")[0], []).append(f)
    for top, fields in groups.items():
        if len(fields) == 1 and "." not in fields[0].key:
            out += _lines(fields[0], fields[0].key, indent="")
        else:
            out.append(f"{top}:")
            for f in fields:
                out += _lines(f, f.key.split(".", 1)[1], indent="  ")
        out.append("")
    return "\n".join(out)


def _lines(f, name, indent):
    notes = [f.meaning]
    if f.choices:
        notes.append("one of " + ", ".join(f.choices))
    if f.minimum is not None and f.maximum is not None:
        notes.append(f"range {f.minimum}..{f.maximum}")
    elif f.minimum is not None:
        notes.append(f"at least {f.minimum}")
    elif f.maximum is not None:
        notes.append(f"at most {f.maximum}")
    if f.env:
        notes.append(f"env {f.env}")
    if f.startup_only:
        notes.append("applied at start, not on reload")
    if not f.writable:
        notes.append("not settable through the API")
    value = ("[]" if f.type == "list" else '""') if f.secret else _yaml_scalar(f.default)
    return [f"{indent}# " + "; ".join(notes), f"{indent}{name}: {value}"]


def _yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
    if isinstance(value, str):
        return '"' + value.replace('"', '\\"') + '"'
    return str(value)
