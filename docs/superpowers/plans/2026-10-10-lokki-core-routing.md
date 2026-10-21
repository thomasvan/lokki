# lokki core-routing (Low Quota Mode) Implementation Plan

> **For agentic workers:** execute this plan task by task, in order, with the `superpowers:executing-plans` skill when the seat has the superpowers plugin, otherwise as written. Steps use checkbox (`- [ ]`) syntax for tracking. Every task ends with the suite green.

**Goal:** The core decides, per request, whether the relay cuts or the request goes through to CLIProxyAPI untouched, from the accounts' remaining 5h quota, and shows the accounts and the decision on its control API.

**Architecture:** Four new pieces under `core/lokki_core/`: `quota/reader.py` (one snapshot of every codex account's meter, through CLIProxyAPI's management API), `quota/policy.py` (one pure function: snapshot + settings + clock → decision), `quota/poller.py` (a daemon thread that keeps the latest snapshot and wakes on reload or refresh), `passthrough.py` (a verbatim forward to CLIProxyAPI with the relay's credential, retry only before the first byte, a teed scan for the terminal frame, one run record). The handler reads the decision once per request and takes one of the two paths. `control.py` gains `/api/accounts`, `/healthz` gains `routing`, `/api/status` fills `decision` and `poll`.

**Tech Stack:** Python 3.12+ standard library plus `pyyaml`; `unittest`; the fake management API is an in-process `ThreadingHTTPServer`, as the suite's stub upstream is.

**Spec:** `docs/superpowers/specs/2026-10-10-lokki-app-design.md`, section 4 (and 3.3 for the keys, 3.6 for `route`). The
plan argues from the spec; read both. Sub-project 1 (its plan,
`docs/superpowers/plans/2026-10-10-lokki-core-platform.md`, removed in
`21f14bc9d33c4966831f6fdac6348ac831008518`) is merged through its PR E (#8): `config.LOW_QUOTA`, `config.CLIPROXY`,
`config.ROUTING`, `settings_api.LOCK`, `handler.BOUND_LISTEN`, `core.run_counts()`, `control.status(decision, poll)`,
`test_control._ControlHarness`, `engine.open_upstream`, `engine.upstream_slot`, `engine.pace`, `engine.set_retry_hook`,
`engine.FORWARDED_HEADERS` exist as this plan uses them. Branch from `main` after #8 is on it.

**Where the plan departs from the spec, on purpose (four places; the reader judged each):**

1. Spec 4.2 names the reason for `routing: relay|direct` as `"routing"`; the plan says which (`"routing: relay"`, `"routing: direct"`) because the UI shows the string and a bare `"routing"` does not tell the operator which way it was forced.
2. Spec 4.1 says the poller reads "an immutable settings object swapped in one assignment". The core's settings are three globals (`ROUTING`, `LOW_QUOTA`, `CLIPROXY`), each swapped in one assignment inside `apply_settings`; the poller reads each once per iteration through `poller.current_settings()` (one place), and the apply hook (`config.ON_APPLY`) runs after all three are assigned, so the read the hook wakes always sees one consistent set. A read that lands between two of the assignments uses a mixed set for one iteration at most, and the hook's wake corrects it. Introducing a fourth object only for the poller would give the API, the relay path and the poller two sources for the same keys.
3. Spec 4.3 says a drain cut is recorded. A drain that expires ends the process with `os._exit` (`handler._drain_and_exit`), which runs no `finally` on either path; the relay path does not record that cut today either. The pass-through `finally` records a client disconnect and every exception; what a drain changes is in the test `test_the_guards_that_run_on_both_paths` (503 while stopping, before any upstream call). The spec sentence (4.3) and the test list's "on drain" (4.6) are corrected in Task 5.
4. Spec 4.3 says a provider 4xx passes through "with CLIProxyAPI's status". The relay's non-streaming path maps 401, 403 and 407 to 502 because those statuses are about the relay's own upstream credential, and passing them on would tell the client its own token is bad; pass-through does the same (Task 4). Task 5 adds the half-sentence to 4.3.

Two existing tests pin what these tasks change and are edited, by name, in the task that changes it: `test_offline.HealthAndVersionTests.test_healthz_needs_no_token_and_carries_no_data` pins the exact key set of `/healthz` (Task 4 adds `routing`); `test_control.ControlRouteTests.test_status_names_the_route_counts_the_revision_and_whether_cliproxy_answers` pins `decision` and `poll` as `None` (Task 5 fills them). Their intent (no data on `/healthz`; the status shape) is kept.

## Global Constraints

- The relay path is unchanged: a request the decision sends to the relay runs exactly the engine it runs today. Every existing test keeps its intent (the two named above gain a field).
- After every task: `cd core && python3 -m unittest discover -s . -p "test_*.py"` green and `python3 -c "import server"` clean (the protocol's typecheck).
- The decision is global (CLIProxyAPI chooses the account), computed per request with the clock, never cached as a decision (spec 4.2).
- Unknown counts as not low; fail-open is logged once per change and shown in `/healthz` (spec 4.2).
- The reader spends no quota and sends what the measurement probe sends today (spec 4.1); the usage URL and headers have one home, `quota/reader.py`, and `billing_probe.py` imports them.
- Pass-through forwards the client's raw bytes unless a knob in `RELAY_KNOB_PARAMS` is present; `Authorization` is always the core's key; the header allow-list is `engine.FORWARDED_HEADERS`; hop-by-hop headers and `Accept-Encoding` are not forwarded; the response header allow-list is `Content-Type`, `Retry-After`, `x-request-id` (spec 4.3).
- The record of a pass-through carries `route: "passthrough"` and `reason`, counts, no content, and is written in a `finally` (spec 4.3, local law).
- Emails are masked at the reader (`a***@domain`); nothing downstream logs one (spec 4.1).
- No network call to the provider or to CLIProxyAPI in the suite. One probe against the running clone of CLIProxyAPI, by the Lead, is Task 4 Step 1 and is reported in the PR body, not tested.
- Commit messages end with the attribution lines the session carries.

## Review Focus

1. An account whose `used_percent` is `0` must be **ok** (remaining 100), not unknown: `0` is a number (Task 2 `test_zero_used_is_ok`).
2. A snapshot read 2.9 × `poll_seconds` ago is current and 3.1 × is unknown, on the boundary the spec names (Task 2 `test_staleness_boundary`).
3. A client body with `"mode": "control"` must reach CLIProxyAPI without `mode` and with every other key byte-equal after re-serialization of the same JSON (Task 4 `test_a_knob_is_removed_and_the_rest_reserialized`).
4. A pass-through whose stream breaks after the first byte must not be retried: one upstream request, `cut: error`, no `last_status` (Task 4 `test_a_stream_broken_after_the_first_byte_is_not_retried`).
5. A reload that turns `low_quota.enabled` off while the poller sleeps must stop the reads and make the next decision "low quota mode off" without waiting a full interval (Task 3 `test_a_reload_wakes_the_poller`).

---

### Task 1: The quota reader against a fake management API

**Files:**
- Create: `core/lokki_core/quota/__init__.py`, `core/lokki_core/quota/reader.py`, `core/test_quota_reader.py`
- Modify: `core/billing_probe.py` (`WHAM_USAGE`, `WHAM_HEADERS` imported from the reader)
- Test: `core/test_quota_reader.py`

**Interfaces:**
- Produces: `reader.read(management_url, secret, accounts=(), per_call_timeout=10.0, deadline=None, cliproxy_version="") -> Snapshot` where `Snapshot` is a dict `{read_at: float, accounts: {auth_index: {label, email_masked, enabled, used_percent, reset_at, window_seconds, error}}}`, every codex account present (one not read carries `error: "disabled"` or `"not in scope"`, which is what `/api/accounts.in_scope` shows); raises `reader.ManagementError(message)` when the management API as a whole fails (connection, non-200 on the credentials route, 401, a version mismatch).
- Produces: `reader.USAGE_URL`, `reader.USAGE_HEADERS`, `reader.mask_email(s) -> str`, `reader.MANAGEMENT_MAJOR = "8"`.
- Produces for tests: `test_quota_reader.FakeManagement` (an HTTP server with `credentials: list`, `usage: dict[auth_index -> dict | Exception | float(delay)]`, `seen: list`).

- [ ] **Step 1: Failing tests** (`core/test_quota_reader.py`)

```python
#!/usr/bin/env python3
"""The quota reader: one snapshot of every codex account's meter through
CLIProxyAPI's management API, emails masked, every failure a field or one
exception, bounded in time."""
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core.quota import reader  # noqa: E402

SECRET = "mgmt-test-secret"


class FakeManagement:
    """CLIProxyAPI's management API, the two routes the reader uses.

    `credentials` is the list the credentials route answers; `usage` maps an
    auth_index to the dict the usage call answers with (the provider's body), or
    to an Exception to answer 500, or to a float to sleep that long first."""

    def __init__(self, credentials=None, usage=None, secret=SECRET, status_credentials=200):
        self.credentials = credentials if credentials is not None else []
        self.usage = usage or {}
        self.secret = secret
        self.status_credentials = status_credentials
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, obj):
                body = json.dumps(obj).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass                     # the reader gave up (a timeout test); nothing to tell

            def _auth(self):
                if self.headers.get("Authorization") != f"Bearer {outer.secret}":
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                outer.seen.append(("GET", self.path))
                if not self._auth():
                    return
                if self.path == "/v8/management/credentials":
                    self._send(outer.status_credentials, {"files": outer.credentials})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.seen.append(("POST", self.path, body))
                if not self._auth():
                    return
                if self.path != "/v8/management/requests/api-call":
                    self._send(404, {"error": "not found"})
                    return
                answer = outer.usage.get(body.get("authIndex"))
                if isinstance(answer, float):
                    time.sleep(answer)
                    answer = {"rate_limit": {"primary_window": {"used_percent": 1, "reset_at": 0}}}
                if isinstance(answer, Exception):
                    self._send(500, {"error": str(answer)})
                    return
                if answer is None:
                    self._send(200, {"status_code": 404, "body": "{}"})
                    return
                self._send(200, {"status_code": 200, "body": json.dumps(answer)})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def usage(used, reset_at=4102444800, window=18000):
    return {"plan_type": "plus", "rate_limit": {"primary_window": {
        "used_percent": used, "reset_at": reset_at, "limit_window_seconds": window}}}


CREDS = [
    {"provider": "codex", "auth_index": "a1", "email": "alice@example.com", "disabled": False},
    {"provider": "codex", "auth_index": "b2", "email": "bob@example.com", "disabled": True},
    {"provider": "gemini", "auth_index": "g3", "email": "gem@example.com", "disabled": False},
]


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeManagement(credentials=list(CREDS), usage={"a1": usage(40), "b2": usage(90)})
        self.addCleanup(self.fake.close)

    def read(self, **kw):
        return reader.read(self.fake.url, SECRET, **kw)

    def test_a_snapshot_names_every_codex_account_masked_with_its_meter(self):
        snap = self.read()
        self.assertEqual(set(snap["accounts"]), {"a1", "b2"})          # gemini is not a codex account
        a = snap["accounts"]["a1"]
        self.assertEqual(a["email_masked"], "a***@example.com")
        self.assertNotIn("alice", json.dumps(snap))
        self.assertTrue(a["enabled"])
        self.assertEqual(a["used_percent"], 40)
        self.assertEqual(a["reset_at"], 4102444800)
        self.assertEqual(a["window_seconds"], 18000)
        self.assertIsNone(a["error"])
        self.assertFalse(snap["accounts"]["b2"]["enabled"])
        self.assertAlmostEqual(snap["read_at"], time.time(), delta=5)

    def test_only_the_accounts_in_scope_are_asked_when_a_scope_is_given(self):
        snap = self.read(accounts=("b2",))
        self.assertEqual(set(snap["accounts"]), {"a1", "b2"})          # every codex account is listed
        self.assertEqual(snap["accounts"]["a1"]["error"], "not in scope")
        self.assertEqual(snap["accounts"]["b2"]["used_percent"], 90)   # disabled, but named: read
        posts = [s for s in self.fake.seen if s[0] == "POST"]
        self.assertEqual([p[2]["authIndex"] for p in posts], ["b2"])

    def test_a_disabled_account_is_not_asked_when_no_scope_is_given(self):
        snap = self.read()
        posts = [s[2]["authIndex"] for s in self.fake.seen if s[0] == "POST"]
        self.assertEqual(posts, ["a1"])
        self.assertEqual(snap["accounts"]["b2"]["error"], "disabled")

    def test_the_usage_call_carries_the_probe_s_headers_and_url(self):
        self.read()
        post = [s for s in self.fake.seen if s[0] == "POST"][0][2]
        self.assertEqual(post["url"], reader.USAGE_URL)
        self.assertEqual(post["method"], "GET")
        self.assertEqual(post["header"], dict(reader.USAGE_HEADERS))
        self.assertIn("$TOKEN$", post["header"]["Authorization"])

    def test_a_per_account_failure_is_a_field_not_an_exception(self):
        self.fake.usage["a1"] = RuntimeError("boom")
        a = self.read()["accounts"]["a1"]
        self.assertIsNone(a["used_percent"])
        self.assertIn("HTTP 500", a["error"])

    def test_a_missing_rate_limit_block_is_an_error_never_zero(self):
        self.fake.usage["a1"] = {"plan_type": "free"}
        a = self.read()["accounts"]["a1"]
        self.assertIsNone(a["used_percent"])
        self.assertEqual(a["error"], "no rate_limit block")

    def test_a_non_200_from_the_provider_is_an_error(self):
        self.fake.usage["a1"] = None            # the fake answers status_code 404
        self.assertEqual(self.read()["accounts"]["a1"]["error"], "HTTP 404")

    def test_a_wrong_secret_is_a_management_error(self):
        with self.assertRaises(reader.ManagementError) as caught:
            reader.read(self.fake.url, "wrong")
        self.assertTrue(str(caught.exception).startswith("HTTP 401"))

    def test_a_per_account_transport_failure_is_a_field(self):
        from unittest import mock
        import http.client
        # IncompleteRead is an HTTPException and not an OSError: the one class the
        # tuple adds (RemoteDisconnected would pass as an OSError).
        with mock.patch.object(reader, "_request", side_effect=http.client.IncompleteRead(b"")):
            with self.assertRaises(reader.ManagementError):     # the credentials call: the whole read
                self.read()
        real = reader._request

        def flaky(url, secret, data=None, timeout=10.0):
            if data and b'"a1"' in data:
                raise http.client.IncompleteRead(b"")
            return real(url, secret, data, timeout)
        with mock.patch.object(reader, "_request", side_effect=flaky):
            snap = self.read()
        self.assertEqual(snap["accounts"]["a1"]["error"], "IncompleteRead")
        self.assertEqual(snap["accounts"]["b2"]["error"], "disabled")

    def test_an_unreachable_management_api_is_a_management_error(self):
        with self.assertRaises(reader.ManagementError):
            reader.read("http://127.0.0.1:9", SECRET, per_call_timeout=1.0)

    def test_a_404_on_the_management_route_names_the_version(self):
        self.fake.status_credentials = 404
        with self.assertRaises(reader.ManagementError) as caught:
            self.read()
        self.assertIn("management route not found: CLIProxyAPI version?", str(caught.exception))

    def test_a_version_of_another_major_is_refused_before_any_call(self):
        with self.assertRaises(reader.ManagementError) as caught:
            self.read(cliproxy_version="9.1.0")
        self.assertIn("major 9", str(caught.exception))
        self.assertEqual(self.fake.seen, [])
        self.read(cliproxy_version="8.0.23")          # the known major passes
        self.read(cliproxy_version="v8.0.23")         # a release tag's leading v is not a major
        self.read(cliproxy_version="")                 # unknown: the check is skipped

    def test_a_slow_account_hits_the_per_call_timeout(self):
        self.fake.usage["a1"] = 3.0
        t0 = time.monotonic()
        a = self.read(per_call_timeout=0.5)["accounts"]["a1"]
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual(a["error"], "timeout")

    def test_accounts_are_read_in_parallel_and_the_deadline_marks_the_rest(self):
        creds = [dict(c, auth_index=f"x{i}", provider="codex", disabled=False) for i, c in enumerate(CREDS * 3)]
        self.fake.credentials = creds
        for c in creds:
            self.fake.usage[c["auth_index"]] = 0.6
        t0 = time.monotonic()
        snap = self.read(per_call_timeout=5.0, deadline=1.5)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.5, "nine 0.6 s reads took longer than four in parallel allow")
        states = [a["error"] for a in snap["accounts"].values()]
        self.assertIn(None, states)                    # some were read
        self.assertIn("deadline", states)              # some were not, and say so

    def test_mask_email(self):
        self.assertEqual(reader.mask_email("alice@example.com"), "a***@example.com")
        self.assertEqual(reader.mask_email("a@b"), "a***@b")
        self.assertEqual(reader.mask_email("noat"), "n***")
        self.assertEqual(reader.mask_email(""), "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see them fail**

Run: `cd core && python3 -m unittest test_quota_reader 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'lokki_core.quota'`.

- [ ] **Step 3: Implement `quota/reader.py`** (and an empty `quota/__init__.py`)

```python
"""One snapshot of every codex account's meter, through CLIProxyAPI's
management API, which spends no quota.

Two calls: the credentials list, then one usage call per account in scope, in
parallel and bounded. Emails are masked here so nothing downstream can log one.
A per-account failure is a field; a failure of the management API as a whole is
one `ManagementError`."""
import concurrent.futures
import http.client
import json
import time
import urllib.error
import urllib.request

# What the measurement probe sends (billing_probe imports these): `$TOKEN$` is
# replaced by CLIProxyAPI with the account's access token, so no credential
# passes through this process. The provider varies its answer by client; this is
# the client its panel presents. An unofficial endpoint read once a minute per
# account: docs/app/limits.md names the risk.
_TRANSPORT_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USAGE_HEADERS = {
    "Authorization": "Bearer $TOKEN$",
    "Content-Type": "application/json",
    "User-Agent": "codex-tui/0.149.1 (Mac OS 15.0; arm64)",
}
MANAGEMENT_MAJOR = "8"          # the `/v8/` route prefix this module was written for
_CREDENTIALS = "/v8/management/credentials"
_API_CALL = "/v8/management/requests/api-call"
MAX_PARALLEL = 4


class ManagementError(Exception):
    """The management API as a whole did not answer as expected."""


def mask_email(s):
    s = s or ""
    if not s:
        return ""
    local, at, domain = s.partition("@")
    return f"{local[:1]}***{at}{domain}" if at else f"{local[:1]}***"


def _request(url, secret, data=None, timeout=10.0):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Authorization": f"Bearer {secret}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        with e:                                 # an HTTPError is a response: close it
            return e.code, e.read()


def _check_version(cliproxy_version):
    major = (cliproxy_version or "").strip().lstrip("vV").split(".")[0]
    if major and major != MANAGEMENT_MAJOR:
        raise ManagementError(f"CLIProxyAPI major {major} is not the major this reader knows "
                              f"({MANAGEMENT_MAJOR}); the management routes may have moved")


def _credentials(management_url, secret, timeout):
    try:
        status, body = _request(management_url + _CREDENTIALS, secret, timeout=timeout)
    except _TRANSPORT_ERRORS as e:
        raise ManagementError(f"management API unreachable: {type(e).__name__}") from None
    if status == 404:
        raise ManagementError("management route not found: CLIProxyAPI version?")
    if status != 200:
        raise ManagementError(f"HTTP {status} on the credentials route")
    try:
        files = json.loads(body).get("files", [])
    except (ValueError, AttributeError):
        raise ManagementError("the credentials route did not answer JSON") from None
    return [f for f in files if isinstance(f, dict) and f.get("provider") == "codex"]


def _usage(management_url, secret, auth_index, timeout):
    """The account's meter fields, or {"error": ...}."""
    payload = json.dumps({"authIndex": auth_index, "method": "GET", "url": USAGE_URL,
                          "header": dict(USAGE_HEADERS)}).encode()
    try:
        status, body = _request(management_url + _API_CALL, secret, data=payload, timeout=timeout)
    except TimeoutError:
        return {"error": "timeout"}
    except _TRANSPORT_ERRORS as e:
        if isinstance(getattr(e, "reason", None), TimeoutError) or "timed out" in str(e):
            return {"error": "timeout"}
        return {"error": type(e).__name__}
    if status != 200:
        return {"error": f"HTTP {status}"}
    try:
        answer = json.loads(body)
        inner = answer.get("body")
        inner = json.loads(inner) if isinstance(inner, str) else inner
    except (ValueError, AttributeError):
        return {"error": "not JSON"}
    if answer.get("status_code") != 200 or not isinstance(inner, dict):
        return {"error": f"HTTP {answer.get('status_code')}"}
    window = (inner.get("rate_limit") or {}).get("primary_window")
    if not isinstance(window, dict) or not isinstance(window.get("used_percent"), (int, float)) \
            or isinstance(window.get("used_percent"), bool):
        return {"error": "no rate_limit block"}
    return {"used_percent": window["used_percent"], "reset_at": window.get("reset_at"),
            "window_seconds": window.get("limit_window_seconds"), "error": None}


def read(management_url, secret, accounts=(), per_call_timeout=10.0, deadline=None,
         cliproxy_version=""):
    """A snapshot. `accounts` limits the scope to those auth_index values; empty
    means every enabled codex account. `deadline` bounds the whole read in
    seconds; accounts not read by then carry error "deadline"."""
    _check_version(cliproxy_version)
    management_url = management_url.rstrip("/")
    entries = _credentials(management_url, secret, per_call_timeout)
    out = {}
    for i, e in enumerate(entries, 1):
        idx = str(e.get("auth_index", ""))
        out[idx] = {"label": f"acct-{i}", "email_masked": mask_email(e.get("email", "")),
                    "enabled": not e.get("disabled", False), "used_percent": None,
                    "reset_at": None, "window_seconds": None, "error": None}
    wanted = [i for i in out if (i in accounts if accounts else out[i]["enabled"])]
    for i in out:
        if i not in wanted:
            out[i]["error"] = "not in scope" if accounts else "disabled"
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        futures = {pool.submit(_usage, management_url, secret, i, per_call_timeout): i for i in wanted}
        for fut, i in futures.items():
            remaining = None if deadline is None else max(0.0, deadline - (time.monotonic() - started))
            try:
                out[i].update(fut.result(timeout=remaining))
            except concurrent.futures.TimeoutError:
                out[i]["error"] = "deadline"
                fut.cancel()
    return {"read_at": time.time(), "accounts": out}
```

(`ThreadPoolExecutor.__exit__` waits for running futures; a read past the deadline still returns after its own `per_call_timeout`, which the deadline test tolerates at 2.5 s with 0.6 s reads. If the test's elapsed bound fails on a slow host, raise it to 4 s, not the design.)

`billing_probe.py`: replace the `WHAM_USAGE = …` and `WHAM_HEADERS = {…}` definitions with `from lokki_core.quota.reader import USAGE_URL as WHAM_USAGE, USAGE_HEADERS as WHAM_HEADERS` (keep the comment above them, shortened to "one home: the reader"). `billing_probe.py` already inserts `HERE` on `sys.path`? It is run from `core/`; add `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` before the import if the module lacks it.

- [ ] **Step 4: Run the tests and the suite, commit**

Run: `cd core && python3 -m unittest test_quota_reader -v 2>&1 | tail -18 && python3 -m unittest discover -s . -p "test_*.py" 2>&1 | tail -3`
Expected: all pass, `OK`.

```bash
git add -A && git commit -m "core: the quota reader, a masked snapshot of every codex account's meter through the management API"
```

---

### Task 2: The policy

**Files:**
- Create: `core/lokki_core/quota/policy.py`, `core/test_quota_policy.py`

**Interfaces:**
- Produces: `policy.Decision` (a frozen dataclass: `relay_on: bool`, `reason: str`, `per_account: dict[str, str]`), `policy.decide(snapshot, low_quota, routing, now) -> Decision` where `low_quota` is `config.LOW_QUOTA`'s dict and `routing` is `config.ROUTING`; `per_account` is filled from the snapshot whenever there is one, whatever `routing` and `enabled` say, so `/api/accounts` shows each account's state while the mode is off or the routing is forced; `policy.state_of(account, now, stale, threshold) -> "low" | "ok" | "unknown"`.
- Reasons (exact strings, the UI shows them): `"routing: relay"`, `"routing: direct"`, `"low quota mode off"`, `"no snapshot yet"`, `"every account in scope is low"`, `"an account in scope is low"`, `"no account in scope is low"`, `"not every account in scope is low"`, `"scope is empty"`, `"every account in scope is unknown"`.

- [ ] **Step 1: Failing tests** (`core/test_quota_policy.py`)

```python
#!/usr/bin/env python3
"""The policy is one pure function: snapshot + settings + clock -> decision."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core.quota import policy  # noqa: E402

NOW = 1_800_000_000.0
LQ = {"enabled": True, "threshold_percent": 30, "rule": "all", "accounts": [], "poll_seconds": 60}


def acct(used, enabled=True, error=None, reset_at=NOW + 3600, window=18000):
    return {"label": "x", "email_masked": "x***@y", "enabled": enabled, "used_percent": used,
            "reset_at": reset_at, "window_seconds": window, "error": error}


def snap(accounts, read_at=NOW - 10):
    return {"read_at": read_at, "accounts": accounts}


class PolicyTests(unittest.TestCase):
    def test_routing_relay_and_direct_ignore_the_quota_but_still_show_it(self):
        d = policy.decide(None, LQ, "relay", NOW)
        self.assertEqual((d.relay_on, d.reason, d.per_account), (True, "routing: relay", {}))
        d = policy.decide(snap({"a": acct(99)}), LQ, "direct", NOW)
        self.assertEqual((d.relay_on, d.reason), (False, "routing: direct"))
        self.assertEqual(d.per_account, {"a": "low"})

    def test_mode_off_is_todays_behaviour_and_still_shows_the_states(self):
        d = policy.decide(None, dict(LQ, enabled=False), "auto", NOW)
        self.assertEqual((d.relay_on, d.reason), (True, "low quota mode off"))
        d = policy.decide(snap({"a": acct(10)}), dict(LQ, enabled=False), "auto", NOW)
        self.assertEqual((d.relay_on, d.per_account), (True, {"a": "ok"}))

    def test_no_snapshot_is_unknown_and_direct(self):
        d = policy.decide(None, LQ, "auto", NOW)
        self.assertEqual((d.relay_on, d.reason), (False, "no snapshot yet"))

    def test_all_rule_needs_every_account_low(self):
        d = policy.decide(snap({"a": acct(80), "b": acct(50)}), LQ, "auto", NOW)
        self.assertEqual((d.relay_on, d.reason), (False, "not every account in scope is low"))
        self.assertEqual(d.per_account, {"a": "low", "b": "ok"})
        d = policy.decide(snap({"a": acct(80), "b": acct(71)}), LQ, "auto", NOW)
        self.assertEqual((d.relay_on, d.reason), (True, "every account in scope is low"))

    def test_any_rule_needs_one(self):
        lq = dict(LQ, rule="any")
        d = policy.decide(snap({"a": acct(80), "b": acct(50)}), lq, "auto", NOW)
        self.assertEqual((d.relay_on, d.reason), (True, "an account in scope is low"))
        d = policy.decide(snap({"a": acct(10), "b": acct(50)}), lq, "auto", NOW)
        self.assertEqual((d.relay_on, d.reason), (False, "no account in scope is low"))

    def test_the_threshold_is_a_strict_remaining_bound(self):
        lq = dict(LQ, threshold_percent=30)
        self.assertEqual(policy.decide(snap({"a": acct(70)}), lq, "auto", NOW).per_account["a"], "ok")    # remaining 30
        self.assertEqual(policy.decide(snap({"a": acct(71)}), lq, "auto", NOW).per_account["a"], "low")   # remaining 29

    def test_zero_used_is_ok(self):
        self.assertEqual(policy.decide(snap({"a": acct(0)}), LQ, "auto", NOW).per_account["a"], "ok")

    def test_unknown_by_error_missing_percent_age_or_window(self):
        for a in (acct(None, error="HTTP 500"), acct(None), acct(90, window=604800)):
            d = policy.decide(snap({"a": a}), LQ, "auto", NOW)
            self.assertEqual(d.per_account["a"], "unknown", a)
            self.assertEqual((d.relay_on, d.reason), (False, "every account in scope is unknown"))

    def test_staleness_boundary(self):
        for age, state in ((60 * 2.9, "low"), (60 * 3.1, "unknown")):
            d = policy.decide(snap({"a": acct(90)}, read_at=NOW - age), LQ, "auto", NOW)
            self.assertEqual(d.per_account["a"], state, age)

    def test_a_window_that_reset_reads_as_full(self):
        d = policy.decide(snap({"a": acct(90, reset_at=NOW - 1)}), LQ, "auto", NOW)
        self.assertEqual(d.per_account["a"], "ok")

    def test_unknown_counts_as_not_low_under_both_rules(self):
        both = snap({"a": acct(90), "b": acct(None, error="timeout")})
        self.assertFalse(policy.decide(both, LQ, "auto", NOW).relay_on)                    # all: b is not low
        self.assertTrue(policy.decide(both, dict(LQ, rule="any"), "auto", NOW).relay_on)   # any: a is low

    def test_scope_names_accounts_and_an_empty_scope_is_direct(self):
        s = snap({"a": acct(90), "b": acct(10, enabled=False)})
        d = policy.decide(s, dict(LQ, accounts=["b"]), "auto", NOW)
        self.assertEqual(d.per_account, {"b": "ok"})
        self.assertFalse(d.relay_on)
        d = policy.decide(snap({}), LQ, "auto", NOW)
        self.assertEqual((d.relay_on, d.reason), (False, "scope is empty"))
        d = policy.decide(s, LQ, "auto", NOW)              # empty setting = every enabled account
        self.assertEqual(set(d.per_account), {"a"})

    def test_disabled_accounts_are_outside_the_default_scope_and_a_named_one_is_inside(self):
        s = snap({"a": acct(90, enabled=False)})
        self.assertEqual((policy.decide(s, LQ, "auto", NOW).reason), "scope is empty")
        self.assertTrue(policy.decide(s, dict(LQ, accounts=["a"]), "auto", NOW).relay_on)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see them fail**

Run: `cd core && python3 -m unittest test_quota_policy 2>&1 | tail -3`
Expected: `ImportError: cannot import name 'policy'`.

- [ ] **Step 3: Implement `quota/policy.py`**

```python
"""The Low Quota Mode decision: one pure function of a snapshot, the settings
and the clock. Called per request, so a stalled poller cannot leave a stale
decision in force."""
from dataclasses import dataclass, field

FIVE_HOURS = 18000


@dataclass(frozen=True)
class Decision:
    relay_on: bool
    reason: str
    per_account: dict = field(default_factory=dict)


def state_of(account, now, stale, threshold):
    """"low" | "ok" | "unknown" for one account of a snapshot read at `read_at`."""
    used = account.get("used_percent")
    if stale or account.get("error") or not isinstance(used, (int, float)) or isinstance(used, bool):
        return "unknown"
    window = account.get("window_seconds")
    if window is not None and window != FIVE_HOURS:
        return "unknown"
    reset_at = account.get("reset_at")
    remaining = 100 if isinstance(reset_at, (int, float)) and reset_at <= now else 100 - used
    return "low" if remaining < threshold else "ok"


def states_of(snapshot, low_quota, now):
    """The state of every account in scope, from a snapshot; {} without one. The
    scope is `low_quota.accounts` when named, else every enabled account."""
    if not snapshot:
        return {}
    accounts = snapshot.get("accounts") or {}
    scope = list(low_quota.get("accounts") or ())
    if scope:
        chosen = {i: a for i, a in accounts.items() if i in scope}
    else:
        chosen = {i: a for i, a in accounts.items() if a.get("enabled")}
    stale = (now - float(snapshot.get("read_at") or 0)) > 3 * low_quota["poll_seconds"]
    return {i: state_of(a, now, stale, low_quota["threshold_percent"]) for i, a in chosen.items()}


def decide(snapshot, low_quota, routing, now):
    states = states_of(snapshot, low_quota, now)      # shown whatever is decided below
    if routing == "relay":
        return Decision(True, "routing: relay", states)
    if routing == "direct":
        return Decision(False, "routing: direct", states)
    if not low_quota.get("enabled"):
        return Decision(True, "low quota mode off", states)
    if not snapshot:
        return Decision(False, "no snapshot yet")
    if not states:
        return Decision(False, "scope is empty")
    if all(s == "unknown" for s in states.values()):
        return Decision(False, "every account in scope is unknown", states)
    low = [s == "low" for s in states.values()]
    if low_quota.get("rule") == "any":
        return Decision(any(low), "an account in scope is low" if any(low) else "no account in scope is low", states)
    return Decision(all(low), "every account in scope is low" if all(low) else "not every account in scope is low", states)
```

- [ ] **Step 4: Run the tests and the suite, commit**

Run: `cd core && python3 -m unittest test_quota_policy -v 2>&1 | tail -16 && python3 -m unittest discover -s . -p "test_*.py" 2>&1 | tail -3`
Expected: all pass, `OK`.

```bash
git add -A && git commit -m "core: the Low Quota Mode policy, one pure function of snapshot, settings and clock"
```

---

### Task 3: The poller

**Files:**
- Create: `core/lokki_core/quota/poller.py`, `core/test_quota_poller.py`
- Modify: `core/lokki_core/config.py` (`ON_APPLY` callbacks run at the end of `apply_settings`), `core/lokki_core/handler.py` (`main()` starts the poller after the reload thread)

**Interfaces:**
- Produces: `poller.Poller(read=reader.read, clock=time.time, log=None)` with `start()` (starts the thread and registers `wake` in `config.ON_APPLY`), `stop()` (unregisters it), `latest() -> snapshot | None`, `status() -> {last_ok_at, last_error, reads, running}`, `wake()`, `refresh(timeout=15.0) -> snapshot | None` (joins the read in flight or starts one, waits for it at most `timeout`, returns the latest snapshot; returns at once when the mode is off), and the module-level `POLLER` the handler starts; `poller.current_settings() -> (low_quota, cliproxy, routing)` reads `config.LOW_QUOTA`, `config.CLIPROXY`, `config.ROUTING` once, so one loop iteration sees one consistent set; `poller.management_url()` is `config.UPSTREAM` without credentials (`control.without_credentials`), the one address both paths use (a `cliproxy.port` reload holds the running upstream until the next start, and the poller follows the same hold). `log` is a callable taking one line (default: a `[quota]` line on stderr); the poller says it once per change of `last_error`, including the recovery.
- Produces: `config.ON_APPLY: list[callable]`, each called with no arguments at the end of `apply_settings`; exceptions are caught and printed.
- Consumes: `reader.read`, `reader.ManagementError`, `control.without_credentials`.

- [ ] **Step 1: Failing tests** (`core/test_quota_poller.py`)

```python
#!/usr/bin/env python3
"""The poller: a daemon thread that keeps the latest snapshot, wakes on reload
and refresh, never dies, and reads the secret from its file each time."""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import config  # noqa: E402
from lokki_core.quota import poller, reader  # noqa: E402


class FakeRead:
    def __init__(self):
        self.calls = []
        self.answer = {"read_at": 1.0, "accounts": {}}
        self.raise_once = None
        self.started = threading.Event()

    def __call__(self, management_url, secret, **kw):
        self.calls.append((management_url, secret, kw))
        self.started.set()
        if self.raise_once:
            exc, self.raise_once = self.raise_once, None
            raise exc
        return dict(self.answer, read_at=time.time())


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.secret_file = tempfile.NamedTemporaryFile("w", suffix=".secret", delete=False)
        self.secret_file.write("s3cr3t\n")
        self.secret_file.close()
        self.addCleanup(os.unlink, self.secret_file.name)
        self.saved = (config.LOW_QUOTA, config.CLIPROXY, config.ROUTING, config.UPSTREAM)
        config.LOW_QUOTA = {"enabled": True, "threshold_percent": 30, "rule": "all", "accounts": [], "poll_seconds": 30}
        config.CLIPROXY = {"port": 8317, "url": "", "version": "8.0.23", "management_secret_file": self.secret_file.name}
        config.ROUTING = "auto"
        config.UPSTREAM = "http://user:pw@127.0.0.1:8317"       # the legacy form may carry credentials
        self.addCleanup(self._restore)
        self.read = FakeRead()
        self.lines = []
        self.p = poller.Poller(read=self.read, log=self.lines.append)
        self.addCleanup(self.p.stop)

    def _restore(self):
        config.LOW_QUOTA, config.CLIPROXY, config.ROUTING, config.UPSTREAM = self.saved

    def wait_for(self, cond, timeout=5.0):
        """Poll `cond` until true or `timeout`; the assertion that follows says what did not happen."""
        end = time.monotonic() + timeout
        while not cond() and time.monotonic() < end:
            time.sleep(0.02)

    def finished_reads(self):
        with self.p._cond:
            return self.p._finished_reads

    def test_it_reads_at_start_with_the_secret_from_the_file_and_the_scope(self):
        self.p.start()
        self.assertTrue(self.read.started.wait(5))
        url, secret, kw = self.read.calls[0]
        self.assertEqual(url, "http://127.0.0.1:8317")           # config.UPSTREAM, credentials stripped
        self.assertEqual(secret, "s3cr3t")
        self.assertEqual(kw["accounts"], ())
        self.assertEqual(kw["cliproxy_version"], "8.0.23")
        self.assertEqual(kw["per_call_timeout"], 10.0)
        self.assertEqual(kw["deadline"], 15.0)
        for _ in range(50):
            if self.p.latest():
                break
            time.sleep(0.05)
        self.assertIsNotNone(self.p.latest())
        self.assertIsNone(self.p.status()["last_error"])

    def test_it_does_not_read_while_the_mode_is_off(self):
        config.LOW_QUOTA = dict(config.LOW_QUOTA, enabled=False)
        self.p.start()
        time.sleep(0.5)
        self.assertEqual(self.read.calls, [])
        self.assertIsNone(self.p.latest())

    def test_a_raising_read_is_recorded_said_once_and_the_thread_lives_on(self):
        self.read.raise_once = reader.ManagementError("HTTP 401 on the credentials route")
        self.p.start()
        self.assertTrue(self.read.started.wait(5))
        for _ in range(50):
            if self.p.status()["last_error"]:
                break
            time.sleep(0.05)
        self.assertEqual(self.p.status()["last_error"], "HTTP 401 on the credentials route")
        self.assertIsNone(self.p.latest())
        self.wait_for(lambda: len(self.lines) >= 1)           # the line is written after the status, outside the lock
        self.assertEqual(len(self.lines), 1)
        self.assertIn("HTTP 401", self.lines[0])
        self.read.raise_once = reader.ManagementError("HTTP 401 on the credentials route")
        self.p.wake()                                        # the same error again: no new line
        self.wait_for(lambda: self.finished_reads() >= 2)
        self.assertEqual(len(self.lines), 1)
        self.p.wake()                                        # a good read: the recovery is said once
        self.wait_for(lambda: self.finished_reads() >= 3)
        self.assertGreaterEqual(len(self.read.calls), 3, "the thread died on the exception")
        self.assertIsNone(self.p.status()["last_error"])
        self.assertIsNotNone(self.p.latest())
        self.wait_for(lambda: len(self.lines) >= 2)
        self.assertEqual(len(self.lines), 2)
        self.assertIn("answering again", self.lines[1])

    def test_an_unexpected_exception_is_also_caught(self):
        self.read.raise_once = RuntimeError("bug")
        self.p.start()
        self.assertTrue(self.read.started.wait(5))
        for _ in range(50):
            if self.p.status()["last_error"]:
                break
            time.sleep(0.05)
        self.assertIn("RuntimeError", self.p.status()["last_error"])
        self.assertTrue(self.p.status()["running"])

    def test_a_reload_wakes_the_poller(self):
        self.p.start()                                       # start registers wake in config.ON_APPLY
        self.assertIn(self.p.wake, config.ON_APPLY)
        self.assertTrue(self.read.started.wait(5))
        n = len(self.read.calls)
        config.LOW_QUOTA = dict(config.LOW_QUOTA, enabled=False)
        for cb in config.ON_APPLY:
            cb()
        time.sleep(0.3)
        self.assertEqual(len(self.read.calls), n)           # off: no read, no wait for the interval
        config.LOW_QUOTA = dict(config.LOW_QUOTA, enabled=True, accounts=["a1"])
        for cb in config.ON_APPLY:
            cb()
        for _ in range(50):
            if len(self.read.calls) > n:
                break
            time.sleep(0.05)
        self.assertEqual(self.read.calls[-1][2]["accounts"], ("a1",))
        self.p.stop()
        self.assertNotIn(self.p.wake, config.ON_APPLY)      # stop unregisters it

    def test_refresh_returns_at_once_when_the_mode_is_off(self):
        config.LOW_QUOTA = dict(config.LOW_QUOTA, enabled=False)
        self.p.start()
        t0 = time.monotonic()
        self.assertIsNone(self.p.refresh(timeout=15))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(self.read.calls, [])

    def test_refresh_joins_the_read_in_flight_or_starts_one_and_two_share_one(self):
        self.p.start()
        self.assertTrue(self.read.started.wait(5))
        n = len(self.read.calls)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.p.refresh(timeout=5))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r is not None for r in results))
        self.assertLessEqual(len(self.read.calls) - n, 2)     # one read, two at most, never one per caller plus the loop's

    def test_refresh_answers_with_the_latest_when_its_wait_ends(self):
        def slow(*a, **kw):
            self.read.started.set()
            time.sleep(2)
            return {"read_at": time.time(), "accounts": {}}
        self.p = poller.Poller(read=slow)
        self.addCleanup(self.p.stop)
        self.p.start()
        self.assertTrue(self.read.started.wait(5))
        t0 = time.monotonic()
        self.assertIsNone(self.p.refresh(timeout=0.3))
        self.assertLess(time.monotonic() - t0, 1.5)

    def test_a_missing_secret_file_is_the_error(self):
        config.CLIPROXY = dict(config.CLIPROXY, management_secret_file="/nonexistent/secret")
        self.p.start()
        for _ in range(50):
            if self.p.status()["last_error"]:
                break
            time.sleep(0.05)
        self.assertIn("secret", self.p.status()["last_error"])
        self.assertEqual(self.read.calls, [])


class OnApplyTests(unittest.TestCase):
    def test_apply_settings_calls_every_hook_and_survives_one_that_raises(self):
        seen = []
        hooks = [lambda: seen.append(1), lambda: 1 / 0]
        config.ON_APPLY.extend(hooks)
        self.addCleanup(lambda: [config.ON_APPLY.remove(h) for h in hooks])   # only what this test added
        config.apply_settings(dict(config._settings))
        self.assertEqual(seen, [1])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see them fail**

Run: `cd core && python3 -m unittest test_quota_poller 2>&1 | tail -3`
Expected: `ImportError: cannot import name 'poller'`.

- [ ] **Step 3: Implement**

`config.py`: module level `ON_APPLY = []`, defined above the import-time `apply_settings(_settings)` call at the bottom of `config.py`, documented "called at the end of every apply, start and reload alike; a hook must not block"; at the end of `apply_settings`, before `return`:

```python
    for hook in list(ON_APPLY):
        try:
            hook()
        except Exception as e:  # noqa: BLE001 - a hook must not take the apply down
            print(f"settings hook {hook!r} failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
```

`quota/poller.py`:

```python
"""A daemon thread that keeps the latest quota snapshot. It stores only the
snapshot; the decision is computed per request (`policy.decide`). It wakes on
`wake()` (a reload or a refresh) and otherwise sleeps `poll_seconds`. Every
exception in the loop is recorded as `last_error` and said once per change (a
dead management API makes every request a full-price one while the endpoint
looks healthy, so silence here is the costly failure); the thread never dies."""
import sys
import threading
import time

from .. import config, control
from . import reader

PER_CALL_TIMEOUT = 10.0


def current_settings():
    """One consistent read of the three settings the loop uses: each is swapped in
    one assignment by `apply_settings`, and the apply hook runs after all three."""
    return config.LOW_QUOTA, config.CLIPROXY, config.ROUTING


def management_url():
    """Where CLIProxyAPI is: the running upstream, the one address pass-through
    uses, with any credentials of the legacy forms taken out."""
    return control.without_credentials(config.UPSTREAM).rstrip("/")


def _say(line):
    print(f"[quota] {line}", file=sys.stderr, flush=True)


def _secret(cliproxy):
    path = cliproxy.get("management_secret_file") or ""
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
    except OSError as e:
        raise reader.ManagementError(f"management secret file unreadable: {type(e).__name__}") from None
    if not value:
        raise reader.ManagementError("management secret file is empty")
    return value


class Poller:
    def __init__(self, read=reader.read, clock=time.time, log=None):
        self._read, self._clock, self._log = read, clock, log or _say
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._latest = None
        self._status = {"last_ok_at": None, "last_error": None, "reads": 0, "running": False}
        self._finished_reads = 0           # reads ended, under _cond
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._status["running"] = True
        config.ON_APPLY.append(self.wake)
        self._thread = threading.Thread(target=self._loop, name="lokki-quota-poller", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.wake in config.ON_APPLY:
            config.ON_APPLY.remove(self.wake)

    def wake(self):
        self._wake.set()

    def latest(self):
        with self._cond:
            return self._latest

    def status(self):
        with self._cond:
            return dict(self._status)

    def refresh(self, timeout=15.0):
        """One read now: joins the read in flight or wakes the loop to start one,
        waits for that read at most `timeout`, and returns the latest snapshot.
        Two refreshes during one read wait for the same read. With the mode off
        there is nothing to read: the latest snapshot, at once."""
        if not config.LOW_QUOTA.get("enabled"):
            return self.latest()
        with self._cond:
            wanted = self._finished_reads + 1      # the next read to finish, in flight or not
        self._wake.set()
        with self._cond:
            self._cond.wait_for(lambda: self._finished_reads >= wanted, timeout)
            return self._latest

    def _loop(self):
        while not self._stop.is_set():
            low_quota, cliproxy, _ = current_settings()
            if low_quota.get("enabled"):
                self._one_read(low_quota, cliproxy)
            interval = float(low_quota.get("poll_seconds") or 60)
            self._wake.wait(interval)
            self._wake.clear()
        with self._cond:
            self._status["running"] = False

    def _one_read(self, low_quota, cliproxy):
        error = None
        try:
            snapshot = self._read(management_url(), _secret(cliproxy),
                                  accounts=tuple(low_quota.get("accounts") or ()),
                                  per_call_timeout=PER_CALL_TIMEOUT,
                                  deadline=float(low_quota.get("poll_seconds") or 60) / 2,
                                  cliproxy_version=cliproxy.get("version") or "")
        except reader.ManagementError as e:
            error = str(e)
        except Exception as e:  # noqa: BLE001 - the thread must outlive any bug
            error = f"{type(e).__name__}: {e}"
        with self._cond:
            previous = self._status["last_error"]
            if error is None:
                self._latest = snapshot
                self._status.update(last_ok_at=self._clock(), last_error=None, reads=self._status["reads"] + 1)
            else:
                self._status["last_error"] = error
            self._finished_reads += 1
            self._cond.notify_all()
        if error != previous:                 # once per change, the recovery included
            self._log(f"quota reads failed: {error}" if error else "management API answering again")


POLLER = Poller()
```

(`_log` is called outside the condition so a slow stderr never holds the lock. The `_say` default writes the `[quota]` line; the tests pass a list.)

`handler.main()`: after the reload thread starts, `from .quota import poller as _poller; _poller.POLLER.start()`. In the test harness nothing starts it (`POLLER` stays idle), so the suite's other tests are unaffected.

- [ ] **Step 4: Run the tests and the suite, commit**

Run: `cd core && python3 -m unittest test_quota_poller -v 2>&1 | tail -16 && python3 -m unittest discover -s . -p "test_*.py" 2>&1 | tail -3 && python3 -c "import server"`
Expected: all pass, `OK`, no output from the import.

```bash
git add -A && git commit -m "core: the quota poller, a daemon thread that keeps the latest snapshot and wakes on reload or refresh"
```

---

### Task 4: Pass-through

**Files:**
- Create: `core/lokki_core/passthrough.py`, `core/lokki_core/routing.py`, `core/test_passthrough.py`
- Modify: `core/lokki_core/handler.py` (`RELAY_KNOB_PARAMS`; the branch in `_do_POST`; `/healthz`), `core/test_offline.py` (the `/healthz` key-set test gains `routing`)
- Test: `core/test_passthrough.py`

**Interfaces:**
- Produces: `routing.decide_now() -> Decision` (reads `poller.POLLER.latest()`, `config.LOW_QUOTA`, `config.ROUTING`, `time.time()`; logs one `[routing]` line to stderr when `relay_on` or `reason` differs from the last decision logged); `routing.last() -> Decision | None`.
- Produces: `passthrough.serve(handler, raw: bytes, payload: dict, decision)`: forwards and streams back; writes one record; counts in `active_run()` (the handler wraps it).
- Produces: `handler.RELAY_KNOB_PARAMS = frozenset(("mode", "abort_after", "seg_budget", "total_cap", "max_segments", "payload_cap", "imagegen_cut", "merge_text", "text_calls", "text_calls_pad", "pad_tools", "retries"))`, the keys `resolve_knobs` returns; a test ties the two.
- Produces: pass-through's answer to CLIProxyAPI's 401, 403 or 407 is a 502 "CLIProxyAPI refused the relay's credential" (those statuses are about the core's key, not the client's; the relay's non-streaming path does the same in `_do_POST`'s non-streaming branch, the comment "401/403/407: those are about the relay's upstream credential"); every other status passes through as it is, with `cut: error` for any status of 400 or more and `terminal` only for an answer that ended with its terminal frame (or, non-streaming, a 2xx body).
- Consumes: `engine.open_upstream(req, rec, retries)`, `engine.upstream_slot()`, `engine.FORWARDED_HEADERS`, `engine.PROVIDER_TERMINALS`, `core.log_run`, `core.summarise_usage`, `core.ResponseError`, `handler.active_run`, `handler.STOPPING`.

- [ ] **Step 1: The one probe, before any code**

The Lead (not a Peer) sends one request directly to the measurement clone of CLIProxyAPI on this host, the container `cli-proxy-api-8318`, on the address it binds (the live pair stays untouched). The key is the harness's `CLI_PROXY_API_KEY` environment variable, read into a shell variable and passed in a header; it never appears in a command line that is logged, in output, or in the PR. Body: `{"model": "gpt-5.5", "input": "hi", "stream": false, "max_output_tokens": 16}`. Record the status, the `Content-Type`, and whether the body was one JSON object or an event stream. That is the sub-project's only network call; it spends a few tokens, which the PR body reports to the Human with the result. The engine always asks for a stream and aggregates itself, so nothing in the tree shows what `stream: false` gets from CLIProxyAPI; the pass-through code below handles both answers, and `docs/app/api.md` (Task 5) says which one it is.

- [ ] **Step 2: Failing tests** (`core/test_passthrough.py`; the stub upstream streams real frames, unlike `test_offline`'s empty one)

```python
#!/usr/bin/env python3
"""Pass-through: the client's bytes reach CLIProxyAPI with the core's credential,
the answer comes back byte for byte, one record says what went where."""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import config, core, engine, handler, routing  # noqa: E402
from lokki_core.quota import poller  # noqa: E402
from test_offline import TOKEN, _ServingHarness  # noqa: E402

FRAMES = (b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi","output_index":0,"item_id":"i0"}\n\n'
          b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}\n\n')


def read_all(port, data, timeout=10):
    """Send bytes, read until the server closes. `test_offline.raw_request` reads
    one `recv`, which is not the whole of a stream."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(data)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except ConnectionResetError:        # a refusal that left the body unread (Windows resets)
                return out
            if not chunk:
                return out
            out += chunk
    finally:
        s.close()


class StreamingUpstream:
    """Answers POST /v1/responses with FRAMES (or a scripted status), records the request."""

    def __init__(self):
        self.seen = []
        # per request: ("status", code) | ("frames", bytes) | ("break_after", n_bytes) | ("json", obj)
        #            | ("slow", [bytes, ...], delay_s): each piece its own chunk, `delay_s` apart
        self.script = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                outer.seen.append({"path": self.path, "raw": raw, "headers": {k.lower(): v for k, v in self.headers.items()}})
                action = outer.script.pop(0) if outer.script else ("frames", FRAMES)
                if action[0] == "status":
                    body = json.dumps({"error": {"message": "nope", "type": "server_error"}}).encode()
                    self.send_response(action[1])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Retry-After", "1")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if action[0] == "json":
                    body = json.dumps(action[1]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("x-request-id", "req-xyz")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.flush()
                try:
                    if action[0] == "slow":
                        for piece in action[1]:
                            self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                            self.wfile.flush()
                            time.sleep(action[2])
                        self.wfile.write(b"0\r\n\r\n")
                    else:
                        data = FRAMES[:action[1]] if action[0] == "break_after" else action[1]
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                        if action[0] != "break_after":
                            self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass                                   # the core stopped reading: the client left
                self.close_connection = True

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _PassThroughHarness(_ServingHarness):
    def setUp(self):
        super().setUp()
        self.up = StreamingUpstream()
        self.addCleanup(self.up.close)
        tmp = tempfile.mkdtemp(prefix="lokki-passthrough-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.saved = (config.UPSTREAM, core.RUNS_LOG, config.PACING, config.RETRIES, config.ROUTING, config.API_KEY)
        config.UPSTREAM = f"http://127.0.0.1:{self.up.port}"
        core.RUNS_LOG = os.path.join(tmp, "runs.jsonl")
        config.PACING = 0
        config.RETRIES = 3
        config.ROUTING = "direct"
        config.API_KEY = "core-upstream-key"
        engine.set_retry_hook(lambda *a: None)         # no backoff sleep in the suite
        routing.reset()
        self.addCleanup(self._restore)

    def _restore(self):
        (config.UPSTREAM, core.RUNS_LOG, config.PACING, config.RETRIES, config.ROUTING, config.API_KEY) = self.saved
        engine.set_retry_hook(None)
        routing.reset()

    def post(self, body, headers=None, token=TOKEN):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        head = (f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Relay-Token: {token}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n")
        for k, v in (headers or {}).items():
            head += f"{k}: {v}\r\n"
        return read_all(self.port, head.encode() + b"\r\n" + data)

    def last_record(self):
        with open(core.RUNS_LOG, encoding="utf-8") as f:
            return json.loads(f.read().splitlines()[-1])


class PassThroughTests(_PassThroughHarness):
    def test_raw_bytes_reach_cliproxy_with_the_cores_key_and_come_back_byte_for_byte(self):
        raw = b'{"model":"gpt-5.5","input":"hi","temperature":1.0,"store":true,"previous_response_id":"r0"}'
        resp = self.post(raw, headers={"session_id": "s1", "Accept-Encoding": "gzip", "originator": "codex",
                                       "Authorization": "Bearer client-secret"})
        self.assertEqual(self.up.seen[0]["raw"], raw)                      # untouched, store and previous_response_id included
        h = self.up.seen[0]["headers"]
        self.assertEqual(h["authorization"], "Bearer core-upstream-key")   # never the client's
        self.assertNotIn("client-secret", json.dumps(h))
        self.assertEqual(h["session_id"], "s1")
        self.assertEqual(h["originator"], "codex")
        self.assertNotEqual(h.get("accept-encoding"), "gzip")             # http.client sends identity, not the client's
        self.assertNotIn("x-relay-token", h)
        head, _, body = resp.partition(b"\r\n\r\n")
        self.assertIn(b"200", head.split(b"\r\n")[0])
        self.assertIn(b"content-type: text/event-stream", head.lower())
        self.assertIn(b"x-request-id: req-xyz", head.lower())
        self.assertNotIn(b"transfer-encoding", head.lower())
        self.assertNotIn(b"content-length", head.lower())
        self.assertEqual(body, FRAMES)
        rec = self.last_record()
        self.assertEqual(rec["route"], "passthrough")
        self.assertEqual(rec["reason"], "routing: direct")
        self.assertEqual(rec["status"], 200)
        self.assertEqual(rec["cut"], "terminal")
        self.assertEqual(rec["usage"]["total_tokens"], 4)
        self.assertEqual(rec["upstream_requests"], 1)
        self.assertFalse(rec.get("reserialized"))
        self.assertEqual(rec["client"], "offline-test-client")
        self.assertNotIn("hi", json.dumps(rec))

    def test_a_knob_is_removed_and_the_rest_reserialized(self):
        body = {"model": "gpt-5.5", "input": [{"role": "user", "content": "x"}], "mode": "control", "seg_budget": 5, "temperature": 0.5}
        self.post(body)
        sent = json.loads(self.up.seen[0]["raw"])
        self.assertEqual(sent, {"model": "gpt-5.5", "input": [{"role": "user", "content": "x"}], "temperature": 0.5})
        self.assertTrue(self.last_record()["reserialized"])

    def test_the_knob_set_is_what_resolve_knobs_reads(self):
        self.assertEqual(set(handler.resolve_knobs({}, True)), set(handler.RELAY_KNOB_PARAMS))

    def recv_until(self, s, marker):
        """Read from `s` until `marker` is seen; a closed connection is a failure, not a spin."""
        got = b""
        while marker not in got:
            chunk = s.recv(65536)
            self.assertTrue(chunk, f"the connection closed before {marker!r}: {got[:200]!r}")
            got += chunk
        return got

    def wait_for_record(self):
        """The handler's `finally` writes the record after the client has gone; wait for
        it so it lands in this test's log, not in the one the cleanup restores."""
        for _ in range(200):
            try:
                if self.last_record()["route"] == "passthrough":
                    return
            except (OSError, IndexError, ValueError):
                pass
            time.sleep(0.05)
        self.fail("no pass-through record was written")

    def test_the_first_frame_reaches_the_client_before_the_last(self):
        first, second = FRAMES.split(b"\n\n", 1)
        self.up.script = [("slow", [first + b"\n\n", second], 3.0)]
        data = json.dumps({"model": "m", "input": "x"}).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(s.close)
        s.sendall((f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Relay-Token: {TOKEN}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n").encode() + data)
        t0 = time.monotonic()
        got = self.recv_until(s, b"output_text.delta")
        self.assertLess(time.monotonic() - t0, 1.5, "the first frame waited for the second")
        self.assertNotIn(b"response.completed", got)
        s.close()
        self.wait_for_record()

    def test_the_guards_that_run_on_both_paths(self):
        self.assertIn(b"401", self.post({"model": "m", "input": "x"}, token="nope").split(b"\r\n")[0])
        self.assertIn(b"400", self.post(b"{not json").split(b"\r\n")[0])
        self.assertIn(b"400", self.post({"model": "bad model!", "input": "x"}).split(b"\r\n")[0])
        handler.STOPPING.set()
        self.addCleanup(handler.STOPPING.clear)
        self.assertIn(b"503", self.post({"model": "m", "input": "x"}).split(b"\r\n")[0])
        self.assertEqual(self.up.seen, [])

    def test_retry_before_the_first_byte(self):
        self.up.script = [("status", 503), ("frames", FRAMES)]
        resp = self.post({"model": "m", "input": "x"})
        self.assertIn(b"200", resp.split(b"\r\n")[0])
        self.assertEqual(len(self.up.seen), 2)
        rec = self.last_record()
        self.assertEqual((rec["upstream_requests"], rec["last_status"], rec["cut"]), (2, 503, "terminal"))

    def test_a_stream_broken_after_the_first_byte_is_not_retried(self):
        self.up.script = [("break_after", 40), ("frames", FRAMES)]     # a second answer is ready; it must not be asked for
        resp = self.post({"model": "m", "input": "x"})
        self.assertEqual(len(self.up.seen), 1, "a broken stream was retried after the first byte")
        self.assertIn(b"200", resp.split(b"\r\n")[0])                   # the status was already sent
        rec = self.last_record()
        self.assertEqual((rec["route"], rec["cut"], rec["upstream_requests"]), ("passthrough", "error", 1))
        self.assertNotIn("last_status", rec)
        self.assertIsNone(rec["usage"])

    def test_a_4xx_is_passed_through_as_it_is_and_recorded_as_an_error(self):
        self.up.script = [("status", 400)]
        resp = self.post({"model": "m", "input": "x"})
        self.assertIn(b"400", resp.split(b"\r\n")[0])
        self.assertIn(b'"nope"', resp)
        rec = self.last_record()
        self.assertEqual((rec["status"], rec["cut"]), (400, "error"))
        self.assertEqual(rec["upstream_requests"], 1)

    def test_a_refusal_of_the_relays_credential_is_a_502(self):
        for status in (401, 403, 407):
            self.up.script = [("status", status)]
            resp = self.post({"model": "m", "input": "x"})
            self.assertIn(b"502", resp.split(b"\r\n")[0], status)
            self.assertIn(b"refused the relay's credential", resp)
            rec = self.last_record()
            self.assertEqual((rec["status"], rec["cut"]), (status, "error"))

    def test_non_streaming_passes_through_as_one_json_body(self):
        self.up.script = [("json", {"id": "resp_2", "object": "response", "usage": {"total_tokens": 7}})]
        resp = self.post({"model": "m", "input": "x", "stream": False})
        head, _, body = resp.partition(b"\r\n\r\n")
        self.assertIn(b"content-length", head.lower())
        self.assertEqual(json.loads(body)["id"], "resp_2")
        self.assertEqual(self.last_record()["usage"]["total_tokens"], 7)

    def test_a_client_that_hangs_up_is_recorded_as_client_gone(self):
        first = FRAMES.split(b"\n\n", 1)[0] + b"\n\n"
        self.up.script = [("slow", [first] * 30, 0.1)]            # 3 s of frames; the client leaves after the first
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        data = json.dumps({"model": "m", "input": "x"}).encode()
        s.sendall((f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Relay-Token: {TOKEN}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n").encode() + data)
        self.recv_until(s, b"output_text.delta")
        s.close()
        self.wait_for_record()
        rec = self.last_record()
        self.assertEqual((rec["route"], rec["cut"]), ("passthrough", "client_gone"))
        self.assertIsNone(rec["usage"])

    def test_the_slot_is_taken_and_released(self):
        saved = config.MAX_CONCURRENT
        config.MAX_CONCURRENT = 1
        self.addCleanup(setattr, config, "MAX_CONCURRENT", saved)
        with engine.upstream_slot():
            pass                                       # settle the semaphore on 1
        self.post({"model": "m", "input": "x"})
        self.assertTrue(engine._slots.acquire(timeout=2), "the slot was not released")
        engine._slots.release()

    def test_cliproxy_down_is_a_502_a_record_and_a_warning_line(self):
        import io
        from unittest import mock
        config.UPSTREAM = "http://127.0.0.1:9"
        config.RETRIES = 1
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            resp = self.post({"model": "m", "input": "x"})
        self.assertIn(b"502", resp.split(b"\r\n")[0])
        rec = self.last_record()
        self.assertEqual((rec["route"], rec["cut"]), ("passthrough", "error"))
        self.assertIn("[passthrough] CLIProxyAPI did not answer", err.getvalue())
        self.assertNotIn("core-upstream-key", err.getvalue())

    def test_the_relay_path_is_taken_when_the_decision_says_so(self):
        config.ROUTING = "relay"
        self.post({"model": "m", "input": "x"})
        self.assertNotEqual(self.up.seen[0]["raw"], b'{"model": "m", "input": "x"}')   # the engine built its own body
        self.assertEqual(self.last_record()["route"], "relay")

    def test_api_run_always_runs_the_engine(self):
        data = json.dumps({"prompt": "hello"}).encode()
        head = (f"POST /api/run HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Relay-Token: {TOKEN}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n").encode()
        read_all(self.port, head + data)
        self.assertEqual(self.last_record()["route"], "relay")


class RoutingLogTests(_PassThroughHarness):
    def test_a_decision_change_is_one_stderr_line_and_healthz_shows_it(self):
        import io
        from unittest import mock
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            routing.decide_now()
            routing.decide_now()
            config.ROUTING = "relay"
            routing.decide_now()
        lines = [l for l in err.getvalue().splitlines() if l.startswith("[routing]")]
        self.assertEqual(len(lines), 2)
        self.assertIn("routing: direct", lines[0])
        self.assertIn("routing: relay", lines[1])
        status, body = read_all(self.port, b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").partition(b"\r\n\r\n")[::2]
        self.assertEqual(json.loads(body)["routing"], {"relay_on": True, "reason": "routing: relay"})


if __name__ == "__main__":
    unittest.main()
```

(The harness writes its run log in a temporary directory, never in the checkout; `core.log_run` refuses a path inside the repository anyway. `engine.set_retry_hook` replaces the backoff sleep, as `test_engine` does.)

`core/test_offline.py`, `HealthAndVersionTests.test_healthz_needs_no_token_and_carries_no_data`: the key set becomes `{"status", "uptime_s", "routing"}` and the test adds `self.assertEqual(set(body["routing"]), {"relay_on", "reason"})`; its docstring gains "and the two routing fields, which name no account, no meter value and no address".

- [ ] **Step 3: Run to see them fail**

Run: `cd core && python3 -m unittest test_passthrough 2>&1 | tail -3`
Expected: `ImportError: cannot import name 'routing'`.

- [ ] **Step 4: Implement**

`routing.py`:

```python
"""The decision, computed per request, and the one stderr line when it changes.
Unknown counts as not low: a dead management API makes every request a
full-price one while the endpoint looks healthy, so every change of the
decision or its reason is said once, and /healthz carries the decision."""
import sys
import threading
import time

from . import config
from .quota import policy, poller

_lock = threading.Lock()
_last = None


def reset():
    global _last
    with _lock:
        _last = None


def last():
    return _last


def decide_now():
    global _last
    d = policy.decide(poller.POLLER.latest(), config.LOW_QUOTA, config.ROUTING, time.time())
    with _lock:
        changed = _last is None or (d.relay_on, d.reason) != (_last.relay_on, _last.reason)
        _last = d
    if changed:
        print(f"[routing] relay {'on' if d.relay_on else 'off'}: {d.reason}", file=sys.stderr, flush=True)
    return d
```

`handler.py`: the constant `RELAY_KNOB_PARAMS` (the `resolve_knobs` set); in `_do_POST`, after the drain check and the body read, before the `previous_response_id` refusal:

```python
        if path == "/v1/responses":
            decision = routing.decide_now()
            if not decision.relay_on:
                with active_run():
                    passthrough.serve(self, raw, payload, decision)
                return
```

For that, `_read_json_body` must hand back the raw bytes too: give it `return_raw=False` and have this call site use `raw, payload = self._read_json_body(return_raw=True)` (the body cap, the capture and the JSON check run as today; the `model` id rule is applied in `passthrough.serve` with the handler's own `_MODEL_NAME` and `DEFAULT_MODEL`, imported there, and the same 400 text). `/healthz` gains `"routing": {"relay_on": d.relay_on, "reason": d.reason}` with `d = routing.decide_now()`, computed for the probe like for a request (a monitor must see the current state, not the last request's); the comment there says why this is the one piece of configuration the public route carries: it names no secret and no address.

`passthrough.py`:

```python
"""A request the decision sends straight through: the client's bytes to
CLIProxyAPI with the core's credential, the answer back byte for byte, one run
record. No instructions, no padding tool, no marker scan; retry only before the
first byte; the record in a finally."""
import http.client
import json
import sys
import time
import urllib.error
import urllib.request

from . import config, core, engine
from .core import log_run, summarise_usage
from .sse import iter_sse_frames

ANSWER_HEADERS = ("Content-Type", "Retry-After", "x-request-id")
# Statuses about the core's own upstream credential, not the client's request:
# passed on, they would tell the client its token is bad (the relay's
# non-streaming path maps the same three).
CREDENTIAL_STATUSES = (401, 403, 407)
_CHUNK = 64 * 1024
_TERMINALS = ("response.completed",) + engine.PROVIDER_TERMINALS


def _body_for_upstream(raw, payload, knobs):
    """The raw bytes unless a relay knob is present; then the parsed body without
    the knobs, re-serialized. Returns (bytes, reserialized)."""
    present = [k for k in knobs if k in payload]
    if not present:
        return raw, False
    cleaned = {k: v for k, v in payload.items() if k not in knobs}
    return json.dumps(cleaned, ensure_ascii=False).encode(), True


def _headers(h):
    headers = {"Authorization": f"Bearer {config.API_KEY}",
               "Content-Type": h.headers.get("Content-Type") or "application/json"}
    for name in engine.FORWARDED_HEADERS:
        value = h.headers.get(name)
        if value and name not in headers:
            headers[name] = value
    return headers


class _Scanner:
    """Looks at the bytes on their way to the client for the terminal event and
    its usage; never alters them. Frames are parsed by `sse.iter_sse_frames`, the
    one SSE parser (CRLF, multi-line `data:`), fed one complete frame at a time."""

    # A terminal frame carries the response's output (an image item can be
    # megabytes), so the caps are generous: a line or a frame beyond them is not
    # scanned, the stream still reaches the client whole, and the record says
    # `eof` with no usage rather than holding the whole stream in memory.
    MAX_LINE = 16 << 20
    MAX_FRAME_LINES = 4096

    def __init__(self):
        self.buf = b""              # the unfinished line
        self.lines = []             # the unfinished frame
        self.terminal = None
        self.usage = None

    def feed(self, chunk):
        self.buf += chunk
        *done, self.buf = self.buf.split(b"\n")
        for line in done:
            if len(self.lines) < self.MAX_FRAME_LINES:
                self.lines.append(line)
            if line.rstrip(b"\r") == b"":
                for _, data in iter_sse_frames(self.lines):
                    self._frame(data)
                self.lines = []
        if len(self.buf) > self.MAX_LINE:
            self.buf = self.buf[-(1 << 16):]       # a line that never ends is not kept whole

    def _frame(self, data):
        try:
            obj = json.loads(data) if data else None
        except ValueError:
            return
        t = obj.get("type") if isinstance(obj, dict) else None
        if t in _TERMINALS:
            self.terminal = t
            self.usage = summarise_usage((obj.get("response") or {}).get("usage"))


def serve(h, raw, payload, decision):
    from .handler import DEFAULT_MODEL, RELAY_KNOB_PARAMS, _MODEL_NAME   # local: handler imports this module
    model = payload.get("model") or DEFAULT_MODEL
    if not isinstance(model, str) or not _MODEL_NAME.match(model):
        raise core.ResponseError(400, "'model' must be a model id: letters, digits and . _ : / ( ) -, at most 100 characters")
    body, reserialized = _body_for_upstream(raw, payload, RELAY_KNOB_PARAMS)
    rec = {"route": "passthrough", "reason": decision.reason, "model": model,
           "client": getattr(h, "client_name", None), "reserialized": reserialized,
           "stream": bool(payload.get("stream", True)), "upstream_input_chars": len(body),
           "upstream_requests": 0, "status": None, "cut": None, "usage": None, "usage_source": None}
    started = time.monotonic()
    req = urllib.request.Request(config.UPSTREAM + "/v1/responses", data=body, headers=_headers(h), method="POST")
    try:
        with engine.upstream_slot():
            try:
                resp = engine.open_upstream(req, rec, config.RETRIES)
            except urllib.error.HTTPError as e:
                resp = e                                   # a refusal is an answer: passed through as it is
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                rec["error"] = type(getattr(e, "reason", None) or e).__name__
                rec["cut"] = "error"
                # Unknown → direct with CLIProxyAPI down is the fail-open case: every
                # request is a full-price one while the endpoint looks healthy, so it is
                # said. The exception's text, never the request or the credential.
                print(f"[passthrough] CLIProxyAPI did not answer ({rec['error']}): "
                      f"{getattr(e, 'reason', e)}", file=sys.stderr, flush=True)
                h._error(502, "CLIProxyAPI did not answer; the relay's log has the detail")
                return
            rec["upstream_requests"] = rec.get("attempts", 1)
            _relay_response(h, resp, rec)
    finally:
        rec["elapsed_s"] = round(time.monotonic() - started, 3)
        log_run(rec)


def _relay_response(h, resp, rec):
    status = getattr(resp, "status", None) or resp.code
    rec["status"] = status
    if status in CREDENTIAL_STATUSES:
        rec["cut"] = "error"
        with resp:
            pass
        h._error(502, "CLIProxyAPI refused the relay's credential; the relay's log has the detail")
        return
    ctype = (resp.headers.get("Content-Type") or "").lower()
    streaming = ctype.startswith("text/event-stream")
    scanner = _Scanner()
    h.send_response(status)
    for name in ANSWER_HEADERS:
        if resp.headers.get(name):
            h.send_header(name, resp.headers[name])
    h.send_header("Connection", "close")
    if not streaming:
        try:
            with resp:
                body = resp.read()
        except (OSError, http.client.HTTPException) as e:
            rec["error"], rec["cut"] = type(e).__name__, "error"
            h._error(502, "CLIProxyAPI's answer broke off; the relay's log has the detail")
            return
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h._response_started = True
        if status < 400:
            try:
                obj = json.loads(body)
                rec["usage"] = summarise_usage(obj.get("usage")) if isinstance(obj, dict) else None
            except ValueError:
                pass
        rec["usage_source"] = "provider" if rec["usage"] else None
        rec["cut"] = "error" if status >= 400 else "terminal"
        _write(h, body, rec)
        return
    h.end_headers()
    h._response_started = True
    try:
        with resp:
            while True:
                chunk = resp.read1(_CHUNK)           # read1: what has arrived, not a full _CHUNK (a stream is live)
                if not chunk:
                    break
                scanner.feed(chunk)
                _write(h, chunk, rec)
                if rec.get("cut") == "client_gone":
                    return
    except (OSError, http.client.HTTPException) as e:
        rec["error"], rec["cut"] = type(e).__name__, "error"
        return
    rec["usage"] = scanner.usage
    rec["usage_source"] = "provider" if scanner.usage else None
    rec["cut"] = "terminal" if scanner.terminal else "eof"


def _write(h, data, rec):
    try:
        h.wfile.write(data)
        h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
        rec["cut"] = "client_gone"
```

(`engine.open_upstream` paces, retries on 429/502/503 before the first byte with the engine's backoff and `_retry_hook`, and records `attempts`, `retries`, `last_status`; nothing in it writes to the client, so "never after the first byte" holds by construction: the stream is read only after it returns. `HTTPResponse.read1` returns what the socket has, where `read(n)` on a chunked body waits for `n` bytes or the end: with `read` the client saw the first frame only when the last arrived.) `log_run` adds `ts`, `revision`, `config_fingerprint`, the chain fields and counts the route.

- [ ] **Step 5: Run the tests and the suite, commit**

Run: `cd core && python3 -m unittest test_passthrough -v 2>&1 | tail -22 && python3 -m unittest discover -s . -p "test_*.py" 2>&1 | tail -3 && python3 -c "import server"`
Expected: all pass, `OK`, no output from the import.

```bash
git add -A && git commit -m "core: pass-through, the decision per request, and the routing line in /healthz"
```

---

### Task 5: `/api/accounts`, status, docs

**Files:**
- Modify: `core/lokki_core/control.py` (`accounts()`, `status()` filled), `core/lokki_core/handler.py` (`GET /api/accounts`, `POST /api/accounts/refresh`), `core/test_control.py` (the new class, `import time`, the status test's `decision`/`poll` assertions), `core/test_offline.py` (`json_post`), `docs/app/api.md`, `docs/app/limits.md` (new), `docs/relay/relay-8371.md` (one paragraph: which guards run on which path), `README.md` ("Low Quota Mode"), `docs/README.md`, `docs/superpowers/specs/2026-10-10-lokki-app-design.md` (the four sentences the departures name)

**Interfaces:**
- Produces: `control.accounts(decision) -> {read_at, age_s, accounts: [...], decision: {relay_on, reason}}`; `GET /api/accounts` (operator), `POST /api/accounts/refresh` (operator; one read now, at most 15 s, then the latest); `/api/status.decision` = `{relay_on, reason, computed_at}` and `/api/status.poll` = `poller.POLLER.status()`.

- [ ] **Step 1: Failing tests** (append to `core/test_control.py`, which gains `import time` and imports `json_post` with the rest; `_ControlHarness` is the one there, whose upstream is a listener the test owns)

```python
class AccountsRouteTests(_ControlHarness):
    def setUp(self):
        super().setUp()
        from lokki_core.quota import poller
        self.saved = (config.LOW_QUOTA, config.ROUTING, poller.POLLER._latest)
        config.LOW_QUOTA = {"enabled": True, "threshold_percent": 30, "rule": "all", "accounts": [], "poll_seconds": 60}
        config.ROUTING = "auto"
        with poller.POLLER._cond:
            poller.POLLER._latest = {"read_at": time.time(), "accounts": {
                "a1": {"label": "acct-1", "email_masked": "a***@x", "enabled": True, "used_percent": 80, "reset_at": time.time() + 3600, "window_seconds": 18000, "error": None},
                "b2": {"label": "acct-2", "email_masked": "b***@x", "enabled": True, "used_percent": None, "reset_at": None, "window_seconds": None, "error": "timeout"}}}
        self.addCleanup(self._restore)

    def _restore(self):
        from lokki_core.quota import poller
        config.LOW_QUOTA, config.ROUTING, latest = self.saved
        with poller.POLLER._cond:
            poller.POLLER._latest = latest

    def test_accounts_lists_each_with_its_state_and_the_decision(self):
        status, body = json_get(self.port, "/api/accounts")
        self.assertEqual(status, 200)
        rows = {r["auth_index"]: r for r in body["accounts"]}
        self.assertEqual(rows["a1"]["state"], "low")
        self.assertEqual(rows["a1"]["remaining_percent"], 20)
        self.assertTrue(rows["a1"]["in_scope"])
        self.assertEqual(rows["b2"]["state"], "unknown")
        self.assertEqual(rows["b2"]["error"], "timeout")
        self.assertEqual(body["decision"], {"relay_on": False, "reason": "not every account in scope is low"})
        self.assertNotIn("@x", json.dumps(body).replace("a***@x", "").replace("b***@x", ""))
        self.assertLess(body["age_s"], 5)

    def test_status_carries_the_decision_and_the_poll(self):
        status, body = json_get(self.port, "/api/status")
        self.assertEqual(body["decision"]["reason"], "not every account in scope is low")
        self.assertIn("computed_at", body["decision"])
        self.assertEqual(set(body["poll"]), {"last_ok_at", "last_error", "reads", "running"})

    def test_refresh_is_an_operator_route_that_answers_with_a_snapshot(self):
        from unittest import mock
        from lokki_core.quota import poller
        with mock.patch.object(poller.POLLER, "refresh", return_value=poller.POLLER.latest()) as m:
            status, body = json_post(self.port, "/api/accounts/refresh", {})
            self.assertEqual(status, 200)
            m.assert_called_once()
        self.assertIn("accounts", body)
        config.TOKENS = (("app", TOKEN, "operator"), ("default", "cli-tok", "client"))
        self.assertEqual(json_post(self.port, "/api/accounts/refresh", {}, token="cli-tok")[0], 403)
        self.assertEqual(json_get(self.port, "/api/accounts", token="cli-tok")[0], 403)
```

(`json_post(port, path, body, token=TOKEN, timeout=5)` is a helper to add beside `json_get` in `test_offline.py`: a POST with `X-Relay-Token`, `Content-Type: application/json` and a JSON body, returning `(status, json)`; `(e.code, None)` on an `HTTPError`, as `json_get` does. `test_control.py` imports it with the rest.)

`ControlRouteTests.test_status_names_the_route_counts_the_revision_and_whether_cliproxy_answers` asserted `decision` and `poll` are `None`; now: `self.assertEqual(set(body["decision"]), {"relay_on", "reason", "computed_at"})` and `self.assertEqual(set(body["poll"]), {"last_ok_at", "last_error", "reads", "running"})`.

- [ ] **Step 2: Run to see them fail**

Run: `cd core && python3 -m unittest test_control.AccountsRouteTests 2>&1 | tail -3`
Expected: 404 or 403 from the routes.

- [ ] **Step 3: Implement**

`control.py`:

```python
def accounts(decision):
    from .quota import poller
    snap = poller.POLLER.latest()
    now = time.time()
    rows = []
    for idx, a in sorted((snap or {}).get("accounts", {}).items()):
        state = decision.per_account.get(idx, "unknown")
        used = a.get("used_percent")
        rows.append({"auth_index": idx, "label": a.get("label"), "email_masked": a.get("email_masked"),
                     "enabled": a.get("enabled"), "used_percent": used,
                     "remaining_percent": (100 - used) if isinstance(used, (int, float)) else None,
                     "reset_at": a.get("reset_at"), "state": state, "error": a.get("error"),
                     "in_scope": idx in decision.per_account})
    return {"read_at": (snap or {}).get("read_at"), "age_s": round(now - snap["read_at"], 1) if snap else None,
            "accounts": rows, "decision": {"relay_on": decision.relay_on, "reason": decision.reason}}
```

`status()` is called as `control.status(decision={"relay_on": d.relay_on, "reason": d.reason, "computed_at": <iso now>}, poll=poller.POLLER.status())` from the handler, where `d = routing.decide_now()`. Routes in `handler.py`: `/api/accounts` in `_OPERATOR_GET` → `self._json(control.accounts(routing.decide_now()))`; `POST /api/accounts/refresh` beside `/api/reload` (guard, token, operator) → `poller.POLLER.refresh(timeout=15.0)` then the same payload.

- [ ] **Step 4: Docs**

- `docs/app/api.md`: the two routes, `status.decision` and `status.poll`, `/healthz.routing`, and a section "Pass-through" with the guards per path (both / relay-only), the header allow-lists, the retry rule, the 502 for a credential refusal, the record fields, and what the probe (Task 4 Step 1) found for `stream: false`.
- `docs/app/limits.md` (new): the usage endpoint is unofficial and read once per `poll_seconds` per account with the client identity the probe sends; if the provider changes or refuses it, every account becomes unknown and the UI banner says so; the CLIProxyAPI panel downloads its assets from GitHub at run time unless `management.disable-control-panel` is set. (Only what this sub-project knows; later sub-projects add their own sections.)
- `docs/relay/relay-8371.md`, in "Serving a real client" or beside the `itemdone` section: one paragraph saying which guards run on both paths and which only on the relay path (`previous_response_id`, `store: true`), with a pointer to `docs/app/api.md`.
- `README.md`: a short "Low Quota Mode" paragraph under "Using it": what it does, the three `routing` words, that unknown means direct, where to read the decision.
- `docs/README.md`: index `limits.md`.
- `docs/superpowers/specs/2026-10-10-lokki-app-design.md`, the departures named at the top of this plan: 4.2's `"routing"` reason becomes `"routing: relay"` / `"routing: direct"`; 4.3's record sentence says "written in a `finally`, so a client disconnect (`cut: client_gone`) is recorded too; a drain that expires ends the process with `os._exit` and records nothing on either path"; 4.3's response bullet gains "except 401, 403 and 407, which are about the core's credential and become a 502, as on the relay path"; 4.6's test list drops "on drain" and names the 502 for a credential refusal.

- [ ] **Step 5: Run the suite, commit**

Run: `cd core && python3 -m unittest discover -s . -p "test_*.py" 2>&1 | tail -3 && python3 -c "import server"`
Expected: `OK`, no output from the import.

```bash
git add -A && git commit -m "core: /api/accounts and the refresh, the decision and the poll in status, the docs"
```

---

## Self-review

- **Spec coverage.** 4.1 Task 1 (reader) and Task 3 (poller, timeouts, parallel reads, refresh join, secret from file, version check, 404 message); 4.2 Task 2 (policy, every rule, unknown, reset, window) and Task 4 (`routing.decide_now`, the `[routing]` line, `/healthz`); 4.3 Task 4 (guards per path, raw bytes or the knob set, headers both ways, the client's `Authorization` dropped, retry rule, tee, record in finally, `active_run`, non-streaming, `/api/run` untouched); 4.4 Task 5; 4.5 Task 4 (`cliproxy_down`, the 502 and its line) and Task 3 (`last_error`, said once per change); 4.6 every test named is in Tasks 1 to 5, except "on drain", which Task 5 removes from the spec.
- **Placeholders.** None.
- **Type consistency.** `reader.read(management_url, secret, accounts=(), per_call_timeout, deadline, cliproxy_version)` is what `poller._one_read` calls and what `FakeRead` accepts; `policy.decide(snapshot, low_quota, routing, now)` is what `routing.decide_now` calls; `Decision.per_account` is what `control.accounts` reads, filled whenever a snapshot exists; `poller.POLLER.latest()/status()/refresh()` are what `routing`, `control` and the handler call; `poller.management_url()` reads `config.UPSTREAM` through `control.without_credentials`; `handler.RELAY_KNOB_PARAMS`, `_MODEL_NAME` and `DEFAULT_MODEL` are what `passthrough.serve` imports; `engine.open_upstream(req, rec, retries)` returns the response or raises `HTTPError` for a 4xx, as `passthrough.serve` expects.
- **Review Focus.** 1 → Task 2 `test_zero_used_is_ok`; 2 → Task 2 `test_staleness_boundary`; 3 → Task 4 `test_a_knob_is_removed_and_the_rest_reserialized`; 4 → Task 4 `test_a_stream_broken_after_the_first_byte_is_not_retried`; 5 → Task 3 `test_a_reload_wakes_the_poller`.
