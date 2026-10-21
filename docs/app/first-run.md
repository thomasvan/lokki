# First run

lokki creates its data directory, starts its core and CLIProxyAPI sidecars, and
opens the core UI on Home. Home shows the endpoint URLs and the decision, and
offers “Add an account”; the top bar above it shows the mode and the routing
decision. A first run is in Auto with no account, so once the quota is read
the decision is “Passing requests through” with the reason `no account to
watch`. The endpoint URLs carry the core's port. No existing CLIProxyAPI
installation or account directory is adopted.

On macOS, move lokki into Applications and open it from there. An app opened
from its disk image, or a quarantined copy that macOS runs from a temporary
location, such as one opened from Downloads, can run, but cannot update itself.
A copy that macOS runs where it is, in a folder you can write to, updates in
place, whether it was opened with Open Anyway or cleared with `xattr -cr`.

## Data and ports

The data directory is `~/Library/Application Support/lokki` on macOS,
`%APPDATA%\lokki` on Windows, and `$XDG_DATA_HOME/lokki` or
`~/.local/share/lokki` on Linux. `LOKKI_DATA_DIR` overrides the location.
The core and CLIProxyAPI use ports 8381 and 8317 when available, then the next
free ports. Both listen on loopback. Find the address and port of the core under
Settings › Connection; the CLIProxyAPI port is under Settings › Advanced ›
CLIProxyAPI.
The files and their owners are listed in [Data directory](data-directory.md).

The shell writes `logs/app.log`; the sidecars write `logs/core.log` and
`logs/cliproxy.log`. Logs rotate and do not contain the operator token or
management secret.

## Accounts and the panel

Choose “Add a Codex account” on Accounts to start browser login.
lokki opens the provider's login URL in the system browser on the same machine
as the app. The callback returns to loopback port 1455. See [API](api.md) for
the flow and [Limits](limits.md) for its constraints.

To add an account from a browser on another device, open the login link the
card shows (“Copy login link”) there and sign in. That browser then lands on a
page that does not load, at an address that starts
`http://localhost:1455/auth/callback`. Copy that whole address from its address
bar and paste it into the card's field on Accounts, then choose “Send”. The
paste is how a login finishes when the browser is on a different machine from
the core, which is always the case for a Docker core; a browser on the server's
own machine can reach `127.0.0.1:1455`.

The CLIProxyAPI management panel is off by default. Turn on Web panel in
Settings › Advanced › CLIProxyAPI and save the group. Settings marks a CLIProxyAPI restart
as needed and offers that restart through the shell. Under the Web panel switch,
choose “Open the panel” to open it in the system browser, then “Copy the
management secret” to copy its login secret. See [UI](ui.md) for the UI's views and [Limits](limits.md) for the
panel's runtime dependency.

## The tray, errors, and quit

The tray menu shows the core's decision, “Cutting every request” or “Passing
requests through”, with its reason under it; then “Direct”, “Auto” and “Relay”
as check items, the one checked being the `routing` the core reports; then
“Open lokki” and “Quit lokki”. Auto's item says the threshold it returns to,
“Auto (below 30 %)”, from the status it shows, and “Auto” while the core does
not answer. Choosing a mode writes the mode alone, as the mode control in the
window does, so Auto returns to the stored threshold; “Direct” is the pause,
sending every request to CLIProxyAPI untouched. Choosing the mode
already checked writes nothing. A failed write is one line in `logs/app.log`,
and the menu keeps showing the mode the core reports.

The tray icon is the mark: solid while the decision cuts, an open line while it
passes through. The menu and the icon follow `/api/status` every 10 seconds and
at once after a choice. While the core does not answer, the menu says “The core
is not answering”, shows no reason, and the three modes are unavailable.

Closing the main window hides it while a tray is available; use “Open lokki” in
the tray to show it again. Without a tray, closing the main window quits lokki.

On Linux, some desktops have no tray host; GNOME without an AppIndicator
extension is one example. The tray can build without an icon appearing. In that
case, closing the window hides it with no tray item to reopen it. End lokki with
a signal, such as logging out or running `kill` for its process; lokki drains
the core and stops CLIProxyAPI as it exits.

The bundled error page titles are “lokki could not set up its data directory”,
“lokki could not start its services”, “lokki could not open its window”, and
“lokki stopped the core” or “lokki stopped CLIProxyAPI” after four exits within
a minute. It shows a sentence describing the failure and, when there is a log,
the last lines of the named log. “Try again” repeats first run after a data
directory failure, repeats startup after a service or window failure, or starts
the stopped sidecar again after it exhausts its restarts.

Before starting either sidecar, lokki checks whether another program answers
on its configured port. If so, it refuses to start that sidecar and shows the
error page with the port conflict and relevant log. This can happen when another
CLIProxyAPI or relay already uses that port.

Quit drains the core before stopping it, then stops CLIProxyAPI. To reset lokki,
quit it and delete the data directory. The next start initializes fresh
configuration and an empty local account directory. The credentials, settings,
logs and run history in the deleted directory are removed.

See [Data directory](data-directory.md) for files and owners, [API](api.md) for
the API and login flow, [UI](ui.md) for the views, [Limits](limits.md) for
runtime constraints, and [Updates](update.md) for new releases.
