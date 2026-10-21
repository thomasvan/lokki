"""One snapshot of every codex account's meter, through CLIProxyAPI's
management API, which spends no quota.

Two calls: the credentials list, then one usage call per enabled or named account, in
parallel and bounded. Emails are masked here so nothing downstream can log one.
A per-account failure is a field; a failure of the management API as a whole is
one `ManagementError`."""
import concurrent.futures
import http.client
import json
import time
import urllib.error
import urllib.request

_TRANSPORT_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)

# What the measurement probe sends (billing_probe imports these): `$TOKEN$` is
# replaced by CLIProxyAPI with the account's access token, so no credential
# passes through this process. The provider varies its answer by client; the
# `User-Agent` is the provider's CLI's (`codex-tui`), the one CLIProxyAPI's panel
# sends. An unofficial endpoint read once per `poll_seconds`
# per account: named in `docs/app/limits.md`.
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USAGE_HEADERS = {
    "Authorization": "Bearer $TOKEN$",
    "Content-Type": "application/json",
    "User-Agent": "codex-tui/0.149.1 (Mac OS 15.0; arm64)",
}
MANAGEMENT_MAJOR = "8"          # the `/v8/` route prefix this module uses
_CREDENTIALS = "/v8/management/credentials"
_API_CALL = "/v8/management/requests/api-call"
MAX_PARALLEL = 4


class ManagementError(Exception):
    """The management API as a whole did not answer as expected. `status` is the
    HTTP status that said so, or None when no answer did: the poller holds on a
    401 or a 403, which repeated would earn this address CLIProxyAPI's ban."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# CLIProxyAPI is the core's loopback neighbour, or the operator's own `cliproxy.url`:
# a system proxy must not see the management secret or a provider's callback.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def mask_email(s):
    s = s or ""
    if not s:
        return ""
    local, at, domain = s.partition("@")
    return f"{local[:1]}***{at}{domain}" if at else f"{local[:1]}***"


def _request(url, secret, data=None, timeout=10.0):
    """(status, body) of one management call; `secret` None sends no key."""
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["Authorization"] = f"Bearer {secret}"
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET", headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
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
    except ValueError:
        raise ManagementError("the management URL is unusable") from None
    except _TRANSPORT_ERRORS as e:
        raise ManagementError(f"management API unreachable: {type(e).__name__}") from None
    if status == 404:
        raise ManagementError("management route not found: CLIProxyAPI version?", status=status)
    if status != 200:
        raise ManagementError(f"HTTP {status} on the credentials route", status=status)
    try:
        files = json.loads(body).get("files", [])
    except (ValueError, AttributeError):
        raise ManagementError("the credentials route did not answer JSON") from None
    if not isinstance(files, list):
        raise ManagementError("the credentials route did not answer a list")
    return [f for f in files if isinstance(f, dict) and f.get("provider") == "codex"
            and isinstance(f.get("auth_index"), str)]


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
    rate_limit = inner.get("rate_limit")
    window = rate_limit.get("primary_window") if isinstance(rate_limit, dict) else None
    if not isinstance(window, dict) or not isinstance(window.get("used_percent"), (int, float)) \
            or isinstance(window.get("used_percent"), bool):
        return {"error": "no rate_limit block"}
    return {"used_percent": window["used_percent"], "reset_at": window.get("reset_at"),
            "window_seconds": window.get("limit_window_seconds"), "error": None}


def read(management_url, secret, accounts=(), per_call_timeout=10.0, deadline=None,
         cliproxy_version=""):
    """A snapshot. Every enabled codex account is asked, and every one of `accounts`
    (auth_index values) besides, a disabled one included. `deadline` bounds the whole read in
    seconds, timed from after the credentials call; accounts not read by then
    carry error "deadline". A read already running finishes within its own
    `per_call_timeout`, so the worst case is the credentials call plus `deadline`
    plus `per_call_timeout`."""
    _check_version(cliproxy_version)
    management_url = management_url.rstrip("/")
    entries = _credentials(management_url, secret, per_call_timeout)
    out = {}
    for i, e in enumerate(entries, 1):
        idx = e["auth_index"]
        out[idx] = {"label": f"acct-{i}", "email_masked": mask_email(e.get("email", "")),
                    "enabled": not e.get("disabled", False), "used_percent": None,
                    "reset_at": None, "window_seconds": None, "error": None}
    wanted = [i for i in out if out[i]["enabled"] or i in accounts]
    for i in out:
        if i not in wanted:
            out[i]["error"] = "disabled"
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
