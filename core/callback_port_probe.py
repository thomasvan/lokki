#!/usr/bin/env python3
"""What else can bind beside the core's callback listener, and where "localhost" lands.

The core waits for a login's callback on 127.0.0.1 (`lokki_core.quota.oauth`,
`Callback.start`). This tool starts the core's own listener class,
`_CallbackServer`, which keeps SO_REUSEADDR on POSIX, and then tries to bind
the same port with a socket of the same user in each of four address and four
option combinations. It prints which binds were refused and which succeeded,
what "localhost" resolves to, and which address a connection to "localhost"
reaches while a socket holds [::1]. docs/app/limits.md quotes its output for
each system.

Run it from anywhere: `python3 -B core/callback_port_probe.py`. It only binds, on
loopback and the wildcard address, one port the OS picks (port 0), and closes
everything on exit. It reads no file and, run as `python3 -B`, writes none. It
refuses to go on if the OS picks a port below 1024, 1455 or one of lokki's own
ports, so it never touches a login in progress or a running lokki. `unittest`
discovery does not import it: its name does not begin with `test_`.
"""
import os, platform, socket, sys, threading
from http.server import BaseHTTPRequestHandler
from lokki_core.quota.oauth import _CallbackServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"core")
    def log_message(self, *a): pass

print("system:", platform.system(), platform.mac_ver()[0], platform.machine())
print("localhost resolves to:", [ai[4][0] for ai in socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)])
srv = _CallbackServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
if port < 1024 or port in (1455, 8317, 8318, 8381, 8382):
    srv.server_close()
    sys.exit(f"the OS picked port {port}, which this probe leaves alone; run it again")
print("core listener on 127.0.0.1:<port>, SO_REUSEADDR =", srv.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0)
threading.Thread(target=srv.serve_forever, daemon=True).start()

def squat(fam, addr, opts):
    s = None
    try:
        s = socket.socket(fam, socket.SOCK_STREAM)
        if fam == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        for o in opts:
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, o), 1)
        s.bind((addr, port)); s.listen()
        return s, "BOUND"
    except OSError as e:
        if s:
            s.close()
        if s is None:
            return None, f"not tried: {e}"
        return None, f"refused, errno {e.errno} ({os.strerror(e.errno)})"

for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::1"), (socket.AF_INET6, "::")):
    for opts in ((), ("SO_REUSEADDR",), ("SO_REUSEPORT",), ("SO_REUSEADDR", "SO_REUSEPORT")):
        if not all(hasattr(socket, o) for o in opts):
            print(f"squatter {addr:9} {'+'.join(opts):25} not tried: this system has no such option")
            continue
        s, what = squat(fam, addr, opts)
        print(f"squatter {addr:9} {'+'.join(opts) or 'no option':25} {what}")
        if s:
            s.close()

# Where "localhost" lands with a squatter on [::1] (no option), as a client that follows getaddrinfo's order does.
s6, what = squat(socket.AF_INET6, "::1", ())
print("squatter [::1] for the landing test:", what)
c = socket.create_connection(("localhost", port), timeout=3)
print("a connection to localhost:<port> reached", c.getpeername()[0])
c.close()
if s6:
    s6.close()
c = socket.create_connection(("127.0.0.1", port), timeout=3)
c.sendall(b"GET / HTTP/1.0\r\n\r\n")
reply = b""
while chunk := c.recv(200):
    reply += chunk
print("a request to 127.0.0.1:<port> answered by the core listener:", reply.endswith(b"core"))
c.close()
srv.shutdown(); srv.server_close()
