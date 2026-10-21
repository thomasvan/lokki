# lokki: feedback — design

## 1. What changes, and what does not

Six points of feedback on the app built from `main` 47ff922.

| point | what changes | section |
| --- | --- | --- |
| 4 | nothing: two accounts on the Linux app work; recorded as tested | 2 |
| 5 | the Low Quota Mode switch goes: Relay always cuts, Direct always passes through, Auto is the low-quota rule; the quota is read in every mode | 3 |
| 2 | a Codex login finishes from a browser on another machine: the user pastes the address the browser ended on | 4 |
| 3 | `text_calls` is on unless a file or the environment turns it off | 5 |
| 6 | Settings shows what a user of the app changes; the rest folds under Advanced | 6 |
| 1 | on macOS the app installs its own updates, as on Linux and Windows | 7 |

Kept from the app design (`2026-10-10-lokki-app-design.md`) and the interface design
(`2026-10-14-lokki-ux-design.md`), and held by every section here:

- the UI works in a plain browser against a server or Docker core, with no shell hook;
- the seven `window.lokki` hooks; this design adds no page command;
- loopback only; the operator token never in the page; no secret, e-mail or request content
  in a log, a page or a response beyond what the routes already return;
- every operator route behind the Host/Origin guard, the token gate and the operator check,
  in that order, and in `test_control.GUARDED`;
- controls stay enabled and say `aria-busy` while their write or read runs, so a focused
  control keeps the focus; a failed read says so in the error's words;
- every write followed by a read; the control shows what the core reports, never the click
  (the mode slider shows the position a user holds or steps to until it is written, 3.7);
- `ui/src/lib/mode.ts` is the home of the modes' words, `schema.py` of every setting's default,
  range, label and group, `activity.outcome()` of the run-record partition;
- the writable boundary (`writable`, `env_overrides`, the 409), the per-service restart flow,
  the stop sequence, the update banner's states;
- no new dependency.

The docs in `docs/app/` and the README are where each fact below ends up; the dated specs
before this one are design records and are not edited. Where this spec and an earlier one
differ, this one holds: app design 3.3 (`low_quota.enabled`), 4.2, 4.7 (the same-machine
browser), 5, 6.3 and 7 (the install switch); interface design 2 (Home's mode control), 3,
4.2, 5 and 6.1 (the order of the tray's modes and Auto's label).

## 2. Point 4: two accounts on the Linux app — nothing to build

Two Codex accounts added through the Linux app are both read and both serve requests, on the
relay path and on pass-through, and those requests show in `/api/activity`. Logins from a
browser on another machine time out; point 2 answers that.

Out of scope: which account served a request. CLIProxyAPI picks it and the core does not learn
which; no record holds it.

## 3. Point 5: the three modes, and Auto as the low-quota rule

### 3.1 Semantics

| mode | `routing` | decides | sentence (`mode.ts`) | slider |
| --- | --- | --- | --- | --- |
| Direct | `direct` | every request passes through | Every request goes to CLIProxyAPI untouched, at full price. | 0 |
| Auto | `auto` | a request is cut when the quota is low, by the rule below; otherwise it passes through | Cut when the watched accounts run low; otherwise pass through at full price. | 1 to 99, the threshold |
| Relay | `relay` | every request is cut | Every request is cut: lokki ends the answer early so it costs less. | 100 |

The modes are listed in this order wherever a user sees them (the slider from left to right,
the top bar, the tray); `schema.py`'s `choices` keep their own order (3.2).

**One control, the threshold.** A user sets the mode by moving one value, from 0 to 100 % left
(3.7): 0 writes `routing: direct`, 100 writes `routing: relay`, and 1 to 99 writes `routing:
auto` with that value as `low_quota.threshold_percent`. The ends are modes and never
thresholds, because as a threshold 100 would leave an account just reset (100 % left) not low
and would pass through every request whose quota cannot be read. So an end writes `routing`
alone and leaves the stored threshold, which Auto returns to. The core keeps `routing` and
`low_quota.threshold_percent` as 3.2 has them; the slider is the UI's.

**Auto's rule** is four settings, shown only while Auto is the mode (between the slider's
ends):

- the threshold: an account is low when less than `low_quota.threshold_percent` of its
  5-hour window is left (default 30);
- the watched accounts, `low_quota.accounts`: empty (the default) watches every enabled
  Codex account, otherwise the ones named, one or several;
- with more than one watched, `low_quota.rule`: `all` (the default) cuts when every watched
  account is low, `any` when one is;
- the models cut when low, `low_quota.models`: empty (the default) is every model, otherwise
  the ones named; a request for a model not named always passes through.

A quota that cannot be read counts as not low, and the reason says which read is missing. A
cut is cheaper than a finalized turn, never free (`docs/relay/cost-of-a-cut.md`); no sentence
says "free" or names a saving.

### 3.2 Settings

| key | type | default | meaning (`schema.py`) | label | group |
| --- | --- | --- | --- | --- | --- |
| `routing` | choice `relay`, `auto`, `direct` | `relay` | relay: cut every request; auto: cut when the watched accounts are low; direct: pass every request through | Mode | `saving` |
| `low_quota.threshold_percent` | int 1..99 | 30 | Auto cuts when an account has less than this share of its 5-hour window left | Cut below, % left | `saving` |
| `low_quota.accounts` | list | `[]` | the accounts Auto watches, as CLIProxyAPI numbers them; empty = every enabled Codex account | Watched accounts | `saving` |
| `low_quota.rule` | choice `all`, `any` | `all` | with several watched accounts: all = cut when every one is low; any = when one is | Cut when | `saving` |
| `low_quota.models` | list | `[]` | the models Auto cuts when the quota is low; empty = every model; any other model always passes through | Models cut when low | `saving` |
| `low_quota.poll_seconds` | int 30..600 | 60 | how often the accounts' quota is read, in every mode | Read every | `tuning.quota_reading` |

`low_quota.enabled` leaves the schema. The key names stay `low_quota.*`: renaming the section
would migrate every file for a word, and a read-only file cannot be migrated.

**The default of `routing` is `relay`**, so that no file that never chose a mode changes what
it does. A hand-written server file without `routing`, with `low_quota.enabled` absent, cut
every request, and `relay` keeps it doing so, where `auto` would start passing every request
through at full price the day its image is rebuilt, with nothing in the file changed. A server
file made from `config.example.yaml` names `routing: auto`; 3.3 keeps it cutting too. `init` keeps writing `routing: auto`, so a new app starts in
Auto.

The PUT allow-list gains `low_quota.models` and loses `low_quota.enabled`; a body naming
`low_quota.enabled` is the 400 any unknown key gets ("low_quota.enabled is not a setting").

### 3.3 Existing files

`migrate.py` gains `RETIRED`, the one list of keys read and dropped, beside `RENAMES`:
`("low_quota.enabled",)`. `migrate(doc)` drops each retired key it finds (and a section it
leaves empty). Two documents carry a choice of mode in that key, and `migrate` sets `routing`
before dropping it, in memory for a file it cannot write, so the choice holds at every load:

- `low_quota.enabled: true` and no `routing`: the file chose Auto under the old default, so
  `routing: auto`.
- `low_quota.enabled` with any other value, `routing: auto`, and no
  `cliproxy.management_secret_file` (absent or empty): the file cut every request and could never have read a
  quota (it is what `cp core/config.example.yaml core/config.yaml` makes), so `routing: relay`.
  A document that names the secret file, as every app install does, follows Auto's rule.

`migrate` reads the key's value as `_strict_flag` does (`config.py:246`): `true`, `"yes"`, `"on"`
and `"1"` (any case) are true; any other value, a typo included, is not true, and never refuses
the file. Any other document loses the key with nothing set. `migrate` returns what it applied as
`(old, new)` pairs, as for renames: a retired key is `("low_quota.enabled", None)`, or
`("low_quota.enabled", "routing: auto")` / `"routing: relay"` when it set the mode, and `init`
prints the pair as it prints a rename (`low_quota.enabled -> routing: relay`), a `None` as
`low_quota.enabled -> (dropped)`.

- **Load** (`config.load_settings`, which already runs `migrate`): the key is dropped before
  `resolve_settings`, so any value it holds is accepted, never validated, never listed in
  `unknown_keys`, and decides nothing.
- **Write** (`settings_api.put`): the loaded document goes through `migrate` before the merge,
  so the next write leaves the key out. (The renames take the same path; today `put` merges
  the document as read.)
- **`init` on an existing data directory** rewrites the core's file once when `migrate`
  applied anything, as it does for renames, and its note names the pairs. `init` no longer writes `low_quota` at all on a new directory (`init.py:150-151` keeps
  only `routing: "auto"`).
- **A read-only file** (the Docker deployment) keeps the key; load ignores it each time.
- **The loader's check** "low_quota.enabled needs cliproxy.management_secret_file to name a
  readable file" (`config.py:664-666`) goes: nothing refuses a file for the secret any more (3.5).

**When the retired key goes.** `RETIRED`'s entry and both of its `routing` cases are a
read-time upgrade, and they go together when no settings file in use still holds
`low_quota.enabled`: every app install has started once on a core that drops it (its `init`
rewrites the file), and every hand-written server file that holds it has been edited, the
example's copies included. Removing the entry earlier would put the key in `unknown_keys`,
where it decides nothing; a file with `enabled: true` and no `routing` would fall to `relay`
and cut every request, and a copy of the example (`routing: auto`, no secret) would follow
Auto's rule and pass every request through at full price.

What each existing file does after the update:

| the file says | before | after |
| --- | --- | --- |
| no `routing`, `low_quota.enabled` false or absent (hand-written server files) | cut every request (`auto`, Low Quota Mode off) | cut every request (`relay`, the new default): it never chose a mode, so it keeps what it did |
| no `routing`, `low_quota.enabled: true` | Auto's rule | Auto's rule (`migrate` sets `routing: auto`) |
| `routing: auto`, `low_quota.enabled` not `true`, no `management_secret_file` (a copy of `config.example.yaml`) | cut every request, reason `low quota mode off` | cut every request (`migrate` sets `routing: relay`) |
| `routing: auto`, `low_quota.enabled: false`, a `management_secret_file` (every installed app: `init` wrote it) | cut every request, reason `low quota mode off` | Auto's rule: cuts only when the watched accounts are low |
| `routing: auto`, `low_quota.enabled: true`, a readable secret | Auto's rule | Auto's rule, the same decision; the reason strings are the new ones (3.4) |
| `routing: auto`, `low_quota.enabled: true`, no readable secret | refused at load ("low_quota.enabled needs cliproxy.management_secret_file …") | loads; Auto passes every request through, reason `the quota cannot be read` (3.5) |
| `routing: auto` and no `low_quota.enabled` | Auto with Low Quota Mode off: cut every request | Auto's rule; with no readable secret it passes every request through (`the quota cannot be read`) |
| `routing: relay` or `direct`, with any `enabled` | cut / pass every request | the same |

The fourth row is the decision of point 5: an installed app that was cutting every request
passes requests through at full price while its accounts are not low. The last row is a file
that named `auto` and nothing else; it chose the mode, and the mode now means the quota rule.
The README's update note says both for the release that carries it, and says to a server
operator: a file that names `routing: auto` passes requests through when its quota is not low
or cannot be read; set `routing: relay` to keep cutting every request.

The live pair's settings file was checked for `routing`, `low_quota` and
`cliproxy.management_secret_file`, and holds none of them: it is the first row, and keeps
cutting every request.

### 3.4 The decision

`quota/policy.py` (`decide` at lines 44-61) keeps one pure function for the quota and adds one
for the model:

```
decide(snapshot, low_quota, routing, now, read_error) -> Decision(relay_on, reason, per_account)
for_model(decision, low_quota, model) -> Decision
```

`read_error` is the poller's `last_error` (`None` when the last read answered). `per_account`
holds every account of the snapshot with its own state, `low | ok | unknown`, whether it is
watched or not; `Decision` gains `watched`, the set of indexes in scope. `decide` is unchanged
in its arithmetic (remaining, staleness at three poll intervals, a passed `reset_at` counting
as 100, a window other than 5 h counting as unknown); what changes is the scope's emptiness, the
unknowns and the words.

**Reasons**, exact, in the order `decide` tests them. `policy.REASONS` is their one home in the
code; `docs/app/api.md` lists them, and a core test holds that list equal to `REASONS`.

| when | `relay_on` | `reason` |
| --- | --- | --- |
| `routing: relay` | true | `routing: relay` |
| `routing: direct` | false | `routing: direct` |
| Auto, no snapshot, `read_error` set | false | `the quota cannot be read` |
| Auto, no snapshot, no error yet | false | `the quota is not read yet` |
| Auto, no account in scope (none enabled, or none of the named ones in the snapshot) | false | `no account to watch` |
| Auto, every watched account unknown | false | `no watched account could be read` |
| Auto, `all`, every watched account low | true | `every watched account is low` |
| Auto, `all`, every one read is low, one or more unknown | false | `a watched account could not be read` |
| Auto, `all`, a watched account read and not low | false | `not every watched account is low` |
| Auto, `any`, a watched account low | true | `a watched account is low` |
| Auto, `any`, none read is low, one or more unknown | false | `a watched account could not be read` |
| Auto, `any`, every watched account read and not low | false | `no watched account is low` |
| `for_model`: the decision cuts, `models` is not empty, and the request's `model` (its thinking-level suffix removed or not) is not in it | false | `this model is not cut when low` |
| `decide` raised | false | `the quota rule failed` |

`low quota mode off`, `no snapshot yet`, `scope is empty` and the five `… in scope …` strings
leave the code. Run records written before keep them, and Activity shows each reason as its
records hold it.

`for_model` compares the request's `model`, as the core receives it in the JSON body, with
`low_quota.models`. A request with no `model`, or a `model` that is not a string, is not
a named model and passes through at full price when the quota is low. A model at a thinking
level counts as its model: CLIProxyAPI 8.0.23 reads `gpt-5.5(high)` as `gpt-5.5` (`ParseSuffix`
in `internal/thinking/suffix.go` takes the text before the last `(` when the model ends with `)`),
and the picker can only offer plain ids, so under exact matching a picked model sent with a suffix
would pass through at full price, the opposite of what picking it asks. A request is named when it,
or its name with that one suffix removed, is in the list: `gpt-x(high)` is named by `gpt-x` or by
`gpt-x(high)`, `gpt-x(high` and `gpt-y(high)` are not named by `gpt-x`, and `gpt-x(a)(b)` loses only
`(b)`. It runs only on a decision that cuts, so the quota's own
reason stays on every request the quota alone passes through.

**`routing.decide_now(snapshot=..., model=...)`** is the one caller:

- It reads the snapshot and `poller.POLLER.status()["last_error"]`, calls `decide` with the
  clock, then `for_model` when a model is given. One decision per request, computed when the
  request is read, never cached; a request in flight is not switched.
- **The guard.** `decide` raising is caught there: the decision is `the quota rule failed`,
  and one stderr line `[routing] the quota rule raised <ExceptionType>` says so once per
  change (the type only: an exception's text is not the log's to keep). The `routing` value
  is read before `decide` runs, so Relay and Direct never reach it. A raise therefore cannot
  answer 500 to every `/v1/responses`.
- **Change tracking** (the `[routing]` line and the `/api/activity` events) follows the quota
  decision, the one without a model. `for_model` changes a request's route and its record's
  `reason`, and is logged by neither: two clients on two models would otherwise log a change
  per request.
- `/v1/responses` passes the body's `model` (`handler.py:748`, where the body is already read); `/healthz`, `/api/status` and `/api/accounts`
  pass none, so they show the quota decision: what a request for a model cut when low gets.
- `/api/run` always runs the engine and calls no decision.

### 3.5 The quota is read in every mode

- The poller reads every `low_quota.poll_seconds` whatever the mode; its `enabled` checks go
  (`_can_read` and `_loop`, `poller.py:110-117`), so `POST /api/accounts/refresh` reads in every mode. Switching to
  Auto has a snapshot at once. The hold after a 401 or 403 stays: a wrong secret is not
  retried until a reload or a refresh.
- The reader reads every enabled Codex account and every named one, so an account Auto does
  not watch still has its numbers on Home and Accounts. The scope enters only the decision.
  A disabled account that is not named is not read and carries `error: "disabled"`, in every
  scope.
- **No readable management secret** no longer refuses the file. The poller records the read
  error and holds no snapshot: `cliproxy.management_secret_file` empty is `"no management
  secret file is set"`; a file it cannot read or that is empty keeps today's
  `"management secret file unreadable: <ExceptionType>"` and `"management secret file is
  empty"`. Neither reaches CLIProxyAPI, so neither counts toward its ban. Auto then passes
  every request through with `the quota cannot be read`; Relay and Direct decide as before.
- Unknown counts as not low, so a dead management API in Auto makes every request a
  full-price one while the endpoint looks healthy. The `[quota]` and `[routing]` lines,
  `/healthz.routing` and the Home banner (3.7) are how that shows, as today.

### 3.6 Routes

| route | change |
| --- | --- |
| `GET /healthz` | `routing` is `{relay_on, reason}` of the quota decision (3.4); unchanged in shape |
| `GET /api/status` | `low_quota` is `{threshold_percent, rule, accounts, models, poll_seconds}` (no `enabled`); `decision` is the quota decision; `poll` unchanged |
| `GET /api/accounts`, `POST /api/accounts/refresh` | each row's `state` is the account's own (`low`, `ok`, `unknown`), watched or not; `in_scope` says whether Auto watches it; `decision` as in status |
| `GET /api/models` | new, below |
| `PUT /api/settings` | allow-list: `low_quota.models` in, `low_quota.enabled` out |
| `POST /v1/responses` | the decision takes the request's `model`; the pass-through record's `reason` is the request's decision |

**`GET /api/models`** — an operator route, in `_OPERATOR_GET` and `GUARDED`. It reads
CLIProxyAPI's `GET /v1/models` with the core's key, as the core's `/v1/models` does, and
answers `{models: [id, …]}`, the `data[].id` strings, sorted and without duplicates.
CLIProxyAPI not answering, answering other than 200 or answering a body without `data` is a
502 `{error}` naming which (`"CLIProxyAPI did not answer"`, `"CLIProxyAPI answered <status>"`,
`"CLIProxyAPI's model list has no data"`). It is not the client route `/v1/models`: the
Settings picker reads the list under the Host/Origin guard and the operator check, which
`/v1/models` lacks, and `/v1/models`'s 502 carries an exception's text (`handler.py:586`)
where this route answers the fixed sentences above. Its `GUARDED` row runs against a fake
CLIProxyAPI or an injected closed port, never the default `127.0.0.1:8317`.

### 3.7 The UI and the tray

`mode.ts` (lines 7-25) loses `LQM_NOTE`, `LQM_ACTION`, `LQM_NOT_READ`, `LQM_NEW_ACCOUNT` and
`LQM_NO_COUNT`; `turnOnLowQuota` goes from `state.svelte.ts:146` with its callers
(`Home.svelte:6,38`, `TopBar.svelte:4,32`); `modeBody(mode)` is `{routing: mode}` for all three, and it gains the
words below. The UI shows reasons and never branches on one: `Home.svelte`'s copy of a reason
string (`FULL_PRICE_REASON`, line 13) goes, and what the UI decides by is a field it reads (`routing`, `poll.last_error`,
the settings' `cliproxy.management_secret_file`). A one-click choice writes only `routing`;
the slider writes `routing`, and between its ends the threshold with it (below). The 400 a core
gave for Auto without a readable secret no longer exists.

**Top bar**, on every screen. The three one-click choices, Direct · Auto · Relay, and the
decision line: the headline from `relay_on`, the reason beneath it as the core gives it. Auto's
label carries the threshold from `/api/status.low_quota.threshold_percent`, "Auto 30 %", so a
user sees where Auto returns to; each choice writes `modeBody`, `{routing}` alone, and leaves
the threshold as stored. Each choice's sentence is its description, shown on hover or focus,
as today. The note "Low Quota Mode is off, so Auto cuts every request" and its button go. On
Home the top bar is full size: the same choices and decision line, and under them, the width
of the bar, the mode slider. Until the window has read a status, the top bar shows its waiting
line in place of the choices and the slider, so no choice is drawn without its number; after a
failed read the controls keep the last status read, as the top bar does today.

**One mode write at a time.** The window holds one mode write at a time in its shared state,
not in each control, so the top bar's choices, the slider and Settings › Mode share it: while a
write and its reads are in flight every mode control says `aria-busy`, stays enabled and keeps
the focus; a choice made meanwhile from any of them replaces any choice already waiting, shows
as pending on its control, and is sent when the write in flight returns, so no click or key is
lost and the latest choice wins; and the slider's held position (key steps not yet sent) lives
in that shared state too, so a choice from any mode control first queues a held position and
then itself, whatever the focus does, and the click is the final state. A waiting choice that a
later one replaces is dropped, and its control shows what the core reports; the checked mark
always follows the core. The tray writes through the shell, not the window, so the window shows
a tray change at its next status read; a slider position held while a tray change is made can
overwrite it.

**The mode slider** (`ui/src/components/ModeSlider.svelte`, one component for Home's top bar
and Settings › Mode). One `<input type="range">`, 0 to 100 in steps of 1, named "Mode and
threshold", along "% left": Direct at the left end, Relay at the right.

- *Position.* What the core reports: `routing: direct` at 0, `routing: relay` at 100, `routing:
  auto` at `low_quota.threshold_percent`. With no status read (in Settings › Mode, where the
  slider is drawn before one), the track has no thumb, takes no input, and its readout and words
  are empty. A threshold outside 1 to 99 cannot reach it: the core refuses one at load (`config.py:648-649`), and a reload it
  refuses keeps the running value (`reload_settings`, `config.py:840-856`). The track is filled from 0 to the thumb in the
  cutting colour, the share of the quota left below which requests are cut; under the track the
  scale reads "Direct · 0 %", "% left" and "100 % · Relay"; the readout beside the name is the
  mode's label as the top bar says it ("Direct", "Auto 30 %", "Relay").
- *Words.* Under the slider, from `mode.ts`: Direct's sentence at 0, Relay's at 100, and between
  them the rule line at the slider's position: "Cut when every watched account has less than
  30 % left" (`all` with several watched), "Cut when any watched account has less than 30 %
  left" (`any`), "Cut when the watched account has less than 30 % left" (one watched), each
  followed by " · every model" or " · 3 models" from `models`; while the accounts are unread
  the count is left out ("Cut when every watched account …"). `aria-valuetext` is the readout
  and the words: "Auto 30 %: Cut when every watched account has less than 30 % left · every
  model".
- *Writes.* 0 writes `{routing: "direct"}`, 100 writes `{routing: "relay"}`, and 1 to 99 writes
  `{routing: "auto", low_quota: {threshold_percent: N}}`, one `PUT`. An end leaves the stored
  threshold as it is (3.1). A position equal to the one the core reports writes nothing.
- *When it writes.* A drag writes once, when the pointer lets go, inside the window or
  outside it (the slider holds the pointer's capture, and losing it counts as letting go); a
  drag the browser cancels writes nothing and returns to what the core reports. A press on the
  track is a drag of no length. Keys move the thumb and the words at once and write when they stop: 500 ms
  after the last step, or when the slider loses focus, whichever comes first, so holding an
  arrow from 30 to 45 writes once. Steps of 1 with the arrows, 10 with PageUp and PageDown,
  Home and End to the ends. Until it writes, the slider shows the position held or stepped to,
  and a status read does not move it.
- *Busy.* While a mode write runs, from any control, the slider says `aria-busy`, stays
  enabled and keeps the focus; keys and drags still move it, Home and End to the ends included,
  and the position they leave waits as the latest choice (One mode write at a time, above).
  The read after the write decides where it rests.
- *Refused or failed.* The slider goes back to what the core reports and says why under it in
  a `role="alert"` line: the core's sentence for a refusal ("the settings file names no client
  token; the reload would refuse it, so nothing is written"), the request's error for a failed
  write. A write the core answers 500 "written but not applied: …" has put the position in the
  file while the core keeps running the old value; the slider shows the old one, with that
  sentence. A write that succeeds but whose read fails leaves the slider on the position the
  last read reported, which can be the old one, with the read's error on the same line, until
  the next status read moves it. The line stays until the next write returns: when a choice was
  waiting behind the refused or failed write, the line stays while that choice's write runs,
  then is cleared if it succeeds or replaced by its own reason if it is refused.
- *Locked.* With the settings file not writable the slider is disabled with the lock line "the
  settings file is not writable here", as the mode control is today.

**Home**, in Auto:

- The slider's words are the rule line (above).
- The accounts strip: the watched accounts first, each gauge with its tick at the threshold
  and its chip (`low`, `ok`, `unknown` with its error); then the accounts Auto does not watch,
  outlined, with their figure and "not watched", no tick. "Add an account" when there is none.
- The decision stays in the top bar above them, so the slider, the ticks and the reason sit on
  one screen.

Home in Relay or Direct: the same strip with every account's figure and its reset, no tick and
no `low`/`ok` chip (the threshold decides nothing there); an account that could not be read
keeps `unknown` and its error. The slider's words are the mode's sentence.

Home, every mode: the quota-read banner shows whenever `poll.last_error` is set, "The last
quota read failed: <error>", adding in Auto "; Auto counts an account it cannot read as not
low, and passes requests through at full price for it". When the settings'
`cliproxy.management_secret_file` is empty (a server deployment that gives the core no
secret) there is nothing to fail: the strip says "lokki does not read the quota here: no
management secret file is set", adding in Auto "; Auto passes every request through at full
price" (3.5), with no banner. Between the slider's ends such a core still writes Auto, as
anywhere. Before the first read the strip says "Reading
the quota…"; when there is no snapshot yet and `poll.last_error` is set, the error wins: the
banner shows it and the strip says "No quota read yet: the last read failed (above)".

**Accounts** (`Accounts.svelte`; the Low Quota Mode strings at 171 and 177). The cards, "last read … ago" and "Read now" in every mode ("Read now" no longer
waits on a switch). In Auto, above the cards, the scope choice as today ("Every enabled
account" / "Only the ones I pick", the Watched switches, "Watched but not found" with Remove),
the threshold tick on each gauge and the chips; in Relay and Direct, no scope choice, no
switch, no tick, and one line "Auto watches accounts; choose Auto to pick them". The Low Quota
Mode card and its switch go. "Account added" no longer says the account is not read.

**Settings** (section 6 for the layout; the Saving card is `Settings.svelte:338-373`). The Mode
group's first row is the mode slider, with its words, at the width of the card; it is the
threshold's control, so "Cut below, % left" has no number field of its own. Under it, only
while the core reports Auto (between the slider's ends):

- with the settings' `cliproxy.management_secret_file` empty, Home's line with its Auto clause:
  "lokki does not read the quota here: no management secret file is set; Auto passes every
  request through at full price";
- "Watched accounts: 2 of 3" with "Manage on Accounts";
- "Cut when", only when more than one account is watched (the scope's length, or the enabled
  rows of `/api/accounts` under "every enabled account"; shown while that count is unread);
- "Models cut when low": "Every model" / "Only the ones I pick", and under the second one
  checkbox per model from `GET /api/models`. Like the scope, it writes at once, one `PUT` of
  `low_quota.models` per change, busy while it runs; "Only the ones I pick" with none picked is
  local until one is, and says every model is still cut. A named model the list does not carry
  shows "Chosen but not listed: <model>" with "Remove". A failed `/api/models` read says
  "lokki could not read the models CLIProxyAPI offers: <error>" with "Retry", and every name
  already chosen stays listed with its Remove; with no list to compare against, none is marked
  "Chosen but not listed".

"Cut when" drafts and saves with the group, as today; it is the group's one drafted row.
Leaving Auto keeps these values and the threshold in the file; they show again when Auto is
chosen.

**Tray.** `Mode::body` (`logic/src/tray.rs:40-47`) is `{routing: <id>}` for all three, and its
test `each_mode_writes_the_body_of_spec_3_1` (`:231-237`) with it, so the tray's Auto returns
to the stored threshold. The modes are listed Direct, Auto, Relay (`Mode::ALL`), and Auto's
item says the threshold, "Auto (below 30 %)", from `low_quota.threshold_percent` of the
`/api/status` the tray already reads every 10 s and after a choice (`src/lib.rs`: `TRAY_REFRESH`
at 44, the loop at 591-594, `refresh_tray` at 330 through `status` at 335-344); "Auto" while the core does not answer or names no threshold.
The menu is otherwise as today, its reason line the core's new strings. `tray::View` gains
the Auto item's label, and `src/tray.rs`'s `Tray::apply` (40-71) sets that item's text each
time it applies a view, as it sets the headline: the items are built once with `Mode::label`
(147-148) and nothing sets their text after.

**Docs.** `docs/app/api.md`: the reasons list, `/api/status.low_quota`, the account rows'
`state`, `GET /api/models`, the allow-list, and the refresh's "with Low Quota Mode off" (line
83), which goes; its "Adding an account" section changes with point 2 (4.7). `docs/app/ui.md`: the top bar, Home, Accounts and
Settings as above, the mode slider with what each position writes. `docs/app/first-run.md`: a
first run is in Auto with no account, reason `no account to watch`, and its "Turn on Low Quota
Mode" goes; the tray's Auto writes only the mode and says the threshold it returns to. `docs/app/limits.md`: the quota is read in every mode. README › "Low Quota Mode" becomes
"Relay, Auto and Direct", with Auto's rule and what a quota it cannot read does.

### 3.8 Tests

- `policy`: the reason table above, one row per line, with the `all`/`any` unknown rows, the
  empty scope by no enabled account and by a named account the snapshot lacks, `for_model`
  (named, named at a thinking level, not named, no model, empty list, a decision that passes), and the states of
  unwatched accounts.
- `routing`: a raising `decide` gives `the quota rule failed`, one log line per change with the
  type and not the message; two requests on two models change the route without a log line or
  an event; Relay and Direct never call `decide`.
- `migrate`: `RETIRED` drops the key and an emptied section; `enabled: true` with no `routing`
  sets `routing: auto`, and with a `routing` sets nothing; `enabled: false` with `routing: auto`
  and no `management_secret_file` sets `routing: relay`, and with the secret file named sets
  nothing; `config.example.yaml` itself loads as `relay`; each applied pair as `init` prints it;
  `enabled: true`, `routing: auto` and an unreadable secret loads and decides `the quota cannot
  be read`; `load_settings` on a file with
  `low_quota.enabled` of any value loads, lists no unknown key, and decides as without it;
  `put` on such a file writes it without the key, and on a copy of the example writes
  `routing: relay` into the file; `init` on such a directory rewrites it once
  and says so; `init` on a new one writes no `low_quota`.
- `migrate`, the cases it leaves alone: a bare `routing: auto` with no `low_quota` and no
  secret sets nothing and loads as Auto, which passes every request through (`the quota cannot
  be read`), the file having chosen the mode; `routing: relay` and `routing: direct` with
  `enabled: false` and no secret keep their mode. `"yes"`, `"on"` and `"1"` read as true, and a
  typo as not true.
- `config`: a file with no `routing` resolves to `relay`; `routing: auto` with no readable
  secret loads.
- `poller`: it reads under Relay and Direct; a refresh under Relay reads; an empty secret path
  records `"no management secret file is set"` and calls nothing.
- `reader`: an enabled account out of scope is read.
- `/api/models`: the guard rows in `GUARDED`; the id list sorted and deduplicated; each 502.
- `/v1/responses` with `low_quota.models` set routes a named model by the quota and passes an
  unnamed one, the record carrying `this model is not cut when low`.
- UI: each one-click choice writes `{routing}` alone, and Auto's label carries the threshold;
  one mode write at a time across the top bar, the slider and Settings › Mode: a choice made
  while a write is in flight waits, marked pending, and is sent after it, the latest of several
  waiting wins, and a held slider position is queued before a choice from any control without
  a blur; a refusal's line stays through a waiting write and gives way to its result; with no
  status read the top bar shows its waiting line and Settings' slider has no thumb and takes no
  input;
  the slider writes each end as a mode and a position between them as Auto with its threshold,
  in one `PUT`, once per drag and once per run of key steps, stays busy and focused while it
  writes, and goes back with the reason when refused; its words and `aria-valuetext` at 0,
  between and 100; the rule line's three forms and the unread count; Accounts in Relay shows
  no switch; the models picker's write, its unlisted entry and its failed read; the banner's
  Auto sentence and the no-secret line with its Auto clause.
- Tray: `each_mode_writes_the_body_of_spec_3_1` holds `{routing}` for all three; the menu lists
  Direct, Auto, Relay, and Auto's item says the threshold of the status it shows.

### 3.9 What point 5 does not change

The quota arithmetic, the 5-hour window rule, staleness, the reader's endpoint and masking,
the poller's hold and its 15 s refresh wait, pass-through itself, the images routes, the run
record's other fields, `/api/activity`'s shape, the tray's icons and its menu but for the order
of the modes and Auto's label (3.7), and which account CLIProxyAPI picks (the decision stays
global).

## 4. Point 2: finishing a login from a browser on another machine

### 4.1 The flow

The login link CLIProxyAPI builds sends the browser back to
`http://localhost:1455/auth/callback?code=…&state=…`. On the machine the core runs on, the
core's listener takes it, as today. A browser on any other machine is sent to its own
`localhost:1455`, which does not answer; its address bar then holds the whole callback. The
user copies that address and pastes it into lokki, which hands it on exactly as the listener
would. The login link is the same in both cases; the provider's page is the same.

### 4.2 One hand-on, two ways in

`quota/oauth.py` holds the pending login: its `state` (set when CLIProxyAPI names it), its
deadline (300 s from the bind, CLIProxyAPI's own wait) and what became of an answer handed
on. `hand_on(state, code, error)` is the one function both the listener and the paste route
call:

1. No login pending, or its deadline passed: refused, `expired`.
2. `state` is not the pending login's (including a login whose `state` CLIProxyAPI has not yet
   named): refused, `another login`. This is the comparison the listener lacks today. It
   matters beyond tidiness: when a second "Add a Codex account" replaces a login, CLIProxyAPI
   still holds the first one as pending for its five minutes, and would take its callback.
3. An answer was already handed on for this login: refused, `already answered` while
   CLIProxyAPI is still being asked or after its 200; after its 404 or 409, that same result,
   `expired` or `refused`, again, since CLIProxyAPI will not finish that login and "already
   answered" would tell the user to wait for it. CLIProxyAPI accepts a second callback for a
   state whose code it is still exchanging, so the core keeps the once rule itself.
4. Otherwise the core marks the login answered and posts `{code, state, error}` to
   `/v0/management/oauth-callback`. CLIProxyAPI's 200 is `accepted`; 404 is `expired`; 409 is
   `refused`; any other status, no answer, or any other failure of the post, is
   `unreachable`, and the login is marked unanswered again so the same address can be sent once more (a 400 or a 500 from
   CLIProxyAPI re-arms a resend that will fail the same way; that costs nothing). The core reads only the status:
   CLIProxyAPI's body is never passed on (below).

**Where the CLIProxyAPI facts come from.** Every CLIProxyAPI line this section cites is in
`github.com/router-for-me/CLIProxyAPI` at tag `v8.0.23`, commit `d318bcc3afb9`, the release
`app/cliproxy.lock` pins: the Codex login registers its session
(`internal/api/handlers/management/auth_files_provider_oauth.go:231`), waits five minutes for
the callback file (`:257`), and stays pending, so a second callback is accepted, until it
completes (`:353`) or fails; `IsPending` is `oauth_sessions.go:210`, and
`WriteOAuthCallbackFileForPendingSession` (`:479`) calls `IsOAuthSessionPending` at `:484`; a
replaced login's session stays pending for that wait.

**What CLIProxyAPI's text can hold.** In the pinned 8.0.23, the callback handler's 409 bodies
are three fixed strings and the login's session status
(`internal/api/handlers/management/oauth_callback.go:95,114,126,129`). The session status is
set by `SetOAuthSessionError`, and the Codex flow sets it once with the cause of a failed code
exchange (`auth_files_provider_oauth.go:296`, `oauthSessionErrorWithCause`), whose text for a
refused exchange is the provider's response body verbatim
(`internal/auth/codex/openai_auth.go:139`, "token exchange failed with status %d: %s"). That body
is the provider's, unbounded, and may repeat what the request carried, so nothing shows it
cannot hold the code. The paste route and the listener's page therefore answer fixed sentences
and never CLIProxyAPI's text. `GET /api/accounts/oauth/<state>` passes the same session status
as its `error` today (`get-auth-status`), so it is held to the same rule: its `error` is
CLIProxyAPI's text only when it is one of the fixed strings the pinned release sets
(`Timeout waiting for OAuth callback`, `Bad request`, `Bad Request`, `State code error`,
`Failed to exchange authorization code for tokens`, `Failed to save authentication tokens`,
`Authentication failed`), held as one allow-list in `oauth.py`; any other text, the exchange's
cause included, is answered as `Failed to exchange authorization code for tokens; CLIProxyAPI's
log has the detail` when it begins with that sentence; `get-auth-status`'s own `unknown or
expired state` is answered as `CLIProxyAPI no longer holds this login: it expired, or it
finished more than a minute ago; check Accounts, else start again`, because CLIProxyAPI logs
nothing for it and keeps a finished login only one minute
(`internal/api/handlers/management/oauth_sessions.go:19,167`), so the state may be a login
that succeeded; any other text is `the login failed; CLIProxyAPI's log has the detail`.
CLIProxyAPI's own log keeps the detail, as it does today (`docs/app/first-run.md` names that
log).

The code is held only for the length of that call: not in the pending login, not in a log,
not in an answer.

### 4.3 The listener

`GET /auth/callback` on `127.0.0.1:1455`, as today, with the query read as one value per name:
a request without `state`, or with neither `code` nor `error`, or with both, or with a name
given twice, or on another path, is a 404 "Not found." and nothing is handed on. Otherwise `hand_on`, and the page:

| result | status | page |
| --- | --- | --- |
| accepted | 200 | You can close this tab. |
| expired | 409 | This login expired; start again. |
| another login | 409 | This page belongs to another login; start again from lokki. |
| already answered | 409 | lokki already has this login's answer; you can close this tab. |
| refused | 409 | CLIProxyAPI did not take this login; start again. |
| unreachable | 502 | CLIProxyAPI did not take this login; start again. |

A reload after `expired` or `refused` shows that page again (4.2, step 3): "already answered"
is only for a login CLIProxyAPI took or is still being asked about.

### 4.4 The paste route

`POST /api/accounts/oauth/<state>` — an operator route beside `GET /api/accounts/oauth/<state>`,
in `GUARDED`, behind the guard, the token gate and the operator check, in that order. The path
names the login the page started; the body is `{"url": "<the pasted address>"}`, at most
16 KiB, read after the operator check and never captured (`capture_dir` and `debug_log` do not
see it).

The address must be, after trimming spaces: scheme `http`, host `localhost` or `127.0.0.1`,
port 1455, path `/auth/callback`, a query with exactly one `state` and either one `code` or
one `error`, never both (the provider sends one; an address with both is refused rather than
guessed). That is the address CLIProxyAPI's link sends the browser to, and nothing else.

| case | status | body |
| --- | --- | --- |
| handed on | 200 | `{"handed_on": true}` |
| the path's state is malformed | 400 | `the state must match [A-Za-z0-9_-]{8,128}` |
| the body is not an object with a string `url` | 400 | `the body must be {"url": "<the address the browser ended on>"}` |
| the address is not the callback address above | 400 | `this is not the address the login ended on: it starts http://localhost:1455/auth/callback?` |
| the body is over 16 KiB | 413 | the core's usual sentence |
| no login pending, its 300 s passed, or the path names a login a newer one replaced | 409 | `this login is no longer waiting; start again` |
| the address's `state` is not the path's | 409 | `this address is from another login; paste the address of the login started last` |
| already handed on, by the listener or a paste, and CLIProxyAPI took it or is still being asked | 409 | `lokki already has this login's answer; wait for it to finish` |
| CLIProxyAPI 404, for this answer or one before it | 409 | `this login expired; start again` |
| CLIProxyAPI 409, for this answer or one before it | 409 | `CLIProxyAPI did not take this login; start again` |
| CLIProxyAPI other status, no answer, or the post failed | 502 | `CLIProxyAPI did not take this login` |

Then the page polls `GET /api/accounts/oauth/<state>` as today; on `ok` the listener closes and
the poller wakes, as today. When CLIProxyAPI still answers `wait` for a login this core no longer
takes an answer for (a newer login replaced it, the core restarted, or its 300 s passed), the status
answers `error` with `this login is no longer waiting; start again`, so the page's polling ends at
once. The pending login's own status stays `pending` while it is the pending login and within its
300 s, after an accepted hand-on too; a deadline or a replacement
ends it as that `error`. No response, error sentence or log line carries the address, the
code or the query: every refusal is one of the fixed sentences above, and `_internal_error`
prints the path, which holds only the state.

### 4.5 Accounts' login card

The card is `Accounts.svelte:124-160` (the add button at 77-83); the flow is
`state.svelte.ts:148-209`, which gains `sendCallback(url)` beside `startOauth`.

While a login waits ("pending"):

- "Finish the login in a browser" and "lokki waits up to 5 minutes."
- "lokki opened the login page in this computer's browser." when it opened it (the shell's
  `openExternal`, or `window.open` in a browser).
- "Logging in from another device? Open this link there:" with the link, "Copy login link"
  ("Copied" for 3 s) and the link as text.
- "When the login ends, that browser shows a page that does not load, at an address that
  starts `http://localhost:1455/auth/callback`. Paste that address here:" with one text field
  (`autocomplete="off"`, `spellcheck="false"`, `inputmode="url"`) and "Send".
- "Send" posts the field. While it runs it stays enabled and says `aria-busy`, and a press
  sends nothing. On 200 the field empties and the card says "Sent; finishing the login…"
  while the polling goes on; a refusal shows its sentence in a `role="alert"` under the field,
  which keeps what the user pasted so it can be corrected. An empty field sends nothing and
  says "paste the address first".
- "Cancel", as today.

The field's text lives in the component, never in `app.oauth` or storage, and is cleared on
success, on Cancel and when the login ends. "Account added" and the failure line are as today.
The paste is how a login finishes when the browser is on a different machine from the core, which
is always the case for a Docker core; a browser on the server's own machine can reach
`127.0.0.1:1455`.

### 4.6 Tests

- `hand_on`'s rows through both entries: the listener's page and the route's answer for each
  result, one table.
- A sentinel code through every refusal and through an accepted hand-on: absent from every
  response body, every page and the core's stderr. The rows include a fake CLIProxyAPI that
  answers the callback's 409 with the sentinel in its `error`, and one whose `get-auth-status`
  answers `error` with the sentinel after "Failed to exchange authorization code for tokens: ";
  neither reaches a response, a page or a log.
- Two logins: the first one's callback after the second started is refused at the core and
  never reaches the fake CLIProxyAPI.
- A hand-on CLIProxyAPI did not answer can be sent again; one it answered cannot.
- The route in `GUARDED`; the 413; a client token's 403 before the body is read. Every test of
  the listener and the paste route binds an injected ephemeral callback port, as the existing
  listener tests do, never `:1455`.
- UI: the paste field's send, its busy state, a refusal kept beside the field, the field empty
  after success and after Cancel, the address absent from `app` after each.

### 4.7 What point 2 does not change

`POST /api/accounts/oauth` and its 409 when port 1455 is held, the listener's bind on
`127.0.0.1` only, the 300 s, the page's 2 s polling and its stop on any answer but a 200,
CLIProxyAPI's forwarder left unused, and the provider's login page. `docs/app/limits.md` loses
"the browser must run on the machine the core runs on"; `docs/app/api.md`,
`docs/app/first-run.md` and `ui.md` describe the paste, and `api.md`'s "`error` is
CLIProxyAPI's text" becomes the allow-list rule above.

## 5. Point 3: text tool calls on by default

`text_calls` defaults to `true` in `schema.py` (line 57). A file that says `text_calls: false`, or
`RELAY_TEXT_CALLS=0` in the environment, keeps it off; a file that never named it now has it
on. No file is rewritten. It stays in Settings, writable, with the meaning "the model writes
its exec call as text, and lokki makes the call and cuts there", and the word "experimental"
goes from the schema, `config.py:68-72`, `handler.py:1082`, `textcalls.py:1`, `renamer.py:116`,
`test_textcalls.py`'s docstrings, `docs/relay/relay-8371.md` and `docs/relay/live-8381.md`.

**What a relay with no setting now does to a request it cuts** (the relay path only;
pass-through adds nothing): the first request of a turn carries, as a `developer` message, the
instruction to write a call to `exec` as text between `<<<CALL exec>>>` and `<<<END>>>`; the
relay reads the text as it streams, hands the client the `exec` call it builds, and ends the
request there with the cut `textcall`. `text_calls_pad` (built in: on) keeps the padding tool.
`test_it_is_off_by_default_everywhere` (`test_textcalls.py:580`) becomes the test that a relay
with no setting runs a request this way; the rows of `LOADED`, `FLAGS`, `KNOBS` and `APPLIED`
(`:531-565`) whose expected value is the built-in default turn with it.

**Its limits**, named next to the setting in `relay-8371.md` and in the schema's meaning where
it fits:

- the call is always built as a custom tool call; a client whose `exec` is another kind of
  tool gets a call it cannot match;
- the first line `<<<END>>>` in the call's input ends the call, so an input holding such a line
  is cut short;
- the first answer at high effort comes later (`docs/relay/latency.md`);
- with `pad_tools` off it does nothing.

**The records.** `config_fingerprint` holds `text_calls`, so the fingerprint changes for every
deployment whose resolved value flips: every file and environment that never named it. A
deployment that set it (the live pair sets `true`) keeps its fingerprint. `/api/run`'s
measurement runs carry it too, unless the request turns it off where client tuning is allowed.

Also: `config.example.yaml` regenerated (held by `test_schema.py:68-72`), `ui/src/views/schema.fixture.json` regenerated, the
README's two "(off unless configured)" lines say it is on unless a file turns it off,
`relay-8371.md`'s "No operator has turned it on" goes.

## 6. Point 6: Settings for a user of the app

### 6.1 What shows

Three groups open, in this order, then one fold:

| group (UI title) | rows | why it is visible |
| --- | --- | --- |
| **Mode** (`saving`) | the mode slider, which also sets the threshold; between its ends: Watched accounts, Cut when, Models cut when low (3.7) | the one decision a user makes about cost; Auto's rule is meaningless without it, and hidden without Auto |
| **Connection** (`connection`) | Address (`listen`): the address and port clients use | a user changes it when the port is taken or a client needs another; the endpoint itself stays on Home |
| **Updates and About** (the UI's own) | lokki's version and the update state in one line, "Check now"; the core's revision, CLIProxyAPI's version and whether it is reachable | where a user looks for "which version am I on, and is there a newer one". The line reads `app.update`, the same slot the banner reads (filled by the status poll's `updateStatus()`), with no fetch or copy of its own, and "Check now" is the banner's own action on that slot; the update state keeps one home and is shown twice. In a plain browser `app.update` is `null`: no update line, About only |
| **Advanced** (folded, closed by default) | CLIProxyAPI (port, remote URL, web panel with "Open the panel" and "Copy the management secret", version read-only); Quota reading (Read every); Cutting; Tool calls (`text_calls`, `text_calls_pad`); Traffic; Set in the file (read-only) | each changes behaviour a user of the app should not need to touch; a server operator opens it |

**No Experimental section.** After point 3 nothing in the schema or the docs is called
experimental, and an empty fold is a control that does nothing. The fold returns, folded and
closed by default with its own id in `schema.GROUPS`, when a setting is experimental again.

"Set in the file" stays inside Advanced rather than leaving the form: it is the only place the
UI shows the run log's path, the upstream of a server deployment and the switches only a file
sets, and it is also where a read-only row of a group the UI does not know lands.

### 6.2 Rules kept

- The group of a row is `schema.py`'s `group`; the UI owns only titles and order
  (`GROUP_TITLES` at `fields.ts:7-19`, `group()` at `:27-43`). The ids stay (`saving`, `connection`, `cliproxy`,
  `tuning.*`, `file`, `hidden`); `cliproxy` and `tuning.*` move under Advanced in the UI's
  order, `saving`'s title becomes "Mode".
- A row of a group the UI does not know is never dropped: a writable one shows under Advanced ›
  Other, a read-only one under Advanced › Set in the file. A component test holds every fixture
  row to a known group.
- The writable boundary, the per-group Save with its count, the restart bar above the groups
  (it stays in view when the Advanced fold holds the saved key), the locks ("set by the
  environment", several addresses, the file not writable) and a failed read's "Retry", as
  today.
- The column of links beside the groups (above 1000 px) lists the three groups and Advanced;
  following a link inside Advanced opens it.

**Docs.** `docs/app/ui.md`'s Settings paragraph and the README's "The UI" paragraph name the
groups as above; `first-run.md`'s "Settings › CLIProxyAPI" becomes "Settings › Advanced ›
CLIProxyAPI", and its "Settings → Ports" the Connection group (the CLIProxyAPI port is under
Advanced).

### 6.3 What point 6 does not change

The schema rows' writability, startup flags and meanings (beyond 3.2 and 5), `/api/schema`'s
shape, the restart flow and the hooks.

## 7. Point 1: updates installed on macOS

### 7.1 What the user sees

The banner and its states are those of `docs/app/ui.md` on every OS. On macOS, "Install and
restart" shows when `can_install`; it downloads the `.app.tar.gz` the release already
publishes (`app.yml`'s macOS upload step), verifies its minisign signature against the key in
`tauri.conf.json` (`6086C47021DA70B5`), runs the stop sequence, proves both sidecars are gone,
replaces the bundle and relaunches, as on Linux and Windows. When the bundle cannot be replaced
where it runs, `can_install` is false and the banner offers "Download" with the reason (7.4), as
it does for a `.deb`. A failed install keeps its button and its sentence, the sidecars start
again, and the data directory is never touched.

The first install still needs the Gatekeeper step, `xattr -cr /Applications/lokki.app` (or
Control-click › Open): the `.dmg` a browser downloads is quarantined and unsigned. `-cr` clears
every attribute of every file in the bundle, so no copied file keeps its quarantine; the README
and the hand test both use it.

### 7.2 What the updater does on macOS

`tauri-plugin-updater` 2.13.3 (`=2.13.3` in `Cargo.toml`), `src/updater.rs`:

- The bundle it replaces is the `.app` holding the running executable: the executable's
  directory, two levels up when it is `Contents/MacOS` (`extract_path_from_executable`,
  1697–1724). It does not look for App Translocation.
- `install_inner` (1384–1529) makes its backup and extraction directories on the bundle's
  volume (the system temp directory when it is there, else the bundle's parent; neither is
  `TempDirNotOnSameMountPoint`), extracts the archive itself with `tar` and `flate2` from the
  bytes in memory, then exchanges the two bundles with `renamex_np(…, RENAME_SWAP)`. Where the
  file system cannot swap it falls back to two renames with a restore; a failure it cannot
  restore from is `PreviousAppNotRestored`, with the previous app kept beside the bundle or in
  the home folder.
- **When the swap or the renames fail with `PermissionDenied`, it runs `do shell script …
  with administrator privileges` through AppleScript** (1483–1527): macOS's administrator
  password dialog. Cancelling it is an error, and the previous app stays.
- The whole bundle is replaced as one directory, so the sidecars in `Contents/MacOS` are
  replaced with it. lokki stops them and proves them gone before `install` (`update.rs`
  `Machine::install`, line 198), as on the other platforms.

What macOS does around it (Apple's pages, read 2026-10-15):

- **Quarantine.** `LSFileQuarantineEnabled` "specifies whether files this app creates are
  quarantined by default"; its default is false
  ([Launch Services Keys](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/LaunchServicesKeys.html)).
  lokki does not set it, and the updater writes the new bundle itself, not through a browser
  or Finder.
- **App Translocation.** A newly downloaded app may be run from a randomised path, for example
  `/private/var/folders/…/T/AppTranslocation/<uuid>/d/<name>.app/…`; "the exact circumstances
  … [are] not documented and [have] changed over time"; "there is no supported way to detect
  if your app is being run translocated", and moving the app with Finder ended it in Apple's
  example ([App Translocation Notes](https://developer.apple.com/forums/thread/724969), Apple
  DTS).
- **Signatures on Apple silicon.** "the operating system enforces that any executable must be
  signed before it's allowed to run … a simple ad-hoc signature is sufficient", and the
  toolchains apply one at link time
  ([macOS Big Sur 11.0.1 Universal Apps Release Notes](https://developer.apple.com/documentation/macos-release-notes/macos-big-sur-11_0_1-universal-apps-release-notes)).

### 7.3 The administrator prompt, and how the shell keeps it away

The decision is "a bundle the user cannot write: an error the banner shows", never a password
dialog the app did not announce. The shell keeps the plugin from reaching its privileged path
by not calling `install` on a bundle it cannot replace:

- **The check.** `mac_bundle()` (7.4) reads whether the user can write both the bundle and the
  folder that holds it, with `access(W_OK)` through `libc` (already a direct dependency on
  unix). A directory renamed to another parent needs write on itself as well as on both
  parents, so both are checked. `access` answers for the real user, as the rename will run.
- **When.** At start, so the banner offers "Download" from the first look; again when "Install
  and restart" is pressed, before the download, so a refusal stops nothing; and again after
  the stop sequence has proved the sidecars gone, immediately before `install`. A refusal at
  that last point starts the sidecars again and reports the reason as the install's error.
- **What remains.** Between that last check and the swap the plugin extracts the archive, a
  few seconds. A permission change inside that window, or a refusal `access` did not predict
  (a rename refused for a reason the permission bits do not show), still reaches the plugin's
  prompt. That is a known limit, written in `docs/app/update.md`: the prompt is macOS's own
  dialog, cancelling it leaves the previous app in place, and the banner shows the error.
  Ruling it out entirely means the shell doing the extraction and the swap itself instead of
  calling the plugin's `install`, which would make `tar` (0.4.46) and `flate2` (1.1.10),
  already built today through the plugin, direct pins of the shell. Not done: the check covers
  every cause a user meets (an app in a folder they do not own, a read-only disk). **Reopen
  when** the hand test (7.6) shows a password dialog in a case the check passed.

### 7.4 `macInstall` goes; `mac_bundle` and the reasons

`plugins.lokki.macInstall` goes, with the `plugins.lokki` section of `tauri.conf.json` (line 36)
and its read in `lib.rs:562-578`. `Policy::detect(os, env, mac_bundle)` (`update.rs:57-73`)
decides macOS from the running bundle, the way it decides Linux from `APPIMAGE`, and is the same
pure, tested function; the refusal at `update.rs:212-218` then reads the policy computed at that
moment (7.3), not the one from start.

`MacBundle`, in `lokki_logic` so its decision is tested on Linux, read by the shell on macOS from
`std::env::current_exe()`:

| field | what it holds |
| --- | --- |
| `app` | the `.app` the executable is in (the path ends `<name>.app/Contents/MacOS/<exe>`), or none |
| `translocated` | a component of `app`'s path is `AppTranslocation` |
| `read_only` | `statfs(app)` reports `MNT_RDONLY` (a mounted `.dmg`; a translocated copy) |
| `writable` | `access(W_OK)` succeeds on `app` and on its parent |

The policy, in this order, with the exact reasons:

| when | `can_install` | `reason` |
| --- | --- | --- |
| no `app` | false | `this lokki is not running from lokki.app, so it cannot replace itself; download the new .dmg` |
| `translocated` | false | `macOS is running lokki from a temporary copy, which cannot be updated: quit lokki, move lokki.app into Applications with Finder, open it from there, then install` |
| `read_only` | false | `lokki is running from a read-only disk, such as its disk image: move lokki.app into Applications, open it from there, then install` |
| not `writable` | false | `your macOS user cannot change lokki.app or the folder it is in, and lokki does not ask for an administrator's password; download the new .dmg, or move lokki.app to a folder you can write to` |
| otherwise | true | — |

The translocation test reads a path Apple calls an implementation detail. It is there for a
reason that tells the user what to do; it is not the guard. The guard is `read_only` and
`writable`, which refuse a copy the user cannot replace whatever its path looks like; that a
translocated copy is one of those is the expectation the hand test's step 7 checks.

No signing identity, no Apple Developer ID and no new key: the updater's minisign signature is
the trust, as on the other platforms.

### 7.5 Quarantine and the signature: what the spec expects, and what decides

These are expectations until the hand test runs on a Mac.

- **Quarantine.** The replaced bundle carries no `com.apple.quarantine`: lokki creates its files
  with `LSFileQuarantineEnabled` at its default, and the first install's `xattr -cr` cleared
  the running app's own. So the relaunch meets no Gatekeeper dialog.
- **Signature.** The `.app.tar.gz` and the `.dmg` of a release come from one `tauri build` of
  one bundle (`--bundles app,dmg`), so the replaced bundle's signatures are those of a fresh
  install from the `.dmg`, whatever they are: on Intel, possibly none; on Apple silicon at
  least the linker's ad-hoc signature on each Mach-O (the shell, both sidecars), which is what
  lets a fresh install run there at all.

**The hand test decides.** The pull request that turns the macOS install on carries the hand
test's result, and merges only when it passes on a Mac with an Intel CPU. If either expectation is wrong
(an attribute on the replaced bundle, a Gatekeeper dialog at the relaunch, a signature that
differs from the fresh install's or fails `codesign -v`), the shell does not strip attributes
or re-sign: that pull request does not merge, macOS keeps offering the download, and this
section returns to the spec with what the Mac showed. The arm64 checks below decide the same
way for Apple silicon; until they pass on an Apple silicon Mac, the README says the install in
place is proven on Intel only.

### 7.6 The hand test

`app/scripts/mac-update-handtest.sh`, with its steps in `docs/app/update.md` › "Testing an
install on macOS". It runs on a Mac with an Intel CPU, and its arm64 checks on an Apple silicon
Mac; it needs the tree, Rust, Node and the Python of the sidecar build, as `app.yml`'s macOS
job does.

**The test build.** A release build (no `debug_assertions`, the code users run) with an overlay
`--config` the script writes into its scratch directory and never into the tree:
`plugins.updater.pubkey` set to a throwaway key's public half, `plugins.updater.endpoints` set
to `http://127.0.0.1:<port>/latest.json` on a port the script picks, and
`plugins.updater.dangerousInsecureTransportProtocol: true`, which the plugin needs for a
non-`https` endpoint in a release build (`config.rs:198-220`). A local `https` endpoint is not
used: it would need a certificate the Mac trusts, an act on the Mac's trust store the test
should not take. The overlay goes after `sidecars.py --config-json`'s on the command line, and
the throwaway private key signs through `TAURI_SIGNING_PRIVATE_KEY` in the script's own
environment. The overlay carries the endpoint rather than `LOKKI_UPDATE_ENDPOINT` because the
test launches lokki as a user does, from Finder, and an app Finder opens does not inherit the
shell's environment. A build-time overlay is a different build, not a runtime override:
`update.md`'s "a release build accepts only `https` endpoints" and "the public key cannot be
overridden" stay true of every build, and `update.md` says so beside the hand test. `test_release_workflow.py` gains the boundary: `tauri.conf.json`, `sidecars.py
--config-json --release`'s output and `app.yml` never name
`dangerousInsecureTransportProtocol`, and the release job's pubkey is the configured one.

**Steps.** Each states what passes; the script prints `PASS` or `FAIL` for each check it makes
and stops at the first `FAIL`.

0. Quit any lokki; move `~/Library/Application Support/lokki` aside if it exists; no `lokki.app`
   in `/Applications`. *Pass:* the script finds no lokki process, no data directory, no bundle.
1. `mac-update-handtest.sh build`: makes a scratch directory outside the checkout, a throwaway
   key pair (`tauri signer generate`), the overlay, and builds version `A` (`.dmg`) and version
   `B` (`.app.tar.gz` and `.sig`), `A` < `B`; writes `latest.json` naming `B` for this Mac's
   platform. *Pass:* both builds exist, `B`'s signature verifies with the throwaway public key.
2. `mac-update-handtest.sh serve`: serves the scratch directory on `127.0.0.1:<port>` and
   prints its pid. *Pass:* `latest.json` answers 200 there.
3. By hand: open `A`'s `.dmg`, drag lokki to Applications, run `xattr -cr
   /Applications/lokki.app`, open it, let first run finish, then "Check now" (or wait the minute before the first check).
   *Pass:* Home shows; the banner says "lokki B is available" with "Install and restart".
4. `mac-update-handtest.sh before`, once lokki has been quit and opened again after its first
   run, so what it records has been through a restart: records the bundle's `codesign -dv`
   and `xattr -lr`, the sidecars' `codesign -dv`, a checksum list of the data directory's
   `config.yaml`, `secrets/` and `cliproxy/auth/`, and a checksum of `cliproxy/config.yaml`'s
   parsed document, whose bytes every start rewrites. *Pass:* recorded.
5. By hand: "Install and restart". *Pass:* no password dialog, no Gatekeeper dialog; lokki
   relaunches; the banner names lokki B.
6. `mac-update-handtest.sh after`. *Pass:* the running app's version is `B`; both running
   sidecars' executables are inside `/Applications/lokki.app` and their checksums are those in
   `B`'s archive; `xattr -lr /Applications/lokki.app` shows no `com.apple.quarantine`;
   `codesign -dv` on the bundle and on both sidecars reports what step 4 recorded for the fresh
   install of the same build kind (the identifier and `Signature=` lines); the data checksums
   are unchanged, `cliproxy/config.yaml` compared as its parsed document; `logs/app.log` shows the drain, the stop, the proof and the install, and no
   error.
7. The refusals, by hand, each from a copy of `A` the script makes on request
   (`mac-update-handtest.sh copy dmg|readonly|translocate`):
   - opened from the mounted `.dmg`. *Pass:* "Download" and the read-only reason;
   - in a folder the script makes and `chmod 555`s. *Pass:* "Download" and the not-writable
     reason; pressing nothing else, no password dialog appears;
   - in `~/Downloads` with a quarantine attribute the script writes, opened from Finder (and
     approved in System Settings when macOS asks). *Pass:* "Download" and the translocation
     reason, or, when macOS does not translocate it, the step says so and is not a failure.
8. `mac-update-handtest.sh clean`: stops the server by its pid, removes the scratch directory,
   the throwaway key, both builds and every copy, and `/Applications/lokki.app`. By hand: remove
   the test's data directory, then move yours back. *Pass:* nothing of the test is left.

**What an Apple silicon Mac must check that Intel cannot.** Apple silicon refuses to run an
executable without a valid signature; Intel runs it. So on arm64, in steps 4 and 6, `codesign
-v` passes on the shell and on both sidecars, before and after the replace; step 5's relaunch
and the sidecars' start prove the kernel ran them; and the core sidecar, a PyInstaller one-file
build that unpacks its libraries at start, answers `/api/status` after the update. None of this
can fail on Intel, so an Intel pass says nothing about it.

### 7.7 Docs

- `docs/app/update.md`: "When it offers a download instead" keeps the Linux case and gives
  macOS the reasons of 7.4; the `macInstall` sentence goes; "What an install does" says the
  macOS package is the `.app.tar.gz`, and names the known limit of 7.3; the hand test's steps.
- `README.md` › Desktop app › macOS (lines 134-148): the first install's step becomes
  `xattr -cr /Applications/lokki.app`, as in 7.1 and the hand test; a sentence says updates
  then install in place when lokki runs from a folder you can write to. `docs/app/update.md:65`
  goes with the macOS bullet it sits in.
  › Updates (lines 174-178): macOS leaves the list of installs that offer Download, and the
  sentence on a re-quarantined `.dmg` goes.
- `docs/app/first-run.md`: "Move lokki into Applications and open it from there; an app opened
  from its disk image or from Downloads can run, but cannot update itself."

### 7.8 Tests

- `Policy::detect` on macOS: one row per line of 7.4's table, with the order between them
  (translocated and read-only together gives the translocation reason).
- `MacBundle` from a path: `…/lokki.app/Contents/MacOS/lokki` gives the `.app`; a path outside
  a bundle gives none; an `AppTranslocation` component sets `translocated`.
- `Machine::install` (`update.rs:198`): a policy that turns false between start and the press refuses before the
  download; one that turns false after the stop resumes the sidecars and reports the reason.
- `test_release_workflow.py`: the boundary of 7.6.

### 7.9 What point 1 does not change

The update endpoint, the key, `requireSignedVersion`, the downgrade refusal, the Linux and
Windows install paths, the banner's states, the stop sequence and its proof, the 15-minute
download allowance, and the release workflow's assets.

## 8. Prototypes

`docs/design/ux/` keeps its conventions (sample data only, opens from disk, a frozen copy of the
tokens). These change:

| file | change | states |
| --- | --- | --- |
| `settings.html` | the Mode group as the mode slider with Auto's rows between its ends, the models picker, Updates and About, the Advanced fold | Auto, every model (between); Auto, chosen models with one not listed; Auto, the model list could not be read; Direct (0, no rule rows); Relay (100, no rule rows); the slider's write refused; Auto with no management secret set (the reason the quota cannot be read, no watched count); Advanced open; a plain browser (no update line); and the states it had (a refused field, the restarts, read-only, a field set by the environment) |
| `accounts.html` | the login card with the paste field; the screen in Relay; the top bar's choices in the slider's order | Auto, every enabled account; Auto, only the ones I pick; Relay (no scope, no switches); no account yet; login waiting with the paste field; each of the paste route's six refusals; Send with an empty field; sent, finishing the login; login done; login failed; a watched entry not found; a window reset since the last read |
| `home.html` | the top bar's choices and the mode slider; its words and the strip by mode | Auto cutting; Auto passing through; Auto passing with one account unread; Relay (100); Direct (0); the slider writing; a top-bar choice waiting while the slider writes; the slider's write refused; a refused write with the choice that waited behind it in flight; first run (Auto, no account); the quota cannot be read; no management secret set (Auto); update available |
| `tray.html` | the order of the modes and Auto's label | as it had |
| `activity.html` | the top bar's choices in the slider's order, Auto's label | as it had |
| `system.html` | the mode control's samples in the slider's order, Auto's label and sentence | as it had |
| `logo.html` | the tray sample's order, Auto's label and its reason | as it had |

The tray's reason line is the core's new strings, which `tray.html`'s sample does not redraw.
The states of the removed Low Quota Mode switch leave `home.html`, `accounts.html` and
`settings.html`.

## 9. Out of scope

- Which account served a request (2).
- Per-account routing; CLIProxyAPI picks the account.
- A device-code login; the pinned CLIProxyAPI has none.
- Cancelling a login inside CLIProxyAPI: "Cancel" stops the page's polling, and CLIProxyAPI
  drops the login after its five minutes, as today.
- Code signing and notarization on any OS.
