# Limits

What the app depends on that it does not control, and what it does when that
changes.

## The quota reading

The core reads each account's 5-hour meter from the provider's usage endpoint
(`quota/reader.py` holds its URL and headers), in every mode: Auto decides by
it, and Home and Accounts show it in all three. That endpoint is unofficial: no
contract keeps it stable. The core reads it once per `low_quota.poll_seconds`
per account, through CLIProxyAPI's management API, with a client identity that
`quota/reader.py` documents. A core given no `cliproxy.management_secret_file`
reads nothing, and says "no management secret file is set".

If the provider changes the endpoint or refuses the reading, every account reads
`unknown` and, under Auto, requests go straight to CLIProxyAPI at full price
while the endpoint still looks healthy; Relay and Direct decide as before. `GET /api/status` carries the poll error and the
decision's reason, and `/healthz` carries the decision.

## Adding an account

Adding a Codex account uses CLIProxyAPI's legacy management routes
(`/v0/management/codex-auth-url`, `get-auth-status`, `oauth-callback`), which
the pinned release keeps beside its `/v8/` ones; a release that drops them
breaks the flow, and the route answers 502.

The provider sends the browser back to `http://localhost:1455/auth/callback`,
a URL CLIProxyAPI builds. The core's listener takes it from a browser on the
machine the core runs on; from any other browser, a core on a server or in
Docker included, the login finishes by pasting the address that browser ended
on (`docs/app/api.md`, Adding an account). Port 1455 is the one `codex login` uses too; while either holds it,
the other's login is refused ("another login is in progress"). The page the
user logs in on is the provider's.

The code binds the listener to `127.0.0.1:1455` only, on every system, and
leaves `[::1]:1455` free; the redirect URI names `localhost`
(`http://localhost:1455/auth/callback`), which CLIProxyAPI builds
(`core/lokki_core/quota/oauth.py:1-7`) and a browser may resolve to `::1`.
The core does not guard that address: a program running as the
same user can bind it, and a browser that tries `::1` for `localhost` hands the
code to that program. Such a program runs inside the boundary that can already
read the data directory and CLIProxyAPI's credentials, so the core does not
bind `[::1]` as well.

What was measured:

- Windows (hosted runner, thomasvan/lokki#3, runs 39716610311 and 39717434134,
  on a port the OS picked): while a login waits, no other socket binds
  `127.0.0.1:<port>`, whatever options it sets, and a connection to `127.0.0.1`
  reaches the core's listener. A socket binds `[::1]:<port>`. A wildcard
  `0.0.0.0:<port>` binds with no option and with `SO_REUSEADDR`, and is refused
  with `SO_EXCLUSIVEADDRUSE`: run 39716610311's log has `squatter 0.0.0.0
  SO_EXCLUSIVEADDRUSE: bind refused, winerror 10048 errno 10048`. `localhost`
  resolves to `::1` first, and a connection to `localhost` reached the program on
  `::1`. Run 39716610311 is red because its test asserted that the wildcard
  could not bind, and the measurement broke that test.
- Linux (this project's development host, 2026-10-22, `python3 -B
  core/callback_port_probe.py`, output below): with the core's listener on
  `127.0.0.1:<port>`, a socket on `127.0.0.1` or on `0.0.0.0` was refused with
  no option, `SO_REUSEADDR`, `SO_REUSEPORT` or both (`EADDRINUSE`), and a
  socket on `[::1]` or on `[::]` bound with any of the four. `localhost`
  resolved to `['127.0.0.1']` only there, a connection to `localhost:<port>`
  reached `127.0.0.1` with a socket on `[::1]`, and the core's listener
  answered; that depends on the host's `/etc/hosts`.
- macOS (Intel, macOS 26.7.1, 2026-10-22, on a port the OS picked): while the
  listener held `127.0.0.1:<port>`, no other socket bound that address, with no
  option, `SO_REUSEADDR`, `SO_REUSEPORT` or both (`EADDRINUSE`). A wildcard
  `0.0.0.0:<port>` bound beside it only with `SO_REUSEADDR` or `SO_REUSEPORT`;
  `[::1]` and `[::]` bound with any of the four. `localhost` resolved to
  `['127.0.0.1', '::1']`, and with a socket on `[::1]` a connection to
  `localhost:<port>` reached `127.0.0.1`, and the core's listener answered a
  request to `127.0.0.1:<port>`. This ran an earlier copy of
  `core/callback_port_probe.py`, at 8236d37, with the core's own listener class,
  which keeps `SO_REUSEADDR` on POSIX. The committed file adds the docstring, the
  port guard and a "not tried" branch for an option or an address family a
  system lacks. It reads the listener's reply to its end
  (`core/callback_port_probe.py:76-78`), where the earlier copy read one
  `recv(200)`, and it drops a `sys.path` line. The measuring is otherwise the
  same.

The Linux output, in full:

```
system: Linux  x86_64
localhost resolves to: ['127.0.0.1']
core listener on 127.0.0.1:<port>, SO_REUSEADDR = True
squatter 127.0.0.1 no option                 refused, errno 98 (Address already in use)
squatter 127.0.0.1 SO_REUSEADDR              refused, errno 98 (Address already in use)
squatter 127.0.0.1 SO_REUSEPORT              refused, errno 98 (Address already in use)
squatter 127.0.0.1 SO_REUSEADDR+SO_REUSEPORT refused, errno 98 (Address already in use)
squatter 0.0.0.0   no option                 refused, errno 98 (Address already in use)
squatter 0.0.0.0   SO_REUSEADDR              refused, errno 98 (Address already in use)
squatter 0.0.0.0   SO_REUSEPORT              refused, errno 98 (Address already in use)
squatter 0.0.0.0   SO_REUSEADDR+SO_REUSEPORT refused, errno 98 (Address already in use)
squatter ::1       no option                 BOUND
squatter ::1       SO_REUSEADDR              BOUND
squatter ::1       SO_REUSEPORT              BOUND
squatter ::1       SO_REUSEADDR+SO_REUSEPORT BOUND
squatter ::        no option                 BOUND
squatter ::        SO_REUSEADDR              BOUND
squatter ::        SO_REUSEPORT              BOUND
squatter ::        SO_REUSEADDR+SO_REUSEPORT BOUND
squatter [::1] for the landing test: BOUND
a connection to localhost:<port> reached 127.0.0.1
a request to 127.0.0.1:<port> answered by the core listener: True
```

`core/callback_port_probe.py` is the Linux and macOS measurement; run it with
`python3 -B core/callback_port_probe.py`. It binds one port the OS picks, on loopback
and the wildcard address, and nothing else: no port below 1024, not 1455, not
lokki's own. The Windows runs used a temporary probe step that is no longer in
the workflow.

A browser's own address choice was not measured on any system, and Happy
Eyeballs may try `::1` whatever order the resolver gives.

CLIProxyAPI makes two version checks when it starts, one from npm and one
whose source its log does not name; that traffic is its own.

## When the shell ends without its stop

Quitting lokki stops both sidecars, the core drained first, and so does SIGTERM,
SIGINT or SIGHUP to the shell on Linux and macOS. A shell that ends without running
that stop (a crash; on Windows, End task in Task Manager or a logoff; on Linux and
macOS, SIGKILL) leaves its sidecars to the system, and the systems differ:

- On Windows each sidecar runs in a job of its own, set to end every process in it
  when its last handle closes, and only the shell holds that handle. When the shell
  ends, however it ends, the system ends the sidecar and every process it started,
  the core's Python child included. The core is not drained: a request in flight is
  cut. A sidecar is suspended until it is in its job, so a shell that ends in that
  moment leaves it suspended, holding no port, until the next reboot.
- On Linux and macOS each sidecar leads a process group of its own, which a signal to
  the shell does not reach. A shell that ends this way leaves both sidecars running
  and holding their ports, the core still serving clients with the settings it
  started with. The next start of lokki on that data directory stops them
  ([first run](first-run.md)); until then nothing does. Neither system has an object
  that ends a process tree when its holder dies: Linux's parent-death signal reaches
  the direct child only, and comes when the thread that started it ends, not the
  process.

A leftover CLIProxyAPI is stopped by its pid. Windows and Linux hold that pid while
lokki checks and stops the process, by a handle and a pidfd. macOS has neither: lokki
checks that the pid still holds the port before each signal, and a process that exits
between that check and the signal could lend its pid to another.

## The CLIProxyAPI panel

CLIProxyAPI's management panel downloads its assets from GitHub when it runs,
unless `management.disable-control-panel` is set in its configuration.

## Platforms and engines no one has checked

The mode slider has been checked in Chromium, by automated tests with a real
mouse drag and not by hand, and rendered once in WebKitGTK. Not checked: WebKit
and WKWebView (the macOS app's webview), Firefox, and touch input. Nor has
anyone checked which order an engine delivers `pointerup`, `change` and
`lostpointercapture` in (the slider writes once, on the first), or whether a
release outside the window delivers `lostpointercapture`. In Firefox the fill
may end short of or past the thumb's centre: its thumb is 16 px wide, and the
fill assumes 20 px.

If a pointer loses both its release and its cancel, the slider keeps that drag.
Its thumb and reading stay on the position last dragged to, and nothing is
written for it: not by a key step, by leaving the slider, or by a timer.
Choosing a mode with another control writes that position first. A later press
on the slider that moves the thumb, or any mouse press, ends the drag and writes
the position then held. Leaving the page, or reloading, drops it, and the slider
shows what the core reports.

An Intel Mac is slow to start. In the hand test of 2026-10-21 the first run was
ready in 77 s and a restart in 59 s. Where the time goes was not measured. In the
hand test of 2026-10-22 ([updates](update.md), The hand test on an Intel Mac), the
updated lokki's `app.log` has the install asked at 05:20:02Z and `ready` at 05:21:11Z.

The Intel build has been run through the hand test ([updates](update.md), The hand
test on an Intel Mac). An x86_64 build that Rosetta runs on an Apple silicon Mac
installs its updates in place too, and no one has run that.
