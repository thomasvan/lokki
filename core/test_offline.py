#!/usr/bin/env python3
"""Offline tests for the relay's request surface, its configuration and its supervisor
scripts, and for the billing oracle's evidence.

None of these spends quota or needs a cliproxy: the HTTP rows run against one relay this
module starts and a stub upstream that records what it was sent. They cover config-driven
auth, the loopback/Tailscale binds, input validation (no silent default prompt), guarded
error statuses and framing, what the relay forwards and records, and the guards that keep
client bodies and the evidence file out of the checkout. The billing oracle writes to a
committed file, so account emails must be redacted out of it and a failed meter read must
never be reported as "no change".

One table per entry point: each row is a behaviour, and names what it binds as paths into
views of what the call produced, with the checks `test_engine` defines. A bare value is
compared for equality, a bool or None with `is`. A row's stderr is captured and is part of
what it asserts: a row that names none asserts there was none. The frames a client sees for
a cut and for a provider failure, and the printed tokens report, are held by goldens,
`test_offline.<shape>.golden`, which are read and never written by a test.

Run:  python3 core/test_offline.py
"""

import argparse
import contextlib
import copy
import functools
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import billing_probe  # noqa: E402
import server as shim  # noqa: E402  - imported before any setting resolves: its scalars are snapshots
from lokki_core import config, control, core, renamer, routing, trace  # noqa: E402
from lokki_core.handler import start_listeners  # noqa: E402
from lokki_core import handler  # noqa: E402
from lokki_core.sse import iter_sse_frames  # noqa: E402
from test_engine import (  # noqa: E402
    MISSING, Op, Ref, _Checks, Each, Ends, Eq, Falsy, Golden, Gt, Has, IsA, Lacks, Len, Lt, Ne, NotNone, Starts,
    Truthy, transcript)

HERE = os.path.dirname(os.path.abspath(__file__))


def assert_mode(test, path, mode):
    """The file's permission bits are `mode`. POSIX only: Windows reports 0o666/0o777 whatever the mode asked."""
    if os.name != "nt":
        test.assertEqual(os.stat(path).st_mode & 0o777, mode)


TOKEN = "offline-test-token"
CLIENT = "offline-test-client"


# Tests that drive `main()` or `ensure_settings` have to put the module globals
# back: those two replace every global, including the client token list. Done by
# hand at each site, that would leak a temp config into later tests (a
# class-order-dependent green), so it is one helper.
_GLOBAL_KEYS = ("CONFIG_PATH", "RELAY_TOKEN", "TOKENS",
                "API_KEY", "UPSTREAM", "LISTEN")


_TEMP_CONFIGS = []


def write_config(body):
    """Write a throwaway config.yaml, cleaned up when the module finishes."""
    f = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yaml", delete=False)
    f.write(body)
    f.close()
    _TEMP_CONFIGS.append(f.name)
    return f.name


def tearDownModule():
    for path in _TEMP_CONFIGS:
        try:
            os.unlink(path)
        except OSError:
            pass
    _Relay.stop()


def _snapshot_globals():
    return ({k: getattr(config, k) for k in _GLOBAL_KEYS},
            config._settings, config._settings_resolved_for)


def _restore_globals(snapshot):
    values, settings, resolved_for = snapshot
    config._settings = settings
    config._settings_resolved_for = resolved_for
    config.apply_settings(settings)
    for k, v in values.items():
        setattr(config, k, v)
    # A test that ran `main()` also took the run-log lock. Holding it for the rest
    # of the process makes a *later* test's relay refuse to start, which is the
    # guard doing its job on the wrong process.
    core.release_runs_lock()


def raw_request(port, data, read=True, timeout=5):
    """Send bytes to a listener and return what comes back, or the socket.

    Needed where urllib would hide the thing under test: a refused oversized body
    (nothing is read), and a connection held open to occupy a slot."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(data)
    if not read:
        return s
    try:
        return s.recv(65536)
    finally:
        s.close()


def json_get(port, path, token=TOKEN, timeout=5):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers={"X-Relay-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, None


def json_post(port, path, body, token=TOKEN, timeout=5):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"X-Relay-Token": token, "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, None


class _ServingHarness(unittest.TestCase):
    """One listener on an ephemeral port, with the token set for the duration."""

    def setUp(self):
        self._saved_tokens = config.TOKENS
        config.TOKENS = ((CLIENT, TOKEN, "operator"),)
        self.servers = start_listeners(("127.0.0.1:0",), strict=False)
        self.addCleanup(self._stop)
        self.port = self.servers[0][1].server_address[1]
        self._saved_bound = handler.BOUND_LISTEN
        handler.BOUND_LISTEN = (f"127.0.0.1:{self.port}",)
        threading.Thread(target=self.servers[0][1].serve_forever,
                         daemon=True).start()

    def _stop(self):
        config.TOKENS = self._saved_tokens
        handler.BOUND_LISTEN = self._saved_bound
        for _, srv in self.servers:
            srv.shutdown()
            srv.server_close()


# --- the checks a row names, and how a row runs ------------------------------------

def In(container): return Op("assertIn", container)        # it is one of these
def Matches(pattern): return Op("assertRegex", pattern)
def Mode(bits): return Op("mode", bits)                    # the file it names has these permission bits


PATCHABLE = {"config": config, "core": core, "handler": handler, "control": control,
             "billing_probe": billing_probe}


def fill(value, scratch):
    """`value` with `{scratch}` (the row's own directory) and `{repo}` (this checkout's
    core/) put in, wherever they stand in a string."""
    if isinstance(value, str):
        return value.replace("{scratch}", scratch).replace("{repo}", HERE)
    if isinstance(value, (list, tuple)):
        return type(value)(fill(v, scratch) for v in value)
    if isinstance(value, dict):
        return {fill(k, scratch): fill(v, scratch) for k, v in value.items()}
    return value


def _show_warning_on(stream):
    """A `warnings.showwarning` that writes to `stream`: a warning is not a row's stderr."""
    def show(message, category, filename, lineno, file=None, line=None):
        stream.write(warnings.formatwarning(message, category, filename, lineno, line))
    return show


class _Rows(_Checks):
    """Runs a row: its patches applied, its stderr captured, then its `expect` checked."""

    GOLDEN = "test_offline"

    def holds(self, got, want, views):
        want = self.resolve(want, views)
        if isinstance(want, Op) and want.kind == "mode":
            assert_mode(self, got, want.args[0])
        else:
            super().holds(got, want, views)

    def target(self, name):
        root, *path, attr = name.split(".")
        owner = PATCHABLE[root]
        for part in path:
            owner = getattr(owner, part)
        self.assertTrue(hasattr(owner, attr), f"{name} is not a global")
        return owner, attr

    def run_row(self, row, keys, build, base=None):
        """`build(row, scratch, stack)` makes the run and returns its views."""
        self.unread(row, keys)
        with contextlib.ExitStack() as stack:
            scratch = tempfile.mkdtemp(prefix="offline-row-")
            stack.callback(shutil.rmtree, scratch, True)
            for name, value in fill({**(base or {}), **row.get("patch", {})}, scratch).items():
                stack.enter_context(mock.patch.object(*self.target(name), value))
            err = io.StringIO()
            stack.enter_context(mock.patch.object(warnings, "showwarning", _show_warning_on(sys.stderr)))
            stack.enter_context(contextlib.redirect_stderr(err))
            views = {}
            views.update(self.common(views, scratch))
            views.update(build(row, scratch, stack))
            views["stderr"] = lambda _: err.getvalue().splitlines()
            expect = row["expect"]
            last = {k: v for k, v in expect.items() if k.split(".")[0] == "stderr"} or {"stderr": []}
            self.check(views, {**{k: v for k, v in expect.items() if k not in last}, **last})

    def common(self, views, scratch):
        return {
            "scratch": lambda _: scratch,
            "config": lambda _: config, "core": lambda _: core, "handler": lambda _: handler,
            "dumps": lambda path: json.dumps(self.at(views, path)),
            "keys": lambda path: sorted(self.at(views, path)),
            "names": lambda path: [entry[0] for entry in self.at(views, path)],
            "real": lambda path: os.path.realpath(self.at(views, path)),
            "inside_repo": lambda path: core.inside_repo(self.at(views, path)),
        }

    def outcome(self, call, row):
        """What `call` returned and what it raised. An exception is the row's to assert
        only where the row asks about it; anywhere else it fails the row."""
        try:
            return call(), None
        except Exception as e:                        # noqa: BLE001 - asserted below, or raised
            if not any(re.match(r"(raised|message)\b", path) for path in row["expect"]):
                raise
            return MISSING, e

    def raised_views(self, raised):
        return {"raised": lambda _: raised, "message": lambda _: MISSING if raised is None else str(raised)}


def _file(scratch, body, name="config.yaml"):
    path = os.path.join(scratch, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


# --- core.extract_prompt ------------------------------------------------------------

# It must not lose content or silently substitute a default.
PROMPTS = (
    dict(name="a string is the prompt", input="hello", expect={"out": "hello"}),
    dict(name="a message list keeps the system role",
         input=[{"role": "system", "content": [{"type": "input_text", "text": "SYS"}]},
                {"role": "user", "content": [{"type": "input_text", "text": "USER"}]}],
         expect={"out": "SYS\nUSER"}),
    dict(name="a message whose content is a string", input=[{"role": "user", "content": "plain"}],
         expect={"out": "plain"}),
    dict(name="a block that is not text is skipped, not fatal",
         input=[{"role": "user", "content": [{"type": "input_image", "image_url": "x"},
                                             {"type": "input_text", "text": "after"}]}],
         expect={"out": "after"}),
    # a dict is not a prompt: None, not a default prompt
    dict(name="a dict is rejected", input={"role": "user"}, expect={"out": None}),
    dict(name="a bare string item is a 400", input=["bare string"],
         expect={"raised": IsA(core.ResponseError), "raised.status": 400}),
    dict(name="text that is not a string is rejected", input=[{"content": [{"type": "input_text", "text": 7}]}],
         expect={"raised": IsA(core.ResponseError)}),
)


# --- core.read_runs -----------------------------------------------------------------

def _fat_runs(rows):
    # Deliberately fat records: the bound is on *bytes touched*, so a line count only
    # means something next to the record size.
    return [json.dumps({"i": i, "cut": "terminal", "pad": "x" * 400}) + "\n" for i in range(rows)]


# `lines` are the run log, or what makes it; `limit` is passed when the row names one.
READS = (
    dict(name="a half-written append and lines that are not json do not raise",
         lines=['{"mode": "control", "cut": "terminal"}\n', '{"truncated": \n', "\n", "not json at all\n"],
         expect={"out": Len(1), "out.0.mode": "control"}),
    dict(name="the limit is applied", lines=[json.dumps({"i": i}) + "\n" for i in range(10)], limit=3,
         expect={"out.*.i": [7, 8, 9]}),
    # The result is identical either way, so only the work done separates a tail read from
    # parsing the history behind it. `/api/runs` is a monitor endpoint on an append-only
    # file. One 64 KB chunk holds ~150 of these; the whole file holds 20000.
    dict(name="the tail read does not scan the whole file", lines=lambda: _fat_runs(20000), limit=3,
         expect={"out.*.i": [19997, 19998, 19999], "scanned": Lt(1000)}),
)


# --- config.parse_listen ------------------------------------------------------------

# (what the config says, the addresses bound)
LISTENS = (
    ("8371", ("127.0.0.1:8371",)),
    (["127.0.0.1:1", "100.0.0.1:1"], ("127.0.0.1:1", "100.0.0.1:1")),
    (None, core._DEFAULT_LISTEN),
)


class InputTests(_Rows, unittest.TestCase):
    def test_the_prompt_extracted(self):
        for row in PROMPTS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "input", "expect"), self.prompt)

    def prompt(self, row, scratch, stack):
        out, raised = self.outcome(lambda: core.extract_prompt(copy.deepcopy(row["input"])), row)
        return {"out": lambda _: out, **self.raised_views(raised)}

    def test_the_runs_read_back(self):
        for row in READS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "lines", "limit", "expect"), self.read)

    def read(self, row, scratch, stack):
        lines = row["lines"]() if callable(row["lines"]) else row["lines"]
        stack.enter_context(mock.patch.object(core, "RUNS_LOG", _file(scratch, "".join(lines), "runs.jsonl")))
        out = core.read_runs(**({"limit": row["limit"]} if "limit" in row else {}))
        scanned = core._last_read_lines
        return {"out": lambda _: out, "scanned": lambda _: scanned}

    def test_the_listen_addresses(self):
        for raw, want in LISTENS:
            with self.subTest(raw=raw):
                self.assertEqual(config.parse_listen(raw), want)


# --- config.load_settings -----------------------------------------------------------

T = "auth:\n  token: t\n"
INTO_REPO = "must never be committable"
INSIDE = "inside the repository"


def _refused(*needles, kind=config.ConfigError):
    return {"raised": IsA(kind), **({"message": Has(*needles)} if needles else {})}


# The relay is configured from a file, mirroring cliproxy: no secret on the command line,
# no secret in the repository. A row is one `load_settings` call: `file` is the config it is
# given (none: the default path), `path` an explicit path instead, `env` its environment,
# `link` makes `{scratch}/into-repo`, a symlink to this checkout. `s` is what it resolved;
# `bare` is the same file resolved with no environment.
LOADS = (
    dict(name="the file names the token, the listen addresses, the key and the upstream",
         file="listen:\n  - '127.0.0.1:9001'\n  - '100.64.0.1:9001'\nauth:\n  token: 'from-file'\n"
              "upstream: 'http://127.0.0.1:9999'\ncli_proxy_api_key: 'key-from-file'\n",
         expect={"s.token": "from-file", "s.api_key": "key-from-file", "s.upstream": "http://127.0.0.1:9999",
                 "s.listen": ("127.0.0.1:9001", "100.64.0.1:9001")}),
    dict(name="the environment overrides the file", file="auth:\n  token: 'from-file'\nlisten: '127.0.0.1:9002'\n",
         env={"RELAY_TOKEN": "from-env", "RELAY_LISTEN": "127.0.0.1:9003"},
         expect={"s.token": "from-env", "s.listen": ("127.0.0.1:9003",)}),
    # the default path is optional; only an explicitly named one must exist
    dict(name="a missing default path falls back to the environment defaults",
         patch={"config.CONFIG_PATH": "/nonexistent/relay-config.yaml"},
         expect={"s.token": "", "s.upstream": "http://127.0.0.1:8317", "s.config_path": None}),
    dict(name="an explicit path that is missing is an error", path="/nonexistent/relay-config.yaml",
         expect=_refused()),
    dict(name="a file that is not a mapping is an error", file="- just\n- a list\n", expect=_refused()),
    dict(name="a bad listen entry is an error", file=T + "listen: 'not-a-port'\n", expect=_refused()),
    # The upstream Authorization value comes from this secret; a token carrying CR/LF could
    # smuggle a second header.
    dict(name="a secret with a CR LF is refused", file='auth:\n  token: "evil\\r\\nX-Injected: 1"\n',
         expect=_refused()),
    dict(name="pacing is read from the environment", file=T, env={"RELAY_PACING": "2"}, expect={"s.pacing": 2.0}),
    dict(name="retries are read from the environment", file=T, env={"RELAY_RETRIES": "5"}, expect={"s.retries": 5}),
    dict(name="one retry is the documented no retry value", file=T, env={"RELAY_RETRIES": "1"},
         expect={"s.retries": 1}),
    dict(name="retries that are not a number are refused", file=T + "retries: soon\n", expect=_refused()),
    # An `or` chain cannot tell 0 from unset, and `pacing_seconds: 0` is the only way to
    # disable pacing, in the file and in the environment override alike.
    dict(name="a zero pacing in the file is honoured", file=T + "pacing_seconds: 0\n", expect={"s.pacing": 0.0}),
    dict(name="a zero pacing in the environment is honoured", file=T, env={"RELAY_PACING": "0"},
         expect={"s.pacing": 0.0}),
    dict(name="unset pacing is not zero", file=T, expect={"s.pacing": Ne(0.0)}),
    dict(name="a blank pacing means unset", file=T, env={"RELAY_PACING": ""}, expect={"s.pacing": Ref("bare.pacing")}),
    dict(name="concurrency comes from the file", file=T + "max_concurrent_upstream: 6\n",
         expect={"s.max_concurrent": 6}),
    dict(name="concurrency in the environment overrides the file", file=T + "max_concurrent_upstream: 6\n",
         env={"RELAY_MAX_CONCURRENT": "3"}, expect={"s.max_concurrent": 3}),
    # 0 slots can never be right, whatever the file says
    dict(name="zero upstream slots are refused", file=T + "max_concurrent_upstream: 0\n", expect=_refused()),
    # `allow_public_bind` is the only thing standing between a typo and an endpoint on every
    # interface, so an unparseable value reads as off.
    dict(name="the public bind flag is read", file=T + "allow_public_bind: true\n",
         expect={"s.allow_public_bind": True}),
    dict(name="a public bind flag that does not parse fails closed", file=T + "allow_public_bind: 'definitely'\n",
         expect={"s.allow_public_bind": False}),
    # The relay is the client-facing endpoint, so an absent setting means the operator's
    # knobs: a caller does not choose the cut policy unless the operator allows it.
    dict(name="client tuning defaults to off", file=T, expect={"s.allow_client_tuning": False}),
    dict(name="client tuning can be turned on", file=T + "allow_client_tuning: true\n",
         expect={"s.allow_client_tuning": True}),
    # the env override wins, and "0" is a real value rather than absent
    dict(name="an explicit false is not unset and the environment wins with a zero",
         file=T + "allow_client_tuning: false\n", env={"RELAY_ALLOW_CLIENT_TUNING": "0"},
         expect={"s.allow_client_tuning": False}),
    dict(name="the environment turns client tuning on over an explicit false",
         file=T + "allow_client_tuning: false\n", env={"RELAY_ALLOW_CLIENT_TUNING": "1"},
         expect={"s.allow_client_tuning": True}),
    # One shared token would mean no attribution and no revocation that did not mean
    # restarting every client: each configured token names itself.
    dict(name="a legacy single token still works and is named default", file="auth:\n  token: legacy-token\n",
         expect={"s.tokens": (("default", "legacy-token", "operator"),)}),
    # it is the operator's own secret; demoting it would lock the operator out of their path
    dict(name="the legacy single token stays an operator", file="auth:\n  token: legacy\n",
         expect={"s.tokens": (("default", "legacy", "operator"),)}),
    # two names on one token is one identity with two labels
    dict(name="two clients may not share one token",
         file="auth:\n  tokens:\n    - {name: a, token: same}\n    - {name: b, token: same}\n",
         expect=_refused("share one token")),
    dict(name="a duplicate token name is refused",
         file="auth:\n  tokens:\n    - {name: a, token: t1}\n    - {name: a, token: t2}\n",
         expect=_refused("both named")),
    dict(name="a token with an empty name is refused", file="auth:\n  tokens:\n    - {name: '', token: t1}\n",
         expect=_refused("no 'name'")),
    dict(name="a token with an empty secret is refused", file="auth:\n  tokens:\n    - {name: a, token: ''}\n",
         expect=_refused("no 'token'")),
    dict(name="a token name with whitespace is refused", file="auth:\n  tokens:\n    - {name: 'a b', token: t1}\n",
         expect=_refused("whitespace")),
    # The role is what the handler and /version compare against, so a bool here would make
    # every real operator a client without failing anything.
    dict(name="the operator flag is parsed and defaults to client",
         file="auth:\n  tokens:\n    - {name: op, token: t1, operator: true}\n    - {name: cli, token: t2}\n",
         expect={"s.tokens": (("op", "t1", "operator"), ("cli", "t2", "client")),
                 "s.tokens.*.2": Each(IsA(str))}),
    dict(name="the environment can carry the token list", file="auth:\n  token: ignored\n",
         env={"RELAY_TOKENS": json.dumps([{"name": "env", "token": "env-token"}])},
         expect={"s.tokens": (("env", "env-token", "client"),)}),
    dict(name="a token list in the environment that is not json is refused", file=T,
         env={"RELAY_TOKENS": "{not json"}, expect=_refused()),
    # On is the default and the safe state; the setting exists so an operator can back out
    # during an incident with a SIGHUP, not a redeploy.
    dict(name="the previous_response_id refusal can be turned off in the file",
         file=T + "reject_previous_response_id: false\n", expect={"s.reject_previous_response_id": Falsy()}),
    dict(name="the previous_response_id refusal is on by default", file=T,
         expect={"s.reject_previous_response_id": Truthy()}),
    dict(name="the previous_response_id refusal can be turned off from the environment",
         file=T + "reject_previous_response_id: false\n", env={"RELAY_REJECT_PREVIOUS_RESPONSE_ID": "0"},
         expect={"s.reject_previous_response_id": Falsy()}),
    # Structural, not a note in a doc: the one place a captured body must never go is
    # somewhere `git add` can reach. `abspath` follows no symlinks, so a link outside the
    # repository pointing inside it is refused too.
    dict(name="a capture directory inside the repository is refused", file=T + "capture_dir: {repo}/captures\n",
         expect=_refused(INTO_REPO)),
    dict(name="a capture directory through a symlink into the repository is refused", link=True,
         file=T + "capture_dir: {scratch}/into-repo\n", expect=_refused(INTO_REPO)),
    dict(name="a capture directory outside the repository is accepted", file=T + "capture_dir: {scratch}\n",
         expect={"real(s.capture_dir)": Ref("scratch", os.path.realpath)}),
    # the config file and the environment each name the trace file, and the guard holds for either
    dict(name="a trace inside the repository is refused from the file", file=T + "debug_log: {repo}/debug.jsonl\n",
         expect=_refused(INTO_REPO)),
    dict(name="a trace inside the repository is refused from the environment",
         env={"RELAY_DEBUG_LOG": "{repo}/debug.jsonl"}, expect=_refused(INTO_REPO)),
    dict(name="a trace outside the repository is accepted", file=T + "debug_log: {scratch}/debug.jsonl\n",
         expect={"real(s.debug_log)": Ref("scratch", lambda d: os.path.realpath(os.path.join(d, "debug.jsonl")))}),
    # The evidence file lives outside the checkout, and neither a config file nor the
    # environment can point it back in: the relay appends here while it serves, so an
    # in-repo path leaves the tree dirty with live evidence that a `checkout`, `stash` or
    # `clean` drops and a `git add` commits.
    dict(name="the run log defaults to outside the repository", file=T,
         expect={"inside_repo(core.DEFAULT_RUNS_LOG)": Falsy(), "s.runs_log": Ref("core.DEFAULT_RUNS_LOG"),
                 "s.runs_log_source": "default"}),
    dict(name="a run log inside the repository is refused", file=T + "runs_log: {repo}/runs.jsonl\n",
         expect=_refused(INSIDE)),
    # a guard that only covers the config file is one the next deployment walks around
    dict(name="the environment cannot smuggle the run log into the repository", file=T,
         env={"RELAY_RUNS_LOG": "{repo}/runs.jsonl"}, expect=_refused(INSIDE)),
    # both callers use one check, so a hole cannot reappear in only one of them
    dict(name="a run log through a symlink into the repository is refused", link=True,
         file=T + "runs_log: {scratch}/into-repo/runs.jsonl\n", expect=_refused(INSIDE)),
    dict(name="a run log outside the repository is accepted and named", file=T + "runs_log: {scratch}/runs.jsonl\n",
         expect={"real(s.runs_log)": Ref("scratch", lambda d: os.path.realpath(os.path.join(d, "runs.jsonl"))),
                 "s.runs_log_source": "config runs_log"}),
    dict(name="the environment wins over the config file for the run log",
         file=T + "runs_log: {scratch}/from-config.jsonl\n", env={"RELAY_RUNS_LOG": "{scratch}/from-env.jsonl"},
         expect={"real(s.runs_log)": Ref("scratch", lambda d: os.path.realpath(os.path.join(d, "from-env.jsonl"))),
                 "s.runs_log_source": "env RELAY_RUNS_LOG"}),
    # The default is outside by construction only if the environment cannot put it inside:
    # startup refuses rather than start a relay whose every run would fail at the write.
    dict(name="the default run log is validated when the environment moves it inside", file=T,
         env={"XDG_STATE_HOME": "{repo}"}, expect=_refused(INSIDE)),
    # the continuation tail is a setting, so the fidelity tradeoff is tuned without editing code
    dict(name="the tail is configurable", file=T + "tail_chars: 6000\n", expect={"s.tail_chars": 6000}),
    dict(name="the tail can come from the environment", file=T, env={"RELAY_TAIL_CHARS": "42"},
         expect={"s.tail_chars": 42}),
    *(dict(name=f"a tail of {bad!r} is refused", file=T, env={"RELAY_TAIL_CHARS": bad}, expect=_refused())
      for bad in ("zero", "0", "-5")),
    # The budgets are settings, so a deployment can run a longer segment without editing code:
    # a 300-word answer is about 2,000 characters against a built-in budget of 1,500.
    dict(name="naming no budget leaves the built-in budgets", file=T,
         expect={"s.seg_budget": Ref("config.BUILTIN_SEG_BUDGET"), "s.total_cap": Ref("config.BUILTIN_TOTAL_CAP"),
                 "s.max_segments": Ref("config.BUILTIN_MAX_SEGMENTS")}),
    dict(name="the file names the budgets", file=T + "seg_budget: 4000\ntotal_cap: 9000\nmax_segments: 3\n",
         expect={"s.seg_budget": 4000, "s.total_cap": 9000, "s.max_segments": 3}),
    dict(name="the environment overrides the budgets the file names",
         file=T + "seg_budget: 4000\ntotal_cap: 9000\nmax_segments: 3\n",
         env={"RELAY_SEG_BUDGET": "2500", "RELAY_MAX_SEGMENTS": "2"},
         expect={"s.seg_budget": 2500, "s.total_cap": 9000, "s.max_segments": 2}),
    *(dict(name=f"{var}={bad!r} would unbound the run and is refused", file=T, env={var: bad}, expect=_refused())
      for var in ("RELAY_SEG_BUDGET", "RELAY_TOTAL_CAP", "RELAY_MAX_SEGMENTS") for bad in ("many", "0", "-5")),
    # one segment cannot be bigger than the whole run's cap
    dict(name="a segment budget above the cap is refused", file=T + "seg_budget: 9000\n", expect=_refused()),
    dict(name="a segment budget equal to the cap is accepted", file=T + "seg_budget: 9000\ntotal_cap: 9000\n",
         expect={"raised": None}),
    dict(name="merge_text is on unless named off", file=T, expect={"s.merge_text": Truthy()}),
    dict(name="merge_text can be named off", file=T + "merge_text: false\n", expect={"s.merge_text": Falsy()}),
    # the environment wins over the file, in both directions
    dict(name="the environment turns merge_text on over the file", file=T + "merge_text: false\n",
         env={"RELAY_MERGE_TEXT": "1"}, expect={"s.merge_text": Truthy()}),
    dict(name="the environment turns merge_text off", file=T, env={"RELAY_MERGE_TEXT": "0"},
         expect={"s.merge_text": Falsy()}),
    # a typo is not an "on" it did not ask for: it falls back to the unmerged shape
    dict(name="a merge_text typo is off", file=T, env={"RELAY_MERGE_TEXT": "tru"}, expect={"s.merge_text": Falsy()}),
    # `session_pin` is off, a setting like `merge_text`, and not a request knob
    dict(name="session_pin is off by default", file=T, expect={"s.session_pin": Falsy()}),
    dict(name="session_pin can be turned on", file=T + "session_pin: true\n", expect={"s.session_pin": Truthy()}),
    dict(name="the environment turns session_pin off", file=T + "session_pin: true\n",
         env={"RELAY_SESSION_PIN": "0"}, expect={"s.session_pin": Falsy()}),
    dict(name="the environment turns session_pin on", file=T, env={"RELAY_SESSION_PIN": "1"},
         expect={"s.session_pin": Truthy()}),
    dict(name="a session_pin typo is not a yes", file=T, env={"RELAY_SESSION_PIN": "ture"},
         expect={"s.session_pin": Falsy()}),
)


# --- config.ensure_settings, config.apply_settings, config.reload_settings ----------

class _Asking:
    """What `Handler._authorized` reads of a request carrying `token`."""
    client_name = None
    client_operator = False

    def __init__(self, token):
        self.headers = {"X-Relay-Token": token} if token else {}

    def _error(self, *a, **k):
        pass


def _authorized(token):
    asking = _Asking(token)
    return handler.Handler._authorized(asking), asking.client_operator


BUDGET = T + "seg_budget: 4000\n"
MERGE_OFF = T + "merge_text: false\n"
PIN_ON = T + "session_pin: true\n"

# Resolving settings must reach the module globals the handler reads: a cached but
# unapplied config would make the relay refuse to start with a valid token. `file` is the
# config resolved with no environment, `twice` resolves it again. `config` is the module
# after it, `fingerprint` the evidence fingerprint, `knobs(<payload>|on)` what
# `resolve_knobs` gives a request with tuning on (`|off`: off), `authorized(<token>)` what
# the handler's gate makes of a request carrying it: (accepted, operator).
ENSURES = (
    # The entry point re-exports settings values taken at import, so
    # `import server; server.RELAY_TOKEN = x` is silently ignored. Pinning that makes the
    # footgun visible: change configuration through `config`, or read `server.config.*`.
    dict(name="the shims scalars are snapshots, not live readings",
         file="auth:\n  token: 'changed-for-this-test'\nlisten: '127.0.0.1:9103'\n",
         expect={"config.RELAY_TOKEN": "changed-for-this-test", "shim.RELAY_TOKEN": Ne(Ref("config.RELAY_TOKEN"))}),
    dict(name="the applied globals match the config",
         file="listen: '127.0.0.1:9101'\nauth:\n  token: 'globals-token'\nupstream: 'http://127.0.0.1:9102'\n"
              "cli_proxy_api_key: 'globals-key'\n",
         expect={"config.RELAY_TOKEN": "globals-token", "config.API_KEY": "globals-key",
                 "config.UPSTREAM": "http://127.0.0.1:9102", "config.LISTEN": ("127.0.0.1:9101",)}),
    dict(name="a second resolution does not blank the globals", twice=True,
         file="listen: '127.0.0.1:9101'\nauth:\n  token: 'globals-token'\nupstream: 'http://127.0.0.1:9102'\n"
              "cli_proxy_api_key: 'globals-key'\n",
         expect={"config.RELAY_TOKEN": "globals-token"}),
    # A stubbed `config.TOKENS` cannot see a producer that emits `True` while the handler
    # compares "operator", which makes every real operator a client.
    dict(name="a real config reaches the handler with the right roles",
         file="listen: 127.0.0.1:0\nauth:\n  tokens:\n    - {name: op, token: real-op-token, operator: true}\n"
              "    - {name: cli, token: real-cli-token}\n",
         expect={"config.TOKENS": (("op", "real-op-token", "operator"), ("cli", "real-cli-token", "client")),
                 "authorized(real-op-token).0": Truthy(), "authorized(real-op-token).1": True,
                 "authorized(real-cli-token).0": Truthy(), "authorized(real-cli-token).1": False,
                 "authorized(not-a-token).0": Falsy(), "authorized(not-a-token).1": Falsy()}),
    dict(name="the tail reaches the globals and the fingerprint", file=T + "tail_chars: 6000\n",
         expect={"config.TAIL_CHARS": 6000, "fingerprint.tail_chars": 6000}),
    # On the client path the operator's value is the default of every request: a request's
    # own number is ignored there, and its zero still means unset.
    dict(name="the budgets reach the request and the fingerprint", file=BUDGET,
         expect={"config.DEFAULT_SEG_BUDGET": 4000, "fingerprint.seg_budget": 4000,
                 "knobs({}|off).seg_budget": 4000, 'knobs({"seg_budget": 7}|off).seg_budget': 4000,
                 'knobs({"seg_budget": 0}|on).seg_budget': 4000, 'knobs({"seg_budget": 7}|on).seg_budget': 7}),
    dict(name="merge_text off reaches the request", file=MERGE_OFF,
         expect={"config.DEFAULT_MERGE_TEXT": Falsy(), "knobs({}|off).merge_text": Falsy(),
                 'knobs({"merge_text": true}|off).merge_text': Falsy(),
                 'knobs({"merge_text": true}|on).merge_text': Truthy()}),
    dict(name="session_pin reaches the globals and the fingerprint", file=PIN_ON,
         expect={"config.DEFAULT_SESSION_PIN": Truthy(), "fingerprint.session_pin": Truthy()}),
)

# `core.RUNS_LOG` is patched: `file` is loaded and the settings applied. `s` is what loaded.
APPLIES = (
    # Applying runs again on every SIGHUP, so a path applied there would let a reload move a
    # serving process's evidence mid-stream, one chain split across two files. It would also
    # reach into a caller that chose its own path: the suite passes `RELAY_RUNS_LOG`.
    dict(name="a config that names no run log does not move the process log",
         patch={"core.RUNS_LOG": "{scratch}/mine.jsonl"}, file=T,
         expect={"core.RUNS_LOG": Ref("scratch", lambda d: os.path.join(d, "mine.jsonl")),
                 "s.runs_log_source": "default"}),
)

# `start` is resolved first when the row names it, then `file` is written over it and
# reloaded. `before` is the module as `start` left it, `applied` what the reload returned,
# `said` what it logged and `told` all of that as one text.
RELOADS = (
    # adding a client must not mean restarting, and the running configuration is readable
    dict(name="a reload adds a token without dropping the process", start="auth:\n  token: one\n",
         file="auth:\n  tokens:\n    - {name: one, token: t1}\n    - {name: two, token: t2}\n",
         expect={"names(before.TOKENS)": ["default"], "applied": NotNone(), "names(config.TOKENS)": ["one", "two"],
                 "said.0": Has("2 client token(s): one, two")}),
    dict(name="a broken reload keeps serving the previous configuration", start="auth:\n  token: one\n",
         file="auth:\n  tokens: {not: a list}\n",
         expect={"applied": None, "config.TOKENS": Ref("before.TOKENS"), "said.0": Has("reload refused")}),
    # Resolving an empty deployment is legitimate (the process refuses to start); applying it
    # to a serving process would revoke every client at once.
    dict(name="a reload may not empty the token list", start="auth:\n  token: one\n",
         file="listen: 127.0.0.1:8371\n",
         expect={"applied": None, "config.TOKENS": Ref("before.TOKENS"), "said.0": Has("no client tokens")}),
    # the operator's edit is neither obeyed (that would split the chain) nor swallowed
    dict(name="a reload that changes the run log says so and keeps writing",
         patch={"core.RUNS_LOG": "{scratch}/mine.jsonl"}, file=T + "runs_log: {scratch}/other.jsonl\n",
         expect={"core.RUNS_LOG": Ref("scratch", lambda d: os.path.join(d, "mine.jsonl")),
                 "told": Has("takes effect at the next start", "reloaded"),
                 "applied.runs_log_source": "config runs_log"}),
    dict(name="a reload that no longer names the budget returns to the built-in", start=BUDGET, file=T,
         expect={"applied": NotNone(), "config.DEFAULT_SEG_BUDGET": Ref("config.BUILTIN_SEG_BUDGET"),
                 "fingerprint.seg_budget": Ref("config.BUILTIN_SEG_BUDGET"),
                 "knobs({}|off).seg_budget": Ref("config.BUILTIN_SEG_BUDGET")}),
    dict(name="a reload that no longer names merge_text returns to the built-in", start=MERGE_OFF, file=T,
         expect={"applied": NotNone(), "config.DEFAULT_MERGE_TEXT": Truthy(), "knobs({}|off).merge_text": Truthy()}),
    dict(name="a reload that no longer names session_pin turns it off", start=PIN_ON, file=T,
         expect={"applied": NotNone(), "config.DEFAULT_SESSION_PIN": Falsy(), "fingerprint.session_pin": Falsy()}),
)


class SettingsTests(_Rows, unittest.TestCase):
    def test_the_settings_loaded(self):
        for row in LOADS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "file", "path", "env", "link", "patch", "expect"), self.load)

    def load(self, row, scratch, stack):
        if row.get("link"):
            os.symlink(HERE, os.path.join(scratch, "into-repo"))
        kw = {"env": fill(row.get("env", {}), scratch)}
        if "file" in row:
            kw["config_path"] = _file(scratch, fill(row["file"], scratch))
        elif "path" in row:
            kw["config_path"] = row["path"]
        s, raised = self.outcome(lambda: config.load_settings(**kw), row)
        return {"s": lambda _: s, **self.raised_views(raised),
                "bare": lambda _: config.load_settings(config_path=kw["config_path"], env={})}

    def test_the_settings_resolved(self):
        for row in ENSURES:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "file", "twice", "expect"), functools.partial(self.settled, "ensure"))

    def test_the_settings_applied(self):
        for row in APPLIES:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "patch", "file", "expect"), functools.partial(self.settled, "apply"))

    def test_the_settings_reloaded(self):
        for row in RELOADS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "patch", "start", "file", "expect"),
                             functools.partial(self.settled, "reload"))

    def settled(self, call, row, scratch, stack):
        """One resolution of the settings into the module: `ensure`, `apply` or `reload`."""
        stack.callback(_restore_globals, _snapshot_globals())
        path = os.path.join(scratch, "config.yaml")
        views = {"fingerprint": lambda _: core.config_fingerprint(), "shim": lambda _: shim,
                 "knobs": lambda arg: handler.resolve_knobs(json.loads(arg.rpartition("|")[0]),
                                                            tuning=arg.rpartition("|")[2] == "on"),
                 "authorized": _authorized}
        if call == "reload":
            if "start" in row:
                _file(scratch, row["start"])
                config.ensure_settings(force=True, config_path=path, env={})
            before = {"TOKENS": config.TOKENS}
            _file(scratch, fill(row["file"], scratch))
            said = []
            applied = config.reload_settings(config_path=path, env={}, log=said.append)
            views.update(before=lambda _: before, applied=lambda _: applied, said=lambda _: said,
                         told=lambda _: "\n".join(said))
        elif call == "apply":
            s = config.load_settings(config_path=_file(scratch, fill(row["file"], scratch)), env={})
            config.apply_settings(s)
            views["s"] = lambda _: s
        else:
            _file(scratch, row["file"])
            for _ in range(2 if row.get("twice") else 1):
                config.ensure_settings(force=True, config_path=path, env={})
        return views

    def test_the_cut_policy_is_the_operators_before_any_setting_resolves(self):
        """A handler that reads the global before `ensure_settings` runs must not see a
        more permissive cut policy than the resolved configuration, and the evidence
        fingerprint has to say who chose the cut policy."""
        self.assertFalse(config.ALLOW_CLIENT_TUNING)
        self.assertFalse(config._settings["allow_client_tuning"])
        self.assertIs(core.config_fingerprint()["allow_client_tuning"], False)


# --- handler.resolve_knobs, handler.resolve_mode, handler.dropped_param_names --------

BUDGET_KNOBS = ("abort_after", "seg_budget", "total_cap", "max_segments")


def _client_error(knob):
    return {"raised": IsA(core.ResponseError), "raised.status": 400, "message": Has(knob)}


# On the client-facing path the relay's own knobs are the operator's: a caller choosing
# `mode: control` bills the account, and `pad_tools: false` removes the abort surface the
# relay exists for. `payload` is the request, `tuning` whether its caller may tune.
KNOBS = (
    # `int()` on caller JSON must not escape as a 500 from the catch-all; the error names the field
    *(dict(name=f"a non numeric {knob} is a client error, not a 500", payload={knob: "many"}, tuning=True,
           expect=_client_error(knob))
      for knob in BUDGET_KNOBS + ("retries",)),
    dict(name="a non numeric knob is ignored where the caller may not tune", payload={"seg_budget": "many"},
         tuning=False, expect={"out.seg_budget": Ref("config.DEFAULT_SEG_BUDGET")}),
    dict(name="with tuning off every client knob is ignored",
         payload={"mode": "control", "abort_after": 3, "seg_budget": 1, "total_cap": 2, "max_segments": 9,
                  "payload_cap": 10 ** 9, "imagegen_cut": False, "pad_tools": False, "retries": 1},
         tuning=False,
         expect={"out.mode": "segmented", "out.abort_after": 0, "out.seg_budget": Ref("config.DEFAULT_SEG_BUDGET"),
                 "out.total_cap": Ref("config.DEFAULT_TOTAL_CAP"),
                 "out.max_segments": Ref("config.DEFAULT_MAX_SEGMENTS"),
                 # Raising the payload cap is how a client would get the relay to hand back the
                 # ~2M-character item its own next turn would then be billed for.
                 "out.payload_cap": Ref("config.DEFAULT_PAYLOAD_CAP"),
                 # `false` hands a caller its images back, at the price of a ~2M-character
                 # artifact and a run ten times longer.
                 "out.imagegen_cut": Ref("config.DEFAULT_IMAGEGEN_CUT"), "out.pad_tools": True,
                 "out.retries": Ref("config.RETRIES")}),
    *(dict(name=f"a negative {knob} is a client error", payload={knob: -1}, tuning=True, expect=_client_error(knob))
      for knob in BUDGET_KNOBS + ("payload_cap", "retries")),
    dict(name="zero retries means no retries, not the default", payload={"retries": 0}, tuning=True,
         expect={"out.retries": 0}),
    dict(name="with tuning on the client is honoured", payload={"mode": "control", "seg_budget": 7,
                                                                "payload_cap": 4096},
         tuning=True, expect={"out.mode": "control", "out.seg_budget": 7, "out.payload_cap": 4096}),
    # An unknown mode skips the marker scan and follows `pad_tools` off, so a degraded run
    # would name a mode it never ran.
    *(dict(name=f"the unknown mode {raw!r} is not passed through to the engine", payload={"mode": raw}, tuning=True,
           expect={"out.mode": In(handler.MODES) & Eq("segmented") if raw == "banana" else In(handler.MODES)})
      for raw in ("banana", "control ", "", None, 7)),
    dict(name="a known mode is passed through", payload={"mode": "control"}, tuning=True,
         expect={"out.mode": "control"}),
    dict(name="a payload cap that is not a number names the field", payload={"payload_cap": "big"}, tuning=True,
         expect={"raised": IsA(core.ResponseError), "message": Has("payload_cap")}),
    # The cap is the one knob whose 0 is a setting: the documented escape hatch for a client
    # whose real images the cap would break.
    dict(name="a zero payload cap turns the cap off", payload={"payload_cap": 0}, tuning=True,
         expect={"out.payload_cap": 0}),
    dict(name="a zero payload cap as text turns the cap off", payload={"payload_cap": "0"}, tuning=True,
         expect={"out.payload_cap": 0}),
    *(dict(name=f"a payload cap of {unset!r} is unset", payload=unset, tuning=True,
           expect={"out.payload_cap": Ref("config.DEFAULT_PAYLOAD_CAP")})
      for unset in ({}, {"payload_cap": None}, {"payload_cap": ""})),
    # The reading the cap does not share, held here so the difference is not "fixed" into a
    # leak: a zero budget means no bound, which is the direction the relay exists to cut.
    dict(name="zero still means unset for the budgets", payload={"seg_budget": 0, "total_cap": 0, "max_segments": 0},
         tuning=True,
         expect={"out.seg_budget": Ref("config.DEFAULT_SEG_BUDGET"), "out.total_cap": Ref("config.DEFAULT_TOTAL_CAP"),
                 "out.max_segments": Ref("config.DEFAULT_MAX_SEGMENTS")}),
    *(dict(name=f"session_pin is not a knob a request can set, tuning {tuning}", payload={"session_pin": True},
           tuning=tuning, expect={"out": Lacks("session_pin")})
      for tuning in (False, True)),
)

# whatever the caller sent -> the mode the engine runs
MODES_RUN = (("banana", "segmented"), ("control ", "segmented"), ("", "segmented"), (None, "segmented"),
             (7, "segmented"))

# The relay consumes its own knobs on the operator's behalf, so a caller that sends one is
# not told it was dropped. `owned` is the relay's own parameters, `forwarded` the ones the
# upstream body carries, `out` the names reported dropped.
DROPPED = (
    *(dict(name=f"{knob} is the relays own, not a dropped parameter", payload={knob: 0},
           expect={"owned": Has(knob), "out": []})
      for knob in ("mode", "abort_after", "seg_budget", "total_cap", "max_segments", "imagegen_cut", "pad_tools",
                   "retries")),
    # ...and it is still not silently forwarded upstream as a parameter
    dict(name="the payload cap is not reported as dropped, nor forwarded", payload={"payload_cap": 0},
         expect={"owned": Has("payload_cap"), "out": [], "forwarded": Lacks("payload_cap")}),
)


class KnobTests(_Rows, unittest.TestCase):
    def test_the_knobs_resolved(self):
        for row in KNOBS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "payload", "tuning", "expect"), self.knobs)

    def knobs(self, row, scratch, stack):
        out, raised = self.outcome(lambda: handler.resolve_knobs(copy.deepcopy(row["payload"]),
                                                                 tuning=row["tuning"]), row)
        return {"out": lambda _: out, **self.raised_views(raised)}

    def test_the_mode_resolved(self):
        for raw, want in MODES_RUN:
            with self.subTest(raw=raw):
                self.assertEqual(handler.resolve_mode(raw), want)

    def test_the_parameters_reported_dropped(self):
        for row in DROPPED:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "payload", "expect"), lambda row, scratch, stack: {
                    "out": lambda _: handler.dropped_param_names(copy.deepcopy(row["payload"])),
                    "owned": lambda _: handler.RELAY_OWNED_PARAMS, "forwarded": lambda _: renamer.FORWARDED_PARAMS})


# --- core.session_pin_id, core.set_runs_log, the run log resolved at import ---------

# `client` -> `out`, its pinned session id; `pin(<name>)` is another client's
PINS = (
    dict(name="the id is stable per client, never shared, and a version 5 uuid", client="alpha",
         expect={"out": Eq(Ref("pin(alpha)")) & Ne(Ref("pin(beta)"))
                 & Matches(r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")}),
    dict(name="no client is the default client", client=None, expect={"out": Ref("pin(default)")}),
)

# `core.RUNS_LOG` is put back after each row. `target` is the path given (`writing`: by a
# writer); `dir` its directory; `cache` the chain cache, seeded with a stale head when the
# row says `stale`.
RUNLOGS = (
    # A copy taken when `config` was imported is a second answer to "which file?" the moment
    # the serving process can be pointed elsewhere.
    dict(name="config.RUNS_LOG is the running path, not a snapshot", path="{scratch}/moved.jsonl",
         expect={"config.RUNS_LOG": Ref("out")}),
    # A reader may point at the checkout (anchoring the committed seed is legitimate), and
    # `log_run` refuses an in-repo write whatever this was called with.
    dict(name="a writer may not point into the checkout", path="{repo}/runs.jsonl", writing=True,
         expect=_refused()),
    dict(name="a reader may point into the checkout", path="{repo}/runs.jsonl",
         expect={"out": Ref("target", os.path.realpath)}),
    dict(name="a writer creates the directory owner only and drops the chain cache",
         path="{scratch}/state/cliproxy/runs.jsonl", writing=True, stale=True,
         expect={"dir_is_dir": Truthy(), "dir": Mode(0o700), "cache": {}}),
    # a read-only tool has no side effect: a typo in a cron entry does not become a directory tree
    dict(name="a reader does not create the directory it reads", path="{scratch}/never-created/runs.jsonl",
         expect={"real(out)": Ref("target", os.path.realpath), "dir_exists": False}),
    dict(name="an empty path is refused", path="   ", expect=_refused()),
)


class RunLogTests(_Rows, unittest.TestCase):
    def test_the_session_pin_ids(self):
        for row in PINS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "client", "expect"), lambda row, scratch, stack: {
                    "out": lambda _: core.session_pin_id(row["client"]), "pin": core.session_pin_id})

    def test_the_run_log_pointed_at(self):
        for row in RUNLOGS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "path", "writing", "stale", "expect"), self.pointed)

    def pointed(self, row, scratch, stack):
        stack.enter_context(mock.patch.object(core, "RUNS_LOG", core.RUNS_LOG))
        target = fill(row["path"], scratch)
        if row.get("stale"):
            core._chain_cache["stale"] = {"size": 1, "seq": 9, "hash": "x"}
        out, raised = self.outcome(lambda: core.set_runs_log(target, for_writing=row.get("writing", False)), row)
        return {"out": lambda _: out, **self.raised_views(raised), "target": lambda _: target,
                "dir": lambda _: os.path.dirname(target), "cache": lambda _: core._chain_cache,
                "dir_is_dir": lambda _: os.path.isdir(os.path.dirname(target)),
                "dir_exists": lambda _: os.path.exists(os.path.dirname(target))}

    def test_a_blank_run_log_variable_means_unset_at_import(self):
        """`RELAY_RUNS_LOG='   '` is truthy and `load_settings` strips it, so the two
        resolutions must agree: a blank is the default, never the path `'   '` (a lock
        file in the cwd, every run refused as inside the checkout). `RUNS_LOG` is computed
        at import, so this needs a subprocess."""
        out = subprocess.run(
            [sys.executable, "-c", "from lokki_core import core; print(core.RUNS_LOG)"],
            cwd=HERE, env=dict(os.environ, RELAY_RUNS_LOG="   "), capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), core.DEFAULT_RUNS_LOG)


# --- handler.startup_notes, handler.wildcard_addresses, handler.main ----------------

# A tail shorter than the segment budget is allowed and costs quality: a continuation is not
# shown the answer so far. The relay says so at start-up. (tail, budget, what the notes hold)
NOTES = (
    (1500, 1500, {"notes": []}),
    (1500, 4000, {"notes": Len(1), "notes.0": Has("tail_chars (1500)", "seg_budget (4000)")}),
    (8000, 4000, {"notes": []}),
)

# (listen addresses, the ones that bind every interface)
WILDCARDS = (
    (("0.0.0.0:1", ":::1", "*:1", ":1"), ["0.0.0.0:1", ":::1", "*:1", ":1"]),
    (("127.0.0.1:1", "100.95.1.1:1"), []),
)

# `argv` with `{config}` for a file holding `file`; `out` is what `main` returned. The
# globals, `RELAY_CONFIG` and the run-log lock are put back after each row.
MAINS = (
    # driven through an explicit --config, so the refusal comes from resolution, not from a
    # patched global
    dict(name="main refuses to start without a token", argv=["--config", "{config}"],
         file="listen: 127.0.0.1:0\nupstream: http://127.0.0.1:1\n",
         expect={"out": 2, "stderr": Len(1), "stderr.0": Starts("refusing to start: no client token. ")}),
    # Binding every interface exposes an endpoint that forwards the operator's upstream
    # credential. The protocol says never; the code refuses rather than warning.
    dict(name="main refuses a wildcard bind", argv=["-c", "{config}"], file="listen: '0.0.0.0:8371'\nruns_log: {scratch}/runs.jsonl\n" + T,
         expect={"out": 2, "stderr": Len(1),
                 "stderr.0": Starts("refusing to start: ['0.0.0.0:8371'] bind every interface. ")}),
)


class StartupTests(_Rows, unittest.TestCase):
    def test_the_startup_notes(self):
        for tail, budget, expect in NOTES:
            with self.subTest(tail=tail, budget=budget), \
                    mock.patch.object(config, "TAIL_CHARS", tail), \
                    mock.patch.object(config, "DEFAULT_SEG_BUDGET", budget):
                notes = handler.startup_notes()
                self.check({"notes": lambda _: notes}, expect)

    def test_the_wildcard_addresses(self):
        for listen, want in WILDCARDS:
            with self.subTest(listen=listen):
                self.assertEqual(handler.wildcard_addresses(listen), want)

    def test_what_main_refuses(self):
        for row in MAINS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "argv", "file", "expect"), self.main)

    def main(self, row, scratch, stack):
        stack.callback(core.release_runs_lock)
        stack.callback(_restore_globals, _snapshot_globals())
        saved_env = os.environ.get("RELAY_CONFIG")
        stack.callback(lambda: os.environ.pop("RELAY_CONFIG", None) if saved_env is None
                       else os.environ.__setitem__("RELAY_CONFIG", saved_env))
        path = _file(scratch, fill(row["file"], scratch))
        out = handler.main([path if arg == "{config}" else arg for arg in row["argv"]])
        return {"out": lambda _: out}


# --- handler.start_listeners --------------------------------------------------------

# `addresses` bound with `strict` (default True). `servers` is what it returned, `made` every
# server it built, `leaked` those it neither returned nor closed. `served` asks each listener
# for `/api/runs` with the token; `beyond_cap` holds one connection open and asks again.
# 127.0.0.1:0 always binds; an address in 203.0.113.0/24 is not local.
LISTENERS = (
    dict(name="every configured address is bound and serves", addresses=("127.0.0.1:0", "127.0.0.1:0"),
         patch={"config.TOKENS": ((CLIENT, TOKEN, "operator"),)},
         expect={"servers": Len(2), "distinct(ports)": Len(2), "served": [200, 200]}),
    # skipping is something the caller asks for, not the default
    dict(name="an optional address that cannot be bound is skipped and said",
         addresses=("203.0.113.7:9", "127.0.0.1:0"), strict=False,
         expect={"servers": Len(1), "servers.0.0": "127.0.0.1:0", "stderr": Len(1),
                 "stderr.0": Starts("warning: cannot bind 203.0.113.7:9: ") & Ends("; skipping")}),
    # A relay that comes up loopback-only because Tailscale was not ready yet is a silent
    # reduction in service, so startup fails and says which address it could not bind.
    dict(name="a required address that cannot be bound refuses startup and names it",
         addresses=("203.0.113.7:9", "127.0.0.1:0"), expect=_refused("203.0.113.7:9")),
    # bound in order: a later failure closes the ones already up, or the process leaks a
    # listening socket it will not serve
    dict(name="a failed required bind does not leak the earlier listener", addresses=("127.0.0.1:0", "203.0.113.7:9"),
         expect={**_refused(), "made": Len(1), "leaked": []}),
    dict(name="no address that can be bound is an error", addresses=("203.0.113.7:9",), expect=_refused()),
    # the hostile-client bounds, each asserted as the property it is
    dict(name="the listen backlog is raised above the stdlib default", addresses=("127.0.0.1:0",), strict=False,
         expect={"servers.0.1.request_queue_size": Ref("config.LISTEN_BACKLOG"), "config.LISTEN_BACKLOG": Gt(5)}),
    dict(name="a connection beyond the cap is refused with a 503", addresses=("127.0.0.1:0",), strict=False,
         patch={"config.MAX_CLIENT_CONNECTIONS": 1}, expect={"beyond_cap": Has(b"503")}),
)


class ListenerTests(_Rows, unittest.TestCase):
    def test_the_listeners_bound(self):
        for row in LISTENERS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "addresses", "strict", "patch", "expect"), self.bound)

    def bound(self, row, scratch, stack):
        made = []

        class Recorded(handler._ReusableServer):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                made.append(self)

        stack.callback(lambda: [srv.server_close() for srv in made])
        with mock.patch.object(handler, "_ReusableServer", Recorded):
            servers, raised = self.outcome(lambda: start_listeners(row["addresses"], strict=row.get("strict", True)),
                                           row)
        ports = [] if raised else [srv.server_address[1] for _, srv in servers]

        def serve():
            for _, srv in servers:
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                stack.callback(srv.shutdown)

        def served():
            serve()
            return [json_get(port, "/api/runs")[0] for port in ports]

        def beyond_cap():
            serve()
            # Hold the single slot with a connection that never finishes its request.
            held = raw_request(ports[0], b"POST /v1/responses HTTP/1.1\r\n", read=False)
            stack.callback(held.close)
            time.sleep(0.3)
            return raw_request(ports[0], b"POST /v1/responses HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
                               ).split(b"\r\n")[0]

        views = {"servers": lambda _: servers, "ports": lambda _: ports, "made": lambda _: made,
                 "leaked": lambda _: [s for s in made if s.socket.fileno() != -1
                                      and all(s is not srv for _, srv in servers or ())],
                 "served": lambda _: served(), "beyond_cap": lambda _: beyond_cap(), **self.raised_views(raised)}
        views["distinct"] = lambda path: set(self.at(views, path))
        return views


# --- Handler.do_POST and Handler.do_GET, against one relay and one stub upstream -----

def _sse_bytes(frames):
    return "".join(f"event: {f['type']}\ndata: {json.dumps(f)}\n\n" for f in frames).encode()


def _error_reply(status, message):
    return (status, "application/json", json.dumps({"error": {"message": message, "code": "invalid_value"}}).encode())


EMPTY = (200, "text/event-stream", b"")       # the run ends by EOF: the request is all a row reads
# a message cut at the marker: the relay closes it and completes the response itself
CUT = (200, "text/event-stream", "".join([
    "event: response.output_item.added\n",
    'data: {"type":"response.output_item.added","output_index":0,'
    '"item":{"id":"msg_1","type":"message","status":"in_progress","role":"assistant","content":[]}}\n\n',
    "event: response.content_part.added\n",
    'data: {"type":"response.content_part.added","output_index":0,"item_id":"msg_1","content_index":0,'
    '"part":{"type":"output_text","annotations":[],"text":""}}\n\n',
    "event: response.output_text.delta\n",
    'data: {"type":"response.output_text.delta","output_index":0,"item_id":"msg_1","content_index":0,'
    '"delta":"the answer ' + renamer.MARKER + '"}\n\n']).encode())
FAILED = (200, "text/event-stream", _sse_bytes([
    {"type": "response.created", "response": {"id": "resp_1"}},
    {"type": "response.failed", "response": {"id": "resp_1", "status": "failed",
                                             "error": {"code": "server_error", "message": "x"}}}]))
INCOMPLETE = (200, "text/event-stream", _sse_bytes([
    {"type": "response.created", "response": {"id": "resp_1"}},
    {"type": "response.incomplete", "response": {"id": "resp_1", "status": "incomplete",
                                                 "usage": {"input_tokens": 9, "output_tokens": 1,
                                                           "total_tokens": 10}}}]))
COMPLETED = (200, "text/event-stream", _sse_bytes([{"type": "response.completed",
                                                    "response": {"id": "r", "usage": {}}}]))


class _Relay:
    """The relay the HTTP rows are sent to, and the upstream it calls: a stub that answers
    each request with `reply` and records what it received. Built once; a row sets its
    own `reply`, and `seen` is emptied for it."""

    built = None

    @classmethod
    def get(cls):
        if cls.built is None:
            cls.built = cls()
        return cls.built

    @classmethod
    def stop(cls):
        if cls.built is not None:
            for srv in (cls.built.relay, cls.built.stub):
                srv.shutdown()
                srv.server_close()
            routing._last = cls.built.routing_before
            cls.built = None

    def __init__(self):
        self.reply, self.seen = EMPTY, []
        outer = self

        class _Stub(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"null")
                except ValueError:
                    body = None
                outer.seen.append({"path": self.path, "body": body,
                                   "headers": {k.lower(): v for k, v in self.headers.items()}})
                status, ctype, data = outer.reply
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.stub = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        self.upstream = f"http://127.0.0.1:{self.stub.server_address[1]}"
        self.relay = start_listeners(("127.0.0.1:0",), strict=False)[0][1]
        self.port = self.relay.server_address[1]
        for srv in (self.stub, self.relay):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        # The routing decision is announced once, when it changes: here, so that no row's
        # stderr depends on which row ran first.
        self.routing_before = routing._last
        routing._last = None
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            routing.decide_now()
        if said.getvalue() != "[routing] relay on: routing: relay\n":
            raise AssertionError(f"the routing decision announced {said.getvalue()!r}")


def _chunked(payload, size=7):
    parts = []
    for i in range(0, len(payload), size):
        part = payload[i:i + size]
        parts.append(f"{len(part):x}\r\n".encode() + part + b"\r\n")
    return b"".join(parts) + b"0\r\n\r\n"


def _refused_body(size):
    """A body this relay refuses for its `input`, padded with JSON whitespace to `size`
    bytes: sent as one-byte chunks it is exactly `size` chunks, and however the count goes
    it never reaches upstream."""
    body = b'{"input": {"role": "user"}}'
    return body + b" " * (size - len(body))


def _post_raw(head, body):
    """A POST to /v1/responses with the token, as bytes: urllib hides the framing."""
    return (f"POST /v1/responses HTTP/1.1\r\nHost: x\r\nX-Relay-Token: {TOKEN}\r\n"
            f"Content-Type: application/json\r\n{head}\r\n").encode() + body


def _oversized():
    # Claims far more than the cap while sending almost nothing: a relay that buffered
    # Content-Length bytes would block, and a prompt 413 is the difference.
    return _post_raw(f"Content-Length: {config.MAX_BODY_BYTES + 1}\r\n", b"{")


def _get_raw(path, token, extra=b""):
    return f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n".encode() + extra + b"X-Relay-Token: " + token + b"\r\n\r\n"


HELLO = {"model": "gpt-5.5", "input": "hello"}
HI = b'{"input": "hi"}'
CODEX_FIELDS = {
    "client_metadata": {"session_id": "s-1", "thread_id": "t-1"},
    "include": ["reasoning.encrypted_content"],
    "text": {"verbosity": "low"},
    "tool_choice": "auto",
    "parallel_tool_calls": False,
    "reasoning": {"effort": "low", "context": "all_turns"},
    "prompt_cache_key": "t-1",
    "store": False,
}
ENCRYPTED = "gAAAAAB" + "x" * 40
CODEX_ITEMS = [
    {"type": "additional_tools", "id": "at_1", "role": "developer",
     "tools": [{"type": "namespace", "name": "functions", "tools": [
         {"type": "custom", "name": "exec", "description": "run"}]}]},
    {"type": "message", "id": "msg_1", "role": "developer",
     "content": [{"type": "input_text", "text": "instructions"}]},
    {"type": "reasoning", "id": "rs_abc", "summary": [], "content": None, "encrypted_content": ENCRYPTED},
    {"type": "message", "id": "msg_2", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
]
TWO_TOKENS = {"config.TOKENS": (("first", TOKEN, "operator"), ("second", "second-client-token", "client"))}
TUNE = {"prompt": "hello", "mode": "control", "pad_tools": False}
PINNED = {"config.DEFAULT_SESSION_PIN": True}
CAPTURING = {"config.CAPTURE_DIR": "{scratch}/captures"}
PIN_OF_THE_CLIENT = Ref("record.client", core.session_pin_id)
OPENAI_ERROR_KEYS = ["code", "message", "param", "type"]

# One row per run of the handler: the requests `send` makes in order, each `path` with
# `json` (or `body`, its bytes) by POST, else by GET; `token` is the X-Relay-Token (TOKEN
# unless named; None sends none), `headers` the rest. A `raw` request is bytes on a socket,
# read by `read`: "framed" (to its Content-Length), "recv" (one read) or "close" (until the
# relay closes it). The stub answers with `reply`; `trace` names a trace file for the run.
# Every row runs as the operator CLIENT, with pacing off and one attempt, unless `patch`
# says otherwise. Views: `resp` (each response: `status`, `headers`, `body`, `json`,
# `message`, `frames`, `completed`; a raw one also `data`, `line`, `head`) and the same
# names for the last one; `upstream` what the stub received (`path`, `body`, `headers`);
# `record`/`records` the run log; `counted` the run counts it added; `captures`,
# `captured` (parsed) and `capture_paths`; `trace`, `traced(<dir>)`, `trace_dirs`;
# `transcript(<n>)` the frames of response n, as the goldens hold them.
POSTS = (
    # --- /api/run, the operator's path, and an unknown path ---
    dict(name="a missing token is a 401 that says token", send=dict(path="/api/run", json={"prompt": "x"}, token=None),
         expect={"status": 401, "message": Has("token")}),
    dict(name="a wrong token is a 401", send=dict(path="/api/run", json={"prompt": "x"}, token="nope"),
         expect={"status": 401}),
    # authorized, then rejected on input
    dict(name="a bearer token is accepted too",
         send=dict(path="/api/run", json={"input": {"role": "user"}}, token=None,
                   headers={"Authorization": f"Bearer {TOKEN}"}),
         expect={"status": 400}),
    dict(name="an empty body is a 400 that says empty", send=dict(path="/api/run", body=b""),
         expect={"status": 400, "message": Has("empty")}),
    dict(name="malformed json is a 400", send=dict(path="/api/run", body=b"{not json"), expect={"status": 400}),
    dict(name="a json array body is a 400", send=dict(path="/api/run", body=b"[]"), expect={"status": 400}),
    dict(name="a dict input is a 400, not a default prompt", send=dict(path="/api/run", json={"input": {"role": "user"}}),
         expect={"status": 400, "message": Lacks("poem")}),
    dict(name="a bare string input item is a 400, not a crash", send=dict(path="/api/run", json={"input": ["bare string"]}),
         expect={"status": 400}),
    dict(name="no prompt and no input is a 400", send=dict(path="/api/run", json={"mode": "control"}),
         expect={"status": 400}),
    dict(name="an unknown path is a 404", send=dict(path="/nope", json={"prompt": "x"}), expect={"status": 404}),
    # A caller of the client-facing path must not pick `mode: control` (which bills) or drop
    # the tool padding (which removes the abort surface), while the operator's own `/api/run`
    # keeps both. Asserted on the body sent upstream, so knobs built somewhere else still fail.
    dict(name="the operator path still honours both knobs", send=dict(path="/api/run", json=TUNE),
         expect={"upstream": Truthy(), "upstream.0.body.input": "hello", "upstream.0.body": Lacks("tools"),
                 "dumps(upstream.0.body)": Lacks(renamer.MARKER)}),
    # each configured token names itself in the record: what a revocation is decided against
    *(dict(name=f"the token named {name} is accepted and names itself in the record", patch=TWO_TOKENS,
           send=dict(path="/api/run", json={"prompt": "x", "mode": "control"}, token=token),
           expect={"record.client": name})
      for name, token in (("first", TOKEN), ("second", "second-client-token"))),
    # Knob rights follow who is asking, not the path: keyed on the path, any authenticated
    # client could POST the operator's `/api/run` and set the relay's own knobs from there.
    dict(name="the operator token tunes through /api/run", patch=TWO_TOKENS, send=dict(path="/api/run", json=TUNE),
         expect={"upstream": Truthy(), "upstream.0.body.input": "hello"}),
    dict(name="a client token does not tune through /api/run", patch=TWO_TOKENS,
         send=dict(path="/api/run", json=TUNE, token="second-client-token"),
         expect={"upstream": Truthy(), "upstream.0.body.input": Ne("hello"),
                 "dumps(upstream.0.body.input)": Has(renamer.MARKER), "upstream.0.body": Has("tools")}),
    dict(name="an unknown token is refused before any upstream call", patch=TWO_TOKENS,
         send=dict(path="/api/run", json={"prompt": "x"}, token="not-a-token"), expect={"upstream": []}),
    # a different message per failure mode is how a token list is enumerated
    dict(name="the 401 does not say which tokens exist", patch=TWO_TOKENS,
         send=[dict(path="/api/run", json={"prompt": "x"}, token=None),
               dict(path="/api/run", json={"prompt": "x"}, token="wrong")],
         expect={"resp.0.message": Ref("resp.1.message")}),
    # GET is query-tolerant for monitors; POST is too, including the trailing-slash typo
    dict(name="a trailing slash does not change which surface answered",
         send=dict(path="/api/run/", json={"prompt": "hello", "mode": "control"}),
         expect={"status": Ne(404), "upstream.0.body.input": "hello"}),
    dict(name="the session pin covers the operator path", patch=PINNED, send=dict(path="/api/run", json={"prompt": "hello"}),
         expect={"upstream": Truthy(), "upstream.0.headers.session-id": PIN_OF_THE_CLIENT}),
    # --- /v1/responses: framing, read off the socket ---
    # A client whose HTTP stack streams its upload sends no Content-Length; the body is read
    # and judged on its content, here refused for its `input`.
    dict(name="a chunked body is read, not called empty",
         send=dict(raw=_post_raw("Transfer-Encoding: chunked\r\n", _chunked(b'{"input": {"role": "user"}}')),
                   read="framed"),
         expect={"status": 400, "message": Ne("empty request body") & Has("'input'")}),
    dict(name="both framings at once are refused",
         send=dict(raw=_post_raw(f"Transfer-Encoding: chunked\r\nContent-Length: {len(HI)}\r\n", _chunked(HI)),
                   read="framed"),
         expect={"status": 400, "message": Has("not both")}),
    dict(name="a chunked body is held to the size limit", patch={"config.MAX_BODY_BYTES": 64},
         send=dict(raw=_post_raw("Transfer-Encoding: chunked\r\n", _chunked(b'{"input": "' + b"x" * 200 + b'"}')),
                   read="framed"),
         expect={"status": 413}),
    # the byte cap left the count open: the cap's worth of one-byte chunks is seconds of parsing
    dict(name="a chunked body is held to a chunk count", patch={"handler.Handler._MAX_CHUNKS": 4},
         send=dict(raw=_post_raw("Transfer-Encoding: chunked\r\n", _chunked(b'{"input": {"role": "user"}}', size=1)),
                   read="framed"),
         expect={"status": 400, "message": Has("chunks")}),
    # unmocked, at the real constant: where the check sits (`>=` for `>`, or a lower constant)
    dict(name="the real chunk limit admits exactly its count",
         send=dict(raw=lambda: _post_raw("Transfer-Encoding: chunked\r\n",
                                         _chunked(_refused_body(handler.Handler._MAX_CHUNKS), size=1)), read="framed"),
         expect={"status": 400, "message": Has("'input'")}),
    dict(name="one chunk past the real limit is refused for its count",
         send=dict(raw=lambda: _post_raw("Transfer-Encoding: chunked\r\n",
                                         _chunked(_refused_body(handler.Handler._MAX_CHUNKS + 1), size=1)),
                   read="framed"),
         expect={"status": 400, "message": Has(Ref("handler.Handler._MAX_CHUNKS", lambda n: f"more than {n} chunks"))}),
    # the constant's own claim: the 8 MB cap at 512 bytes a chunk is refused for its content
    dict(name="the full body cap fits in the chunk limit at 512 bytes",
         send=dict(raw=lambda: _post_raw("Transfer-Encoding: chunked\r\n",
                                         _chunked(_refused_body(config.MAX_BODY_BYTES), size=512)), read="framed"),
         expect={"handler.Handler._MAX_CHUNKS": Ref("config.MAX_BODY_BYTES", lambda n: n // 512), "status": 400,
                 "message": Has("'input'")}),
    # A 413 must not leave the unread body on a keep-alive socket, or the next request on the
    # connection is parsed from those bytes: a request-boundary desync.
    dict(name="an oversized body is refused before it is read, and the connection closed",
         send=dict(raw=_oversized, read="recv"), expect={"line": Has(b"413"), "head": Has(b"connection: close")}),
    # --- /v1/responses: what is refused ---
    # `model` is written into the run record, so it is an identifier or a 400
    *(dict(name=f"the model {bad!r} is not an identifier and is a 400",
           send=dict(path="/v1/responses", json={"model": bad, "input": "hi"}),
           expect={"status": 400, "message": Has("'model'")})
      for bad in ({"x": 1}, ["gpt"], "a" * 101, "gpt 5", "gpt\n5")),
    # clients switch on `type`; a value no SDK knows hides a relay fault
    dict(name="a bad request returns an openai shaped error", send=dict(path="/v1/responses", body=b""),
         expect={"status": 400, "keys(json)": ["error"], "keys(json.error)": OPENAI_ERROR_KEYS,
                 "json.error.type": "invalid_request_error"}),
    dict(name="an empty prompt string is a 400", send=dict(path="/v1/responses", json={"prompt": "   "}),
         expect={"status": 400}),
    # The relay aborts before the provider finalizes, so it stores nothing: sending `false`
    # for a client that asked for `true` would give it the opposite and let it find out later.
    dict(name="store true is refused with the reason, not overridden", send=dict(path="/v1/responses",
                                                                                json={**HELLO, "store": True}),
         expect={"status": 400, "body": Has(b"store"), "upstream": []}),
    # There is no stored response to continue from; forcing inline history is a client-side
    # change the caller has to know about. A refused request never reaches the run log, so
    # stderr is where the operator finds which source keeps its history server-side.
    dict(name="a previous_response_id is refused and names the fix",
         send=dict(path="/v1/responses", json={**HELLO, "previous_response_id": "resp_abc"}),
         expect={"status": 400, "message": Has("previous_response_id", "full conversation in 'input'"),
                 "upstream": [], "stderr": [f"refused previous_response_id from client={CLIENT}"]}),
    # --- /v1/responses: what reaches the upstream, and what is recorded ---
    dict(name="the client path cannot choose control mode or drop padding",
         send=dict(path="/v1/responses", json={**HELLO, "mode": "control", "pad_tools": False, "seg_budget": 1,
                                               "max_segments": 1}),
         expect={"upstream": Truthy(), "upstream.0.body.input": Ne("hello"),
                 "dumps(upstream.0.body.input)": Has(renamer.MARKER), "upstream.0.body": Has("tools")}),
    dict(name="a relayed run is recorded with its route and counted", send=dict(path="/v1/responses", json=HELLO),
         expect={"record.route": "relay", "counted.relay": 1}),
    dict(name="the whitelisted parameters reach upstream and the rest are named",
         send=dict(path="/v1/responses", json={**HELLO, "temperature": 0.3, "top_p": 0.9, "max_output_tokens": 40,
                                               "reasoning": {"effort": "low"}, "metadata": {"run": "x"},
                                               "user": "someone", "safety_identifier": "abc", "store": False}),
         expect={"upstream": Truthy(), "upstream.0.body.temperature": 0.3, "upstream.0.body.top_p": 0.9,
                 "upstream.0.body.max_output_tokens": 40, "upstream.0.body.reasoning": {"effort": "low"},
                 "upstream.0.body.metadata": {"run": "x"}, "upstream.0.body": Lacks("user", "safety_identifier"),
                 "record.params_forwarded": Has("temperature"),
                 # `store` is the relay's own: neither forwarded nor lost
                 "record.dropped_params": Has("user") & Lacks("store"),
                 # values are client body content and must not reach the committed record
                 "dumps(record)": Lacks("0.3", "someone")}),
    # The Codex CLI 0.160.0 field set: every one arrives on a real request, and a field
    # dropped in silence is the failure. On the record and the body, so forwarding a field
    # but no longer naming it, or the reverse, fails.
    dict(name="none of the fields a real codex client sends is dropped",
         send=dict(path="/v1/responses", json={**HELLO, **CODEX_FIELDS}),
         expect={"upstream": Truthy(),
                 **{f"upstream.0.body.{name}": value for name, value in CODEX_FIELDS.items() if name != "store"},
                 "record.dropped_params": [],
                 "record.params_forwarded": Has(*(name for name in CODEX_FIELDS if name != "store"))}),
    dict(name="the relays own fields cannot be overridden", send=dict(path="/v1/responses", json={**HELLO, "stream": False}),
         expect={"upstream": Truthy(), "upstream.0.body.stream": True, "upstream.0.body.store": False}),
    # `tool_choice` is a client behaviour field: the abort does not depend on it, because the
    # client-facing path pins `mode` to segmented and the segment budget is the guarantee
    dict(name="a clients tool_choice is carried, not overwritten",
         send=dict(path="/v1/responses", json={**HELLO, "tool_choice": "required"}),
         expect={"upstream": Truthy(), "upstream.0.body.tool_choice": "required"}),
    # A resumed turn depends on an item shape: a reasoning item's id and its encrypted content
    # reach upstream together, because the provider verifies one against the other. The relay
    # appends its own instruction message and changes nothing else.
    dict(name="a codex shaped input round trips item for item",
         send=dict(path="/v1/responses", json={"model": "gpt-5.5", "input": CODEX_ITEMS,
                                               "include": ["reasoning.encrypted_content"]}),
         expect={"upstream": Truthy(), "upstream.0.body.input": Len(len(CODEX_ITEMS) + 1),
                 "upstream.0.body.input.0": CODEX_ITEMS[0], "upstream.0.body.input.1": CODEX_ITEMS[1],
                 "upstream.0.body.input.2.type": "reasoning", "upstream.0.body.input.2.id": "rs_abc",
                 "upstream.0.body.input.2.encrypted_content": ENCRYPTED, "upstream.0.body.input.3": CODEX_ITEMS[3],
                 "dumps(upstream.0.body.input.4)": Has(renamer.MARKER),
                 "upstream.0.body.include": ["reasoning.encrypted_content"]}),
    dict(name="a query string does not change which surface answered",
         send=dict(path="/v1/responses?api-version=2025-04-01", json=HELLO),
         expect={"status": Ne(404), "upstream": Truthy()}),
    # with the refusal off, the source that keeps its history server-side is still visible:
    # that is how the operator learns who will break when it is turned on
    dict(name="a previous_response_id is recorded when it is accepted",
         patch={"config.REJECT_PREVIOUS_RESPONSE_ID": False},
         send=dict(path="/v1/responses", json={**HELLO, "previous_response_id": "resp_abc"}),
         expect={"upstream": Truthy(), "dumps(upstream.0.body)": Lacks("previous_response_id"),
                 "record.previous_response_id_seen": Truthy(),
                 # the id itself is client data
                 "dumps(record)": Lacks("resp_abc")}),
    dict(name="a request without the field records it as absent", send=dict(path="/v1/responses", json=HELLO),
         expect={"upstream": Truthy(), "record.previous_response_id_seen": Falsy()}),
    dict(name="the client headers are forwarded from an allow list only",
         send=dict(path="/v1/responses", json=HELLO,
                   headers={"OpenAI-Beta": "responses=v1", "originator": "codex_cli", "session_id": "sess-1",
                            "chatgpt-account-id": "acct-9", "X-Secret-Client-Header": "leak-me",
                            "Authorization": "Bearer client-supplied"}),
         expect={"upstream": Truthy(), "upstream.0.headers.openai-beta": "responses=v1",
                 "upstream.0.headers.originator": "codex_cli", "upstream.0.headers.session_id": "sess-1",
                 "upstream.0.headers.chatgpt-account-id": "acct-9",
                 "upstream.0.headers": Lacks("x-secret-client-header"),
                 # the upstream credential is the relay's, never the caller's
                 "upstream.0.headers.authorization": Ref("config.API_KEY", lambda key: f"Bearer {key}")}),
    # Codex's hyphenated headers are not on the allow list, and the pin is off
    dict(name="the relay sends no session id of its own unless it is asked to",
         send=dict(path="/v1/responses", json=HELLO, headers={"session-id": "caller-chosen", "thread-id": "caller-thread"}),
         expect={"upstream": Truthy(), "upstream.0.headers": Lacks("session-id", "thread-id"),
                 "record.knobs.session_pin": Falsy()}),
    dict(name="session_pin sends a constant for the client, not the callers", patch=PINNED,
         send=[dict(path="/v1/responses", json=HELLO, headers={"session-id": "caller-chosen"}),
               dict(path="/v1/responses", json={"model": "gpt-5.5", "input": "other"})],
         expect={"upstream": Truthy(),
                 "upstream.0.headers.session-id": Eq(Ref("records.0.client", core.session_pin_id)) & Ne("caller-chosen"),
                 "records.0.knobs.session_pin": Truthy(),
                 # a flag in the record, never the id
                 "dumps(records.0)": Lacks(Ref("records.0.client", core.session_pin_id)),
                 # and the same value for the next request of the same client
                 "upstream.1.headers.session-id": Ref("records.0.client", core.session_pin_id)}),
    # off: a `session_pin` in the body, a `session-id` and a `session_pin` header do nothing
    dict(name="a caller cannot ask for the pin", patch={"config.ALLOW_CLIENT_TUNING": True},
         send=dict(path="/v1/responses", json={**HELLO, "session_pin": True},
                   headers={"session-id": "caller-chosen", "session_pin": "1"}),
         expect={"upstream": Truthy(), "upstream.0.headers": Lacks("session-id"), "record.knobs.session_pin": Falsy()}),
    dict(name="a caller cannot turn the pin off", patch={"config.ALLOW_CLIENT_TUNING": True, **PINNED},
         send=dict(path="/v1/responses", json={**HELLO, "session_pin": False}),
         expect={"upstream": Truthy(), "upstream.0.headers.session-id": Truthy(), "record.knobs.session_pin": Truthy()}),
    dict(name="the session pin covers the non streaming path", patch=PINNED,
         send=dict(path="/v1/responses", json={**HELLO, "stream": False}),
         expect={"upstream": Truthy(), "upstream.0.headers.session-id": PIN_OF_THE_CLIENT}),
    dict(name="the header names are recorded but not their values",
         send=dict(path="/v1/responses", json=HELLO,
                   headers={"OpenAI-Beta": "responses=v1", "session_id": "sess-secret-value"}),
         expect={"upstream": Truthy(), "record.headers_forwarded": Has("openai-beta", "session_id"),
                 "dumps(record)": Lacks("sess-secret-value")}),
    # --- /v1/responses: the capture a real-client round trip needs, guarded ---
    dict(name="a deliberate capture writes the body owner only", patch=CAPTURING,
         send=dict(path="/v1/responses", json=HELLO),
         expect={"upstream": Truthy(), "captures": Len(1), "capture_paths.0": Mode(0o600),
                 "captured.0.input": "hello",
                 "stderr": Len(1), "stderr.0": Ref("capture_paths.0", lambda p: f"captured a client body to {p}")}),
    # the counter in the file name is per process: per connection, a second capture in the
    # same second would be named the same and refused by O_EXCL
    dict(name="two captures in the same second both land",
         patch={**CAPTURING, "handler.time.strftime": lambda *a: "20261006T000000"},
         send=[dict(path="/v1/responses", json={"model": "gpt-5.5", "input": "one"}),
               dict(path="/v1/responses", json={"model": "gpt-5.5", "input": "two"})],
         expect={"captures": Len(2), "stderr": Len(2),
                 "stderr.0": Ref("capture_paths.0", lambda p: f"captured a client body to {p}"),
                 "stderr.1": Ref("capture_paths.1", lambda p: f"captured a client body to {p}")}),
    dict(name="capture is off unless it is configured", send=dict(path="/v1/responses", json=HELLO),
         expect={"upstream": Truthy(), "captures": []}),
    # --- /v1/responses: the diagnostic trace, carrying exactly what the evidence must not ---
    dict(name="the trace is off unless it is named", send=dict(path="/v1/responses", json=HELLO),
         expect={"tracer_before": None, "upstream": Truthy(), "trace_files": []}),
    # The body the relay actually sent, injected instructions and all: the trace shows what
    # upstream was asked. The record names the trace and counts its failures (how a trace
    # that stopped writing is noticed), and none of its content reaches the record.
    dict(name="named, the trace records both directions and the body sent upstream", trace=True,
         send=dict(path="/v1/responses", json=HELLO),
         expect={"upstream": Truthy(), "trace_path": Mode(0o600), "trace_dirs": Has("request", "down", "note"),
                 "traced(request).0.body.input": Starts("hello") & Has(renamer.MARKER),
                 "record.trace": Ref("trace_path"), "record": Has("trace_records"), "dumps(record)": Lacks("hello")}),
    # --- /v1/responses: what a provider's answer becomes ---
    # A non-streaming caller is handed the same response object a streaming one finalizes:
    # the engine's view is the one source of what the client was shown.
    dict(name="the two paths describe the same output", reply=CUT,
         send=[dict(path="/v1/responses", json={**HELLO, "stream": False}),
               dict(path="/v1/responses", json={**HELLO, "stream": True})],
         expect={"resp.0.status": 200, "resp.0.json.synthetic": Truthy(), "resp.0.json.output.0.status": "completed",
                 "resp.0.json.output.0.content.0.text": "the answer ",
                 # the synthetic usage is an estimate, never a zero it did not measure
                 "resp.0.json.usage.input_tokens": Gt(0),
                 "resp.1.status": 200, "resp.1.completed.output": Ref("resp.0.json.output"),
                 "transcript(1)": Golden("cut")}),
    # The handler does not rebuild `output` from the deltas when the engine's terminal frame
    # lacks one: that is a second home for the same fact. The honest answer is a status.
    dict(name="a run with no terminal frame is a status, not an invented answer",
         patch={"handler.run_upstream": lambda cfg, wfile: {"cut": "marker", "real_completed": False, "error": None}},
         send=dict(path="/v1/responses", json={**HELLO, "stream": False}), expect={"status": 502}),
    dict(name="a provider 4xx keeps its status and message", reply=_error_reply(400, "Item id rs_1 not found"),
         send=dict(path="/v1/responses", json={"model": "gpt-5.5", "input": "hi", "stream": False}),
         expect={"status": 400, "message": Has("Item id rs_1 not found")}),
    dict(name="a credential rejection upstream is not passed on as the clients", reply=_error_reply(401, "bad upstream key"),
         send=dict(path="/v1/responses", json={"model": "gpt-5.5", "input": "hi", "stream": False}),
         expect={"status": 502}),
    dict(name="a failed response is not an answer", reply=FAILED,
         send=[dict(path="/v1/responses", json={"model": "gpt-5.5", "input": "hi", "stream": False}),
               dict(path="/v1/responses", json={"model": "gpt-5.5", "input": "hi"})],
         expect={"resp.0.status": 502, "resp.0.message": Has("response.failed"), "transcript(1)": Golden("failed")}),
    # cliproxy's `model(level)` names are model ids, not caller JSON
    dict(name="a thinking suffixed model reaches the upstream", reply=COMPLETED,
         send=dict(path="/v1/responses", json={"model": "gpt-5.1(high)", "input": "hi", "stream": False}),
         expect={"status": 200}),
    dict(name="a stream says it ends with the connection", send=dict(path="/v1/responses",
                                                                   json={"model": "gpt-5.5", "input": "hi"}),
         expect={"status": 200, "headers.connection": "close"}),
    dict(name="an incomplete response is an answer", reply=INCOMPLETE,
         send=dict(path="/v1/responses", json={"model": "gpt-5.5", "input": "hi", "stream": False}),
         expect={"status": 200, "json.status": "incomplete"}),
)

GETS = (
    # There is no browser surface and no `cors_origin` knob: no page may drive a process
    # that forwards the operator's upstream credential.
    *(dict(name=f"no cors header is ever sent on {path}", send=dict(path=path),
           expect={"headers": Lacks("access-control-allow-origin", "access-control-allow-headers",
                                    "access-control-allow-methods")})
      for path in ("/healthz", "/api/runs")),
    # `/` is the UI's and public; with no UI built it answers 404 to a caller with no token
    dict(name="the root path serves nothing without a ui", patch={"control.STATIC_DIR": "{scratch}/no-ui"},
         send=dict(path="/", token=None), expect={"status": 404}),
    dict(name="the runs need the token", send=dict(path="/api/runs", token=None), expect={"status": 401}),
    # The probe the supervisor uses must stay a liveness fact: no account, no meter value, no
    # configuration, and the two routing fields, which name no account, no meter value and no
    # address. A monitor appending `?ts=` must not get a 401 the supervisor reads as down.
    dict(name="healthz needs no token and carries no data", send=dict(path="/healthz", token=""),
         expect={"status": 200, "keys(json)": ["routing", "status", "uptime_s"],
                 "keys(json.routing)": ["reason", "relay_on"], "json.status": "ok", "dumps(json)": Lacks("acct")}),
    *(dict(name=f"healthz ignores {why}", send=dict(path=path, token=""), expect={"status": 200, "json.status": "ok"})
      for path, why in (("/healthz?ts=1", "a query string"), ("/healthz/", "a trailing slash"))),
    # the revision stays gated
    dict(name="version names the clients, who may tune, the revision and the config",
         send=dict(path="/version"),
         expect={"status": 200, "json.clients": [CLIENT], "json.operators": [CLIENT], "json.revision": Truthy(),
                 "json.config_fingerprint": Has("pacing", "max_concurrent", "retries", "seg_budget", "total_cap",
                                                "max_segments", "tail_chars", "allow_client_tuning")}),
    dict(name="version requires the token", send=dict(path="/version", token="nope"), expect={"status": In((401, 403))}),
    dict(name="a query string does not bypass the version auth", send=dict(path="/version?x=1", token=""),
         expect={"status": In((401, 403))}),
    # a failed catalogue fetch is an error status: a client could not tell 200 from an empty catalogue
    dict(name="models reports 502 when the upstream is unreachable", patch={"config.UPSTREAM": "http://127.0.0.1:1"},
         send=dict(path="/v1/models"), expect={"status": 502}),
    # a GET the relay refuses still has to end where a client can find it
    dict(name="an unknown path is a 404 that ends",
         send=dict(raw=f"GET /nope HTTP/1.1\r\nHost: x\r\nX-Relay-Token: {TOKEN}\r\n\r\n".encode(), read="close"),
         expect={"data": Starts(b"HTTP/1.1 404") & Has(b"Content-Length:")}),
    dict(name="a non ascii token is a 401, not a crash",
         send=dict(raw=_get_raw("/version", "été".encode("latin-1")), read="close"),
         expect={"data": Starts(b"HTTP/1.1 401")}),
    # a 200 legitimately keeps the connection; the request asks for it to close
    dict(name="a utf8 token still matches", patch={"config.TOKENS": ((CLIENT, "töken", "operator"),)},
         send=dict(raw=_get_raw("/version", "töken".encode("utf-8"), b"Connection: close\r\n"), read="close"),
         expect={"data": Starts(b"HTTP/1.1 200")}),
)

REQUEST = ("path", "json", "body", "token", "headers", "raw", "read")


class _Body:
    def __init__(self, data):
        self.data = data

    def text(self):
        return self.data.decode("utf-8", "replace")


def _response(status, headers, body, **raw):
    ctype = headers.get("content-type", "")
    try:
        parsed = json.loads(body) if body and not ctype.startswith("text/event-stream") else MISSING
    except ValueError:
        parsed = MISSING
    frames = ([json.loads(p) for _, p in iter_sse_frames(body.decode().splitlines(keepends=True)) if p]
              if ctype.startswith("text/event-stream") else [])
    completed = [f["response"] for f in frames if f.get("type") == "response.completed"]
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return {"status": status, "headers": headers, "body": body, "json": parsed,
            "message": error.get("message", MISSING) if isinstance(error, dict) else MISSING,
            "frames": frames, "completed": completed[0] if completed else MISSING, **raw}


class HttpTests(_Rows, unittest.TestCase):
    def test_what_a_post_is_answered_and_what_reaches_the_upstream(self):
        for row in POSTS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "send", "patch", "reply", "trace", "expect"), self.served, self.base())

    def test_what_a_get_is_answered(self):
        for row in GETS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "send", "patch", "expect"), self.served, self.base())

    def base(self):
        relay = _Relay.get()
        return {"config.TOKENS": ((CLIENT, TOKEN, "operator"),), "config.UPSTREAM": relay.upstream,
                "core.RUNS_LOG": "{scratch}/runs.jsonl", "config.PACING": 0, "config.RETRIES": 1,
                "config.ALLOW_CLIENT_TUNING": False, "config.CAPTURE_DIR": "", "config.DEBUG_LOG": "",
                "handler.BOUND_LISTEN": (f"127.0.0.1:{relay.port}",)}

    def served(self, row, scratch, stack):
        relay = _Relay.get()
        for sub in ("captures", "trace", "no-ui"):
            os.mkdir(os.path.join(scratch, sub))
        _file(scratch, "", "runs.jsonl")
        relay.reply, relay.seen[:] = row.get("reply", EMPTY), []
        stack.callback(trace.disable)
        tracer_before = trace.get()
        trace_path = os.path.join(scratch, "trace", "debug.jsonl")
        if row.get("trace"):
            trace.enable(trace_path)
        before = core.run_counts()
        sends = row["send"] if isinstance(row["send"], list) else [row["send"]]
        resp = []
        for spec in sends:
            self.unread(spec, REQUEST)
            resp.append(self.request(relay.port, spec))
        trace.disable()
        seen, after = list(relay.seen), core.run_counts()

        def records():
            with open(core.RUNS_LOG, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]

        def traced():
            with open(trace_path, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]

        def captured():
            out = []
            for path in capture_paths():
                with open(path, encoding="utf-8") as f:
                    out.append(json.load(f))
            return out

        capture_paths = lambda: [os.path.join(scratch, "captures", n)                      # noqa: E731
                                 for n in sorted(os.listdir(os.path.join(scratch, "captures")))]
        views = {"resp": lambda _: resp, "upstream": lambda _: seen,
                 "records": lambda _: records(), "record": lambda _: (records() or [None])[-1],
                 "counted": lambda _: {k: v - before.get(k, 0) for k, v in after.items()},
                 "captures": lambda _: sorted(os.listdir(os.path.join(scratch, "captures"))),
                 "capture_paths": lambda _: capture_paths(), "captured": lambda _: captured(),
                 "tracer_before": lambda _: tracer_before, "trace_path": lambda _: trace_path,
                 "trace_files": lambda _: os.listdir(os.path.dirname(trace_path)),
                 "trace": lambda _: traced(), "trace_dirs": lambda _: {r["dir"] for r in traced()},
                 "traced": lambda which: [r for r in traced() if r["dir"] == which],
                 "transcript": lambda n: transcript(_Body(resp[int(n)]["body"]))}
        for name in ("status", "headers", "body", "json", "message", "frames", "completed", "data", "line", "head"):
            views[name] = lambda _, name=name: resp[-1].get(name, MISSING)
        return views

    def request(self, port, spec):
        if "raw" in spec:
            raw = spec["raw"]() if callable(spec["raw"]) else spec["raw"]
            return self.raw(port, raw, spec["read"])
        data = json.dumps(spec["json"]).encode() if "json" in spec else spec.get("body")
        req = urllib.request.Request(f"http://127.0.0.1:{port}{spec['path']}", data=data,
                                     method="POST" if data is not None else "GET")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if spec.get("token", TOKEN) is not None:
            req.add_header("X-Relay-Token", spec.get("token", TOKEN))
        for k, v in spec.get("headers", {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return _response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            with e:
                return _response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())

    def raw(self, port, data, read):
        if read == "recv":
            got = raw_request(port, data)
        else:
            s = socket.create_connection(("127.0.0.1", port), timeout=5 if read == "framed" else 3)
            with s:
                s.sendall(data)
                got = self.read_framed(s) if read == "framed" else self.read_until_close(s)
        head, _, body = got.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        headers = dict(line.decode("latin-1").lower().split(": ", 1) for line in lines[1:] if b": " in line)
        return _response(int(lines[0].split()[1]), headers, body, data=got, line=lines[0], head=head.lower())

    @staticmethod
    def read_framed(s):
        data = b""
        while b"\r\n\r\n" not in data:
            data += s.recv(65536)
        head, _, rest = data.partition(b"\r\n\r\n")
        length = int(re.search(rb"Content-Length: (\d+)", head).group(1))
        while len(rest) < length:
            rest += s.recv(65536)
        return head + b"\r\n\r\n" + rest[:length]

    def read_until_close(self, s):
        data = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                self.fail("the relay held the connection open after its answer: " + repr(data[:200]))
            if not chunk:
                return data
            data += chunk


# --- relay-up.sh and restart-relay.sh, run against stubs ----------------------------

def _terminate(pid, timeout=10):
    """Stop a relay this test started, and wait for the port to be free."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(int(timeout / 0.1)):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# The cron supervisor does nothing when the relay is healthy and says so when it is not.
# The restart path belongs to cron, which the Human owns: this covers the probe it decides
# on and two start-path properties. A row runs `relay-up.sh` with `args`, where `{config}`
# is a file listening on the `port` named: "relay" (this module's, healthy), "dead" (bound
# and released), "bound" (bound, never answering), "other" (a relay on 127.0.0.2 only) or
# "free" (the script starts a relay there). `env` adds to the environment.
RELAY_UP = (
    dict(name="check reports healthy when the relay answers healthz", args=["--check", "--config", "{config}"],
         port="relay", expect={"rc": 0, "stdout": Has("healthy")}),
    dict(name="check reports not answering for a dead port", args=["--check", "--config", "{config}"], port="dead",
         expect={"rc": 1, "stdout": Has("NOT answering")}),
    # The Tailscale-only posture: probed at 127.0.0.1 it reads as nothing bound, and the
    # watchdog would start a second relay.
    dict(name="check probes the configured host, not only loopback", args=["--check", "--config", "{config}"],
         port="other", expect={"rc": 0, "stdout": Has("healthy")}),
    dict(name="check without a config fails closed", args=["--check", "--config", "/nonexistent/x.yaml"],
         expect={"rc": 2}),
    # A watchdog that started on "not healthy" would spawn a second relay into a failing bind
    # every five minutes with nothing said; starting on "not bound" alone would never repair a
    # wedged process. The branch that separates the two: warn, do not start.
    dict(name="a bound but unhealthy port is warned about and not doubled", args=["--config", "{config}"],
         port="bound", env={"RELAY_LOG_DIR": "{scratch}", "RELAY_LOCK": "{scratch}/lock"},
         expect={"rc": 0, "out": Has("WARNING", "Not starting a second relay"), "files": Lacks("relay.log")}),
    # Without `9>&-` the child holds the flock for its whole life, so a later invocation that
    # needs to start can never take it: free once the script returns, though the relay runs.
    dict(name="the started relay does not inherit the lock descriptor", args=["--config", "{config}"], port="free",
         env={"RELAY_LOG_DIR": "{scratch}", "RELAY_LOCK": "{scratch}/lock", "RELAY_RUNS_LOG": "{scratch}/runs.jsonl"},
         expect={"rc": 0, "stdout": Has("relay is answering"), "pid": NotNone(), "lock_free": 0}),
)

# The restart script outwaits the drain, or a deploy resolves the very cut the drain exists
# to prevent. A row runs `restart-relay.sh --dry-run` on `file`, with `env` added.
RESTARTS = (
    dict(name="the drain window is read from the config", file="listen: '127.0.0.1:0'\n" + T + "drain_seconds: 42\n",
         expect={"rc": 0, "stdout": Has("drain:  42s")}),
    dict(name="the environment overrides the file as it does for the relay",
         file="listen: '127.0.0.1:0'\n" + T + "drain_seconds: 42\n", env={"RELAY_DRAIN_SECONDS": "7"},
         expect={"rc": 0, "stdout": Has("drain:  7s")}),
    dict(name="a drain the relay would refuse is refused here too",
         file="listen: '127.0.0.1:0'\n" + T + "drain_seconds: 42\n", env={"RELAY_DRAIN_SECONDS": "abc"},
         expect={"rc": Ne(0), "stderr_of_script": Has("RELAY_DRAIN_SECONDS")}),
    dict(name="the drain defaults to the window the code uses", file="listen: '127.0.0.1:0'\n" + T,
         expect={"stdout": Has(Ref("config.DRAIN_SECONDS", lambda n: f"drain:  {n}s"))}),
)


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "the supervisor scripts need bash 4 (mapfile) and flock(1): Linux only")
class ScriptTests(_Rows, unittest.TestCase):
    def test_what_relay_up_does(self):
        for row in RELAY_UP:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "args", "port", "env", "expect"), self.relay_up)

    def relay_up(self, row, scratch, stack):
        port = self.port(row.get("port"), stack)
        cfg = _file(scratch, f"listen: '{port}'\n" + T) if port else None
        out = subprocess.run([os.path.join(HERE, "relay-up.sh")] + [cfg if a == "{config}" else a for a in row["args"]],
                             capture_output=True, text=True, timeout=90,
                             env=dict(os.environ, **fill(row.get("env", {}), scratch)))
        pids = [int(m.group(1)) for m in re.finditer(r"started pid (\d+)", out.stdout)]
        for pid in pids:
            stack.callback(_terminate, pid)

        def lock_free():
            return subprocess.run(["flock", "-n", os.path.join(scratch, "lock"), "-c", "true"],
                                  capture_output=True, text=True, timeout=30).returncode

        return self.script_views(out, rc=lambda _: out.returncode, pid=lambda _: pids[-1] if pids else None,
                                 files=lambda _: os.listdir(scratch), lock_free=lambda _: lock_free())

    def port(self, kind, stack):
        """The address the config names, made into what `kind` says it is."""
        if kind is None:
            return None
        if kind == "relay":
            return f"127.0.0.1:{_Relay.get().port}"
        if kind == "other":
            srv = start_listeners(("127.0.0.2:0",), strict=False)[0][1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            stack.callback(srv.server_close)
            stack.callback(srv.shutdown)
            return f"127.0.0.2:{srv.server_address[1]}"
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        if kind == "bound":
            s.listen(5)              # bound, answers nothing: curl will time out
            stack.callback(s.close)
        else:                        # an ephemeral port that was bound and released is reliably free
            s.close()
        return f"127.0.0.1:{port}"

    def test_what_restart_relay_does(self):
        for row in RESTARTS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "file", "env", "expect"), self.restart)

    def restart(self, row, scratch, stack):
        out = subprocess.run([os.path.join(HERE, "restart-relay.sh"), "--config", _file(scratch, row["file"]),
                              "--dry-run"], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, **row.get("env", {})))
        return self.script_views(out, rc=lambda _: out.returncode)

    @staticmethod
    def script_views(out, **more):
        return {"stdout": lambda _: out.stdout, "stderr_of_script": lambda _: out.stderr,
                "out": lambda _: out.stderr + out.stdout, **more}


# --- billing_probe: the meter snapshot, the plugin read, the totals, the report -----

# The oracle writes to a committed file, so it must not write account identities into it:
# an A/B run closed with a leaked email publishes credentials-adjacent data in git history.
# `snap` is a meter read; `labels` is `label_map` of it (of `union` when the row names one),
# `label(<email>)` one of them, `out` the snapshot relabelled, `missing` its accounts
# without a reading.
SNAPSHOTS = (
    # sorted, so the labels are stable across reads of the same pool
    dict(name="labels never carry an at sign and are stable",
         snap={"zoe@example.com": {"live": {}}, "abe@example.com": {"live": {}}},
         expect={"label_values": ["acct-1", "acct-2"], "label(abe@example.com)": "acct-1",
                 "label(zoe@example.com)": "acct-2"}),
    dict(name="relabelling strips every key and keeps the numbers", snap={"a@example.com": {"live": {"primary_percent": 4}}},
         expect={"keys(out)": ["acct-1"], "dumps(out)": Lacks("@"), "out.acct-1.live.primary_percent": 4,
                 "out.acct-1.label": "acct-1"}),
    # built from the first read alone, an account that appeared only in the second would fall
    # through to its raw email and be written into the committed evidence
    dict(name="the label map covers an account that only appears after",
         snap={"a@example.com": {"live": {}}, "new@example.com": {"live": {}}},
         union={"a@example.com": {"live": {}}, "new@example.com": {"live": {}}},
         expect={"dumps(out)": Lacks("@")}),
    dict(name="missing windows names every account without a reading",
         snap={"a@example.com": {"live": {"primary_percent": 3}}, "b@example.com": {"live": {"primary_percent": None}},
               "c@example.com": {"live": {}}},
         expect={"missing": ["b@example.com", "c@example.com"]}),
)


def _usage(events, collect_err=None):
    return {"events": events, "buckets": [], "collect_err": collect_err, "collected_at": "2026-10-04T02:00:00Z",
            "skipped": 0, "version": "0.2.1", "configured": True, "timezone": "CST"}


# `billing_probe.py tokens` reads the cpa-usage plugin's management API. An oracle that cannot
# collect raises rather than print zeros: the plugin answers 200 with an empty event list and a
# `collect_err`, which reads exactly like an idle pool.
PLUGIN_READS = (
    dict(name="a plugin that cannot collect raises instead of reporting zero",
         answer=_usage([], "sqlite migrate: unable to open database file (14)"),
         expect={"raised": IsA(billing_probe.ProbeError), "message": Has("not collecting", "sqlite migrate")}),
    # `error` is the management API's refusal; its body is read into the message and the
    # error is closed, or its socket stays open until garbage collection
    dict(name="an error answer from the management API is read and closed",
         error=dict(code=503, body=b"plugin down"),
         expect={"raised": IsA(billing_probe.ProbeError), "message": Has("HTTP 503", "plugin down"),
                 "error_closed": True}),
)

# The report carries uncached input, the axis the billing arms turn on
# (billing_arms_2026-10-04.json). `events` -> what `token_totals` makes of them.
TOTALS = (
    dict(name="uncached input is input minus cache reads",
         events=[{"key": "key-a", "model": "gpt-6-astra", "input": 1000, "cache": 900, "output": 5, "reasoning": 1,
                  "failed": False},
                 {"key": "key-b", "model": "gpt-6-luna", "input": 300, "cache": 0, "output": 7, "reasoning": 0,
                  "failed": True}],
         expect={"out.total.uncached": 400, "out.total.requests": 2, "out.total.failed": 1,
                 "out.by_model.gpt-6-luna.output": 7, "out.by_key.key-a.uncached": 100}),
    # the API flattens the tokens into the event; the plugin's own SQLite payload nests them
    dict(name="the nested sqlite shape is read too",
         events=[{"model": "m", "tokens": {"input_tokens": 50, "cached_tokens": 20, "output_tokens": 3,
                                           "reasoning_tokens": 0}}],
         expect={"out.total.input": 50, "out.total.cache": 20, "out.total.uncached": 30}),
)

# `cmd_tokens` run on `args` twice, printed (`text`) and as json (`as_json`, `parsed` too),
# each with the plugin answering `answer`: `rc`, `out` and the `paths` asked. `posted` is every
# call of `mgmt_post`, the quota-spending one: a read of a local database never makes it.
REPORTS = (
    dict(name="the json report reads the plugin and never touches the upstream",
         args=dict(start="2026-10-04", end="2026-10-04", bucket="hour"),
         answer=_usage([{"key": "key-a", "model": "m", "input": 10, "cache": 0, "output": 1, "reasoning": 0,
                         "failed": False}]),
         expect={"as_json.rc": 0, "as_json.paths.0": Has("/plugins/usage-report/api/usage", "bucket=hour",
                                                         "start=2026-10-04"),
                 "as_json.out": Has('"uncached": 10'), "posted": []}),
    dict(name="the printed report names uncached input, and the json carries the same totals",
         args=dict(start=None, end=None, bucket="hour"),
         answer=_usage([{"key": "key-a", "model": "m", "input": 100, "cache": 60, "output": 2, "reasoning": 0,
                         "failed": False}]),
         expect={"text.out": Golden("tokens") & Has("uncached", "plugin default", "not the meter"),
                 "as_json.parsed.total": {"requests": 1, "input": 100, "cache": 60, "uncached": 40, "output": 2,
                                          "reasoning": 0, "failed": 0},
                 "posted": []}),
)


class BillingTests(_Rows, unittest.TestCase):
    def test_what_is_made_of_a_meter_snapshot(self):
        for row in SNAPSHOTS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "snap", "union", "expect"), self.snapshot)

    def snapshot(self, row, scratch, stack):
        snap = copy.deepcopy(row["snap"])
        labels = billing_probe.label_map(copy.deepcopy(row.get("union", snap)))
        return {"labels": lambda _: labels, "label": labels.get, "label_values": lambda _: sorted(labels.values()),
                "out": lambda _: billing_probe.relabel(snap, labels),
                "missing": lambda _: billing_probe.missing_windows(snap)}

    def test_the_plugin_read(self):
        for row in PLUGIN_READS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "answer", "error", "expect"), self.plugin_read)

    def plugin_read(self, row, scratch, stack):
        sent = []                  # held, so only an explicit close shows: a dropped error closes itself
        if "error" in row:
            def refuse(req, timeout=None):
                sent.append(urllib.error.HTTPError(req.full_url, row["error"]["code"], "refused", {},
                                                   io.BytesIO(row["error"]["body"])))
                raise sent[0]
            stack.enter_context(mock.patch.object(billing_probe, "MGMT_SECRET", "secret"))
            stack.enter_context(mock.patch.object(urllib.request, "urlopen", refuse))
        else:
            stack.enter_context(mock.patch.object(billing_probe, "mgmt_get",
                                                  lambda path: copy.deepcopy(row["answer"])))
        out, raised = self.outcome(billing_probe.plugin_usage, row)
        return {"out": lambda _: out, "error_closed": lambda _: sent[0].fp.closed, **self.raised_views(raised)}

    def test_the_token_totals(self):
        for row in TOTALS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "events", "expect"), lambda row, scratch, stack: {
                    "out": lambda _: billing_probe.token_totals(copy.deepcopy(row["events"]))})

    def test_the_tokens_report(self):
        for row in REPORTS:
            with self.subTest(row=row["name"]):
                self.run_row(row, ("name", "args", "answer", "expect"), self.report)

    def report(self, row, scratch, stack):
        paths, posted = [], []
        stack.enter_context(mock.patch.object(
            billing_probe, "mgmt_get", lambda path: paths.append(path) or copy.deepcopy(row["answer"])))
        stack.enter_context(mock.patch.object(billing_probe, "mgmt_post", lambda *a, **k: posted.append(a)))
        runs = {}
        for name, as_json in (("text", False), ("as_json", True)):
            del paths[:]
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = billing_probe.cmd_tokens(argparse.Namespace(json=as_json, **row["args"]))
            runs[name] = {"rc": rc, "out": out.getvalue(), "paths": list(paths)}
        runs["as_json"]["parsed"] = json.loads(runs["as_json"]["out"])
        return {"text": lambda _: runs["text"], "as_json": lambda _: runs["as_json"], "posted": lambda _: posted}


if __name__ == "__main__":
    unittest.main(verbosity=2)
