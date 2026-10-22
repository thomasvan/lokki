"""Measurement for the callback port on Windows, run once by core.yml and then removed.

For each listener (the one before SO_EXCLUSIVEADDRUSE, and one with it set), on a
port 0 gave: which other same-user sockets can bind the port while the login waits;
then one loopback request as a browser makes it, the listener closed, and how long
until a new Callback binds the port again. Prints its findings; fails on nothing.
"""
import http.client
import os
import socket
import subprocess
import sys
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core.quota import oauth  # noqa: E402

CAP = 300.0


class Plain(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"     # the listener as c47d411 has it


class Exclusive(Plain):
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def squat(port, host, option):
    s = socket.socket()
    try:
        if option:
            s.setsockopt(socket.SOL_SOCKET, option, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            return f"bind refused, winerror {getattr(e, 'winerror', None)} errno {e.errno}"
        try:
            s.listen(1)
        except OSError as e:
            return f"bound, listen refused, winerror {getattr(e, 'winerror', None)}"
        return "BOUND and listening"
    finally:
        s.close()


def netstat(port):
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
    return [" ".join(line.split()) for line in out.splitlines() if f":{port} " in line]


def start(cls, port):
    oauth._CallbackServer = cls
    try:
        cb = oauth.Callback(port=port, wait=600)
        cb.start("http://127.0.0.1:9")
        return cb
    finally:
        oauth._CallbackServer = BASE


def browse(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/auth/callback?code=probe&state=probe-state")
        resp = conn.getresponse()
        return resp.status, len(resp.read())
    finally:
        conn.close()


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


BASE = oauth._CallbackServer
SQUATTERS = [("127.0.0.1", "SO_REUSEADDR"), ("0.0.0.0", "SO_REUSEADDR"), ("127.0.0.1", None), ("0.0.0.0", None),
             ("127.0.0.1", "SO_EXCLUSIVEADDRUSE"), ("0.0.0.0", "SO_EXCLUSIVEADDRUSE")]


def main():
    print(f"probe: python {sys.version.split()[0]} on {sys.platform}, os.name {os.name}")
    for cls in (Plain, Exclusive):
        name = cls.__name__
        if cls is Exclusive and not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            print(f"probe {name}: no SO_EXCLUSIVEADDRUSE here")
            continue
        port = free_port()
        cb = start(cls, port)
        for host, opt in SQUATTERS:
            if opt and not hasattr(socket, opt):
                continue
            option = getattr(socket, opt) if opt else None
            print(f"probe {name} pending, squatter {host} {opt or 'no option'}: {squat(port, host, option)}")
        cb.close()
        for rep in range(3):
            port = free_port()
            cb = start(cls, port)
            got = browse(port)
            cb.close()
            states = netstat(port) if os.name == "nt" else []
            print(f"probe {name} rep {rep}: request answered {got}; after close netstat: {states}")
            print(f"probe {name} rep {rep}: plain bind at once: {squat(port, '127.0.0.1', None)}")
            print(f"probe {name} rep {rep}: new Callback {rebind(cls, port)}")
        port = free_port()
        start(cls, port).close()
        print(f"probe {name} control, no request: new Callback {rebind(cls, port)}")


if __name__ == "__main__":
    main()
