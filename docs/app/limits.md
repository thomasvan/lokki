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
Lokki's own. The Windows runs used a temporary probe step that is no longer in
the workflow.

A browser's own address choice was not measured on any system, and Happy
Eyeballs may try `::1` whatever order the resolver gives.

CLIProxyAPI makes two version checks when it starts, one from npm and one
whose source its log does not name; that traffic is its own.

## When the shell ends without its stop

Quitting Lokki stops both sidecars, the core drained first, and so does SIGTERM,
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
  and holding their ports. The next start of Lokki on that data directory stops them
  ([first run](first-run.md)); until then nothing does. Neither system has an object
  that ends a process tree when its holder dies: Linux's parent-death signal reaches
  the direct child only, and comes when the thread that started it ends, not the
  process.

A leftover CLIProxyAPI, measured once on macOS and in four runs on Linux, held its port
until its first request and ended on that request without answering it. On Linux it
also ended with no request at all when its output's pipes were closed in its first
second or so. The leftover core therefore does not go on serving clients: its first
request upstream ends CLIProxyAPI, and every later one fails upstream. That consequence
is inferred from what CLIProxyAPI did alone; no run sent a request through a leftover
core. On Windows a leftover CLIProxyAPI answered the
next start's check and was stopped ([updates](update.md), The hand test on Windows).

- Measured once on an M4 Mac mini, macOS 27.0.1: after a `kill -9` of the shell,
  CLIProxyAPI was still alive 5 s later; one `GET /` got no answer (curl code 000); 3 s
  later it was gone.
- Measured on Linux x86_64 with the CLIProxyAPI 8.0.23 that `app/cliproxy.lock` pins,
  its output on pipes whose read ends were then closed, on a port the system chose:
  - with the pipes closed 1 to 5 s after it started, it stayed alive and listening for
    60 s with nothing connecting (4 runs), and through a connection that sent nothing,
    as Lokki's probe of a port is (3 runs);
  - its first `GET /` was closed with no bytes back, and 3 s later it had ended on
    SIGPIPE, exit status −13 (4 runs of 4);
  - with the pipes closed 1 to 1.3 s after it started, it ended on SIGPIPE within 2 to
    5 s with no request in 3 runs of 4, at a line of its own start-up output, as
    the timing suggests.
- Inferred, not proven: CLIProxyAPI writes a line to its output for each request, and
  a Go program whose write to a broken pipe on fd 1 or 2 fails ends on SIGPIPE. On
  Windows such a write returns an error instead.

A core whose shell has ended still drains and exits on `POST /api/shutdown` or
SIGTERM: a line it writes to a pipe no one reads any more is dropped. A core of Lokki
0.1.0 does not. Its drain dies on that write, so it takes the order, answers
`/healthz` with `draining` and never exits. The next start kills such a core once its
wait is over ([first run](first-run.md)).

That kill comes when the start's wait is over: the core's `drain_seconds` plus 5 s, or
20 s when it gives no usable `drain_seconds`, at most 30 s, even if the core still
drains. The cap is there because the start waits holding the sidecars' lock, which a
quit needs, before any window opens. Measured on macOS, [updates](update.md), The
leftover hand test on an M4 Mac mini: a 0.1.0 core was killed after 20 s. Inferred, not
measured: on Windows, where a leftover CLIProxyAPI answered the next start's check, a
stream still running through such a core is cut by that kill; no run sent a request
through a leftover core.

A leftover CLIProxyAPI is stopped by its pid, and so is a leftover core that has to be
killed, with the processes that started it. Windows and Linux hold each pid while
Lokki checks and stops the process, by a handle and a pidfd. Windows keeps the pid of a
parent that has ended, and that pid may name a later process, so a parent counts only
if it was created before its child. macOS has no hold: Lokki checks that the pid still
holds the port before each signal, and that each parent is still the parent of the
process below it, and a process that exits between that check and the signal could
lend its pid to another.

## An install over running leftovers

Windows does not let a program's file be replaced or removed while the program runs.
An install over a leftover sidecar therefore leaves that sidecar's file as it was: a
silent install did so and still exited 0. The evidence is file times: after it, the two
sidecar files kept their times of 2026-10-22 07:58 UTC, and `lokki.exe` had 2026-10-23
09:01. That is before the 09:34 install, so the installer keeps the packaged files'
times, and a sidecar file it had replaced would carry the new build's time, not the
earlier install's. No hash of the installed files was taken. A second run, of `main`'s
build at 909fd89 ([updates](update.md), The hand test on Windows), showed the same and
the size too: over a running 0.1.0, the held files kept their times and the old core's
size, 10,539,519 bytes; the next install, with nothing running, replaced them, the core
with 10,539,913 bytes. Interactive installs and the MSI were
not tried. Each sidecar's file is named for its version, so what this leaves depends on
the versions:

- When the new build carries the same name, Lokki runs the file the earlier install
  left until an install made with nothing running replaces it. For CLIProxyAPI that
  is the same file: `app/cliproxy.lock` pins the archive of each version by its hash.
  A core keeps its name until `core/VERSION` changes, and two builds of one version
  can differ.
- When the name is new, the new file is installed beside the old one. At each start,
  before it stops any leftover, Lokki removes the sidecar files its build does not
  carry. A file a leftover still runs cannot be removed, and Lokki goes on without
  it, so the old file is removed at the next start after its leftover was stopped.

On Linux and macOS a running program's file can be removed and replaced, and none of
this applies.

## A request refused before its body is read

The core refuses a request without a token, and some others, before it reads the body,
and closes the connection after the answer. A close over bytes it never read resets the
connection, and Windows then drops an answer the client has not read yet. So the core
first reads and drops the body a `Content-Length` promises, up to `MAX_BODY_BYTES`, and
stops once no byte has come for 0.25 s, or after 5 s. These still get the reset on
Windows, and on any system when the client is still sending as it comes:

- a client that sends the head, pauses for more than 0.25 s, then sends the body;
- a chunked body, which the drain does not read;
- a body above `MAX_BODY_BYTES`, which the drain does not read either.

No run has sent a chunked or an over-cap body without a token: that those two get the
reset on Windows is the mechanism above applied, not measured. The tests show only that
the drain skips them in time.

Waiting longer for a paused body would hold every such answer as long for a client that
never sends one, and reading a chunked body would mean parsing one from a client that
has not proved a token. Ordinary clients send the head and the body together, with a
`Content-Length`.

## What the app still calls lokki

The product is named Lokki wherever the app's own text names it: the window and error page titles, the dialogs,
the tray, the sidebar, the update banner and the sentences the shell and the core give. What the system or the
release names from the bundle's `productName`, which is still `lokki`, says "lokki" until a later change renames
it: the macOS application menu and the Dock, the macOS prompts that name the app ("could not verify “lokki”"),
the Windows installer's name and its Start-menu shortcut, the Linux desktop entry, and the release title
(`lokki ${TAG#v}`, set in `app.yml`). The bundle identifier, the binaries, the data directory and the `LOKKI_*`
variables keep `lokki`: programs read them, not people.

## The CLIProxyAPI panel

CLIProxyAPI's management panel downloads its assets from GitHub when it runs,
unless `management.disable-control-panel` is set in its configuration.

## Platforms and engines no one has checked

The mode slider has been checked by automated tests with a real mouse drag, not
by hand, in Chromium and in Playwright's WebKit. That WebKit runs headless as
WebKit's WPE port: a `ps` taken during a run lists `minibrowser-wpe/bin/MiniBrowser
--headless`, `WPENetworkProcess` and `WPEWebProcess`. Its test presses, moves and
releases, then moves the mouse with no button down. With a pointer capture taken
on press the thumb did not follow a held move in that WebKit; without it, it does.
The slider takes no capture. Why the capture stops the drag is not known, and the
thumb following the mouse with no button down, as seen on macOS, did not
reproduce there. In WKWebView, Apple's port and not WPE, the person at an Intel Mac
with macOS 26.7.1 reported that the drag works, on the x64 disk image of app run
39927837172 (7469b5d). That is reported, not measured: nothing recorded the drag. Not
checked: WebKitGTK (the Linux app's webview), Firefox, and touch input. The only
look at the UI in WebKitGTK was a CSS check (`oklch()`), not the slider. Nor has
anyone checked which order an engine delivers `pointerup` and `change` in (the
slider writes once, on the first), or whether a release outside the window
delivers `pointerup` to the window. A move with no button down that an engine
sends during a real drag would end it early; no engine is known to. In Firefox
the fill may end short of or past the thumb's centre: its thumb is 16 px wide,
and the fill assumes 20 px.

A move of the drag's pointer with no button down is not part of the drag and
ends it, writing the position then held. If a pointer loses its release and its
cancel and then does not move, the slider keeps that drag. Its thumb and reading
stay on the position last dragged to, and nothing is written for it: not by a
key step, by leaving the slider, or by a timer. Choosing a mode with another
control writes that position first. A later press on the slider that moves the
thumb, or any mouse press, ends the drag and writes the position then held.
Leaving the page, or reloading, drops it, and the slider shows what the core
reports.

An Intel Mac is slow to start. In the hand test of 2026-10-21 the first run was
ready in 77 s and a restart in 59 s. Where the time goes was not measured. In the
hand test of 2026-10-22 ([updates](update.md), The hand test on an Intel Mac), the
updated Lokki's `app.log` has the install asked at 05:20:02Z and `ready` at 05:21:11Z.

The Intel build has been run through the hand test ([updates](update.md), The hand
test on an Intel Mac). An x86_64 build that Rosetta runs on an Apple silicon Mac
installs its updates in place too, and no one has run that.
