# UI

The core serves the built UI at `/` and its assets under `/static/`. The UI
calls the same-origin control API; operator routes need the operator token.

## Development

Start the core on a loopback port, then run:

```sh
cd ui && npm ci && LOKKI_CORE=http://127.0.0.1:<port> npm run dev
```

Open `http://localhost:5173/static/` and enter the operator token once. Vite
proxies `/api`, `/healthz` and `/version` to `LOKKI_CORE`; without it `npm run
dev` refuses to start. The token is kept in `sessionStorage` after the core
accepts it, with an in-memory fallback when storage is unavailable. The prompt
is a dialog over the current page. Requests time out after 20 seconds.

The dev proxy changes `Host` to the core's origin and rewrites each proxied
request's `Origin` to that same origin. The core's Host/Origin guard requires
them to match, so writes and account refreshes work through Vite. A production
page is served by the core itself and is same-origin; it needs no rewrite.

## Views and reads

The window is the update banner over the shell. The shell is a sidebar, a top
bar and the open view. The sidebar holds the mark and the wordmark ("Lokki", drawn,
18 px; `Wordmark.svelte` is its home), the
destinations (Home, Accounts, Activity, Settings) and, at its foot,
"CLIProxyAPI running" or "CLIProxyAPI not reachable" and the Lokki version.
Below 720 px the sidebar folds into a row of tabs under the top bar, its foot
hidden and its brand out of sight but still the page's heading for assistive
technology.

The top bar is on every screen: the three one-click choices, then the decision line, a
dot with "Cutting every request" or "Passing requests through" from
`decision.relay_on` and `decision.reason` as the core gives it. Each choice's
sentence is its radio's description, shown on hover or focus. On Home the bar is
larger and the mode slider sits under the choices at the bar's width (below). The
modes are listed Direct, Auto, Relay, the slider's order from left to right, and
Auto's label carries the threshold it returns to, from
`/api/status.low_quota.threshold_percent`: "Auto 30 %". Until the core has said which mode is on there
is no mode control: before the first status read the bar says "Reading the
core's status" with a spinner, and when the core does not answer (or times out)
it says "The core is not answering", the UI's own sentence for an unreachable
core, which the tray also uses. A 401 or 403 shows "Waiting for the operator
token" under the prompt, and any other failure "The core did not give its
status" with the core's message; none of them offers a radio or the slider to write with.
`ui/src/lib/mode.ts` holds the modes' labels and
sentences and the other sentences the UI says about them.

Choosing a mode is one `PUT /api/settings` (`modeBody`) of `routing` alone:
`{routing: "direct"}`, `{routing: "auto"}` or `{routing: "relay"}`, so Auto
returns to the threshold the file holds. A choice of the mode the core reports
when it would be sent writes nothing. The control shows what the core reports, never the click: the
checked mark follows the read back, and a status poll that lands during a write
changes nothing. A 400 or 409 is shown beside the control with its key (every refused
write in the UI shows its key, unless the core's sentence already names it), the
mode unchanged. `settings.writable: false` disables the control with "the settings
file is not writable here"; while the settings are unread it stays enabled,
and a refused write says why. Arrow keys move between the modes; Space or Enter
chooses one.

**The mode slider** (`ModeSlider.svelte`, one component for Home's top bar and
Settings › Mode) is one `<input type="range">`, 0 to 100 in steps of 1, named
"Mode and threshold", along "% left": Direct at 0, Relay at 100, and between them
Auto with that threshold (`position` in `mode.ts`: where the core reports the mode).
It is the threshold's one control. What a position writes is `positionBody`, one
`PUT`: 0 writes `{routing: "direct"}`, 100 `{routing: "relay"}`, and N between them
`{routing: "auto", low_quota: {threshold_percent: N}}`. The ends are modes and never
thresholds: as a threshold, 100 would leave an account just reset not low and pass
through every request whose quota cannot be read, so an end writes `routing` alone
and leaves the stored threshold for Auto to return to. The track is filled from 0
to the thumb in the cutting colour, the scale under it reads "Direct · 0 %", "% left"
and "100 % · Relay", the readout beside its name is the mode's label ("Direct",
"Auto 30 %", "Relay"), and the words under it are Direct's or Relay's sentence at an
end and the rule line between them (`ruleLine`): "Cut when every watched account has
less than 30 % left · every model", "any watched account" under `rule: any`, "the
watched account" with one watched (the rows the core marks `in_scope`; the `every`
form while the accounts are unread), then "every model" or "3 models" from
`low_quota.models`. `aria-valuetext` is the readout and the words. It writes when it
stops: a drag once, when the pointer lets go, by whichever an engine fires first
of `pointerup` (heard on the slider and, for a release outside it, on the window),
`change`, the loss of a touch's capture (`lostpointercapture`), or a move of the
drag's pointer with no button down. The slider takes no pointer capture itself; with
one, the thumb did not follow a held move in Playwright's WebKit. The drag is its own
pointer's: the `pointerId` of its `pointerdown` is kept, and a `pointerup`,
`pointercancel` or `lostpointercapture` from another pointer (a second touch) neither
ends nor cancels it, while a `pointerup` with no drag down does nothing. While the
pointer is down nothing writes, a key step moving the drag's position and a lost focus
writing nothing; a drag the browser cancels
(`pointercancel`) writes nothing and goes back to what the core reports, and a slider
that goes away mid-drag leaves nothing held. The keys are its own, not the range's
native step: the arrows move 1, PageUp and PageDown 10, Home and End to the ends, and
a run of steps writes once, 500 ms after the last one or when the slider loses focus;
a key with Alt, Ctrl or Meta is left to the browser. A position equal to the one the core
reports writes nothing. Until it writes, it shows the position held or stepped to,
and a status read does not move it. With no status read it has no thumb, takes no
input, and its readout and words are empty. A refused or failed write puts it back
on what the core reports, with the core's sentence or the request's error under it
(`role="alert"`) until the next write returns; a write that succeeds but whose read
back fails leaves it on the last status read, with "written, but the mode could not
be read back: <error>". `settings.writable: false` disables it with the lock line.

**One mode write at a time.** The window holds one mode write in its shared state
(`app.modeWrite` in `state.svelte.ts`), not in each control, so the top bar's
choices and the slider share it: while a write and its reads are in flight every
mode control says `aria-busy`, stays enabled and keeps the focus, and keys and drags
still move the slider. A choice made meanwhile from any of them waits, marked
pending on its control (a spinner), replacing any choice already waiting, and is
sent once the write in flight and its reads return, so the latest choice wins and
no click or key is lost; a choice waiting behind a write that failed is still sent,
and the failure's line stays until that write returns. A waiting choice is compared
with the mode the core reports when it is sent, not when it is chosen, and is sent
without that comparison after a write whose read back failed, the status then being
stale. The checked mark stays on the mode the core reports; a pending choice's
spinner sits beside its label. Every choice goes through
one `choose`: a position the slider holds (stepped to and not yet chosen) is queued
first, then the choice, so a top-bar click after key steps sends the position and
then the click whatever the focus does: WebKit does not focus a button on click,
so the order rests on no `blur` or `pointerdown`. The slider's own choice, from its
timer, its blur or a drag let go, replaces the held position instead of queueing
behind it, and the 500 ms timer lives beside it in the shared state, so `choose`
cancels it and no position is sent twice. The tray writes through the shell, not
the window: the window shows a tray change at its next status read, and a slider
position held while a tray change is made can overwrite it.

On entry, Home loads status, accounts and the 24 h activity and loads the endpoint until it has
one; Accounts loads status and accounts; Activity loads status and the chosen window's activity; Settings loads schema and settings, and the
accounts once (again when the page comes back into view) for the watched count. Any screen also loads the settings while they are
unread (the first entry, a Retry, the token prompt's Use), since the mode
control follows `writable`, which the status does not carry. Status is polled
every 10 seconds on every tab. On Home, each status tick also refreshes
accounts and retries the endpoint until loaded, and the 24 h activity is polled on
its own 30-second interval. Only Accounts polls accounts on
its own 30-second interval, and Activity polls the chosen window's activity on its own. Polling pauses while the document is hidden,
resumes on visibility, and skips a tick while that poll is still in flight.

- **Home** shows, in this order under the top bar with its slider: the quota-read
  error banner, "The last quota read failed: <error>", adding in Auto "; Auto counts
  an account it cannot read as not low, and passes requests through at full price
  for it", whenever `poll.last_error` is set and the settings'
  `cliproxy.management_secret_file` is not empty (while the settings are unread
  it counts as set); the endpoint URLs,
  masked client key and "Copy key" (the key is fetched on the click and never
  rendered); the accounts strip; "Since start" (all requests, then runs by route from
  `runs_since_start`, and "CLIProxyAPI running" or "not reachable"); and "Last 24 h".
  - The strip has one state at a time. With the settings'
    `cliproxy.management_secret_file` empty (a server deployment that gives the
    core no secret), there is nothing to read and so no read to fail: "Lokki does
    not read the quota here: no management secret file is set", adding in Auto
    "; Auto passes every request through at full price", and no banner. No
    snapshot (`read_at` null, or no answer yet): "Reading the quota…", or, with
    `poll.last_error` set, "No quota read yet: the last read failed (above)" under
    the banner; when `GET /api/accounts` itself failed, "The accounts could not be
    read: <error>" from `app.failed.accounts` (`accountsUnread` in `state.svelte.ts`,
    which Accounts says too). Read, no account: "No account yet" and "Add an account". Otherwise
    one gauge per account: the masked e-mail, a thin pill of the quota left, the
    figure ("no reading" without one) and the reset ("resets in 1 h 12 min", or
    "reset time not read yet"). In Auto the watched accounts come first, each with
    a tick at `low_quota.threshold_percent` and its chip (`low`, `ok`, `unknown`
    with its error), then the others, outlined, with their figure and "not
    watched" and no tick. In Relay and Direct the threshold decides nothing: every
    account's figure and reset, no tick and no `low` or `ok` chip, an account that
    could not be read keeping `unknown` with its error. Once an account's
    `reset_at` has passed, the gauge is full and reads "100 %" with "window
    reset", as `policy.state_of` counts it (`ui/src/lib/gauge.ts`). "Add an
    account" opens Accounts.
  - "Last 24 h" is `RequestsChart` of the activity answer: 24 hourly bars
    stacked by route (relay, pass-through, images), errors marked above the bar
    in the error colour, every sixth bar labelled with its start time, the
    legend carrying the answer's `totals`. With no request it keeps its axes
    and says "No requests yet". The state holds one activity slot and keeps it
    to the window the open screen wants: Home draws only an answer whose
    `window` is `24h`, and an answer for any other window, or one that lands
    after the user left Home, is dropped. "Open Activity" beside it opens Activity.
- **Activity** draws one answer of `/api/activity` (docs/app/api.md) for the window in
  `app.activityWindow` ("24 h" or "7 d", kept while the page lives), and only an answer
  whose `window` is that one: until it lands the chart says "Reading the run log…",
  never the other window's bars.
  - The window control is a group of two toggle buttons. Choosing one reads it at
    once; the buttons stay enabled and the group says `aria-busy` while the read runs,
    so a focused button keeps the focus. Choosing the window already chosen reads
    nothing; choosing the other meanwhile is read at once and its answer is the one
    drawn (the earlier read's answer is dropped, and the group stays busy until the
    newest read answers).
  - Coverage, above the chart: with `complete: false`, "counted since <oldest>: Lokki
    reads the last 8 MiB of its run log; older runs in this window are not counted";
    with `complete: true` and `oldest` after `from`, "since <oldest>: the run log holds
    runs since then"; otherwise nothing. A time is its clock within the last day and
    its weekday and clock before that (`stamp`), for the coverage line, the recent
    runs, the decision changes and "since Lokki started at …" (from
    `/api/status.started_at`). They are seen from the wall clock, which ticks every 10
    seconds and is read again when an answer lands, so a screen left open moves them.
  - Then: "Requests over time" (`RequestsChart`); "Relay outcomes" (cut,
    finalized, client left, error, with how the cut runs ended); "Pass-through
    reasons" (verbatim, with how they ended); "Tokens the provider reported", with
    "Cut runs get no token count from the provider, and these totals count none
    for them."; models and clients (`Breakdown`, the "other" row last); latency per route and overall
    (`Latency`: a solid bar to p50 inside an outline to p90, "no runs" for null);
    "Recent runs"; "Decision changes" (the headline and the core's reason, verbatim).
    A window with no request shows "No requests yet" with "Open Home" in place of the
    breakdowns and the runs; the decision changes stay.
  - A model or client name is one line cut with an ellipsis, its whole text in
    `title` and in the bar's accessible name. A recent run shows its `outcome` as the
    route gave it, never derived, with the error or the reason beside it. It is a
    table with the model and client at most 16 `ch`; where the content is under
    about 800 px (a window under 1000 px, which includes every window under 720 px
    with the sidebar folded) each run is two lines (time, route, outcome; model,
    client, status, duration, tokens out) and nothing scrolls sideways.
- **Accounts** shows the cards, "last read 12 s ago" and "Read now" in every mode:
  the quota is read in every mode. In Relay and Direct the threshold and the scope
  decide nothing, so there is no scope card, no switch, no tick and no `low` or `ok`
  chip (`unknown` keeps its error), and one line: "Auto watches accounts; choose
  Auto to pick them".
  - Above the cards: "last read 12 s ago", "Read now" ("Reading…" while it
    runs), "Add a Codex account", and, in Auto, the scope card, "Auto watches these
    accounts": "Every enabled account" writes `[]`, "Only the ones
    I pick" makes each card's Watched switch live and writes
    `low_quota.accounts`. Under the first the switches are disabled and say "the
    switches apply once you pick accounts". A scope entry no card carries shows
    "Watched but not found: <index>" with "Remove". Under "Only the ones I pick"
    with nothing picked it says every enabled account is still watched. A refused
    write is shown beside the card, and the switch stays where the core says.
  - While a write is in flight the controls stay enabled and say `aria-busy`, so
    a focused switch keeps the focus, and a click then writes nothing.
  - One `AccountCard` per row, keyed on `auth_index`: the masked e-mail, the
    `Gauge`, the chip (`low`, `ok`, `unknown` with the row's `error`, or "not
    watched" for an account out of scope), the reset line ("resets in 1 h 12 min
    · 14:32" from `resetsIn` and `clock`; "reset; the next read shows the new
    window" once `reset_at` has passed; "reset time not read yet" when there is
    none, or it has passed on an account whose state is `unknown`: one whose
    read failed, or a disabled one that is not named, which the core does not read) and the Watched
    switch. A disabled account is greyed (`aria-disabled`). The clock behind the
    countdown and the passed reset ticks every 10 seconds and is read again
    whenever an accounts answer lands.
  - Read, no account: "No Codex account yet" and "Add the account your coding
    client uses; Lokki reads its quota in every mode", with the add button. Never
    read: "No account read yet", or, when `GET /api/accounts` itself failed, Home's
    line "The accounts could not be read: <error>" from `app.failed.accounts` (the
    shell's banner clears at the next read that answers). With the settings' `cliproxy.management_secret_file`
    empty, Home's line "Lokki does not read the quota here: no management secret
    file is set" and no banner (`noSecretFile` in `state.svelte.ts`, which both
    views read).
  - "Add a Codex account" works in every state.
    - It posts `/api/accounts/oauth` and opens the login URL through
      `window.lokki.openExternal`, or `window.open` without the hook. A panel
      under the header holds "Finish the login in a browser" and "Lokki waits up
      to 5 minutes."; "Lokki opened the login page in this computer's browser."
      only when `openExternal` resolved or `window.open` returned a window;
      "Logging in from another device? Open this link there:" with the URL, "Copy
      login link" (it says "Copied" for 3 seconds) and "Cancel"; and the paste
      field below.
    - The paste field finishes a login from another device. That device's
      browser ends on a page that does not load, at an address that starts
      `http://localhost:1455/auth/callback`; the user pastes that address and
      presses "Send", which posts `{url}` to `POST /api/accounts/oauth/<state>`
      (the state is the path, the address only the body). Send stays enabled and
      says `aria-busy` while it runs, and a press meanwhile sends nothing. On a
      200 the field empties and the card says "Sent; finishing the login…" while
      the polling goes on. A refusal shows the core's sentence, as it is, in a
      `role="alert"` under the field, which keeps what was pasted; after
      "expired" or "refused" a second Send gets the same sentence. An empty field
      sends nothing and says "paste the address first". The address lives in the
      component only: not in `app`, storage, a URL or a log, and it is cleared on
      success, on "Cancel" and when the login ends.
    - The UI polls `/api/accounts/oauth/<state>` every 2 seconds for up to 5
      minutes, CLIProxyAPI's own wait for the callback. On `ok` it says "Account
      added" and refreshes the accounts 2 seconds later, since CLIProxyAPI's file
      watcher needs a moment.
    - On `error`, or any answer that is not a 200, it shows the text and stops
      (the status route's text for a login CLIProxyAPI no longer holds is
      "CLIProxyAPI no longer holds this login: it expired, or it finished more
      than a minute ago; check Accounts, else start again", shown as any failure):
      a 502 can be CLIProxyAPI refusing the secret, and polling on walks into its
      ban. "Cancel" stops the polling. One login runs at a time, and it lives in
      the shared state, so leaving the screen and coming back shows it still
      waiting.
- **Settings** shows what a user of the app changes in three open groups, and folds
  the rest. Each row's label, group and choice labels come from `/api/schema`
  (`label`, `group`, `choice_labels`), with its `meaning` under it; the UI owns only
  the group titles and their order (`GROUP_TITLES` in `ui/src/lib/fields.ts`): Mode,
  Connection, Updates and About, then Advanced, a fold closed by default holding
  The core's address, CLIProxyAPI, Quota reading, Cutting, Tool calls, Traffic, Other and Set in the file
  (read-only). A row of a group the UI does not know is never dropped: a writable
  one shows under Advanced › Other, a read-only one under Advanced › Set in the
  file. `hidden` rows (`auth.*`) are never shown. Above 1000 px a column of links
  beside the groups (Mode, Connection, Updates and About, Advanced) scrolls to
  each; following Advanced, or a link to a group inside it while Settings is open,
  opens the fold, and the Advanced link and head carry the count of unsaved
  changes inside it.
  - Mode's first row is the mode slider at the card's width (the same component as
    Home's, writing the same bodies through the same shared write); it sets the
    threshold, so "Cut below, % left" has no field of its own. While Auto is the mode
    (between the slider's ends) the group also shows, on a core with no management
    secret file, "Lokki does not read the quota here: no management secret file is
    set; Auto passes every request through at full price", then "Watched accounts: 3 of 4"
    (the rows of the last `/api/accounts` read that the core marks in scope,
    out of every row of that read) with "Manage on Accounts", and Cut when only with more
    than one watched account or while the count is unread, and "Models cut when low":
    "Every model" or "Only the ones I pick", with one checkbox per id of `GET /api/models`
    under the second (read when Settings opens in Auto and on Retry, from CLIProxyAPI, so
    it works on a core that reads no quota). Like the scope on Accounts it writes at once,
    one `PUT` of `low_quota.models` per change, busy while it runs (a press meanwhile writes
    nothing, a focused control keeps the focus, and a spinner beside the label says why a
    ticked box still shows unticked), and shows what the core reports, not the click. Its hint
    says "Every model is cut when the quota is low." under the first choice, and under the
    second that a model not picked passes through at full price and that a model at any thinking
    level, such as gpt-5.5(high), counts as gpt-5.5. "Only the ones I pick" with nothing picked is local until a model is picked, and
    says "Nothing is picked yet, so every model is still cut when low". A chosen model the
    list lacks shows "Chosen but not listed: <model>" with Remove; a failed read says "Lokki
    could not read the models CLIProxyAPI offers: <error>" with Retry, every chosen name
    stays listed with its Remove, and none is marked "not listed", having no list to compare
    with. Under Relay and Direct
    those rows are gone; leaving Auto writes nothing, a draft on a hidden row stays
    and shows again with Auto, and the group's Save counts and sends only the rows
    shown. When a later accounts read fails, the count stays
    with "counted at the last read" and the failure, until a read answers.
  - Updates and About reads `app.update`, the banner's own slot, through
    `updateWords` (`ui/src/lib/update.ts`, the one home of the update state's
    words, which wraps `updateLine` and which the banner uses too): the line, "Lokki checks once a day; a new
    release shows a banner at the top" and "Check now" (`checkNow`, busy while it
    runs). In a plain browser `app.update` is `null` and there is no update line.
    Below it: the core's version and revision, the UI's version, CLIProxyAPI's
    version ("not reported" without one) and whether it is reachable, from
    `/api/status`.
  - The slider and the models write at once, one `PUT` each, and never draft; Cut when is the
    Mode group's one drafted row. Every other field drafts: a changed field is marked
    "edited", and each group (each Advanced subgroup too) saves its own drafts with one button, "Save 1 change"
    or "Save N changes" ("No changes", `aria-disabled`, with none). A save sends
    only the changed keys and reads back settings and status; a draft goes once
    the read back holds the value sent. A refused save keeps the drafts and shows
    the error and its key at the top of the group, the field marked invalid.
  - A row that is not writable inside a writable group (`cliproxy.version`) shows
    its value as text with a lock and "read-only", no input, and is never
    counted. A field the environment sets is locked with "set by the
    environment"; a `listen` of several addresses with "edit the file to change
    several addresses", and Connection's Port with it; a file that is not writable puts "the settings file is not
    writable here" once at the top and disables every field and Save.
  - A saved `startup_only` key puts a notice per service in a bar at the top of
    the groups' column, which stays in view as the page scrolls, "Restart CLIProxyAPI to apply" or "Restart the core to apply", with
    its button when the shell has `restart`. Notices stay pending in
    `app.restart` until `window.lokki.restart(service)` succeeds or the page
    reloads; one restart runs at a time. CLIProxyAPI's restart restarts the core,
    so its success clears both notices. A failed restart reads the settings
    again: a moved port that did not come up has been put back in the file, and
    the shell's sentence says so beside the notice. When the core's own port
    moved in that restart, the window that asked is replaced by one on the new
    port, and that one says the sentence instead (below).
  - Connection's one row is Port, a field of the UI's own (`PORT` in
    `ui/src/lib/fields.ts`) over `listen`: it shows the port of the core's one
    address and saves `listen` as that address with the port changed and the host
    kept. A refusal of `listen` marks it invalid. The whole address is the
    `listen` row under Advanced › The core's address.
  - A window the shell opens at `?moved_from=<port>`, because the core came back on
    a new port, says above the view "Lokki now listens on port N, not M. Clients
    such as Codex CLI still use the old address: set their base URL to
    `http://127.0.0.1:N/v1`", with Dismiss. A restart that went back for
    CLIProxyAPI while the core's own port moved adds `&went_back=<sentence>`, and
    the note adds "The restart also said: …". A `moved_from` that is not a port
    from 1 to 65535 written without a leading zero shows nothing. The query leaves
    the address bar at once, so a reload does not say it again.
  - CLIProxyAPI's panel sits under the Web panel switch and follows the saved
    `cliproxy.panel`. Off, it says to turn it on and save. On, the shell gets
    "Open the panel" and "Copy the management secret" ("Copied" for 3 seconds),
    each only when the shell provides its hook; a browser gets a link to the
    panel. A configured panel URL is linked only when it is `http:` or `https:`;
    the default URL uses the page's hostname and configured port.
  - While a write, a restart, a hook or a read runs, its control stays enabled
    and says `aria-busy`, so a focused control keeps the focus, and a press then
    does nothing. A failed read of the schema or the settings says so in the
    error's words, with "Retry", in place of "Reading the settings…"; a later read
    that fails keeps the last read's values and says so above the groups until
    a read answers.

## The update banner

`app.update` holds the shell's update state, or `null` when the shell has no
`updateStatus` hook. A plain browser therefore shows no banner at all. With the
hook, every status poll (10 seconds, on every tab) also reads `updateStatus()`,
which the shell answers from memory. The banner sits above the shell:

| State | The banner shows |
| --- | --- |
| `idle` | "Lokki <version>" and "Check now" |
| `up_to_date` | "Lokki <version> is up to date", the `reason` when there is one (no build for this platform), and "Check now" |
| `available` | "Lokki <new version> is available", "Release notes", "Check now", and "Install and restart" when `can_install`; otherwise "Download" and the `reason` |
| `checking`, `downloading`, `installing` | one sentence, no buttons |
| `error` | the error sentence and "Check now"; when the state still names a version, the full `available` banner above it, so a failed install keeps its button |

"Release notes" and "Download" open `notes_url` (or the latest release page)
through `openExternal`; a rejection is shown in a `role="alert"` beside the
buttons. "Check now" calls
`checkForUpdate()` and shows `checking` until it answers. "Install and restart"
calls `installUpdate()` and shows `downloading` at once; on success Lokki
restarts and the call never answers, and a rejection puts its sentence in the
state. A hook that rejects, `updateStatus()` included, lands as `error` with
that sentence.

## Tauri contract

The shell wraps `fetch` to add the operator header to requests on the core's
origin. The UI sends its own header only when it has a browser token. The shell
may provide `window.lokki.restart(service)`, where `service` is `"core"` or
`"cliproxy"`; it resolves when that service has restarted. Without the hook the
UI leaves the restart notice for the operator.

The other hooks are `openExternal(url)`, which opens the system browser,
`openPanel()` and `copyManagementSecret()`, and the updater's three:
`updateStatus()`, `checkForUpdate()` and `installUpdate()` (see "The update
banner"). All seven are optional and each returns a promise. A rejection of
the first four is shown in a `role="alert"`; the updater's three are shown by
the banner.
The panel runs in the system browser, never in a shell window. Its login
takes the management secret, which the user pastes from the clipboard.

If the restart hook rejects, the UI shows `could not restart …` in a
`role="alert"`; the pending restart notice remains until a restart succeeds or
the page reloads.

`PANEL_PATH` in `ui/src/lib/panel.ts` is the panel route on the pinned
CLIProxyAPI release.

## Build and checks

`cd ui && npm run build` writes the static app to
`core/lokki_core/static/`. That directory is gitignored and packaged by
`core/pyproject.toml`.

- `cd ui && npm run check` runs Svelte/TypeScript checks and ESLint.
- `cd ui && npm test` runs the component tests with mocked `fetch`; they make
  no network requests.
- `cd ui && npm run e2e` builds the UI and starts Chromium, a real core and a
  fake CLIProxyAPI on loopback with temporary data outside the checkout, whose
  run log `ui/e2e/seed_runs.py` fills before the core starts with sample runs
  over the 72 hours before (every route, relay class and an error on each
  route, eleven models, two clients), written as the core writes them. It
  exercises the token prompt and the decision in the top bar, Home's 24 h bars
  against the activity answer, Activity's counts in both windows against the
  API's own answer, Home's gauges, the Accounts cards, the scope write (the file
  and the API), the quota read under Direct (a refresh reads, and Home shows the
  gauges), the slider's writes (End writes Relay alone and keeps the threshold, a
  run of five arrow steps one `PUT` of Auto with its threshold, a mouse drag one
  `PUT` when it lets go), the Settings slider setting the threshold by keys, the
  models picker's writes of `low_quota.models` (the fake serves two ids), the conflict response beside the CLIProxyAPI group, the narrow window
  (at 480 × 600 on every destination, Activity with 300-character model and client names written into the answer too: the
  tabs in one row under the top bar, the page heading kept, no element past the
  window's edge and no inner scroller wider than itself, and room for Home's
  content under the top bar with the slider and its rule line), the mode control's writes of `routing` alone followed back from
  the core, by click and by keyboard (Space and Enter each write once), and
  schema agreement. The fixture stops
  both processes and removes its data directory. Playwright's output directory
  is temporary; a green run removes it, while a failed run prints and keeps its
  path.
- `cd ui && LOKKI_SCREENS=<dir> npm run screens` starts the same fixture and
  writes each state's screenshot, `<screen>-<state>-<light|dark>.png`, at
  1100 × 1180 for Home, so it shows whole, 1100 × 760 for Accounts, 1100 × 2100
  for Activity (1100 × 760 when it is empty), 1100 wide and as tall as the page
  for Settings, and 480 × 600 for the narrow Home shot
  (480 × 2600 for Activity's), into `<dir>`. The login states are patched with `page.route`: no
  login request reaches the core, and the run asserts every one was answered
  there. The page holds the
  operator token, so the directory must be outside the checkout, and the run
  refuses one inside it; before each shot it checks that neither the token nor
  the client key is in the page, nor, for Settings, the management secret. The PNGs go into a pull request as
  attachments, never into the tree.

`ui/src/views/schema.fixture.json` mirrors `/api/schema` for component tests.
When the core schema changes, regenerate the fixture with
`cd core && python3 -m lokki_core schema > ../ui/src/views/schema.fixture.json`;
the browser run checks it against the live schema.

The favicon is `ui/src/assets/favicon.svg` (32 px) and `favicon-16.svg`, the
small drawings of the mark in the app icon's colours. Their PNGs are made with
the rest of the icon set by `app/scripts/icons.py`.

## Engines

The UI runs in WebView2 on Windows, the system WKWebView on macOS, WebKitGTK on
Linux, and any current browser. Its colours are `oklch()` tokens in
`ui/src/app.css` with no fallback declarations, and the stylesheet uses no
`color-mix()`: each fill over another token is a literal `oklch()` value.
`oklch()` is read from Safari 15.4 and Chromium 111; the token prompt's `inert`
needs Safari 15.5. So the macOS app requires macOS 12.4
(`bundle.macOS.minimumSystemVersion`), the first whose system WebKit has both,
and the build lowers its syntax for `safari15.5` and `chrome111`
(`build.target` in `ui/vite.config.ts`). The target cannot lower a regular
expression newer than Safari 15.5 (a lookbehind, the `v` flag) or an API, so
the bundle must not use them. An engine that cannot read a token's colour
draws the inherited system text colour on a transparent ground: the page stays
legible, and every state keeps its word or icon, the checked mode its check
mark and weight.
