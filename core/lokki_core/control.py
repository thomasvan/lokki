"""The control routes the UI and the shell read, the static UI files, and the
Host/Origin guard. Read-only here; the write path is settings_api."""
import importlib.metadata
import json
import mimetypes
import os
import re
import socket
import time
import urllib.parse

from . import config, core, schema
from .core import ResponseError

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_CHECKOUT = core.HERE              # core/ in a checkout; site-packages when installed
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
OAUTH_START = "/api/accounts/oauth"
OAUTH_PREFIX = OAUTH_START + "/"            # then the state
OAUTH_SEND_LIMIT = 16384                    # the pasted address's body, in bytes
# The binary answers `ok` to an empty state: a malformed one never reaches it.
_OAUTH_STATE = re.compile(r"[A-Za-z0-9_-]{8,128}", re.ASCII)
STARTED_AT = time.time()


def version():
    """core/VERSION when the package runs from its checkout, which an older install
    of lokki-core in the same environment must not shadow; else the installed
    distribution's, which pyproject took from that file; else, as in the runtime
    image, which copies the package and VERSION and installs nothing, the file.
    A VERSION in site-packages is never read: an install always has metadata."""
    if not os.path.isfile(os.path.join(_CHECKOUT, "pyproject.toml")):
        try:
            return importlib.metadata.version("lokki-core")
        except importlib.metadata.PackageNotFoundError:
            pass
    try:
        with open(os.path.join(_CHECKOUT, "VERSION")) as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def without_credentials(url):
    """The URL with any `user:password@` taken out. `cliproxy.url` refuses one, but
    the old `upstream` key and CLIPROXY_URL load what they always loaded."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:              # unparsable: keep nothing before an `@`
        return url.rpartition("@")[2]
    if "@" not in parts.netloc:
        return url
    return urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


def cliproxy_reachable(upstream, timeout=1.0):
    """A TCP connect to the upstream's host and port: CLIProxyAPI has no health
    route the core knows, and a connect is enough for 'is it answering'."""
    try:
        parts = urllib.parse.urlsplit(upstream or "")
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    if not parts.hostname:          # a None host would connect to this machine
        return False
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def status(decision=None, poll=None):
    s = config._settings
    # config.UPSTREAM, not s["upstream"]: a reload holds a port-derived upstream, so
    # the loaded one can name a port nothing listens on until the next start.
    upstream = config.UPSTREAM
    return {
        "revision": core.revision(), "version": version(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(STARTED_AT)),
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "routing": config.ROUTING, "low_quota": s.get("low_quota"),
        "decision": decision, "poll": poll,
        "runs_since_start": core.run_counts(),
        "cliproxy": {"url": without_credentials(upstream), "reachable": cliproxy_reachable(upstream),
                     "version": (s.get("cliproxy") or {}).get("version") or None},
        "unknown_keys": s.get("unknown_keys", []),
    }


def accounts(decision, snap):
    """The snapshot's accounts, each with the state the policy gave it, and the
    decision, which was computed from that same snapshot. `poll` says why the
    snapshot may be old. The e-mail is the reader's masked form; nothing here
    unmasks it."""
    from .quota import poller               # local: the poller imports this module
    rows = []
    for idx, a in sorted((snap or {}).get("accounts", {}).items()):
        used = a.get("used_percent")
        rows.append({"auth_index": idx, "label": a.get("label"), "email_masked": a.get("email_masked"),
                     "enabled": a.get("enabled"), "used_percent": used,
                     "remaining_percent": (100 - used) if isinstance(used, (int, float)) else None,
                     "reset_at": a.get("reset_at"), "state": decision.per_account.get(idx, "unknown"),
                     "error": a.get("error"), "in_scope": idx in decision.watched})
    return {"read_at": (snap or {}).get("read_at"),
            "age_s": round(time.time() - snap["read_at"], 1) if snap else None,
            "accounts": rows, "decision": {"relay_on": decision.relay_on, "reason": decision.reason},
            "poll": poller.POLLER.status()}


def _management():
    """CLIProxyAPI's management URL and secret, or a 409 when the secret cannot be read."""
    from .quota import poller, reader       # local: the poller imports this module
    try:
        return poller.management_url(), poller.management_secret(config.CLIPROXY)
    except reader.ManagementError as e:
        raise ResponseError(409, str(e)) from None


def models():
    """`{models: [id, …]}` from CLIProxyAPI's `/v1/models`: the string ids, sorted, once each.
    Read with the core's key through the reader's opener. A failure is one of three fixed
    sentences; an exception's text, which can name the address, is never the answer."""
    from .quota import reader
    try:
        status, body = reader._request(config.UPSTREAM + "/v1/models", config.API_KEY, timeout=10.0)
    except Exception:
        raise ResponseError(502, "CLIProxyAPI did not answer") from None
    if status != 200:
        raise ResponseError(502, f"CLIProxyAPI answered {status}")
    try:
        data = json.loads(body).get("data")
    except (ValueError, AttributeError, RecursionError):
        data = None
    if not isinstance(data, list):
        raise ResponseError(502, "CLIProxyAPI's model list has no data")
    return {"models": sorted({m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)})}


def oauth_start():
    """`{state, url}` of a new Codex login, its callback listener bound. In this order:
    the secret (409), the listener (409 when the port is held), CLIProxyAPI (502, and
    the listener closes)."""
    from .quota import oauth, reader
    url, secret = _management()
    try:
        listener = oauth.listen(url)
    except oauth.CallbackPortBusy:
        raise ResponseError(409, "another login is in progress (the callback port is held, "
                                 "by another login or by `codex login`)") from None
    try:
        flow = oauth.start(url, secret)
    except reader.ManagementError as e:
        oauth.close_pending()
        raise ResponseError(502, f"CLIProxyAPI did not start the login: {e}") from None
    oauth.named(listener, flow["state"])
    return flow


# What the paste route answers for each `hand_on` result; ACCEPTED is its 200.
_GONE = "this login is no longer waiting; start again"
_HANDED_ON = {"not waiting": (409, _GONE), "another login": (409, _GONE),
              "already answered": (409, "Lokki already has this login's answer; wait for it to finish"),
              "expired": (409, "this login expired; start again"),
              "refused": (409, "CLIProxyAPI did not take this login; start again"),
              "unreachable": (502, "CLIProxyAPI did not take this login")}


def oauth_send(state, raw):
    """`{"handed_on": true}` once the pasted address of the login `state` is handed on.
    Every refusal is a fixed sentence: none carries the address, the code or CLIProxyAPI's
    text."""
    from .quota import oauth
    if not _OAUTH_STATE.fullmatch(state):
        raise ResponseError(400, f"the state must match {_OAUTH_STATE.pattern}")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("url"), str):
        raise ResponseError(400, 'the body must be {"url": "<the address the browser ended on>"}')
    answer = oauth.pasted_callback(body["url"])
    if answer is None:
        raise ResponseError(400, "this is not the address the login ended on: it starts "
                                 "http://localhost:1455/auth/callback?")
    if answer[0] != state:
        if not oauth.waiting(state):
            raise ResponseError(409, _GONE)
        raise ResponseError(409, "this address is from another login; paste the address of the login started last")
    result = oauth.hand_on(*answer)
    if result != oauth.ACCEPTED:
        raise ResponseError(*_HANDED_ON[result])
    return {"handed_on": True}


def oauth_status(state):
    """`{status, error}` of a login. `ok` and `error` end the flow: its listener closes,
    and `ok` wakes the poller so the account shows at its next read."""
    from .quota import oauth, poller, reader
    if not _OAUTH_STATE.fullmatch(state):
        raise ResponseError(400, f"the state must match {_OAUTH_STATE.pattern}")
    url, secret = _management()
    try:
        status, error = oauth.status(url, secret, state)
    except reader.ManagementError as e:
        # A 401 or a 403 too: polling on would walk into CLIProxyAPI's ban.
        raise ResponseError(502, f"CLIProxyAPI did not answer the login's status: {e}") from None
    if status == oauth.PENDING and not oauth.waiting(state):
        # CLIProxyAPI still holds a login this core no longer takes an answer for (a newer login replaced it,
        # the core restarted, or its 300 s passed): it cannot finish, so the poll ends at once.
        status, error = oauth.ERROR, _GONE
    if status != oauth.PENDING:
        oauth.close_pending(state)
    if status == oauth.OK:
        poller.POLLER.wake()
    return {"status": status, "error": error}


def endpoint(reveal, bound):
    token = next((e[1] for e in config.TOKENS if e[0] == "default"), None)
    out = {"urls": [f"http://{addr}/v1" for addr in bound],
           "key_masked": ("…" + token[-4:]) if token else None}
    if reveal and token:
        out["key"] = token
    return out


def schema_listing():
    return schema.listing()


def static_file(path):
    """(bytes, content type) for a UI file, or None. `/` is index.html. Only a
    path that resolves inside STATIC_DIR, after URL-decoding."""
    rel = urllib.parse.unquote(path)
    if "\0" in rel:                # realpath raises on one
        return None
    rel = "index.html" if rel in ("", "/") else rel.removeprefix("/static").lstrip("/")
    full = os.path.realpath(os.path.join(STATIC_DIR, rel))
    root = os.path.realpath(STATIC_DIR)
    try:
        inside = os.path.commonpath([full, root]) == root
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(full):
        return None
    with open(full, "rb") as f:
        return f.read(), mimetypes.guess_type(full)[0] or "application/octet-stream"


_HOST = re.compile(r"(\[[^\]]*\]|[^:\[\]]*)(?::\d+)?", re.ASCII)


def _host_of(header):
    """The host of a Host header or a listen address, lower case, or "" when the
    text is not a host and an optional numeric port: `[::1]evil` is not `[::1]`."""
    match = _HOST.fullmatch((header or "").strip().lower())
    return match.group(1) if match else ""


def host_allowed(host_header, listen):
    host = _host_of(host_header)
    if not host:
        return False
    if host in _LOOPBACK:
        return True
    return host in {_host_of(addr) for addr in listen}


def origin_allowed(origin_header, host_header):
    if origin_header is None:
        return True
    try:
        parts = urllib.parse.urlsplit(origin_header)
    except ValueError:
        return False
    return (parts.scheme.lower() == "http"             # what the core serves
            and bool(parts.netloc) and parts.netloc.lower() == (host_header or "").strip().lower())
