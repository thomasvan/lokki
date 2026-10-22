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

The listener binds `127.0.0.1:1455` only; the code and the redirect URI
(`http://localhost:1455/auth/callback`) say so on every system, and `[::1]:1455`
is left free. The core does not guard that address: a program running as the
same user can bind it, and a browser that tries `::1` for `localhost` hands the
code to that program. Such a program runs inside the boundary that can already
read the data directory and CLIProxyAPI's credentials, so the core does not
bind `[::1]` as well.

What was measured:

- Windows (hosted runner, thomasvan/lokki#3, runs 39716610311 and 39717434134):
  while a login waits, no other socket binds `127.0.0.1:1455`, whatever options
  it sets, and a connection to `127.0.0.1` reaches the core's listener. A socket
  binds `[::1]:1455` and a wildcard `0.0.0.0:1455`; `localhost` resolves to
  `::1` first, and a connection to `localhost` reached the program on `::1`.
- Linux (this project's development host, one scratch run with no socket
  options, on a port the OS picked): with `127.0.0.1:<port>` listening,
  `[::1]:<port>` bound, and `0.0.0.0:<port>` was refused (`EADDRINUSE`).
  `localhost` resolved to `127.0.0.1` only there, so no browser connection
  reached `::1`; that depends on the host's `/etc/hosts`.
- macOS: not measured. Whether `localhost` resolves to `::1` first, and whether
  a wildcard socket binds beside `127.0.0.1`, is unknown here.

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
ready in 77 s and a restart in 59 s. Where the time goes was not measured.

The Intel build is sealed ad hoc too ([updates](update.md), How a macOS build is
signed), and no one has run a sealed build on an Intel Mac: neither macOS's "could
not verify" prompt with Open Anyway, nor the hand test's checks of the seal.

An x86_64 build that Rosetta runs on an Apple silicon Mac installs its updates
in place too, and no one has run that.
