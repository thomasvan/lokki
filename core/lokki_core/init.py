"""First run, owned by the core so the suite tests it: the data directory, the
four secrets, two config files that agree. The shell runs `init` and starts the
sidecars; it writes no file itself."""
import os
import secrets
import socket
import tempfile

import yaml

from . import migrate

CORE_PORT, CLIPROXY_PORT = 8381, 8317
_LAYOUT = ("config.yaml", "cliproxy", "secrets", "logs", "runs.jsonl")     # what `run` makes at the top


class InitError(Exception):
    """A refusal `init`, `sync-cliproxy`, `set-port` or `shell-config` states in one sentence."""


def check_port(port, allow_zero=False):
    """`port` as an int in 1..65535 (0 too when `allow_zero`: any free port), else
    a ValueError. A boolean is not a port."""
    if type(port) is not int or not (0 if allow_zero else 1) <= port <= 65535:
        raise ValueError(f"port must be {'0 or ' if allow_zero else ''}a whole number "
                         f"from 1 to 65535, not {port!r}")
    return port


def free_port(preferred):
    """`preferred` if nothing listens on it (0 = any free port), else the next free
    one above it, up to 65535. A plain bind without SO_REUSEADDR, so a port in use
    fails on every OS."""
    port = check_port(preferred, allow_zero=True)
    for _ in range(200):
        if port > 65535:
            break
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                port += 1
    raise RuntimeError(f"no free port above {preferred}")


def _mkdir(path, mode):
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    if os.name == "nt":
        _owner_only(path)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    _advapi32.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    _advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
                                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    _kernel32.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                                ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                                                wintypes.DWORD]

    def _user_sid():
        """The SID of the user this process runs as, in its string form."""
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            _advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))                  # TokenUser
            buf = ctypes.create_string_buffer(size.value)
            if not _advapi32.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            text = wintypes.LPWSTR()
            if not _advapi32.ConvertSidToStringSidW(ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0],
                                                    ctypes.byref(text)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return text.value
            finally:
                _kernel32.LocalFree(text)
        finally:
            _kernel32.CloseHandle(token)

    def _keeps_acls(path):
        """Whether the volume `path` is on keeps ACLs; True when Windows cannot say, so
        that the DACL is tried and a refusal reported."""
        root = ctypes.create_unicode_buffer(261)
        flags = wintypes.DWORD()
        if not (_kernel32.GetVolumePathNameW(path, root, len(root))
                and _kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0)):
            return True
        return bool(flags.value & 0x00000008)                             # FILE_PERSISTENT_ACLS

    def _owner_only(path):
        """Windows keeps no POSIX mode, so the 0700 above is not enforced there. `path` gets
        a protected DACL in its place, inherited by everything made under it later (the
        files written here, and CLIProxyAPI's credentials): full control for this user and
        for SYSTEM, nobody else. An administrator can still take ownership, as root can
        read a 0600 file. A volume that keeps no ACLs (FAT, exFAT) is passed over, as the
        chmod above is where it fails. Anywhere else a failure is an InitError: on POSIX
        the directory was already made 0700, but here nothing else narrows it, and the
        secrets would be written under the ACL it inherited."""
        if not _keeps_acls(path):
            return
        try:
            sddl = f"D:P(A;OICI;FA;;;{_user_sid()})(A;OICI;FA;;;SY)"
        except OSError as e:
            raise InitError(f"cannot keep {path} to this user: its SID is unreadable ({e})") from None
        sd, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
            raise InitError(f"cannot keep {path} to this user: {ctypes.FormatError(ctypes.get_last_error())}")
        try:
            if not _advapi32.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl),
                                                       ctypes.byref(defaulted)):
                raise InitError(f"cannot keep {path} to this user: {ctypes.FormatError(ctypes.get_last_error())}")
            # SE_FILE_OBJECT; DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
            error = _advapi32.SetNamedSecurityInfoW(path, 1, 0x00000004 | 0x80000000, None, None, dacl, None)
            if error:
                raise InitError(f"cannot keep {path} to this user: {ctypes.FormatError(error)}")
        finally:
            _kernel32.LocalFree(sd)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _dump(doc):
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _write(path, text, mode):
    """UTF-8, created with `mode` and never wider; an existing file keeps the mode
    it has. The text lands in a sibling file first and replaces `path` whole, so a
    failed write leaves the old file."""
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _existing(core_config):
    """What `run` reports for a directory that already has a config file: the
    ports `init` wrote, read the way the loader reads them, or None for a file
    `init` did not make (it lacks the keys `init` writes, or is not a mapping)."""
    from . import config
    ports = {"core_port": None, "cliproxy_port": None}
    if os.path.exists(core_config):
        try:
            doc = _read(core_config)
            listen = config.parse_listen(doc["listen"])
            core_port = int(listen[0].rpartition(":")[2])
            cliproxy_port = check_port(doc["cliproxy"]["port"])
            check_port(core_port)
        except (KeyError, TypeError, ValueError, OSError, yaml.YAMLError, config.ConfigError):
            return None
        ports = {"core_port": core_port, "cliproxy_port": cliproxy_port}
    return ports


def run(data_dir, core_port=CORE_PORT, cliproxy_port=CLIPROXY_PORT, cliproxy_version=""):
    check_port(core_port, allow_zero=True)
    check_port(cliproxy_port, allow_zero=True)
    data_dir = os.path.abspath(data_dir)
    core_config = os.path.join(data_dir, "config.yaml")
    cliproxy_config = os.path.join(data_dir, "cliproxy", "config.yaml")
    present = [p for p in (core_config, cliproxy_config) if os.path.exists(p)]
    if present:
        out = {"created": False, "core_port": None, "cliproxy_port": None,
               "core_config": core_config, "cliproxy_config": cliproxy_config}
        out["migrated"] = []
        ports = _existing(core_config)
        if ports is None:
            out["note"] = (f"{core_config} was not made by init (it lacks the keys init "
                           "writes), nothing changed")
        else:
            out.update(ports)
            out["note"] = f"already initialised: {present[0]} exists, nothing changed"
            if os.path.exists(core_config):
                doc = _read(core_config)
                applied = migrate.migrate(doc)
                if applied:
                    _write(core_config, _dump(doc), 0o600)
                    out["migrated"] = [migrate.said(pair) for pair in applied]
                    out["note"] = (f"already initialised: {present[0]} exists; settings updated: "
                                   + ", ".join(out["migrated"]))
        return out
    # Closing a folder that already holds other files would close them too: 0700 on POSIX
    # shuts other users out of them, and on Windows the protected DACL replaces what each
    # one inherited. An interrupted first run leaves only init's own names, so it is redone.
    try:
        foreign = sorted(name for name in os.listdir(data_dir)
                         if name not in _LAYOUT and not name.startswith(".tmp-"))
    except FileNotFoundError:
        foreign = []
    if foreign:
        raise InitError(f"{data_dir} already holds files init did not make, such as {foreign[0]}; "
                        "nothing changed. Point LOKKI_DATA_DIR at an empty or new folder, or move "
                        "those files out of this one and press Try again")
    for rel in ("", "cliproxy", os.path.join("cliproxy", "auth"), "secrets", "logs"):
        _mkdir(os.path.join(data_dir, rel), 0o700)
    app_token, client_key, cliproxy_key, mgmt_secret = (secrets.token_hex(32) for _ in range(4))
    secret_file = os.path.join(data_dir, "secrets", "cliproxy-mgmt.secret")
    runs_log = os.path.join(data_dir, "runs.jsonl")
    core_port = free_port(core_port)
    cliproxy_port = free_port(cliproxy_port)
    if cliproxy_port == core_port:
        cliproxy_port = free_port(core_port + 1 if core_port < 65535 else 0)
    core_doc = {
        "listen": [f"127.0.0.1:{core_port}"],
        "auth": {"tokens": [{"name": "app", "token": app_token, "operator": True},
                            {"name": "default", "token": client_key}]},
        "cli_proxy_api_key": cliproxy_key,
        "cliproxy": {"port": cliproxy_port, "version": cliproxy_version,
                     "management_secret_file": secret_file, "panel": False},
        "routing": "auto",
        "runs_log": runs_log,
    }
    cliproxy_doc = {
        "server": {"host": "127.0.0.1", "port": cliproxy_port},
        "oauth": {"auth-dir": os.path.join(data_dir, "cliproxy", "auth")},
        "access": {"api-keys": [cliproxy_key]},
        "management": {"secret-key": mgmt_secret, "allow-remote": False,
                       "disable-control-panel": True},
    }
    # The core's file goes last: it is what a second run looks for, so an
    # interrupted first run is redone whole.
    _write(secret_file, mgmt_secret + "\n", 0o600)
    if not os.path.exists(runs_log):
        _write(runs_log, "", 0o600)
    _write(cliproxy_config, _dump(cliproxy_doc), 0o600)
    _write(core_config, _dump(core_doc), 0o600)
    return {"created": True, "core_port": core_port, "cliproxy_port": cliproxy_port,
            "core_config": core_config, "cliproxy_config": cliproxy_config}


def _load(path, section):
    """The text of `path` and its document, which must be a mapping whose `section`
    is a mapping or absent; else an InitError."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        doc = yaml.safe_load(text) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise InitError(f"cannot read {path}: {e}")
    if not isinstance(doc, dict) or not isinstance(doc.get(section, {}), dict):
        raise InitError(f"{path} has no {section} mapping to edit")
    return text, doc


def _settings(data_dir):
    """The core's settings as the server loads them; a refusal is an InitError."""
    from . import config
    path = os.path.join(data_dir, "config.yaml")
    try:
        return config.load_settings(config_path=path)
    except config.ConfigError as e:
        raise InitError(str(e))


def sync_cliproxy(data_dir):
    """The three keys of CLIProxyAPI's file the core's settings decide: the loopback
    host, `cliproxy.port` and the panel switch. CLIProxyAPI owns its file after init
    (its panel edits it, and it hashes the secret at start), so everything else in it
    is kept as loaded; its comments are not, which is acceptable because the panel
    owns the content, not the comments. The file keeps its mode; nothing is written
    when either file is refused."""
    data_dir = os.path.abspath(data_dir)
    cliproxy = _settings(data_dir)["cliproxy"]
    path = os.path.join(data_dir, "cliproxy", "config.yaml")
    text, doc = _load(path, "server")
    if not isinstance(doc.get("management", {}), dict):
        raise InitError(f"{path} has no management mapping to edit")
    doc.setdefault("server", {}).update({"host": "127.0.0.1", "port": cliproxy["port"]})
    doc.setdefault("management", {})["disable-control-panel"] = not cliproxy["panel"]
    _write(path, _dump(doc), 0o600)


def set_port(data_dir, core_port=None, cliproxy_port=None):
    """The shell's way back to a port that worked, when a moved one did not come up:
    `listen` becomes one address on the host it had, and `cliproxy.port` is written
    with CLIProxyAPI's `server.port` in step. Every other key is kept as loaded. What
    listens on the port is not asked: the start that follows says."""
    from . import config
    data_dir = os.path.abspath(data_dir)
    path = os.path.join(data_dir, "config.yaml")
    _, doc = _load(path, "cliproxy")
    if core_port is not None:
        try:
            listen = config.parse_listen(doc.get("listen"))
        except config.ConfigError as e:
            raise InitError(f"{path}: {e}")
        if len(listen) != 1:
            raise InitError(f"{path} names several listen addresses; set the port there by hand")
        doc["listen"] = [f"{listen[0].rpartition(':')[0]}:{check_port(core_port)}"]
    if cliproxy_port is not None:
        doc.setdefault("cliproxy", {})["port"] = check_port(cliproxy_port)
    _write(path, _dump(doc), 0o600)
    if cliproxy_port is not None:
        sync_cliproxy(data_dir)


def shell_config(data_dir):
    """What the shell reads at start, through the loader so `RELAY_*` overrides apply
    as they do to `serve`. It carries the operator token."""
    data_dir = os.path.abspath(data_dir)
    path = os.path.join(data_dir, "config.yaml")
    settings = _settings(data_dir)
    ports = _existing(path)
    token = next((t[1] for t in settings["tokens"] if len(t) > 2 and t[2] == "operator"), "")
    if ports is None or not token:
        raise InitError(f"{path} was not made by init (it lacks the keys init writes or an operator token)")
    return {"core_port": int(settings["listen"][0].rpartition(":")[2]),
            "cliproxy_port": settings["cliproxy"]["port"],
            "operator_token": token,
            "management_secret_file": settings["cliproxy"]["management_secret_file"],
            "panel": settings["cliproxy"]["panel"]}
