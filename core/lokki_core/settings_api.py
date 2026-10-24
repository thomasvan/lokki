"""The write path for the settings file: `PUT /api/settings`.

One lock around read, merge, validate, write and apply, so two concurrent writes
cannot lose an update, and the reload thread (handler) takes the same lock so a
write never races a reload. A refusal writes nothing. The file is written at its
real path, so a symlink keeps its link, as UTF-8 and without comments; the schema
carries every key's meaning. A value that would be written but would not decide
the running one (an environment variable or another key wins) is refused, because
the write would read as applied. A port that changes is refused when its service
could not listen on it at the next start, so a restart never stops a service it
cannot start again."""
import os
import socket
import tempfile
import threading

import yaml

from . import config, control, migrate, schema
from .core import _text
from .quota.oauth import CALLBACK_PORT

LOCK = threading.RLock()
# The core's listener is IPv4 (an AF_INET server), so an IPv6 loopback address never starts.
_LOOPBACK = ("127.0.0.1", "localhost")


class Refused(Exception):
    def __init__(self, status, message, key=None):
        super().__init__(message)
        self.status, self.message, self.key = status, message, key


def _flatten(body, prefix=""):
    for k, v in body.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and any(f.key.startswith(key + ".") for f in schema.FIELDS):
            yield from _flatten(v, key + ".")
        else:
            yield key, v


def _check_writable_keys(body):
    for key, _ in _flatten(body):
        try:
            field = schema.get(key)
        except KeyError:
            raise Refused(400, f"{key} is not a setting", key)
        if not field.writable:
            raise Refused(400, f"{key} is not settable through the API", key)


def _check_listen(body):
    if "listen" in body and not isinstance(body["listen"], list):
        raise Refused(400, "listen must be a list of addresses", "listen")
    for addr in body.get("listen") or []:
        host = str(addr).rpartition(":")[0] if ":" in str(addr) else str(addr)
        if host not in _LOOPBACK:
            raise Refused(400, "listen through the API takes an IPv4 loopback address, 127.0.0.1 "
                               "or localhost: the core listens on IPv4 only", "listen")


def _check_env(body, env):
    for key, _ in _flatten(body):
        field = schema.get(key)
        if field.env and _text(env.get(field.env)):
            raise Refused(409, f"{field.env} is set in the environment and wins over the file; "
                               f"unset it to change {key} here", key)


def _check_upstream(body, loaded, env):
    """The upstream is the first of CLIPROXY_URL, the old `upstream` key and
    `cliproxy.url`, else derived from `cliproxy.port` (`config.resolve_settings`)."""
    cp = body.get("cliproxy") or {}
    file_cp = loaded.get("cliproxy") if isinstance(loaded.get("cliproxy"), dict) else {}
    for key in ("url", "port"):
        if key not in cp:
            continue
        name = f"cliproxy.{key}"
        if _text(env.get("CLIPROXY_URL")):
            raise Refused(409, f"CLIPROXY_URL is set in the environment and decides the upstream; "
                               f"unset it to change {name} here", name)
        if _text(loaded.get("upstream")):
            raise Refused(409, f"the file's upstream key decides the upstream; remove it "
                               f"(the app's setting is cliproxy.url; upstream is a server deployment's) to change {name} here", name)
        if key == "port" and "url" not in cp and _text(file_cp.get("url")):
            raise Refused(409, "cliproxy.url is set and decides the upstream; clear it in the "
                               "same body to use cliproxy.port", name)


def _port_of(addr):
    return int(addr.rpartition(":")[2])


def _unusable(host, port):
    """Why the next start could not listen on `host:port`, or None. Asked as the start
    asks it: the shell refuses a port that already accepts on 127.0.0.1, and the core
    and CLIProxyAPI both bind with SO_REUSEADDR on POSIX and without it on Windows."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return f"port {port} is in use by another program"
    except OSError:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            s.listen(1)
        except OSError as e:
            return f"Lokki cannot listen on {host}:{port}: {e.strerror or e}"
    return None


def _check_ports(body, resolved, bound):
    """The core's port and CLIProxyAPI's, as the next start would use them. A port the
    same service listens on now is not probed: it holds it."""
    moved = []          # (key, host, port, the ports its service holds now)
    if "listen" in body:
        held = {_port_of(a) for a in bound}
        moved += [("listen", a.rpartition(":")[0], _port_of(a), held) for a in resolved["listen"]]
    if "port" in (body.get("cliproxy") or {}):
        moved.append(("cliproxy.port", "127.0.0.1", resolved["cliproxy"]["port"], {config.STARTED_CLIPROXY_PORT}))
    for key, _, port, _ in moved:
        if not 1 <= port <= 65535:
            raise Refused(400, f"a port is a whole number from 1 to 65535, not {port}", key)
        if port == CALLBACK_PORT:
            raise Refused(400, f"port {port} is where a Codex login returns to; choose another port", key)
    shared = resolved["cliproxy"]["port"]
    if moved and shared in {_port_of(a) for a in resolved["listen"]}:
        raise Refused(400, f"the core and CLIProxyAPI cannot both use port {shared}; choose another port", moved[0][0])
    for key, host, port, held in moved:
        why = None if port in held else _unusable(host, port)
        if why:
            raise Refused(409, f"{why}; choose another port", key)


def _merge(base, body):
    out = dict(base)
    for k, v in body.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _writable(real):
    return os.access(real, os.W_OK) and os.access(os.path.dirname(real), os.W_OK)


def put(body, path, env, bound=()):
    """`bound`: the addresses this process listens on."""
    if not isinstance(body, dict) or not body:
        raise Refused(400, "the body must be a JSON object with at least one key")
    _check_writable_keys(body)
    _check_listen(body)
    cp = body.get("cliproxy") or {}
    if cp.get("url") and cp.get("port") is not None:
        raise Refused(400, "set cliproxy.url or cliproxy.port, not both", "cliproxy")
    with LOCK:
        _check_env(body, env)
        real = os.path.realpath(path or "")
        if not path or not os.path.isfile(real):
            raise Refused(409, "settings are read-only here: no settings file is loaded")
        if not _writable(real):
            raise Refused(409, f"settings are read-only here: {real} is not writable")
        try:
            old_text, loaded = config.read_config(real)
        except config.ConfigError as e:
            raise Refused(409, str(e))
        # As the loader reads it, so the write leaves a retired key out and keeps the mode it chose.
        migrate.migrate(loaded)
        _check_upstream(body, loaded, env)
        merged = _merge(loaded, body)
        try:
            resolved = config.resolve_settings(merged, env=env, path=real)
        except config.ConfigError as e:
            raise Refused(400, str(e), _key_of(e, body))
        if not resolved.get("tokens"):
            # The reload below would refuse it and leave the file written but not applied.
            raise Refused(409, "the settings file names no client token; the reload would "
                               "refuse it, so nothing is written")
        _check_ports(body, resolved, bound)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".config.", suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(merged, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
            try:
                os.chmod(tmp, os.stat(real).st_mode & 0o777)
            except OSError:
                pass
            os.replace(tmp, real)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise Refused(500, f"the settings file could not be written: {e.strerror or e}")
        messages = []
        applied = config.reload_settings(config_path=path, env=env, log=messages.append)
        if applied is None:
            raise Refused(500, "written but not applied: " + "; ".join(messages))
        held = applied["upstream"] != config.UPSTREAM      # the reload kept the running upstream
    keys = [k for k, _ in _flatten(body)]
    later = [k for k in keys if schema.get(k).startup_only or (held and k == "cliproxy.url")]
    return {"applied": [k for k in keys if k not in later],
            "restart_required": later,
            "comments_dropped": any(line.lstrip().startswith("#") for line in old_text.splitlines())}


def _key_of(error, body):
    text = str(error)
    for key, _ in _flatten(body):
        if key in text or key.split(".")[-1] in text:
            return key
    return None


def view(settings, env):
    """The settings as loaded, secrets masked, with what the environment overrides."""
    out = {}
    for f in schema.FIELDS:
        top, _, rest = f.key.partition(".")
        if top == "auth":
            continue
        if rest:
            value = (settings.get(top) or {}).get(rest)
        else:
            value = settings.get(f.setting or f.key)
        if f.secret:
            value = "***" if value else value
        elif f.key == "upstream":
            # The old key and CLIPROXY_URL load unvalidated, so it can carry a password.
            value = control.without_credentials(value)
        if rest:
            out.setdefault(top, {})[rest] = value
        else:
            out[f.key] = value
    out["auth"] = {"tokens": [{"name": e[0], "token": "***", "operator": len(e) > 2 and e[2] == "operator"}
                              for e in settings.get("tokens") or ()]}
    overridden = {f.key for f in schema.FIELDS if f.env and _text(env.get(f.env))}
    if "upstream" in overridden:          # CLIPROXY_URL decides what both of these would set
        overridden |= {"cliproxy.url", "cliproxy.port"}
    out["env_overrides"] = sorted(overridden)
    path = settings.get("config_path")
    out["writable"] = bool(path) and _writable(os.path.realpath(path))
    out["unknown_keys"] = settings.get("unknown_keys", [])
    return out
