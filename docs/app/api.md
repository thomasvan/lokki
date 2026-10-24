# Core API

The core serves the routes below. Operator routes require an operator token in
`X-Relay-Token` or `Authorization: Bearer …`; relay client routes accept a
configured client token. `/healthz` and the UI are public. The API's design and
the writable settings boundary are described in the [app design](../superpowers/specs/2026-10-10-lokki-app-design.md).

| Method and route | Role | Request | Response | Statuses |
| --- | --- | --- | --- | --- |
| `GET /`, `GET /static/*` | Public | — | UI asset; `/` serves `index.html` | 200; 404 when absent, outside the UI directory, or the directory itself (`/static`, `/static/`) |
| `GET /healthz` | Public | — | `{status, uptime_s, routing}`; status is `ok` or `draining`; `routing` is `{relay_on, reason}`, the decision as a request would see it now | 200 |
| `GET /version` | Operator | — | `{revision, config_fingerprint, clients, operators, uptime_s}` | 200; 401 missing/invalid token; 403 client token or rejected Host/Origin |
| `GET /v1/models` | Client or operator | — | Upstream model-list response | 200; 401; 502 upstream unavailable |
| `GET /api/status` | Operator | — | Revision, core version, start/uptime, `routing`, `low_quota` `{threshold_percent, rule, accounts, models, poll_seconds}`, `decision` `{relay_on, reason, computed_at}` (decided now; `computed_at` is UTC), `poll` `{last_ok_at, last_error, reads, running}`, run counts, CLIProxyAPI reachability/version, unknown settings | 200; 401; 403 |
| `GET /api/endpoint` | Operator | Optional `?reveal=1` | Client URLs and masked default key; `reveal=1` adds the key | 200; 401; 403 |
| `GET /api/schema` | Operator | — | Rows for settings with type, bounds, choices, default, writable/startup flags and meaning | 200; 401; 403 |
| `GET /api/settings` | Operator | — | Loaded settings, masked secrets, environment overrides, writability and unknown keys | 200; 401; 403 |
| `PUT /api/settings` | Operator | Non-empty JSON object of writable settings | `{applied, restart_required, comments_dropped}` | 200; 400 invalid body/key/value; 401; 403; 409 settings cannot load/be written or another source wins; 413 body over 8 MiB; 500 write/apply failure |
| `GET /api/accounts` | Operator | — | `{read_at, age_s, accounts, decision, poll}`; see [Accounts](#accounts) | 200; 401; 403 |
| `GET /api/models` | Operator | — | `{models}`, the ids CLIProxyAPI lists, sorted, once each; see [Models](#models) | 200; 401; 403; 502 CLIProxyAPI did not answer, answered other than 200, or listed no `data` |
| `GET /api/runs` | Operator | — | Run records | 200; 401; 403 |
| `GET /api/activity` | Operator | Optional `?window=24h` or `7d` (default `24h`) | What the run records hold over the window; see [Activity](#activity) | 200; 400 any other or empty window; 401; 403 |
| `GET /api/chain` | Operator | — | Evidence-chain verification result | 200; 401; 403 |
| `POST /api/reload` | Operator | — | `{ok, error}` | 200; 401; 403 |
| `POST /api/accounts/refresh` | Operator | — | The `GET /api/accounts` body after one read | 200; 401; 403 |
| `POST /api/accounts/oauth` | Operator | Empty, or `{}` | `{state, url}` of a new Codex login; see [Adding an account](#adding-an-account) | 200; 400 any other body; 401; 403; 409 management secret unreadable, or the callback port held; 502 CLIProxyAPI refused or did not answer |
| `GET /api/accounts/oauth/<state>` | Operator | — | `{status, error}`; `status` is `pending`, `ok` or `error` | 200; 400 malformed state; 401; 403; 409 management secret unreadable; 502 CLIProxyAPI refused (401 and 403 included) or did not answer |
| `POST /api/accounts/oauth/<state>` | Operator | `{"url": "<the address the browser ended on>"}`, at most 16 KiB | `{"handed_on": true}`; see [Adding an account](#adding-an-account) | 200; 400 malformed state, any other body, or not the callback address; 401; 403; 409 the login is not waiting or answered, the address is another login's, or CLIProxyAPI did not take it; 413 body over 16 KiB; 502 CLIProxyAPI answered otherwise or not at all |
| `POST /api/shutdown` | Operator | — | `{stopping, drain_seconds}` | 202; 401; 403 |
| `POST /api/run` | Client or operator | JSON with a `prompt` string or Responses-style `input`; optional tools and supported request parameters | Streamed relay response | 200; 400 invalid/unsupported request; 401; 403 rejected Host/Origin; 413 body over 8 MiB; 503 while draining; upstream error status |
| `POST /v1/responses` | Client or operator | Responses API JSON (`input`, optional tools and supported parameters) | Streamed Responses API response (the relay's, or CLIProxyAPI's own on [pass-through](#pass-through)) | 200; 400 invalid/unsupported request; 401; 413 body over 8 MiB; 502 CLIProxyAPI did not answer or refused the core's credential (pass-through); 503 while draining; upstream error status |
| `POST /v1/images/generations`, `POST /v1/images/edits` | Client or operator | Image API request | Upstream image response | Upstream response status; 400 empty body; 401; 413 oversized body; 503 while draining |

`decision.reason` is one of these exact strings, produced by the routing policy:

- `routing: relay` — the mode is Relay: every request is cut.
- `routing: direct` — the mode is Direct: every request passes through.
- `the quota cannot be read` — Auto, no snapshot, and the last read failed (`poll.last_error`).
- `the quota is not read yet` — Auto, no snapshot and no failed read yet.
- `no account to watch` — Auto, no watched account in the snapshot: none enabled, or none of the named ones.
- `no watched account could be read` — Auto, every watched account is `unknown`.
- `every watched account is low` — Auto, rule `all`, every watched account is low: cut.
- `a watched account could not be read` — Auto, no watched account read decides it and one or more is `unknown`: under `all`, every one read is low; under `any`, none read is low.
- `not every watched account is low` — Auto, rule `all`, a watched account was read and is not low.
- `a watched account is low` — Auto, rule `any`, a watched account is low: cut.
- `no watched account is low` — Auto, rule `any`, every watched account was read and none is low.
- `this model is not cut when low` — Auto only (Relay cuts and Direct passes every request, whatever `low_quota.models` holds): the quota is low, and `low_quota.models` names models but not the request's. Only a request carries it: `/healthz`, `/api/status` and `/api/accounts` show the quota's own reason, which is what a request for a named model gets.
- `the quota rule failed` — Auto, and the rule raised; the log says the exception's type.

Under Auto a quota that cannot be read counts as not low, so every reason but the two
that cut passes the request through at full price. A snapshot decides whenever there is
one; the last read's error decides only without one. A run record keeps the reason it was
written with, so Activity can show reasons older than this list.

`GET /api/schema` returns `minimum`, `maximum` and `choices` as separate row
fields and, for the UI, `label`, `group` (one of `schema.GROUPS`) and
`choice_labels` (choice → label). The `PUT` allow-list is `routing`,
`low_quota.threshold_percent`, `low_quota.rule`, `low_quota.accounts`,
`low_quota.models`, `low_quota.poll_seconds`, `cliproxy.url`, `cliproxy.port`, `cliproxy.panel`, `listen` on
`127.0.0.1` or `localhost` (the core's listener is IPv4, so an IPv6 address is a 400), and the relay settings `seg_budget`, `total_cap`, `max_segments`,
`tail_chars`, `merge_text`, `session_pin`, `text_calls`, `text_calls_pad`,
`imagegen_cut`, `payload_cap`, `pacing_seconds`, `max_concurrent_upstream`,
`retries` and `drain_seconds`.
`cliproxy.version`, `reject_previous_response_id`, credentials and all other
non-writable schema fields cannot be set. A bad key or value is 400; a body naming
`low_quota.enabled`, which the core no longer reads, is the 400 "low_quota.enabled is
not a setting". A write migrates the file as a load does first, so it leaves that key
out and writes the `routing` it chose. The
`cliproxy.url` value must be `http(s)://host[:port]`; set either it or
`cliproxy.port`, not both. A 409 also results when `CLIPROXY_URL`, the legacy
`upstream` file key, or an existing `cliproxy.url` for a port update determines
the upstream. Settings controlled by environment variables, a missing or
unwritable settings file, a file that cannot load (with the loader's error), or
a file with no usable client token are also refused with 409. A `listen` or
`cliproxy.port` in the body is checked as the next start would use it: a port
outside 1 to 65535, 1455 (the Codex login's callback), or the core and CLIProxyAPI
on one port is 400; a port something accepts connections on at `127.0.0.1`, or one
the core cannot bind on that address as its listener does (`SO_REUSEADDR` on POSIX,
not on Windows), is 409 with the system's reason. A port the same service listens
on now is not probed. Each refusal names its key and writes nothing. Reload holds a
port-derived upstream at its running value and logs that hold.

The Host/Origin guard checks every `/api/*` route and `/version`: Host must be a
loopback name or a host the core is bound to, with nothing after it but an
optional numeric port, and a supplied Origin must be `http://` and that Host. `/healthz` and `/v1/*` are outside this guard.

The status and settings views remove `user:password@` credentials from the
upstream URL. Settings values marked secret are masked.

A query string and a trailing slash are ignored. A `GET` of a path the table
does not list for `GET` answers 401 without a valid token, and 403 for an
`/api/*` path whose Host or Origin the guard rejects; with a valid token it is
404. A `POST` or `PUT` of a path the table does not list for that method is 404.
Other methods (`HEAD`, `OPTIONS`, `DELETE`, `PATCH`) answer 501 with an HTML body
on every path. An unexpected failure on any route is a 500 that names no detail;
the detail goes to the operator's log.

## Models

`GET /api/models` passes the Host/Origin guard, the token gate and the operator
check, in that order. It reads CLIProxyAPI's `GET /v1/models` with the core's key in
`Authorization: Bearer`, through the reader's opener (no system proxy), each read waiting
at most 10 s (the body is not capped), and answers `{"models": [id, …]}`: the string `data[].id`s, sorted, each once.
It is not `/v1/models`, which any client token may read and whose 502 carries an
exception's text: the Settings picker needs the list under the guard and the operator
check, and a failure it can show whole. A failure is a 502 `{error}` with one of three
fixed sentences, never an exception's text: `CLIProxyAPI did not answer` (no answer, or
none in time), `CLIProxyAPI answered <status>` (any status but 200) and
`CLIProxyAPI's model list has no data` (a body that is not JSON, not an object, or has no
list `data`). The ids are what `low_quota.models` may name: `for_model` compares a
request's `model` with them, and a model at a thinking level counts as its model: CLIProxyAPI reads
`gpt-x(high)` as `gpt-x`, so the request is named when it, or its name with the one suffix
removed (the text before the last `(`, when it ends with `)`), is in the list; `gpt-x(high` is not.

## Accounts

`GET /api/accounts` and `POST /api/accounts/refresh` pass the Host/Origin guard
and the token gate in that order, and need an operator token. The refresh asks
the poller for one read now, joins a read already in flight, waits for it at
most 15 s, and answers with the latest snapshot; with no poller running it answers
at once with what the poller holds. The poller reads in every mode.

`read_at` is the snapshot's time in epoch seconds and `age_s` its age; both are
`null`, and `accounts` is empty, before the first read. Each account carries
`auth_index`, `label`, `email_masked` (the reader's masked form; the core holds
no other), `enabled`, `used_percent`, `remaining_percent` (`null` when the read
gave no number), `reset_at`, `state`, `error` and `in_scope`. `state` is `low`,
`ok` or `unknown` (an error, a missing number, a window other than 5 h, or a
snapshot older than three poll intervals), the account's own whether Auto
watches it or not; `in_scope` says whether Auto watches it. A disabled account
that is not named in `low_quota.accounts` is not read and carries
`error: "disabled"`. `decision` is `{relay_on, reason}`, computed
at the request with the clock from the same snapshot the rows come from. `poll`
is the object `GET /api/status` carries; when a refresh's read failed, the
snapshot is the previous one and `poll.last_error` says why. After a read
CLIProxyAPI refused with 401 or 403, the poller reads again only on a reload or
a refresh, and `poll.last_error` ends "polling paused until a reload or a
refresh": about ten failed management logins from one address earn a
thirty-minute ban that refuses the right secret too.

## Activity

`GET /api/activity` passes the Host/Origin guard, the token gate and the
operator check, in that order. A `window` other than `24h` or `7d`, an empty
one, or `window` given twice is a 400; other query names are ignored. Every
time in the answer is integer epoch seconds:

```
{
  window, now, from,                       // from = now − 24 h or now − 7 d
  coverage: {oldest, records, undated, skipped, complete},
  buckets: [{start, relay, passthrough, images, errors}],
  totals: {requests, relay, passthrough, images, errors},
  relay: {finalized, cut, client_gone, error, by_cut: {<cut>: n}},
  passthrough: {by_reason: {<reason>: n}, by_cut: {<cut>: n}},
  tokens: {runs, input, output, cached, reasoning},
  models: [{model, runs}], clients: [{client, runs}],
  latency: {relay, passthrough, images, all},           // each {p50, p90}
  recent: [{ts, route, model, client, mode, cut, verdict, outcome, status, elapsed_s,
            delivered_chars, segs, input_tokens, output_tokens, reason, error}],
  events: [{at, relay_on, reason}]
}
```

**Window and buckets.** A record is in the window when `from <= ts <= now`.
The 24 h window has 24 buckets of 1 h, the 7 d window 28 of 6 h; bucket `k`
covers `[from + k·step, from + (k + 1)·step)`, and a record at exactly `now`
falls in the last one.

**Outcomes.** Each record counts under one `outcome`, and its `recent` row
carries that word. A relay record (or a record without `route`) is, in this
order: `finalized` when `verdict` is `billed`, or `real_completed` is set and
there is no `verdict`; `error` when `cut` is `error` or `error` is set;
`client_gone` when `cut` is `client_gone`; otherwise `cut`. `relay.by_cut`
counts the `cut` class by its `cut`. A pass-through record is `error` when `cut`
is `error` or `error` is set, else `client_gone` when `cut` is `client_gone`,
else `passed`; `passthrough.by_reason` and `passthrough.by_cut` count every
pass-through record. An image record is `error` when `error` is set or `status`
is 400 or more, else `delivered`. A missing or `null` `cut`, `reason`, `model`
or `client` is counted as `unknown`. `totals.errors` and each bucket's `errors`
count the `error` outcomes inside their routes: `totals.requests` is `relay +
passthrough + images`. A record whose `route` is none of the three counts in
`coverage.records` and nowhere else.

**Tokens and latency.** `tokens.runs` counts the records that carry `usage`;
`input` and `output` sum its top-level counts; `cached` sums
`input_tokens_details.cached_tokens`, or the top-level `cached_tokens` without
that object, and `reasoning` likewise from `output_tokens_details`. A cut run
carries no usage. `p50` and `p90` are nearest-rank percentiles of `elapsed_s`:
the value at 1-based rank ⌈p·n/100⌉ of the sorted values, `null` when there is
none; `all` is over every route.

**Top lists.** `models` and `clients` are the eight most counted, ties by
name, then `{"model": "other", runs}` (or `client`) for the rest when there are
more.

**Coverage and the read.** The core copies the run log from its end, then its
rotated files newest first, under the lock appends take, until the copy's
oldest whole line is older than `from` or the copy holds 8 MiB
(`RUNS_MAX_BYTES`). It parses after releasing the lock. The first line of a
copy that starts inside a file is dropped and counted nowhere. `records`
counts the records in the window; `undated` the JSON objects whose `ts` is not
the run log's own `YYYY-MM-DDTHH:MM:SSZ`; `skipped` the lines that are not a
JSON object; `oldest` is the oldest dated record read, `null` for an empty
log. `complete` is `false` when the copy stopped at 8 MiB and no record read is
older than `from`: the window's older runs are not counted. The answer, all but
`events`, is cached for 10 s per window and live log size.

**Recent.** The 50 newest records in the window, newest first, with only the
fields above. `status` is the record's `status`; for a relay record it is
`upstream_error.status` when the provider refused, else `upstream_status`.
`input_tokens` and `output_tokens` come from `usage`. `error` is cut to 200
characters.

**Events.** The core's in-memory list of decision changes since it started,
newest first, at most 100. `at` is the first evaluation that saw the change,
whichever route made it, so the oldest entry is the first decision the core
made.

## Adding an account

CLIProxyAPI runs the provider's browser login; the core drives it and holds no
credential. `POST /api/accounts/oauth` reads the management secret file, binds
the callback listener on `127.0.0.1:1455`, then asks CLIProxyAPI for the login
URL, and answers `{state, url}`; the caller opens `url` in a browser. A second
call replaces the pending login and its listener. The pending login waits five
minutes from the bind, CLIProxyAPI's own wait for the callback, and the listener
closes then.

The provider sends the browser to `http://localhost:1455/auth/callback?…`, with
the login's `state` and a `code` or an `error`. On the machine the core runs on
the listener takes it; a browser elsewhere shows a page that does not load, and
the user pastes the address it ended on into
`POST /api/accounts/oauth/<state>`. Both hand the answer on the same way: to
CLIProxyAPI, once per login, and only when the `state` is the pending login's,
since CLIProxyAPI still takes the callback of a login a newer one replaced, and
takes a second callback while it exchanges the first. An answer CLIProxyAPI
did not answer with a 200, 404 or 409 can be sent again. After its 404 or 409
CLIProxyAPI will not finish the login, so a second answer gets that row's
sentence again; "already has this login's answer" is for a login CLIProxyAPI
took, or is still being asked about.

In a pasted address, whitespace at its ends is trimmed, and tabs and line
breaks anywhere in it are dropped, which mends a paste that wrapped. The
address is the callback's only when it is then `http`, host `localhost` or `127.0.0.1`, port 1455 and path
`/auth/callback`, with no user part and no fragment, and its query names each
name once, a non-empty `state`, and exactly one of a non-empty `code` and a
non-empty `error`; other names are ignored and never handed on. The listener
answers anything else with a 404 "Not found." and the paste route with its
400. What each answers:

| case | listener | paste route |
| --- | --- | --- |
| handed on | 200 "You can close this tab." | 200 `{"handed_on": true}` |
| the path's state is malformed | — | 400 `the state must match [A-Za-z0-9_-]{8,128}` |
| the body is not an object with a string `url` | — | 400 `the body must be {"url": "<the address the browser ended on>"}` |
| not the callback address | 404 "Not found." | 400 `this is not the address the login ended on: it starts http://localhost:1455/auth/callback?` |
| no login pending, or its five minutes passed | 409 "This login expired; start again." | 409 `this login is no longer waiting; start again` |
| the state is not the pending login's | 409 "This page belongs to another login; start again from Lokki." | 409 `this login is no longer waiting; start again` |
| the address's state is not the path's | — | 409 `this address is from another login; paste the address of the login started last` |
| already handed on, by either, and CLIProxyAPI took it or is still being asked | 409 "Lokki already has this login's answer; you can close this tab." | 409 `Lokki already has this login's answer; wait for it to finish` |
| CLIProxyAPI 404, for this answer or one before it | 409 "This login expired; start again." | 409 `this login expired; start again` |
| CLIProxyAPI 409, for this answer or one before it | 409 "CLIProxyAPI did not take this login; start again." | 409 `CLIProxyAPI did not take this login; start again` |
| CLIProxyAPI another status, no answer, or the post failed | 502 "CLIProxyAPI did not take this login; start again." | 502 `CLIProxyAPI did not take this login` |

The paste route reads its body after the operator check, so a client token is
a 403 whatever the body, and never captures it. No answer, page or log line
carries the address, the code or CLIProxyAPI's text: each is one of the fixed
sentences above.

`GET /api/accounts/oauth/<state>` asks CLIProxyAPI how the login stands.
`<state>` is 8 to 128 characters of `A-Z`, `a-z`, `0-9`, `_` and `-`. `error`
is `null`, or CLIProxyAPI's text when it is one of the fixed sentences the
pinned release sets for a login (`Timeout waiting for OAuth callback`,
`Bad request`, `Bad Request`, `State code error`, `Failed to exchange
authorization code for tokens`, `Failed to save authentication tokens`,
`Authentication failed`); a failed exchange whose cause CLIProxyAPI appends is
`Failed to exchange authorization code for tokens; CLIProxyAPI's log has the
detail`, since that cause is the provider's body, which may repeat the code.
CLIProxyAPI's `unknown or expired state` is `CLIProxyAPI no longer holds this
login: it expired, or it finished more than a minute ago; check Accounts, else
start again`: it logs nothing for it, and keeps a finished login only a minute.
Any other text is `the login failed; CLIProxyAPI's log has the detail`.
When CLIProxyAPI answers `wait` for a login this core no longer takes an answer
for (a newer login replaced it, the core restarted, or its 300 s passed), the
status is `error` with `this login is no longer waiting; start again`, the paste
route's own sentence. The pending login's own `wait` stays `pending` while it is
the pending login and within its 300 s, after an accepted hand-on too; a
deadline or a replacement ends it as `error` with that same sentence.
On `ok` and `error` the login's listener closes; on `ok` the poller is woken, so the account shows at its next read. A caller stops polling on any answer but a 200: a 502 can
be CLIProxyAPI refusing the secret, and polling on walks into its ban.

## Pass-through

When the decision is `relay_on: false`, `POST /v1/responses` goes to CLIProxyAPI
untouched. `POST /api/run` always runs the relay's engine.

**Guards.** On both paths, in this order: the token gate (401), the drain (503
while stopping), the body (a size cap, 413; valid JSON that is an object, 400),
and the `model` id rule (400). `pacing_seconds` and the
`max_concurrent_upstream` slot also apply on both paths, since both share the
CLIProxyAPI pool. Only the relay path refuses `previous_response_id` and
`store: true`, validates `input`, `prompt` and `tools`, and applies the relay's
defaults; pass-through hands those fields to CLIProxyAPI as sent.

**Defaults.** A field the client leaves out (`stream`, `model`) takes
CLIProxyAPI's default on pass-through and the relay's on the relay path. The
pass-through record says `stream` and `model` as sent, `None` when absent.
`"model": ""` is a 400 on pass-through; the relay path turns it into its default
model.

**Request.** The client's bytes are forwarded unchanged unless the body carries
a relay tuning knob (`mode`, `abort_after`, `seg_budget`, `total_cap`,
`max_segments`, `payload_cap`, `imagegen_cut`, `merge_text`, `text_calls`,
`text_calls_pad`, `pad_tools`, `retries`); then the knobs are removed, the rest
is re-serialized, and the record says `reserialized: true`. Nothing is added.
`Authorization` is always the core's CLIProxyAPI key, never the client's;
`Content-Type` is the client's (else `application/json`); of the rest, only
`openai-beta`, `originator`, `session_id` and `chatgpt-account-id` are
forwarded, the same list the relay path forwards. Hop-by-hop headers and
`Accept-Encoding` are not.

**Retry.** Before the first byte, a 429, 502 or 503, or a failure to connect, is
retried within the `retries` budget with the relay's backoff (`Retry-After`
honoured up to 8 s). Once a byte has reached the client nothing is retried, and
a stream that breaks is recorded `cut: error`. A budget spent on 429/502/503
passes that last answer through. CLIProxyAPI not answering at all is a 502
from the core, with a `[passthrough]` line in its log.

**Response.** CLIProxyAPI's status, body and `Content-Type`, `Retry-After` and
`x-request-id` headers; no other header, and the connection closes after it.
A stream is relayed byte for byte, and a scanner on the copy looks only for
the terminal event and its `usage`. A 401, 403 or 407 is about the core's own
credential, not the client's request, so it becomes a 502 as on the relay
path. A `stream: false` request is answered by CLIProxyAPI as one JSON object
(`Content-Type: application/json`) with `Transfer-Encoding: chunked`; the core
reads it whole and answers with its own `Content-Length`.

**Record.** One run record per request, written in a `finally`, with
`route: "passthrough"`, `reason` (the decision's), `model`, `stream`, `client`,
`reserialized`, `upstream_input_chars`, `upstream_requests`, `status`, `cut`,
`usage`, `usage_source`, `elapsed_s`, and `error`, `attempts`, `retries` and
`last_status` when they apply, and the fields every record carries. `cut` is
`terminal` (a terminal event arrived, or a whole JSON body), `eof` (a stream
ended without one), `client_gone` or `error` (a 4xx/5xx status, a refused
credential, a broken answer). No content is recorded. A drain that expires
ends the process with `os._exit`, which writes no record on either path.
