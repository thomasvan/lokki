"""Adding a Codex account through CLIProxyAPI's management API, which runs the
provider's browser login: it builds the login URL, holds the PKCE state,
exchanges the provider's code and writes the credential. The core holds no
credential and opens no browser; it holds one thing, the listener for the
provider's callback.

The login URL sends the browser back to `http://localhost:1455/auth/callback`.
CLIProxyAPI's own forwarder for it (`is_webui=true`) binds every interface, so
the core listens there instead, on 127.0.0.1 only. A browser on another machine
ends on its own `localhost:1455`, and the user pastes that address into the
paste route. Both hand the answer on through `hand_on`, to CLIProxyAPI's
`oauth-callback` route, which takes no management key: once per login, and only
for the pending one. The routes are CLIProxyAPI's legacy `/v0/` management API
(docs/app/limits.md).

No answer, page or log here carries the code or the address: the code is held
only for the call that hands it on, and CLIProxyAPI's texts reach the caller only
when they are one of its fixed sentences, since the exchange's cause is the
provider's body, which may repeat what the request carried.
"""
import html
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .reader import _TRANSPORT_ERRORS, ManagementError, _request

CALLBACK_PORT = 1455        # the port in the login URL CLIProxyAPI builds; `codex login` uses it too
WAIT_SECONDS = 300.0        # CLIProxyAPI's own wait for the callback: a later one earns its 409
_AUTH_URL = "/v0/management/codex-auth-url"
_AUTH_STATUS = "/v0/management/get-auth-status"
_HAND_ON = "/v0/management/oauth-callback"
_READ_TIMEOUT = 10.0        # a browser that connects and sends nothing is dropped after this
PENDING, OK, ERROR = "pending", "ok", "error"
_STATUSES = {"wait": PENDING, "ok": OK, "error": ERROR}      # the binary's word to the core's


# What `hand_on` answers (docs/app/api.md has what each entry says for them).
ACCEPTED, NOT_WAITING, ANOTHER_LOGIN, ALREADY_ANSWERED = "accepted", "not waiting", "another login", "already answered"
EXPIRED, REFUSED, UNREACHABLE = "expired", "refused", "unreachable"
# The session statuses CLIProxyAPI 8.0.23 sets for a login, word for word; any other
# text may be the provider's body (the exchange's cause), which may repeat the code.
PUBLIC_ERRORS = ("Timeout waiting for OAuth callback", "Bad request", "Bad Request", "State code error",
                 "Failed to exchange authorization code for tokens", "Failed to save authentication tokens",
                 "Authentication failed")
_EXCHANGE = "Failed to exchange authorization code for tokens"
# `get-auth-status`'s own answer for a state it no longer holds. It logs nothing there, and
# keeps a finished login only a minute, so this may be a login that succeeded.
_GONE = {"unknown or expired state": "CLIProxyAPI no longer holds this login: it expired, or it finished "
                                     "more than a minute ago; check Accounts, else start again"}
_IN_FLIGHT = "in flight"    # a login's answer while CLIProxyAPI is asked
_CALLBACK_HOSTS = ("localhost", "127.0.0.1")
_CALLBACK_PATH = "/auth/callback"


class CallbackPortBusy(Exception):
    """The callback port is held: another login, or `codex login` itself."""


class _CallbackServer(ThreadingHTTPServer):
    # SO_REUSEADDR on Windows lets a second listener bind a port in use: another core's
    # login would share the port with this one, and either could take the code. POSIX
    # keeps it, for a port whose last login left connections in TIME_WAIT.
    allow_reuse_address = os.name != "nt"


def _get(url, secret, timeout, route):
    try:
        status, body = _request(url, secret, timeout=timeout)
    except ValueError:
        raise ManagementError("the management URL is unusable") from None
    except _TRANSPORT_ERRORS as e:
        raise ManagementError(f"management API unreachable: {type(e).__name__}") from None
    if status != 200:
        raise ManagementError(f"HTTP {status} on the {route} route", status=status)
    try:
        answer = json.loads(body)
    except ValueError:
        raise ManagementError(f"the {route} route did not answer JSON") from None
    if not isinstance(answer, dict):
        raise ManagementError(f"the {route} route did not answer an object")
    return answer


def start(management_url, secret, timeout=10.0):
    """`{state, url}` of a new login. No `is_webui`: with it CLIProxyAPI binds 0.0.0.0:1455."""
    answer = _get(management_url.rstrip("/") + _AUTH_URL, secret, timeout, "auth-url")
    if not isinstance(answer.get("state"), str) or not isinstance(answer.get("url"), str):
        raise ManagementError("the auth-url route answered without a state and a URL")
    return {"state": answer["state"], "url": answer["url"]}


def public_error(text):
    """CLIProxyAPI's text when it is one of its fixed sentences, else a sentence of ours."""
    if text in PUBLIC_ERRORS:
        return text
    if text in _GONE:
        return _GONE[text]
    if text.startswith(_EXCHANGE):
        return _EXCHANGE + "; CLIProxyAPI's log has the detail"
    return "the login failed; CLIProxyAPI's log has the detail"


def status(management_url, secret, state, timeout=10.0):
    """`(status, error)`: status is `pending` (CLIProxyAPI's `wait`), `ok` or `error`,
    and error is `public_error` of CLIProxyAPI's text, or None when it gave none."""
    url = management_url.rstrip("/") + _AUTH_STATUS + "?" + urllib.parse.urlencode({"state": state})
    answer = _get(url, secret, timeout, "auth-status")
    if answer.get("status") not in _STATUSES:
        raise ManagementError("the auth-status route answered an unknown status")
    error = answer.get("error")
    return _STATUSES[answer["status"]], (public_error(str(error)) if error else None)


def callback_query(query):
    """`(state, code, error)` of a callback's query, or None. Every name at most once, a
    blank value counting as given; `state` non-empty; exactly one of `code` and `error`,
    non-empty. Other names (the provider adds `scope`) are ignored and never handed on."""
    names = urllib.parse.parse_qs(query, keep_blank_values=True)
    if any(len(values) != 1 for values in names.values()):
        return None
    state, code, error = (names.get(k, [""])[0] for k in ("state", "code", "error"))
    if not state or ("code" in names) == ("error" in names) or not (code or error):
        return None
    return state, code, error


def pasted_callback(text):
    """`callback_query` of a pasted address that is the callback's, after trimming: `http`,
    host `localhost` or `127.0.0.1`, port `CALLBACK_PORT`, path `/auth/callback`, no user
    part and no fragment. `urlsplit` drops tabs and line breaks anywhere in it, which
    mends a paste that wrapped. None for anything else, an address the parser refuses included:
    its error quotes what it could not read, which is the user's text."""
    text = text.strip()
    try:
        parts = urllib.parse.urlsplit(text)
        if (parts.scheme != "http" or parts.hostname not in _CALLBACK_HOSTS or parts.port != CALLBACK_PORT
                or parts.path != _CALLBACK_PATH or "@" in parts.netloc or "#" in text):
            return None
    except ValueError:
        return None
    return callback_query(parts.query)


def hand_on(state, code, error):
    """Hand a login's answer on to CLIProxyAPI, once, and only for the pending login: one
    of ACCEPTED, NOT_WAITING (no login pending, or its wait passed), ANOTHER_LOGIN (not the
    pending login's state, or CLIProxyAPI has not named it yet), ALREADY_ANSWERED, EXPIRED
    (CLIProxyAPI 404), REFUSED (409), UNREACHABLE (any other status, no answer, or any
    failure of the post; the login may then be answered again). The login is marked
    before the call, so two entries at once cannot both hand on; CLIProxyAPI takes a second
    callback while it exchanges the first. A later answer for it is ALREADY_ANSWERED while
    the call runs or after ACCEPTED, and EXPIRED or REFUSED again after those: CLIProxyAPI
    will not finish that login. Only the status is read: CLIProxyAPI's body may carry its
    texts, and an exception's text may carry the code, so neither is kept."""
    with _pending_lock:
        login = _pending
        if login is None or time.monotonic() >= login.deadline:
            return NOT_WAITING
        if login.state is None or login.state != state:
            return ANOTHER_LOGIN
        if login.answer in (_IN_FLIGHT, ACCEPTED):
            return ALREADY_ANSWERED
        if login.answer is not None:
            return login.answer
        login.answer = _IN_FLIGHT
        url = login.hand_on_url
    result = UNREACHABLE
    try:
        answered, _ = _request(url, None, data=json.dumps({"code": code, "state": state, "error": error}).encode())
        result = {200: ACCEPTED, 404: EXPIRED, 409: REFUSED}.get(answered, UNREACHABLE)
    except Exception:  # noqa: BLE001 - any failure of the post is UNREACHABLE, and its text is not read
        pass
    finally:
        with _pending_lock:
            login.answer = None if result == UNREACHABLE else result
    return result


_PAGES = {ACCEPTED: (200, "You can close this tab."),
          NOT_WAITING: (409, "This login expired; start again."),
          EXPIRED: (409, "This login expired; start again."),
          ANOTHER_LOGIN: (409, "This page belongs to another login; start again from Lokki."),
          ALREADY_ANSWERED: (409, "Lokki already has this login's answer; you can close this tab."),
          REFUSED: (409, "CLIProxyAPI did not take this login; start again."),
          UNREACHABLE: (502, "CLIProxyAPI did not take this login; start again.")}


class Callback:
    """The listener for one pending login, on 127.0.0.1:`port`, and that login: its
    `state` once CLIProxyAPI names it, its deadline `wait` seconds from the bind, what
    became of the answer handed on (`hand_on`), and where to hand it; these are read and written under
    `_pending_lock`. `GET /auth/callback` whose query `callback_query` reads goes to
    `hand_on`, and the browser gets its page; anything else is a 404 with nothing handed
    on. It closes itself after `wait` seconds.

    When `close()` returns, the port is free and the serve loop has ended, so no new
    request is accepted. A request accepted before it may still be in its handler: a
    daemon thread, ended by its own bounds, `_READ_TIMEOUT` for a silent browser and
    the hand-on's 10 s timeout, and it may still hand its one callback on."""

    def __init__(self, port=CALLBACK_PORT, wait=WAIT_SECONDS):
        self.port, self.wait = port, wait
        self.state = None                   # the flow it belongs to, once CLIProxyAPI named it
        self.deadline = None
        self.answer = None                  # None, _IN_FLIGHT, or hand_on's result other than UNREACHABLE
        self.hand_on_url = None
        self.address = None
        self._server = self._timer = self._thread = None
        self._lock = threading.Lock()

    def start(self, management_url):
        self.hand_on_url = management_url.rstrip("/") + _HAND_ON

        class Handler(BaseHTTPRequestHandler):
            timeout = _READ_TIMEOUT

            def do_GET(self):
                try:
                    parts = urllib.parse.urlsplit(self.path)
                except ValueError:
                    parts = None
                answer = callback_query(parts.query) if parts and parts.path == _CALLBACK_PATH else None
                if answer is None:
                    self._page(404, "Not found.")
                    return
                self._page(*_PAGES[hand_on(*answer)])

            def _page(self, code, text):
                page = f"<!doctype html><title>Lokki</title><p>{html.escape(text)}</p>".encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *args):
                pass                         # the default writes the query, the code included, to stderr

        try:
            server = _CallbackServer(("127.0.0.1", self.port), Handler)
        except OSError:
            raise CallbackPortBusy("another login is in progress") from None
        timer = threading.Timer(self.wait, self.close)
        timer.daemon = True
        thread = threading.Thread(target=server.serve_forever, name="lokki-oauth-callback", daemon=True)
        self.deadline = time.monotonic() + self.wait
        with self._lock:
            self._server, self._timer, self._thread = server, timer, thread
            self.address = server.server_address
        thread.start()
        timer.start()

    def close(self):
        with self._lock:
            server, timer, thread = self._server, self._timer, self._thread
            self._server = self._timer = self._thread = None
        if timer is not None:
            timer.cancel()
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join()


_pending = None
_pending_lock = threading.Lock()


def listen(management_url):
    """The listener for a new flow on `CALLBACK_PORT`, replacing a pending one. Raises
    `CallbackPortBusy`."""
    global _pending
    with _pending_lock:
        if _pending is not None:
            _pending.close()
            _pending = None
        callback = Callback(port=CALLBACK_PORT, wait=WAIT_SECONDS)
        callback.start(management_url)
        _pending = callback
        return callback


def named(callback, state):
    """CLIProxyAPI named the login `callback` listens for: `state` is the one it takes."""
    with _pending_lock:
        callback.state = state


def waiting(state):
    """Whether `state` is the pending login's, within its wait."""
    with _pending_lock:
        return _pending is not None and _pending.state == state and time.monotonic() < _pending.deadline


def close_pending(state=None):
    """Close the pending listener; given a state, only when it is that flow's."""
    global _pending
    with _pending_lock:
        if _pending is not None and (state is None or _pending.state == state):
            _pending.close()
            _pending = None
