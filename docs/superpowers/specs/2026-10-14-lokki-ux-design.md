# lokki: the interface redesign — design

## 1. What changes, and what does not

The UI becomes four screens around one control: the mode (relay, auto, direct), always
visible with the decision it produces. The accounts become gauges, a new Activity screen
shows what the run records hold, and Settings is regrouped by what the user is trying to
do. The tray offers the same mode control. A seagull mark (lokki is Finnish for seagull)
becomes the app icon, the tray icon, the header mark and the favicon, all drawn from one
mark (8).

The core gains one read route (`GET /api/activity`, 4.3), an in-memory list of decision
changes it serves there, and three fields on each `/api/schema` row (`label`, `group`,
`choice_labels`, 5.1). Nothing the core stores, decides or writes to a settings file
changes, so an existing config file needs no migration and there is no compatibility path.

Kept as they are, from the app design (`2026-10-10-lokki-app-design.md`):

- the UI works in a plain browser against a server or Docker core, with no shell hook;
- the seven `window.lokki` hooks equal `ui/src/lib/lokki.d.ts`; this design adds no page
  command (6.2 says why the tray needs none);
- the Settings allow-list, the writable boundary (`writable`, `env_overrides`, and the
  409, whose body is the core's error envelope `{error: {message, type, param, code},
  key}`; the sentence a locked control shows is the UI's own) and the per-service
  restart flow (`app.restart`, `window.lokki.restart`);
- the OAuth flow and its waits (2 s polls, 5 min, stop on any answer but a 200);
- CLIProxyAPI's panel off by default, opened in the system browser;
- the update banner's states and the shell's install switch (`docs/app/ui.md`);
- loopback only; the operator token never in the page; no secret, e-mail or request
  content in a response beyond what the routes already return;
- the stop sequence; every write followed by a read; one home per fact.

## 2. Screens and navigation

Four destinations: **Home**, **Accounts**, **Activity**, **Settings**. The CLIProxyAPI tab
goes: adding an account moves to Accounts, the panel to Settings › CLIProxyAPI, the
versions to Settings › About.

The shell (`docs/design/ux/home.html` and the other screens):

- A left sidebar, 208 px: the mark and the wordmark; the four destinations, each with its
  icon; at its foot "CLIProxyAPI running" or "not reachable" with its dot
  (`/api/status.cliproxy.reachable`) and the lokki version.
- A top bar on every screen: the mode control (3) and the decision line, a state dot and
  a headline from `decision.relay_on` ("Cutting every request" / "Passing requests
  through") with `decision.reason` beneath it, verbatim. That keeps the decision and its
  reason visible wherever the user is. Home shows the control at full width with a
  sentence under each mode; the other screens show it compact, with the sentence as its
  accessible description. The note for Auto with Low Quota Mode off (3.1) sits in the
  top bar too.
- The update banner spans the window above the shell, with its states unchanged. The
  token prompt (browser only) stays a dialog over the page.
- Below 720 px the sidebar folds into a row of tabs under the top bar.

| screen | shows | reads | polls |
| --- | --- | --- | --- |
| Home | in this order: the mode control at full size with a sentence per mode, and the decision; the quota-read error banner when there is one; the endpoint URLs, masked key and copy, where a first-run user sees them without scrolling; a strip of account gauges (4.2) with "Add an account" when there is none; requests since start by route and CLIProxyAPI reachable or not; the last 24 h as bars (4.3) | `/api/status`, `/api/accounts`, `/api/endpoint` (until loaded), `/api/activity?window=24h` | status 10 s (accounts with it, as today); activity 30 s |
| Accounts | one card per account (4.2); watched (in scope) per account; the scope choice; "Read now"; "Add a Codex account" and its flow; Low Quota Mode on or off | `/api/status`, `/api/accounts` | status 10 s, accounts 30 s |
| Activity | 4.3: requests over time, outcomes, models, clients, latency, recent runs, decision changes | `/api/activity` (24 h or 7 d); `/api/status.started_at` for "since lokki started at …" | activity 30 s; status 10 s |
| Settings | 5: the groups, the restart notices, "Watched accounts: 3 of 4" (with Low Quota Mode off, "Watched accounts" with "not read while Low Quota Mode is off" and no count, since `/api/accounts` may hold stale rows, 4.2), About (versions, revision, CLIProxyAPI reachable) | `/api/schema`, `/api/settings`, `/api/status`, `/api/accounts` (on entry, for the watched count) | status 10 s |

Polling keeps today's rules: only while the document is visible, never two of the same
poll in flight.

## 3. The mode control

### 3.1 Semantics

`routing` is the mode. The sentence column is the UI's text, word for word, here and in
the prototypes; this table is its home until the UI PR puts it in the code.

| mode | `routing` | sentence | decision reason |
| --- | --- | --- | --- |
| Relay | `relay` | Every request is cut: lokki ends the answer early so it costs less. | `routing: relay` |
| Auto | `auto` | Low Quota Mode decides: cut when the watched accounts are low, otherwise pass through. | with Low Quota Mode on, one of the seven quota reasons in `docs/app/api.md`; with it off, `low quota mode off` |
| Direct | `direct` | Every request goes to CLIProxyAPI untouched, at full price. | `routing: direct` |

Which accounts count as low, and whether every watched one or any one must be, is
`low_quota.threshold_percent` and `low_quota.rule` (5).

**Auto with Low Quota Mode off cuts every request**, reason `low quota mode off`: with
no quota read there is nothing to decide by, and the core falls back to the saving
path. The top bar then shows the note "Low Quota Mode is off, so Auto cuts every
request" with the action "Turn on Low Quota Mode", the same words wherever the note
appears. A cut is cheaper than a finalized turn, never
free (`docs/relay/cost-of-a-cut.md`); no sentence here says "free" or names a saving.

Choosing a mode is one click and one write, with no Save button:

- Relay: `PUT {routing: "relay"}`; Direct: `PUT {routing: "direct"}`.
- Auto: `PUT {routing: "auto", low_quota: {enabled: true}}`, one body, so choosing Auto
  turns on what Auto needs. A core whose loader refuses it (no readable management
  secret file on a server deployment) answers 400; nothing is written and the control
  shows the error beside it, the mode unchanged.
- The control follows the read back (`GET /api/status`), never its own click. While the
  write is in flight the choice is busy, not changed.
- `settings.writable: false` disables the control with the UI's sentence "the settings
  file is not writable here", as the forms are today.

`low_quota.enabled` stays the switch for reading the quota, shown on Accounts and in
Settings › Saving. Turning it off under Auto is allowed and lands in the state above.

### 3.2 Why the core's pair stays

The pair has one redundant state: `auto` with `low_quota.enabled: false` decides exactly
what `relay` decides. It is also the state `init` writes, so every installed app has it,
and the Docker deployment's file is hand-written and mounted read-only. Folding
`low_quota.enabled` into `routing` would change what that state decides (to quota-driven,
which passes requests through at full price whenever an account is not low) unless every
existing file were migrated, and the read-only one cannot be. That migration is a
compatibility path with no removal condition the project controls. The core already
expresses "relay always"; what the UI adds is a first-class control for it and a plain
sentence for the redundant state, with no change to what the core stores or decides.

## 4. What each view reads

### 4.1 Existing routes

| view element | source |
| --- | --- |
| mode, decision, reason | `GET /api/status` `routing`, `decision` |
| requests since start by route | `/api/status.runs_since_start` |
| quota-read failure banner | `/api/status.poll.last_error`, with the full-price sentence on `every account in scope is unknown` (as today) |
| endpoint, masked key, copy | `GET /api/endpoint`, `?reveal=1` on copy only |
| account cards | `GET /api/accounts`, `POST /api/accounts/refresh` |
| threshold line on the gauges, rule | `/api/status.low_quota` |
| add an account | `POST /api/accounts/oauth`, `GET /api/accounts/oauth/<state>` |
| settings | `GET /api/schema`, `GET`/`PUT /api/settings` |
| versions | `/api/status` `version`, `revision`, `cliproxy.version`, `cliproxy.reachable`; the UI's `VERSION` |

`GET /api/runs` is not read by the UI: it returns the last 50 records whole (hashes and
fingerprints included) and serves monitors; Activity needs aggregates over a window.

### 4.2 Accounts

Each card, from one row of `/api/accounts`:

- the masked e-mail (`email_masked`) as the name; the core holds no other;
- a gauge of `remaining_percent` with a tick at `low_quota.threshold_percent`, so "low"
  is visible as the bar crossing the tick; `null` draws an empty gauge with "no reading";
- the state as a chip: `low`, `ok`, `unknown` with its `error`; an account with
  `in_scope: false` shows "not watched" instead of the `unknown` the API gives it;
- the reset as a countdown and a local time ("resets in 1 h 12 min · 14:32") from
  `reset_at`. Once `reset_at` has passed, `policy.state_of` counts the account's
  remaining quota as 100 until the next read, while `remaining_percent` still holds
  the old reading. The gauge then agrees with the chip the policy gives: it draws the
  bar full, reads "100 %" with "window reset", and the reset line reads "reset; the next
  read shows the new window";
- "Watched" as a switch per card, writing `low_quota.accounts` as today; the scope choice
  ("every enabled account" / "only the ones I pick") above the cards, with today's rules
  (the switches apply only under the second; a listed index with no card gets "Remove");
- `enabled: false` greys the card.

Above the cards: "last read 12 s ago" (`age_s`), "Read now", and Low Quota Mode's switch.
The screen decides from `/api/status.low_quota.enabled`, never from an empty
`/api/accounts`: the poller keeps its last snapshot when Low Quota Mode is turned off
live, so `/api/accounts` can still return the old rows (turning `unknown` once they are
older than three poll intervals) until the core restarts. With Low Quota Mode off the
screen shows no cards whatever `/api/accounts` returns: it says the accounts are not
read and offers the switch. "Add a Codex account" works whatever the mode. The core is
unchanged by this; it is the UI's rule.

### 4.3 New route: `GET /api/activity`

What the run records hold, read in the writers (`engine.py`'s record, `passthrough.serve`,
`images.py`, `core.log_run`). Every record carries `ts` (UTC), `client` (the token's
name, never the token), `revision` and `seq`, and `route` (`relay`, `passthrough`,
`images`) on every record this version writes; an older record without `route` is a
relay record, as `core.log_run` counts it.

- A relay record adds `mode`, `model`, `cut` (the last segment's ending), `cuts`,
  `real_completed`, `verdict` (`billed` when the provider's `response.completed` or
  `response.incomplete` arrived, else `usage-discarded`), `truncated`, `segs`,
  `delivered_chars`, `elapsed_s`, `error`, `upstream_error` (`{status, code, type}`
  when the provider refused), `upstream_status` (the status of the answer it streamed)
  and `usage`. It has no `status`.
- A pass-through record adds `reason`, `model`, `stream`, `status`, `cut` (`terminal`,
  `eof`, `client_gone`, `error`), `error`, `usage` and `elapsed_s`.
- An image record adds `model`, `status`, `images_out`, `error`, `upstream_error`
  and `elapsed_s`.
- `usage` is `summarise_usage`'s whitelist: token counts, at the top level and under
  `input_tokens_details` and `output_tokens_details`. No record holds request or
  response content.

**Contract.** An operator route. `/api/activity` joins `_OPERATOR_GET` in `handler.py`,
the tuple that gates every operator `GET`, behind the Host/Origin guard and the token gate
in that order; a client token gets 403.

`GET /api/activity?window=24h|7d` (default `24h`; any other value is a 400) →

```
{
  window: "24h", now, from,                     // epoch seconds; from = now − 24 h or now − 7 d
  coverage: {oldest, records, undated, skipped, complete},
  buckets: [{start, relay, passthrough, images, errors}],   // 24 × 1 h or 28 × 6 h
  totals: {requests, relay, passthrough, images, errors},
  relay: {finalized, cut, client_gone, error, by_cut: {<cut>: n}},
  passthrough: {by_reason: {<reason>: n}, by_cut: {<cut>: n}},
  tokens: {runs, input, output, cached, reasoning},
  models: [{model, runs}],                      // the 8 most used, then {model: "other", runs}
  clients: [{client, runs}],                    // the 8 most used, then {client: "other", runs}
  latency: {relay, passthrough, images, all},   // each {p50, p90} of elapsed_s, null when no run
  recent: [{ts, route, model, client, mode, cut, verdict, outcome, status, elapsed_s,
            delivered_chars, segs, input_tokens, output_tokens, reason, error}],  // newest first, at most 50
  events: [{at, relay_on, reason}]              // newest first, at most 100
}
```

**Buckets.** Bucket `k` covers `[from + k·step, from + (k + 1)·step)`, `step` 1 h or 6 h,
so the last one ends at `now`. A record falls in the bucket its `ts` falls in. The UI
labels buckets from `start` in local time (every sixth hourly bucket as `HH:MM`, every
fourth 6-hour bucket as `ddd HH:MM`), on Home and on Activity alike.

**Relay outcomes: one partition.** Each relay record counts in exactly one class, tested
in this order:

1. `finalized`: `verdict` is `billed` (`real_completed` for a record without `verdict`).
   The provider finalized the answer and billed it as a direct request.
2. `error`: `cut` is `error`, or `error` is set. This covers the provider's
   `response.failed`, a refusal, a broken stream and an upstream that closed early.
3. `client_gone`: `cut` is `client_gone`.
4. `cut`: every other record. The relay ended the answer before the provider
   finalized it: `marker`, `after-deltas`, `cap`, `toolcall`, `textcall`, `itemdone`,
   `imagegen`, or a continuing ending (`budget`, `eof`, …) on the run's last segment,
   as `stream_segment` names them.

`relay.by_cut` counts the `cut` class only, by its `cut` value. A relay record whose `cut`
is `null` or absent lands in the cut class too, and `by_cut` names it `unknown`, never a
`null` key; the writers set `cut` on every run that streamed, so `unknown` is rare.
`passthrough.by_cut` counts every pass-through record by its `cut`, with the same
`unknown`.

**Errors.** A relay record is an error when it falls in the `error` class. A
pass-through record is an error when `cut` is `error` or `error` is set. An image record
is an error when `error` is set or `status >= 400`: an upstream refusal sets only
`status` and `upstream_error`. `totals.errors` and each bucket's `errors` count errors
inside the route counts, not beside them: `totals.requests` is `relay + passthrough +
images`, and an error is one of those. The chart stacks the three routes and marks
the errors above the bar.

**Tokens.** `tokens.runs` counts the records that carry `usage`. `input` and `output`
sum the top-level counts. `cached` sums `input_tokens_details.cached_tokens`, or the
top-level `cached_tokens` when a record has no details object. `reasoning` sums
`output_tokens_details.reasoning_tokens`, or the top-level `reasoning_tokens`. A cut run
carries no usage, so no token or money figure is shown as saved.

**Latency.** `p50` and `p90` of `elapsed_s` per route, and `all` over every record in
the window. The plan fixes the percentile method and its test.

**Recent.** `outcome` is the class the record counts under, computed by the function that
makes the counts: for a relay record its class in the partition (`finalized`, `cut`,
`client_gone`, `error`); for a pass-through record `error` when it is an error (below),
else `client_gone` when `cut` is `client_gone`, else `passed`; for an image record `error`
when it is an error, else `delivered`. The UI shows it and never derives it. `status` is the record's `status`; for a relay record, `upstream_error.status`
when the provider refused, else `upstream_status`. `input_tokens` and `output_tokens`
come from `usage`. `error` is the same text `/api/runs` already serves, cut to 200
characters to bound the answer's size. The cut is not a redaction: the writers keep
content out of `error`, and the route adds no guarantee of its own.

**Events.** `routing.decide_now`, which already prints one `[routing]` line per change,
also appends `{at, relay_on, reason}` to a bounded in-memory deque (100). `at` is the
first evaluation that saw the change, whichever caller made it (a request, `/healthz`,
`/api/status`), not the moment the inputs changed. The deque starts empty at process
start, so its oldest entry is the first decision the core made, not the start itself;
the screen's "since lokki started at …" reads `/api/status.started_at`.

**Coverage.** `records` counts the records whose `ts` falls inside the window, the ones
every other count is made from; the records the last chunk read before `from` only show
that the window is covered and count nowhere. `undated` counts the JSON objects whose
`ts` does not parse: they fall in no bucket, total or class. `skipped` counts the lines
that did not parse as a JSON object (a torn last line, a damaged line). The first line
of the copied bytes, which the 8 MiB cut usually lands inside, is dropped before
parsing and counts nowhere, so a capped read does not report a skipped line it made
itself. `oldest` is the `ts` of the
oldest record read, `null` for an empty log. `complete` is `false` when the read stopped
at its byte limit before reaching `from`. The screen then says "counted since <oldest>:
lokki reads the last 8 MiB of its run log; older runs in this window are not counted".
When `complete` is `true` and `oldest` is after `from`, the log itself starts inside the
window, and the screen says "since <oldest>: the run log holds runs since then". An
empty log is `{oldest: null, records: 0, undated: 0, skipped: 0, complete: true}`.

**Bounds and the lock.** Under `_runs_lock`, the route copies bytes: the live `runs_log`
and its rotated files, newest first, from the end, until it holds a line older than `from`
or 8 MiB (`RUNS_MAX_BYTES`). The lock keeps a rotation from moving files during the copy,
so a window across a rotation seam reads both sides once. It parses and aggregates after
releasing the lock. `read_runs` parses under the lock, but it reads only the tail it needs
for 50 records; parsing 8 MiB under the lock would hold back every `log_run` (the relay
logs before its final frames reach the client) and `verify_chain`. The aggregate is
cached for 10 s, keyed on (window, live file size). A rotation shrinks the live file, so
its size, and the key, change; a log that grew back to exactly the old size would need
to do so within the 10 s the entry lives. `events` is read fresh on every request and is
never cached. Each answer is fixed in shape: at most 28 buckets, 9 model
and 9 client rows, 50 recent rows and 100 events, a few tens of kilobytes.

**Nothing secret.** The route reads run records and the in-memory deque, nothing else:
not the logs, the settings, the snapshot or CLIProxyAPI. `recent` is an allow-list of
record fields: no `hash`, `prev_hash`, `config_fingerprint`, `resp_id`,
`params_forwarded`, `headers_forwarded` or `knobs`. Run records hold no e-mail; the core
never knows which account CLIProxyAPI picked.

**Tests:**

- a client token gets 403, no token 401, a foreign `Host` 403;
- the window and its 400;
- bucket edges at `from + k·step`;
- the relay partition, with one record per class and the precedence between them;
- the `errors` rule per route, image `status >= 400` included;
- token sums, both `cached` shapes included;
- `p50`, `p90` and `all`;
- the top-8 fold;
- an older record without `route` counts as relay;
- a torn line is skipped and counted, an undated record is counted in `undated` only, and the
  half line at the byte cut counts nowhere;
- a window across a rotation seam;
- the byte cap sets `complete: false`;
- an empty log;
- the allow-list (a record with every field yields only the listed ones);
- `error` cut at 200 characters;
- the deque's bound and order;
- the cache does not outlive a write, and the 24 h and 7 d entries never share a key.

`docs/app/api.md` gains the row and a section.

### 4.4 No log route

`logs/app.log` (the shell), `logs/core.log` (the core's stderr: the `[routing]`,
`[quota]` and `[passthrough]` lines, startup refusals, and an `unhandled <METHOD>
<path>: <exception>` line on an unexpected 500, which carries the request's path and the
exception's text) and
`logs/cliproxy.log` (CLIProxyAPI's own, which names account files, and so e-mails) are
free text that other programs write. A route serving them would need a redactor to keep
e-mails, tokens and request fragments out of text whose shape lokki does not control,
and a redactor that misses one leaks it. What a user wants from a log here, "what did
lokki do and why", is the decision changes and the run outcomes, which Activity shows
from structured sources. The error page keeps showing a log's last lines, as today.

## 5. Settings

### 5.1 Groups, and where their text lives

Settings is grouped by what the user is trying to do. The label, group and choice labels
of each key live in `schema.py`, beside its meaning, and reach the UI through
`/api/schema` (three new row fields: `label`, `group`, `choice_labels`); the UI owns
only the group titles and their order. `meaning` stays the help text, shown under its
field, and is rewritten in plain words where it now uses internal terms (`auth_index`,
"the operator's decision"); `config.example.yaml` is regenerated from it and
`schema.fixture.json` with it.

| group (UI title) | keys (proposed label) | notes |
| --- | --- | --- |
| **Saving** — when lokki cuts | `routing` (Mode); `low_quota.enabled` (Low Quota Mode); `low_quota.threshold_percent` (Low below, %); `low_quota.rule` (Cut when: every / any watched account is low) | the mode is the control of 3, not a select; "Watched accounts" links to Accounts (`low_quota.accounts` is written there) |
| **Connection** — how clients reach lokki | `listen` (Address) | core restart; the endpoint and key stay on Home |
| **CLIProxyAPI** — the account service | `cliproxy.port` (Port); `cliproxy.url` (Use a remote CLIProxyAPI); `cliproxy.panel` (Web panel); `cliproxy.version` (Version, read-only) | port and panel restart CLIProxyAPI; "Open the panel" and "Copy the management secret" sit under the panel switch, as on today's CLIProxyAPI tab |
| **Tuning** (folded) › Cutting | `seg_budget`, `total_cap`, `max_segments`, `tail_chars`, `merge_text`, `payload_cap`, `imagegen_cut` | "change these only with a measurement behind it" |
| **Tuning** › Tool calls | `text_calls`, `text_calls_pad` | |
| **Tuning** › Traffic | `pacing_seconds`, `max_concurrent_upstream`, `retries`, `drain_seconds`, `session_pin` | |
| **Tuning** › Quota reading | `low_quota.poll_seconds` | |
| **Set in the file** (folded, read-only) | `cli_proxy_api_key` (`***`), `upstream`, `cliproxy.management_secret_file`, `runs_log`, `debug_log`, `capture_dir`, `reject_previous_response_id`, `allow_public_bind`, `allow_client_tuning` | `auth.*` stays hidden, as today |
| **About** | versions, revision, CLIProxyAPI reachable | from `/api/status` |

A key whose `group` the UI does not know is never dropped. A writable one stays editable
under Tuning, the fold that takes today's unmatched writable keys (`fields.ts`'s
Advanced); a read-only one is shown under "Set in the file". A component test holds
every fixture row to a known group.

### 5.2 Behaviour kept

- These controls write at once, each with one `PUT`, and never draft: the mode control
  (3.1, on every screen and in Settings › Saving), the Low Quota Mode switches (on
  Accounts and in Settings › Saving), "Turn on Low Quota Mode" (the note of 3.1 and the
  Accounts panel), and the Watched switches and the scope choice on Accounts. Every other
  field drafts, and each group saves its drafts on its own with a Save button that counts
  the changed fields.
- Locked fields say why where the field is: "set by the environment", "edit the file to
  change several addresses", and the file's 409 once at the top.
- A `startup_only` key saved puts a notice per service in a bar above the groups
  ("Restart CLIProxyAPI to apply" with its button when the shell has `restart`);
  unchanged in meaning from today.
- A refused save keeps the draft and shows the error and its key beside the group.

## 6. The tray

### 6.1 Menu

```
Cutting every request          (disabled: the decision headline)
  low quota mode off           (disabled: the reason)
────────────
  Relay
✓ Auto
  Direct
────────────
Open lokki
Quit lokki
```

The three modes are check items, one checked, following `/api/status.routing`; choosing
one writes the body 3.1 gives. Pause and Resume, and the "value before the pause" the
shell remembers (`paused_from`), go: Direct is the pause, chosen like any mode. The
refresh reads `/api/status` every 10 s (today 30 s, for the label only) and at once after
a choice. A failed write is one line in `logs/app.log`, as today, and the menu shows the
mode the core reports.

The tray icon is the mark (8) in two forms, so the state reads without opening the menu:
**solid** (`logo-template-solid.svg`) while the decision cuts, and the **line**
(`logo-template-line.svg`, the same wings drawn open, with no fill) while it passes
through. On macOS both are template images. An outline of the solid wings blurs to grey
at 16 px, so the open form is a 1.5 px line, which stays crisp at 16 px on a light or dark
bar. `docs/app/first-run.md` changes with the menu.

### 6.2 No new page command

The tray is the shell's own UI: it reaches the core over HTTP with the operator token the
shell already holds, as Pause does today. The page gains no capability; `build.rs`
`COMMANDS`, `capability.rs`, `inject.js` and `lokki.d.ts` do not change.

## 7. Visual system

The values live in `docs/design/ux/tokens.css`, and `docs/design/ux/system.html` shows
them with every component in its states. The UI PR moves `tokens.css` into
`ui/src/app.css`, which is then their home, and deletes the copy here.

- **Colour.** Sea and sky, quiet: a deep blue-grey (`--accent`) for structure, white and
  soft greys for surfaces, a night sea for dark. Each state has three tokens:
  `--<state>` for marks (dots, bars, icons; at least 3:1), `--<state>-ink` for text in
  that state (at least 4.5:1, on `--bg` or on its tint) and `--<state>-soft` for the tint
  behind chips, banners and rows. The states are **cutting** (sea-teal), **passing**
  (amber, the full-price state, a mark colour and never text), **unknown** (grey) and
  **error** (red); `--images` is the third chart series. An account that is **low** is
  drawn in the cutting colour, because low accounts are what make Auto cut; **ok** is
  neutral; **not watched** is an outline. A state never relies on colour alone: each
  carries its word or its icon.
- **Contrast**, computed from the token values: text 14.4:1 (light) and 15.6:1 (dark) on
  `--bg`; muted text 6.0:1 and 8.4:1; state inks 6.0:1 to 6.6:1 on their tints in light,
  7.7:1 to 9.2:1 in dark; marks 3.3:1 (passing) to 5.1:1 (error) in light; control
  outlines (`--border-strong`) 3.3:1 (light) and 3.4:1 (dark) on `--surface`; `tokens.css`
  gives each token's ratios beside it. Plain `--border`
  is decorative and never the only edge of a control.
- **Colour syntax.** The tokens are written in `oklch()`, which WebKit (Safari 15.4 and
  later) and Chromium (111 and later, so WebView2) read; the UI PR checks the WebKitGTK of
  the Linux build before it relies on it.
- **Type.** The system stacks only: `system-ui, -apple-system, "Segoe UI", Roboto,
  sans-serif` for sentences, `ui-monospace, "SF Mono", Menlo, Consolas, monospace` for
  reasons, keys, endpoints and numbers, with tabular figures. Scale 11 / 12 / 13 / 15 /
  18 / 24 / 32 px (`--fs-xs` … `--fs-2xl`); body is 13 px; weights 400, 500, 600.
- **Space and shape.** A 4 px base (`--sp-1` … `--sp-8`, 4 to 40 px); controls 32 px tall;
  cards pad 16 px with a 10 px radius; pills for the mode control, chips and gauges; two
  shadows, one for a raised segment and one for popovers. The window is 1100 × 760.
- **Light and dark** follow `prefers-color-scheme`, as today; the UI has no theme switch
  of its own (the prototypes' toggle is for viewing them).
- **Icons.** An in-house set on a 20 px grid, 1.5 px stroke, round caps, `currentColor`:
  home, accounts, activity, settings, copy, refresh, external, info, check, warning,
  error, chevron, plus, close, restart, lock, and a busy spinner. Their drawings are in
  `system.html`.
- **Charts**, all inline SVG: requests over time as stacked bars (relay `--cutting`,
  pass-through `--passing`, images `--images`, errors `--error`) whose legend carries the
  counts; the quota gauge, a thin pill of remaining quota with a tick at the threshold;
  horizontal bars for breakdowns; latency as a solid bar to p50 inside an outline to p90.
  An empty chart keeps its axes and says "No requests yet".
- **Focus and motion.** A 2 px `--focus` ring with a 2 px offset on every control; the
  only motion is the busy spinner.
- **No new dependency.** Charts and icons are inline SVG and the type is the system
  stack; each is a few dozen lines a library would replace with tens of kilobytes, and
  the core serves the UI offline.

## 8. The logo

The mark is a seagull in flight reduced to two filled crescent wings that meet in a short
point of body, drawn for lokki on a 64-unit grid. The files, in `docs/design/ux/`:

| file | what it is |
| --- | --- |
| `logo.svg` | the mark alone: one filled path, `currentColor`, no text, filter, gradient or image; the header mark in `--accent` |
| `app-icon.svg` | the app icon: the mark in white over a sky field with a sea band, on a continuous-corner square of the macOS grid (an 80-unit body on a 100-unit canvas) |
| `logo-template-solid.svg` | the tray while cutting: the 16 px drawing of the mark, black on transparent |
| `logo-template-line.svg` | the tray while passing through: the same wings as a 1.5 px open line, black on transparent |

`logo.html` shows the mark at 512, 128, 32 and 16 px on light and dark, its construction, the app icon at 512, 128, 32 and
16 px, the wordmark at 64, 28 and 18 px, both tray forms at 16 and 32 px on light and dark bars, and the favicon.

The wordmark is "Lokki", a proper noun: capital L, the rest lower case. It is drawn, not set in a
font, so it looks the same on every platform and needs no font. The letters are centrelines stroked at
13 units on a 100-unit em (ascender and capital at y 0, x-height at 22, baseline at 72), with round caps
and joins, in `currentColor`. The dot of the i is filled in `--accent` and follows the mark's rule: the
gull at 64 px and up, the gull's 16 px drawing from 28 px, and a plain dot below, where a gull of 2 px
does not read. `size` is the em a text wordmark would be set at; the drawing is 0.8 em tall. The sidebar
sets it at 18 px beside the 26 px mark, where it reads above the destinations' 13 px and does not crowd the
mark; the plain dot is what it draws there. Where the drawing lives:

| file | what it is |
| --- | --- |
| `ui/src/components/Wordmark.svelte` | the wordmark's home: the paths and the size rule; the app draws it from here |
| `app/src-tauri/dist/error.html` | the error page, which is not built from the UI, carries a copy of the 32 px drawing; `Wordmark.test.ts` fails when it differs |
| `docs/design/ux/wordmark-light.svg`, `wordmark-dark.svg` | the gull-dot drawing for the README, one file for each GitHub theme, in the ink and accent of that theme as sRGB; `Wordmark.test.ts` holds them to the same paths |
| `docs/design/ux/wordmark.html` | the three directions the wordmark was chosen from; direction B is the one drawn, and the page stays as the record |

Wherever a person reads the name it is "Lokki"; identifiers, paths, `productName`, the bundle identifier,
the `LOKKI_*` variables, `window.lokki` and the log lines keep `lokki`. The macOS menu and the Dock take
`productName` and say "lokki" until a rename PR (`docs/app/limits.md`).

At 32 px and under, the app icon and the favicon use the 16 px drawing of the mark, so
each wing keeps a full pixel; at 16 px the favicon drops the sea band. The icon PR
generates `icon.icns`, `icon.ico` and the PNG set from `app-icon.svg` (with that small
drawing for 16 and 32 px), the favicon the same way, and the tray images from the two
template files. Windows and Linux have no template images; that PR draws the two forms
white or black to suit the tray's theme where the platform reports it, and white where
it does not.

## 9. Prototypes

The prototypes in `docs/design/ux/` are pictures of the design, not code the UI uses. Each
opens from disk in a browser and loads nothing from the network: inline CSS, inline SVG
and a little inline script. A toolbar above the 1100 × 760 window switches light and dark
and picks the state to show; the toolbar's links move between the files. Every value in
them is sample data (`o***@example.invalid`, `http://127.0.0.1:8381/v1`, the key `…3f9a`).
Each file carries a frozen copy of the tokens it uses and of the mark, inlined so it opens
alone; the sources are `tokens.css` and `logo.svg` (with the tray files beside it) until
the UI PR moves them into the UI, and a copy in a prototype is a picture of them, never
their home.

| file | shows | states |
| --- | --- | --- |
| `home.html` | Home (2, 3, 4.1) | Auto cutting; Auto passing through; Relay; Direct; Auto with Low Quota Mode off; first run (no account, Low Quota Mode off); a quota-read error; update available |
| `accounts.html` | Accounts (4.2) and the login | every enabled account; only the ones I pick; Low Quota Mode off; no account yet; login waiting ("Finish the login in your browser", the link, Copy login link, Cancel); login done; login done with Low Quota Mode off; login failed; a watched entry not found; a window reset since the last read (4.2) |
| `activity.html` | Activity (4.3) | 24 h; 7 d; no runs yet; history that starts inside the window; the read limit reached (`coverage.complete: false`) |
| `settings.html` | Settings (5) | normal; unsaved changes with a refused field; restart needed for CLIProxyAPI, for both, restarting, and a failed restart; read-only (409); a field set by the environment; Low Quota Mode off |
| `tray.html` | the tray menu (6) on a macOS menu bar and a Windows 11 tray, light and dark | Auto cutting (solid icon); Direct passing through (line icon); Relay |
| `logo.html` | the logo (8) | cutting and passing through for the live tray |
| `system.html` | the visual system (7): palette, type, spacing, icons, every component in its states | light and dark side by side |

Where a prototype and this spec differ, the spec holds.

### Sources of the patterns

These patterns appear in ProxyPal (at `7b9331e1b6c145397014407c33cfb220b2a2b7a5`), EasyCLI
(at `8c121b72aed16473c074caed57ec6e98fa7df4ab`) and Mobbin's screen library; no code, asset
or text of theirs is used.

- The three-way mode control with an explanation under Auto: Zoho CRM's Day / Night /
  Auto control (<https://mobbin.com/screens/d7b0fb43-7d42-4a87-adea-4038b3a30988>), and
  a status dot with one sentence of consequence, DoorDash Merchant's store status
  (<https://mobbin.com/screens/e732789a-508d-4abd-9a43-717b91f9812c>).
- A quota meter with its reset on the same card and an "as of" line: Postman's resource
  usage (<https://mobbin.com/screens/36c098cb-d4a0-4511-8b97-c71cb378ace2>); quota as a
  page of account cards, EasyCLI's Quota page
  ([QuotaPage.tsx](https://github.com/router-for-me/EasyCLIProxyAPI/blob/8c121b72aed16473c074caed57ec6e98fa7df4ab/src/pages/QuotaPage.tsx)).
- A broken account shown inside its own card: Rocket Money's connection warning
  (<https://mobbin.com/screens/0cb74dc8-c8d0-48ed-9874-f3a7020d59a6>).
- Requests over time with the range control beside it and the legend carrying the
  counts: Zapier's monitoring chart
  (<https://mobbin.com/screens/b1d9d3ac-9830-4b81-83b4-f18a90dfdb55>).
- Settings as cards with the label and its help at left, the control at right, and a
  save per card: Cal.com (<https://mobbin.com/screens/154699b5-6eec-43c4-8e24-1c4f66bb8ffe>).
- The tray's grey state line above radio items: Portrait's menu-bar menu
  (<https://mobbin.com/sites/sections/e282fbec-ab18-4c01-a8b0-f3ff2693ba4c>) and Podia's
  three-way status menu (<https://mobbin.com/screens/469ef97c-ed6f-463a-b2df-f3777866bb3e>).
- Not in lokki: a log viewer (ProxyPal's
  [LogViewer.tsx](https://github.com/heyhuynhgiabuu/proxypal/blob/7b9331e1b6c145397014407c33cfb220b2a2b7a5/src/pages/LogViewer.tsx); see 4.4) and estimated cost or savings
  figures, which both apps show and lokki cannot measure.

The waiting panel for a browser login in `accounts.html` has no counterpart in these
sources; it is lokki's own.

## 10. Out of scope

- Any change to what the core stores or decides, and the folding of `low_quota.enabled`
  into `routing` (3.2).
- A log view or log route (4.4).
- A money or token figure presented as saved.
- Removing, disabling or renaming accounts (CLIProxyAPI's panel does it); providers other
  than Codex; per-account routing (CLIProxyAPI picks the account).
- History beyond the last 8 MiB of the run log (4.3), and any store of statistics.
- OS notifications, localisation, a theme switch inside the UI, a mobile layout beyond
  a narrow window.
- Changes to the OAuth flow, the update flow and banner states, the panel policy, the
  stop sequence or the data directory.
- The app icon, tray icons, favicon and code themselves: implementation PRs planned from
  this spec (core: schema fields, `/api/activity`, the decision deque; UI: visual system
  and navigation, Home and the mode control, Accounts, Activity, Settings; shell: the
  tray and the icon set).
