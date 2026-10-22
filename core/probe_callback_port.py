"""Measurement for the callback port on Windows, run by core.yml and then removed.

Second run. For the listener before SO_EXCLUSIVEADDRUSE ("Plain") and with it
("Exclusive"): the option read back; a browser-style request, read until the
listener closes so the listener's side holds the TIME_WAIT, and how long until a
new Callback binds the port; which socket a connection to 127.0.0.1 reaches while
a wildcard squatter also holds the port, in either order of binding; a squatter
on [::1]; and where `localhost` resolves. Prints its findings; fails on nothing.
"""
import os
import socket
import subprocess
import sys
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core.quota import oauth  # noqa: E402

CAP = 300.0
EXCL = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
BASE = oauth._CallbackServer


class Plain(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"     # the listener as c47d411 has it


class Exclusive(Plain):
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, EXCL, 1)
        super().server_bind()


def err(e):
    return f"winerror {getattr(e, 'winerror', None)} errno {e.errno}"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(cls, port):
    oauth._CallbackServer = cls
    try:
        cb = oauth.Callback(port=port, wait=600)
        cb.start("http://127.0.0.1:9")
        return cb
    finally:
        oauth._CallbackServer = BASE


def options(cb):
    s = cb._server.socket
    got = {"SO_REUSEADDR": s.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)}
    if EXCL is not None:
        got["SO_EXCLUSIVEADDRUSE"] = s.getsockopt(socket.SOL_SOCKET, EXCL)
    return got


REQUEST = b"GET /auth/callback?code=probe&state=probe-state HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n"


def browse_to_eof(port):
    """As a browser does with `Connection: close`: read until the server closes, then close."""
    with socket.socket() as c:
        c.settimeout(10)
        c.connect(("127.0.0.1", port))
        c.sendall(REQUEST)
        data = b""
        while True:
            chunk = c.recv(4096)
            if not chunk:
                break
            data += chunk
        return data.split(b"\r\n", 1)[0].decode("latin-1")


def netstat(port):
    if os.name != "nt":
        return []
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
    return [" ".join(line.split()) for line in out.splitlines() if f":{port} " in line]


def rebind(cls, port):
    t0 = time.monotonic()
    tries = 0
    while True:
        tries += 1
        try:
            cb = start(cls, port)
        except oauth.CallbackPortBusy:
            if time.monotonic() - t0 > CAP:
                return f"STILL BUSY after {time.monotonic() - t0:.2f} s, {tries} tries"
            time.sleep(0.05)
            continue
        took = time.monotonic() - t0
        cb.close()
        return f"bound after {took:.3f} s, {tries} tries"


def squatter(host, port, family=socket.AF_INET, option=None):
    s = socket.socket(family)
    try:
        if option is not None:
            s.setsockopt(socket.SOL_SOCKET, option, 1)
        s.bind((host, port))
        s.listen(4)
    except OSError:
        s.close()
        raise
    s.settimeout(1.0)
    return s


def who_answers(port, squat):
    """Connect to 127.0.0.1:port: does the squatter accept it, or does lokki's page come back?"""
    with socket.socket() as c:
        c.settimeout(5)
        c.connect(("127.0.0.1", port))
        try:
            conn, _ = squat.accept()
            conn.close()
            return "the SQUATTER accepted it"
        except (socket.timeout, TimeoutError):
            pass
        c.sendall(REQUEST)
        try:
            first = c.recv(4096).split(b"\r\n", 1)[0].decode("latin-1")
        except OSError as e:
            return f"nobody answered ({err(e)})"
        return f"the listener answered: {first!r}"


def main():
    print(f"probe2: python {sys.version.split()[0]} on {sys.platform}, os.name {os.name}")
    print(f"probe2 localhost resolves to: {[a[4][0] for a in socket.getaddrinfo('localhost', 80, type=socket.SOCK_STREAM)]}")
    for cls in (Plain, Exclusive):
        name = cls.__name__
        if cls is Exclusive and EXCL is None:
            print(f"probe2 {name}: no SO_EXCLUSIVEADDRUSE here")
            continue
        port = free_port()
        cb = start(cls, port)
        print(f"probe2 {name} options read back: {options(cb)}")
        cb.close()

        # 1. Browser-style: the listener closes first.
        for rep in range(3):
            port = free_port()
            cb = start(cls, port)
            line = browse_to_eof(port)
            cb.close()
            print(f"probe2 {name} rep {rep}: answered {line!r}; after close netstat: {netstat(port)}")
            print(f"probe2 {name} rep {rep}: new Callback {rebind(cls, port)}")

        # 2. A wildcard squatter that binds after the listener.
        for opt_name, opt in (("no option", None), ("SO_REUSEADDR", socket.SO_REUSEADDR)):
            port = free_port()
            cb = start(cls, port)
            try:
                squat = squatter("0.0.0.0", port, option=opt)
            except OSError as e:
                print(f"probe2 {name} listener first, wildcard squatter {opt_name}: bind refused ({err(e)})")
                cb.close()
                continue
            with squat:
                print(f"probe2 {name} listener first, wildcard squatter {opt_name} bound: {who_answers(port, squat)}")
            cb.close()

        # 3. A wildcard squatter that binds first.
        for opt_name, opt in (("no option", None), ("SO_REUSEADDR", socket.SO_REUSEADDR)):
            port = free_port()
            with squatter("0.0.0.0", port, option=opt) as squat:
                try:
                    cb = start(cls, port)
                except oauth.CallbackPortBusy:
                    print(f"probe2 {name} squatter first ({opt_name}): Callback raised CallbackPortBusy")
                    continue
                print(f"probe2 {name} squatter first ({opt_name}), Callback bound: {who_answers(port, squat)}")
                cb.close()

        # 4. A squatter on [::1] while the listener waits, and where a connection to localhost lands.
        port = free_port()
        cb = start(cls, port)
        try:
            squat6 = squatter("::1", port, family=socket.AF_INET6)
        except OSError as e:
            print(f"probe2 {name} [::1] squatter: bind refused ({err(e)})")
        else:
            with squat6:
                with socket.create_connection(("localhost", port), timeout=5) as c:
                    peer = c.getpeername()[0]
                print(f"probe2 {name} [::1] squatter: BOUND and listening; a connection to localhost reached {peer}")
        cb.close()


if __name__ == "__main__":
    main()
