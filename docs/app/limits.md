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
- Linux (this project's development host, 2026-10-22, `python3
  core/callback_port_probe.py`, output below): with the core's listener on
  `127.0.0.1:<port>`, a socket on `127.0.0.1` or on `0.0.0.0` was refused with
  no option, `SO_REUSEADDR`, `SO_REUSEPORT` or both (`EADDRINUSE`), and a
  socket on `[::1]` or on `[::]` bound with any of the four. `localhost`
  resolved to `['127.0.0.1']` only there, a connection to `localhost:<port>`
  reached `127.0.0.1` with a socket on `[::1]`, and the core's listener
  answered; that depends on the host's `/etc/hosts`.
- macOS (Intel, macOS 26.7.1, 2026-10-22, an earlier copy of
  `core/callback_port_probe.py`, run at 8236d37, with
  the core's own listener class, which keeps `SO_REUSEADDR` on POSIX, on a port
  the OS picked): while the listener held `127.0.0.1:<port>`, no other socket
  bound that address, with no option, `SO_REUSEADDR`, `SO_REUSEPORT` or both
  (`EADDRINUSE`). A wildcard `0.0.0.0:<port>` bound beside it only with
  `SO_REUSEADDR` or `SO_REUSEPORT`; `[::1]` and `[::]` bound with any of the
  four. `localhost` resolved to `['127.0.0.1', '::1']`, and with a socket on
  `[::1]` a connection to `localhost:<port>` reached `127.0.0.1`, and the core's
  listener answered a request to `127.0.0.1:<port>`.

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
`python3 core/callback_port_probe.py`. It binds one port the OS picks, on loopback
and the wildcard address, and nothing else: no port below 1024, not 1455, not
lokki's own. The Windows runs used a temporary probe step that is no longer in
the workflow.

A browser's own address choice was not measured on any system, and Happy
Eyeballs may try `::1` whatever order the resolver gives.

CLIProxyAPI makes two version checks when it starts, one from npm and one
whose source its log does not name; that traffic is its own.

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
hand test of 2026-10-22 (next paragraph), the updated lokki's `app.log` has the
install asked at 05:20:02Z and `ready` at 05:21:11Z.

The Intel build is sealed ad hoc too ([updates](update.md), How a macOS build is
signed), and it has been run. On 2026-10-22, from 04:41 UTC,
`app/scripts/mac-update-handtest.sh` ran steps 0 to 8 of [Testing an install on
macOS](update.md) at 8236d37, on an Intel Core i9-9880H Mac with macOS 26.7.1. The
build venv had Python 3.14.9, not 3.13. The real lokki (0.1.0) and its data
directory were moved aside first and put back afterwards.

- Step 1 built A and B in about 8 minutes.
- Step 3, by hand, through the README's path: A's quarantined `.dmg`
  (`0081;…;Safari;`) opened from Finder, lokki dragged to Applications and
  opened. macOS gave its "could not verify" prompt, not "damaged". Privacy &
  Security offered Open Anyway, which asked for the password. The first run
  reached Home, and so did the restart. "Check now" offered 99.0.1 with "Install
  and restart".
- Step 4: the bundle read `Identifier=dev.lokki.desktop Signature=adhoc` and both
  sidecars `Signature=adhoc`. `codesign --verify --deep --strict` passed on
  `/Applications/lokki.app`, and `codesign -v` passed on `cliproxy-8_0_23` and on
  `lokki-core-0_1_0`. The approved bundle's quarantine read `01c1;…;Safari;`.
- Step 5, by hand: "Install and restart" gave no password dialog and no
  Gatekeeper dialog, and lokki relaunched as 99.0.1. Its `app.log` has the install
  asked at 05:20:02Z, both sidecars stopped drained at 05:20:03Z, "update
  installed; restarting" at 05:20:03Z, init saying "already initialised" at
  05:20:26Z, and ready at 05:21:11Z.
- Step 6: every check passed. The version is 99.0.1, and the shell and both
  sidecars run as B's files from `/Applications/lokki.app`. The bundle carries no
  quarantine. `codesign -dv` matches step 4, and `--deep --strict` and each
  sidecar's `-v` pass again. The data checksums are unchanged. `app.log` shows the
  drain, the stop, the install after the proof, both sidecars started again, and
  no error.
- Step 7, by hand, the banner's reason word for word:
  - 7a: "lokki is running from a read-only disk, such as its disk image: move
    lokki.app into Applications, open it from there, then install"
  - 7b: "your macOS user cannot change lokki.app or the folder it is in, and lokki
    does not ask for an administrator's password; download the new .dmg, or move
    lokki.app to a folder you can write to"
  - 7c, after the "could not verify" prompt and Open Anyway: "macOS is running
    lokki from a temporary copy, which cannot be updated: quit lokki, move
    lokki.app into Applications with Finder, open it from there, then install"
- Step 8: every `clean` check passed. The test's data directory was removed, the
  real install was put back, and nothing of the test was left.

An x86_64 build that Rosetta runs on an Apple silicon Mac installs its updates
in place too, and no one has run that.
