# lokki: the desktop app — design

## 1. What is being built, and the decisions it rests on

`lokki` is a desktop application that installs and runs two things on the user's
own machine: CLIProxyAPI (the `router-for-me/CLIProxyAPI` binary, which holds the
user's Codex OAuth accounts) and the relay in this repository (which ends a
request early so it costs less). The user points any client at one local port
and one key, and the core decides, per request, whether the relay cuts or the
request goes through to CLIProxyAPI untouched.

The owner's decisions:

| decision | choice |
| --- | --- |
| who installs it | end users, on their own macOS, Windows or Linux machine |
| platforms and signing | all three OS from the first release, unsigned at first; signing is bought later |
| Low Quota Mode threshold | the *remaining* share of the 5h window; relay on only when every account in scope is under it |
| endpoint | one port, one key; the core decides cut or pass-through per request |
| the CLIProxyAPI UI | the app's own views show accounts and quota and add a Codex account (OAuth driven by the core, 4.7); CLIProxyAPI's panel is off by default and, when `cliproxy.panel` turns it on, opens in the system browser for everything else (its settings) |
| client configuration | the app edits no client config; it shows the endpoint URL on the configured port and a masked key with copy |
| stack | Tauri thin shell + Python core (the relay as it is, extended) + CLIProxyAPI as a sidecar binary |
| name | `lokki` (repository `thomasvan/lokki`); the relay keeps its codename `midstream-abort` in its documents |

Not in scope: editing `~/.codex`, serving more than one machine, providers other
than Codex in the app's own views (CLIProxyAPI's panel still has them), code
signing, a mobile app.

## 2. Architecture

```
+------------------------------ lokki (Tauri app) -------------------------------+
|  window + tray + updater + spawn/supervise two sidecars         (Rust, thin)   |
|  web UI (TypeScript): Overview | Accounts | Settings | CLIProxyAPI             |
+--------------------+----------------------------------------+------------------+
                     |  HTTP 127.0.0.1 (operator token)        |
          +----------v-----------+                   +---------v----------+
          |  lokki-core (Python) |   upstream        |  CLIProxyAPI (Go)  |
          |  = the relay         | ----------------> |  release binary    |
          |  + control API       |   quota reads     |  its own panel     |
          |  + Low Quota Mode    | <---------------- |  management API    |
          |  + static UI         |                   +--------------------+
          +----------+-----------+
                     ^  one port, one client key
               Codex CLI, or any client that speaks the Responses API
```

Principles:

- **Every decision lives in `lokki-core`** and is testable offline: routing, quota
  reading, settings, the drain. The Tauri shell has no business logic. The UI only
  calls the control API.
- **`lokki-core` alone is a product.** `python -m lokki_core --config FILE` runs
  on a server or in Docker exactly as the relay does today. The desktop app is a
  packaging of it.
- **One endpoint.** The client never changes its configuration when the routing
  changes. Pass-through is a path inside the core, not a second port.
- **One home per fact** (the philosophy `WORKSPACE_PROTOCOL.md` names). The core's settings file holds every
  setting the app exposes, CLIProxyAPI's port included; the core's `init`
  subcommand (3.8) generates CLIProxyAPI's file from it, and the shell only
  runs that subcommand. The UI reads labels, ranges and defaults from the
  core (`GET /api/schema`), never from its own copy.
- **Secrets stay on the machine.** Four values are generated at first run and
  live under the app's data directory, mode 0600 where the OS has modes:
  the client key (shown masked on the UI), the operator token (the UI's own),
  the key CLIProxyAPI accepts from the core (`cli_proxy_api_key`, the existing
  setting), and CLIProxyAPI's management secret (kept in plaintext by lokki in
  its own file, because CLIProxyAPI hashes the copy in its config at start).
  Nothing in the repository holds one.

Five sub-projects, each with its own plan and its own PRs, in this order:

1. **core-platform** (section 3): the package move, the Windows and macOS port
   of the core, the drain and reload routes, the settings extension with its
   write path, the read-only control routes, static serving.
2. **core-routing** (section 4): Low Quota Mode: reader, poller, policy,
   pass-through, `/api/accounts`.
3. **ui** (section 5).
4. **app** (section 6).
5. **update** (section 7).

The relay's measured behaviour (the cuts, the padding, `text_calls`, the image
routes) does not change in any of them.

## 3. Sub-project 1: core-platform

### 3.1 Package layout

`midstream-abort/` becomes `core/`, a Python package `lokki_core` with a
`pyproject.toml`. The runtime dependency is `pyyaml`, as today; nothing is added.
Tests stay beside the code and keep running with `python -m unittest discover`.

```
core/                        the old midstream-abort/ directory, renamed
  pyproject.toml
  server.py                  the re-export shim the tests and the Dockerfile use (kept)
  test_*.py                  the suite, beside the code as today
  config.example.yaml        generated from the schema (3.5)
  VERSION                    the product version, the one home (section 8)
  lokki_core/                the old relay/ package, renamed; flat
    __init__.py  __main__.py python -m lokki_core --config FILE | init | sync-cliproxy | shell-config
    config.py                load_settings / apply_settings / reload_settings, extended with the new keys
    settings_api.py          the PUT path: merge, validate, write, apply (new)
    schema.py                the machine-readable settings schema: key, type, range, default, meaning (new)
    lock.py                  the run-log lock: fcntl on POSIX, msvcrt on Windows (new)
    init.py                  `init`, `sync-cliproxy` and `shell-config` (3.8, new)
    control.py               /api/status, /api/endpoint, /api/schema (this sub-project); /api/accounts (sub-project 2) (new)
    handler.py               routes: /v1/*, /api/*, /, /static/*
    core.py                  run log, chain (lock calls moved to lock.py)
    engine.py renamer.py images.py sse.py stream_view.py trace.py textcalls.py   unchanged
    quota/ passthrough.py    sub-project 2
    static/                  the built UI (sub-project 3 fills it; empty here)
  billing_probe.py           the measurement tool, unchanged
```

The first PR is the move alone (`git mv` of the directory and of the package,
`from relay` becomes `from lokki_core` everywhere, the suite green), so later
diffs are readable. That PR also changes everything that names the old
path: `deploy/relay/Dockerfile` (WORKDIR, COPY, label, ENTRYPOINT),
`deploy/docker-compose.relay-8381.yml` (the healthcheck path), `test_revision.py`
(it pins the Dockerfile's COPY lines), `docs/relay/docker.md`,
`relay-up.sh`, `restart-relay.sh`, and `WORKSPACE_PROTOCOL.md` (its validation
commands), and every `midstream-abort/` path in `README.md` and `docs/` (about
forty, most in `relay-8371.md`, `docker.md` and `live-8381.md`) becomes `core/`;
the codename stays in prose and in the evidence docs, as the protocol's local law
requires; the two cron-line comments in `relay-up.sh` that describe the earlier
pair's checkout, the `session_pin` namespace string in `core.py` (renaming it
would change every client's pinned id) and the SSE event names `relay.error` and
`relay.summary` (the wire protocol) are not paths and do not change. The committed evidence files (`runs.jsonl`, `billing_*.jsonl`,
`e2e_evidence.jsonl`) move with the directory. The untracked, git-ignored
`midstream-abort/config.yaml` symlink on the owner's host is not moved by git:
the move PR adds a line to `docs/relay/live-8381.md` telling the operator to
recreate it at `core/config.yaml` for the native-relay rollback; the running
container mounts the real file and is unaffected.

### 3.2 The core on Windows and macOS

The core is POSIX-only today. Each item below is a work item of this
sub-project, and CI runs the suite on the three OS before the sub-project closes
(Linux on every push and PR on the workstation's runner; macOS and Windows on
`workflow_dispatch`, dispatched once at the close):

- `core.py` imports `fcntl` at module top and uses `flock` for the run-log lock.
  `lock.py` provides `acquire(path)` / `release(handle)`: `fcntl.flock` on
  POSIX, `msvcrt.locking` on Windows. The single-writer refusal (`RunLogBusy`)
  keeps its meaning on both.
- `os.rename` onto an existing name (run-log and trace rotation) becomes
  `os.replace`.
- File modes (`0o600`, `0o700`) are set where `os.chmod` means something and
  skipped on Windows; the tests that assert a mode are skipped there.
- Signals: `SIGTERM`/`SIGINT` start the drain where they exist; `SIGHUP` reloads
  where it exists. On Windows neither is installed. The same two actions are
  routes (3.4), so the shell never depends on a signal. `test_lifecycle.py`'s
  signal tests are skipped on Windows and the same claims are tested through
  the routes on every OS.
- `core.py`'s repository guard (`inside_repo`: evidence must not be written
  under the checkout) has a root only when `REPO_ROOT/.git` exists, as a
  directory (a checkout) or a file (a git worktree); otherwise there is no root
  and the guard is off, unless `RELAY_REPO_ROOT` names a root (the image's test
  stage builds from `git archive` and sets it, so the suite's guard tests run
  there too). A PyInstaller bundle and the runtime image have neither, so the
  guard is inert in both, and the app always names `runs_log` explicitly (3.8). `test_revision.py`'s image-layout tests and the Dockerfile
  comment that describe the guard firing in the image change with this rule. `revision()` reads the
  `REVISION` file the build writes beside the package, as the Docker image does.

### 3.3 Settings: one file

`config.yaml` stays the file; the keys the relay has today keep their names and
meaning. New keys, with their ranges and defaults given once, in `schema.py`,
which also produces this listing for the docs:

```yaml
listen: ["127.0.0.1:8381"]          # existing; a list; the app writes one loopback address
auth:
  tokens:
    - {name: app, token: "...", operator: true}      # the UI's own
    - {name: default, token: "...", operator: false} # the client key shown on the UI
cli_proxy_api_key: "..."             # existing: what CLIProxyAPI accepts from the core
cliproxy:
  port: 8317                         # the local CLIProxyAPI the app runs (3.8); url is derived: http://127.0.0.1:<port>
  url: ""                            # a remote CLIProxyAPI instead (the new name of `upstream`; the old key still reads); empty = derived
  version: "8.0.23"                  # what `init` pinned (app/cliproxy.lock); empty on a server deployment
  management_secret_file: "/.../cliproxy-mgmt.secret"   # a path, never the secret
  panel: false                       # serve CLIProxyAPI's panel at /management.html on its port; a CLIProxyAPI restart applies it
routing: auto                        # auto | relay | direct
low_quota:                           # sub-project 2
  enabled: false
  threshold_percent: 30              # remaining share of the 5h window
  rule: all                          # all | any
  accounts: []                       # auth_index values; empty = every enabled codex account
  poll_seconds: 60
```

`config.py` loads, validates and applies these as it does the others
(`load_settings`, `apply_settings`, `reload_settings`); `settings_api.py` is only
the write path. Rules, enforced in `config.py` and reported as `ConfigError`
with the key name:

- `routing` and `low_quota.rule` take only the words listed; an unknown value is
  an error, not a fallback.
- `low_quota.enabled: true` needs `cliproxy.management_secret_file` to exist and
  be readable.
- The engine's per-request `mode` knob (`control`, `marker`, `after-deltas`,
  `segmented`) keeps its name and meaning. The global setting is `routing`, and
  the run record gains `route` (3.6); `mode` on the record stays the engine's.
- **Startup-only keys**, reported by `PUT` as `restart_required` and applied at
  the next start: `listen`, `runs_log`, `debug_log`, `allow_public_bind` (the
  code applies these once in `main()`), and `cliproxy.port` and
  `cliproxy.panel` (a CLIProxyAPI restart, which the shell does after
  `sync-cliproxy`, 3.8). Every other key, `capture_dir` included, is
  applied by reload, as today.
- `cliproxy.url` and `cliproxy.port` are one fact: the upstream is `url` when
  set, else `http://127.0.0.1:<port>`. A `PUT` body that sets both is a 400.
- `listen` written through the API accepts loopback hosts only (`127.0.0.1`,
  `localhost`, `::1`). A hand-written file keeps today's rule (any address but a
  wildcard, which needs `allow_public_bind`).

### 3.4 Routes

The handler gains `do_PUT`. Public routes are matched by prefix for `/static/`
and exactly otherwise.

| route | role | returns |
| --- | --- | --- |
| `GET /` , `GET /static/*` | public | the built UI; 404 when `static/` is empty. The UI itself sends the operator token on every API call |
| `GET /api/status` | operator | `{revision, version, started_at, uptime_s, routing, decision: {relay_on, reason, computed_at}, poll: {last_ok_at, last_error, reads, running}, runs_since_start: {relay, passthrough}, cliproxy: {reachable, version}, unknown_keys: [...]}` |
| `GET /api/endpoint` | operator | `{urls: ["http://127.0.0.1:8381/v1", ...], key_masked: "…abcd"}` (the token named `default`, last four characters); `?reveal=1` adds `key` |
| `GET /api/schema` | operator | every key `load_settings` reads: `{key, type, range or choices, default, startup_only, writable, meaning}`; the UI builds its forms from it |
| `GET /api/settings` | operator | the settings as loaded, secrets as `"***"`, plus `env_overrides: [keys a RELAY_* variable overrides]` and `writable: bool` |
| `PUT /api/settings` | operator | body: a partial object of writable keys; see below |
| `POST /api/reload` | operator | what `SIGHUP` does; `{ok, error}` |
| `POST /api/shutdown` | operator | starts the drain (what `SIGTERM` does): refuses new requests, lets streams finish up to `drain` seconds, exits; 202 |
| `GET /api/accounts`, `POST /api/accounts/refresh` | operator | sub-project 2 |
| `POST /api/accounts/oauth`, `GET /api/accounts/oauth/<state>` | operator | adding a Codex account (4.7) |

`/version`, `/healthz`, `/api/runs`, `/api/chain`, `/api/run`, `/v1/*` stay as
they are. `SIGHUP` and `POST /api/reload` both set one `Event`; a single reload
thread reads the file and applies it, so the signal handler does no I/O and the
signal and the route cannot race. `POST /api/shutdown` calls the drain function
that `main()` today keeps as a closure, lifted out so a route can call it.
Every `/api/*` route (`/api/run` included) and `/version` check the request's `Host`: it must be a
loopback name or one of the hosts in `listen` (the owner's live pair listens on
a Tailscale address and keeps working); a request carrying `Origin` must carry
the core's own origin (the UI is served from `/`, 6.4), otherwise 403.

`PUT /api/settings`:

- Writable keys: `routing`, `low_quota.*`, `cliproxy.url`, `cliproxy.port`,
  `cliproxy.panel`, `listen` (loopback only), and the relay knobs `seg_budget`, `total_cap`,
  `max_segments`, `tail_chars`, `merge_text`, `session_pin`, `text_calls`,
  `text_calls_pad`, `imagegen_cut`, `payload_cap`, `pacing_seconds`,
  `max_concurrent_upstream`, `retries`, `drain_seconds`. **Never writable**: `auth.*`,
  `cli_proxy_api_key`, `cliproxy.management_secret_file`, `allow_public_bind`,
  `allow_client_tuning`, `runs_log`, `capture_dir`, `debug_log`. A body naming
  one is a 400.
- One lock around read, merge, validate, write and apply, so two concurrent
  `PUT`s cannot lose an update.
- The merged document is validated by the same validator as load; on failure
  400 `{error, key}` and nothing is written.
- 409 `{error: "settings are read-only here", why}` when the file's real path is
  not writable (the Docker deployment mounts it read-only), or when a `RELAY_*`
  environment variable overrides a key in the body (the write would be reported
  applied while the variable still wins).
- The write goes to the real path (`os.path.realpath`, so a symlinked file keeps
  its link), temp file in the same directory then `os.replace`, mode kept. The
  file is written without comments; the response carries
  `comments_dropped: true` when the previous file had any. The app's generated
  file has none; the meaning of each key is served by `/api/schema`.
- Response: `{applied: [...], restart_required: [...]}`.

### 3.5 Defaults have one home

`schema.py` holds every key's default and range, and `config.py`'s parsing
reads them from there (`load_settings` takes each default and bound from the
schema entry, not from a literal). `config.example.yaml` is generated from the
schema's listing and committed; a test compares the two.

The app's generated config (6.3) names only the keys the app owns: `listen`,
`auth`, `cli_proxy_api_key`, `cliproxy.*`, `routing`, `low_quota`, `runs_log`.
Every relay knob is left to its built-in value, so the running values are the
ones `schema.py` reports as defaults, not the ones `config.example.yaml`
suggests for a server (`pacing_seconds: 0.5`, `max_concurrent_upstream: 6`).

### 3.6 Run records

`images.py` already writes `route: "images"`. Every engine record gains
`route: "relay"` (this sub-project) so that sub-project 2 can write
`route: "passthrough"` without reusing `mode`; the three values are `relay`,
`passthrough`, `images`. `runs_since_start` in `/api/status` is an in-memory
counter per route (all three) since the process started; the
run log is not scanned for it (it rotates at 8 MB and `read_runs` reads a tail).

### 3.7 Tests

- `lock.py` on each OS: a second acquire is refused; release frees it.
- Rotation with `os.replace` where the target exists.
- Skips: mode and signal tests are skipped on Windows by name, and the route
  equivalents (`/api/reload`, `/api/shutdown`) are tested on every OS: a reload
  that adds a token takes effect; a shutdown drains an in-flight stream and
  refuses a new request with 503.
- `GET /` and `/static/*` are public; a client token gets 403 on every `/api/*`
  route above; no token gets 401.
- `PUT`: each writable key round-trips; each never-writable key is a 400; an
  invalid merge writes nothing; a read-only file is a 409; an env override is a
  409 naming the key; two concurrent `PUT`s both land (the lock); a symlinked
  file keeps its link; `restart_required` for each startup-only key;
  `comments_dropped` when the file had comments.
- `/api/schema` lists every key `load_settings` reads, and nothing else: the
  test hands `load_settings` a recording mapping as the file's content and
  compares the keys it asked for with the schema's.
- `config.example.yaml` equals the schema's generated listing.
- The `Host` and `Origin` guard: a foreign `Origin` is 403; a `Host` that is
  neither loopback nor a `listen` host is 403; a `listen` host is accepted.
- `init` (3.8): the data directory layout, four distinct secrets, both files
  parse and name the same ports and key, a busy port moves to the next free
  one, a second `init` on an existing directory changes nothing and says so,
  `sync-cliproxy` writes its three keys and nothing else in CLIProxyAPI's file,
  `shell-config` prints what the shell needs and nothing else.
- Every record carries `route`.
- The existing suite stays green and unchanged in intent.

### 3.8 `init`: first run belongs to the core

`python -m lokki_core init --data-dir DIR [--core-port N] [--cliproxy-port N]
[--cliproxy-version V]` does what the app's first run needs, so the Python suite
tests it and the shell only runs it:

1. Creates `DIR` with `config.yaml` (the core's), `cliproxy/config.yaml`,
   `cliproxy/auth/`, `secrets/` (0600 where modes exist), `logs/`, `runs.jsonl`.
2. Generates the four secrets (section 2) with `secrets.token_hex(32)`.
3. Picks ports: the ones asked for (defaults 8381 and 8317) if free, else the
   next free ones, and prints both.
4. Writes the core's `config.yaml` with only the keys the app owns: `listen`
   (one loopback address), `auth.tokens` (the two), `cli_proxy_api_key`,
   `cliproxy.port`, `cliproxy.version`, `cliproxy.management_secret_file`,
   `cliproxy.panel: false`, `routing: auto`, `low_quota` (disabled),
   `runs_log: DIR/runs.jsonl`. Every relay knob is left to its built-in value.
5. Writes CLIProxyAPI's `config.yaml`: `server.host: 127.0.0.1` (its default is
   every interface), `server.port`, `oauth.auth-dir`, `access.api-keys:
   [cli_proxy_api_key]`, `management.secret-key: <the plaintext>`,
   `management.allow-remote: false`, `management.disable-control-panel: true`
   (the panel downloads its page from GitHub; it is off until `cliproxy.panel`
   says otherwise). CLIProxyAPI hashes the secret in its file at start; lokki
   keeps the plaintext in `secrets/`.
6. Is idempotent: on an existing `DIR` it changes nothing and exits 0 saying so.

`python -m lokki_core sync-cliproxy --data-dir DIR` writes the three keys the
core's settings decide, `server.host`, `server.port` (`cliproxy.port`) and
`management.disable-control-panel` (`not cliproxy.panel`), into CLIProxyAPI's
file as single-key changes of the YAML document, keeping the rest as loaded;
the shell runs it before every CLIProxyAPI start. After `init`, CLIProxyAPI
owns its file (its panel edits it); lokki touches it only through that command.

`python -m lokki_core shell-config --data-dir DIR` prints one JSON line with
the core's port, CLIProxyAPI's port, the operator token, the management secret
file's path and `cliproxy.panel`, read through the loader, so the shell parses
no YAML and the config's shape has one home. It is the one subcommand that
prints a secret, for the shell's pipe; the shell never logs that output.

## 4. Sub-project 2: core-routing (Low Quota Mode)

### 4.1 Quota reading

`quota/reader.py` reads what `billing_probe.live_usage` reads today, through
CLIProxyAPI's management API, which spends no quota:

1. `GET /v8/management/credentials` → the codex entries, each with `auth_index`,
   `email`, `disabled`.
2. For each enabled entry (or each named in `low_quota.accounts`),
   `POST /v8/management/requests/api-call` with `{authIndex, method: GET, url:
   <the provider's usage endpoint>, header: <what the probe sends today>}` →
   `rate_limit.primary_window.used_percent`, `reset_at` (epoch seconds) and
   `limit_window_seconds` when present.

The reader returns a **snapshot**: `{read_at, accounts: {auth_index: {label,
email_masked, enabled, used_percent, reset_at, window_seconds, error}}}`.

- Emails are masked at the reader (`a***@domain`); nothing downstream can log
  one.
- A per-account error is a field (`"HTTP 401"`, `"timeout"`, `"no rate_limit
  block"`); a management-API failure as a whole is one exception the poller
  records. A missing percent (the usage endpoint answered without `rate_limit`)
  is an error, never read as zero.
- Timeouts: accounts are read in parallel (at most four at a time), 10 s per
  call, and a whole-read deadline of `poll_seconds / 2` (the schema's minimum
  for `poll_seconds` is 30, so the deadline is never under 15 s); accounts not
  read by the deadline carry `error: "deadline"`.
- `window_seconds`, when present and not 18000, means the account's primary
  window is not the 5h one this design is about: the account is unknown.
- The management route prefix (`/v8/`) is tied to CLIProxyAPI's major version.
  The reader knows the major it was written for; `cliproxy.version` (what
  `init` pinned from `app/cliproxy.lock`) is compared with it at start and the
  mismatch is the poll error; when the setting is empty (a server deployment)
  the check is skipped, and a 404 on a management route is reported as
  `"management route not found: CLIProxyAPI version?"`. A sidecar bump
  therefore cannot fail silently.
- The request carries the client identity the probe sends today (a `codex-tui`
  user agent) because the provider varies its answer by client. This is an
  unofficial endpoint read once a minute per account; the risk that the provider
  changes or refuses it is named in `docs/app/limits.md`, and the app's answer
  to it is "every account unknown, the banner says so".
- The management calls use an opener with no proxy: CLIProxyAPI is the core's
  loopback neighbour (or the operator's own `cliproxy.url`), and a system proxy
  must not see those calls.
- After a read the management API refused with 401 or 403, the poller holds
  until a reload or a refresh wakes it; `poll.last_error` says so. CLIProxyAPI
  may block an address after repeated failed management logins, and a loop
  retrying a wrong secret every minute is such a client.

`quota/poller.py` runs the reader every `poll_seconds` on a daemon thread and
stores only the latest snapshot under a lock (`latest() -> snapshot | None`).
The core's settings are three globals (`ROUTING`, `LOW_QUOTA`, `CLIPROXY`), each
swapped in one assignment by `apply_settings`; the poller reads them once per
iteration through `poller.current_settings()`, and the apply hook that wakes it
runs after all three are assigned, so the read it wakes sees one consistent set.
A reload with a changed `poll_seconds` or `accounts` sets an
`Event` that wakes the loop; the signal handler does no I/O. Every exception in
the loop is caught and recorded as `last_error`; the thread never dies. A refresh
request (`POST /api/accounts/refresh`) sets the same event and waits for the
read in flight, at most 15 s, then answers with the latest snapshot and its
`age_s`; a second refresh during that read joins it instead of starting another.

### 4.2 The policy

`quota/policy.py` is one pure function, called **per request** with the clock,
so a dead or stalled poller cannot leave a stale decision in force:

```
decide(snapshot, settings, now) -> Decision(relay_on: bool, reason: str,
                                            per_account: {auth_index: "low" | "ok" | "unknown"})
```

- `remaining = 100 - used_percent`. An account is **low** when
  `remaining < threshold_percent`.
- An account is **unknown** when it carries an `error`, when `used_percent` is
  missing, when the snapshot is older than `3 × poll_seconds`, or when there is
  no snapshot. Unknown counts as **not low**: with no number, the request goes
  through, and the UI says why.
- When `reset_at` is in the past, `remaining` is 100 until the next read.
- `rule: all` → `relay_on` iff every account in scope is low and the scope is not
  empty; `rule: any` → iff at least one is low.
- `routing: relay` → `relay_on = true`, reason `"routing: relay"`; `routing: direct` →
  false, reason `"routing: direct"`; `routing: auto` with `low_quota.enabled: false` → true, reason
  `"low quota mode off"` (today's behaviour).

The meter is an integer, so a threshold is a clean step; no hysteresis. The
decision is global, not per account: CLIProxyAPI chooses the account for a
request, the core cannot.

**Fail-open is a cost risk and is logged.** Unknown → direct means a dead
management API makes every request a full-price one while the endpoint looks
healthy, the failure `WORKSPACE_PROTOCOL.md` names as dominant. Therefore: every
change of `relay_on` or of its reason is one stderr line tagged `[routing]`
(the core has no log levels); every pass-through record carries the reason;
`/healthz` gains `routing: {relay_on, reason}` so a monitor without the UI sees
it. That is a deliberate exception to `/healthz` carrying no configuration: the
two fields name no secret and no address, and the code comment says why.

### 4.3 Pass-through

When `relay_on` is false, `POST /v1/responses` takes `passthrough.py`. The
decision is read once at the start of the request; a request in flight is not
switched.

- **Guards that run on both paths**: the token, the drain (503 while stopping),
  the body cap, valid JSON, the `model` id rule, `pacing_seconds` and the
  `max_concurrent_upstream` slot (they guard the CLIProxyAPI pool, which both
  paths share). **Guards that run only on the relay path**: the refusal of
  `previous_response_id` and `store: true` (they exist because the relay cuts
  before finalizing; direct, the provider handles both). `docs/relay/relay-8371.md`
  says so once.
- **Body**: the client's raw bytes, forwarded unchanged, when none of the
  relay's tuning knobs is present, which is the normal case. The knob set is
  its own constant, `RELAY_KNOB_PARAMS` (`mode`, `abort_after`, `seg_budget`,
  `total_cap`, `max_segments`, `payload_cap`, `imagegen_cut`, `merge_text`,
  `text_calls`, `text_calls_pad`, `pad_tools`, `retries`: the set
  `resolve_knobs` reads), not `RELAY_OWNED_PARAMS`, which also holds the
  request body's own keys. When a knob is present the body is parsed, the knobs
  removed and the rest re-serialized; the record says `reserialized: true`.
  Nothing is added: no instructions, no padding tool, no marker scan, no
  `text_calls`.
- **Headers to CLIProxyAPI**: `Authorization: Bearer <cli_proxy_api_key>`
  (never the client's key), `Content-Type`, and the same allow-list the relay
  forwards (`engine.FORWARDED_HEADERS`), so prompt-cache behaviour is the same
  on both paths; hop-by-hop headers and `Accept-Encoding` are not forwarded.
- **Retry**: before the first byte, on 429/502/503, the same budget and backoff
  as the relay; never after the first byte.
- **Response**: streamed back byte for byte with CLIProxyAPI's status, except 401,
  403 and 407, which are about the core's credential and become a 502, as on the
  relay path, and an
  allow-list of its headers (`Content-Type`, `Retry-After`, `x-request-id`),
  never `Transfer-Encoding` or, for a stream, `Content-Length` (the stream ends
  with the connection, as the relay's does); no renumbering, no synthetic terminal frame. The
  bytes are teed to a scanner that only looks for the terminal event and its
  `usage`, for the record.
- **Record**: one run record with `route: "passthrough"`, `reason`, status,
  timing, `upstream_requests`, the usage summary when the terminal frame
  arrived, no content; written in a `finally`, so a client disconnect
  (`cut: client_gone`) is recorded too; a drain that expires ends the process with
  `os._exit` and records nothing on either path. The request counts in
  `active_run()`: the drain waits for it up to `drain` seconds and then cuts
  it, the same truncation the relay already produces at drain expiry.
- Non-streaming requests (`stream: false`) pass through the same way, as one
  JSON body with `Content-Length`. CLIProxyAPI answers `stream: false` with one
  JSON object (`Content-Type: application/json`) sent chunked; the core reads it
  whole and answers with its own `Content-Length`.
- `/v1/images/*` keep their own path in `images.py` (buffered, own slots), as
  today. `/api/run` ignores the decision: it always runs the engine, as the
  measurement tools expect.

### 4.4 `/api/accounts`

`GET /api/accounts` → `{read_at, age_s, accounts: [{auth_index, label,
email_masked, enabled, used_percent, remaining_percent, reset_at,
state: low|ok|unknown, error, in_scope}], decision}`. `POST /api/accounts/refresh`
→ the same after one read (4.1).

### 4.5 Errors

- CLIProxyAPI unreachable: the poller records the error; every account becomes
  unknown; `/api/status.poll.last_error` names it; pass-through answers 502 like
  the relay path does. The UI shows one banner from `/api/status`.
- Management secret wrong or rotated: `poll.last_error` names the 401 and the
  hold (4.1), every account unknown, the log line says so once per change.

### 4.6 Tests

- `policy`: a table (all/any; one low; none low; unknown by error, by missing
  percent, by age, by no snapshot; reset in the past; empty scope; each
  `routing` value; `enabled: false`).
- `reader` against a fake management API: masking; per-account error; whole-API
  error; missing `rate_limit` is an error; per-call timeout; whole-read deadline;
  major-version mismatch.
- `poller`: a raising reader does not kill the thread; a settings swap wakes it;
  two refreshes share one read.
- `passthrough` against the stub upstream: the raw bytes reach CLIProxyAPI when
  no knob is present; with a knob, the body minus `RELAY_KNOB_PARAMS` and
  `reserialized: true`; the response header allow-list; `Authorization` is the core's key, never the client's; the
  header allow-list and the strip list; retry before the first byte and none
  after; bytes out equal bytes in; a record with `route: passthrough` and
  `reason` on success and on client disconnect; a 502 for a 401, 403 or 407 from
  CLIProxyAPI; `previous_response_id`
  and `store: true` reach CLIProxyAPI; a slot is taken and released; unknown →
  direct with CLIProxyAPI down is a 502 and a warning line.
- `/healthz` carries `routing`; decision changes are logged once per change.
- `/api/accounts` with the operator token and with a client token.

### 4.7 Adding a Codex account

CLIProxyAPI's management API runs the provider's OAuth for the app: it builds
the login URL, holds the PKCE state, completes the exchange when it is handed
the provider's `code`, and writes the credential into its `auth-dir`. The core
drives it and holds nothing but the callback listener: no credential, no
browser. The pinned release has one Codex flow, the browser one: no route of
its management API gives a device code.

- `POST /api/accounts/oauth` asks CLIProxyAPI for the login URL
  (`/v0/management/codex-auth-url` on the pinned release) and answers
  `{state, url}`. No readable management secret file is a 409; CLIProxyAPI
  refusing or unreachable is a 502 naming it.
- The login URL sends the browser back to `http://localhost:1455/auth/callback`.
  The core listens there, on `127.0.0.1` only, while one flow is pending (five
  minutes, CLIProxyAPI's own wait for the callback), hands what the provider
  sent, `code` or `error` with `state`, to CLIProxyAPI once through
  `POST /v0/management/oauth-callback` (a route without a management key,
  whose 200 means accepted: the exchange with the provider finishes inside
  CLIProxyAPI and shows in the status), and answers the browser from that
  answer: "you can close this tab", or "this login expired, start again".
  CLIProxyAPI's own callback forwarder (`is_webui=true`) is not used: it binds
  every interface. The port is the one `codex login` uses too; a busy port is
  a 409 "another login is in progress".
- `GET /api/accounts/oauth/<state>` asks `/v0/management/get-auth-status` and
  answers `{status: "ok" | "pending" | "error", error}` (`pending` is the
  binary's `wait`). On `ok` the core closes the listener and wakes the poller,
  so the account appears at the next read when Low Quota Mode is on. A `state`
  outside `[A-Za-z0-9_-]{8,128}` is a 400 (the binary answers `ok` to an empty
  one). A 401 or 403 from CLIProxyAPI is a 502, and the UI stops polling on it:
  repeated failed management logins earn a thirty-minute ban of the address.
- Both are operator routes under the Host/Origin guard. The UI opens the URL
  (in the shell, through `window.lokki.openExternal`, which opens the system
  browser; in a browser, a new tab) and polls the status; the user logs in on
  the provider's page, in a browser on the machine the app runs on.
- The routes are CLIProxyAPI's legacy management API, kept by the pinned
  release beside the `/v8/` ones; `docs/app/limits.md` names that dependency,
  the callback port, the same-machine browser (a server or Docker core cannot
  take the callback from a remote browser), and CLIProxyAPI's own traffic at
  start (two version strings fetched from npm).

## 5. Sub-project 3: the UI

A static Svelte 5 web app (TypeScript; its plain JavaScript build needs no
framework runtime in the Python sidecar) built into
`core/lokki_core/static/`. It runs in a plain browser against
a running core during development (the operator token pasted once, kept in
`sessionStorage`); Tauri loads the same files from the core's `/`, so the UI is same-origin with the
API, and injects the token (6.4).

Views:

- **Overview**: the endpoint (URLs; key masked; a copy button that fetches
  `?reveal=1` and copies the full key); the current decision in one sentence
  with its reason; `runs_since_start` by route; CLIProxyAPI reachable or not;
  versions; the banner when `poll.last_error` is set.
- **Accounts**: one row per account from `/api/accounts`: masked email, enabled,
  used %, remaining %, resets at (local time), state, and a checkbox "in scope"
  (writes `low_quota.accounts`). A refresh button. "Add or manage accounts"
  opens the CLIProxyAPI tab.
- **Settings**: built from `/api/schema`: Low Quota Mode (enabled, threshold,
  rule, poll interval), routing (auto / relay / direct), ports (`listen` and
  `cliproxy.port`, both `startup_only`: the form says a restart is needed and
  offers it; in the browser that is a message, in Tauri a sidecar restart), and
  the relay knobs under an "Advanced" fold, each with the `meaning` the schema
  carries. Keys the schema marks never-writable are shown read-only. A 409 is
  shown with its `why`.
- **CLIProxyAPI**: "Add a Codex account" (4.7), in the browser and in the app
  alike, which says, when Low Quota Mode is off, that the Accounts view does
  not read the new account until the mode is on; the panel section follows
  `cliproxy.panel`: off, a sentence pointing at Settings; on, in the browser a
  link to the panel, in the app "Open the panel" and "Copy the management
  secret" through the shell's hooks (6.4). The Overview's "Low Quota Mode is
  off" line offers "Add an account" as its "No account yet" line does, since
  the mode is off on first run.

The UI holds no state beyond the open tab and the token; every `PUT` is followed
by a `GET`.

Tests: component tests for the three forms (what they send for what the user
did; a `startup_only` key shows the restart notice; a 409 shows its reason), and
one browser run against a core with a fake CLIProxyAPI, driven by Playwright, in
CI.

## 6. Sub-project 4: the app

### 6.1 Shell

Tauri v2, Rust, no business logic: it starts the two sidecars, restarts one that
exits (after 1, 2, then 4 s; a fourth exit within a minute of the first stops
it and shows the error; a stop the shell asked for counts for nothing), shows
the window and a tray icon (open, pause = `routing: direct` through `PUT
/api/settings`, resume = the value before the pause, quit), and owns the
updater. A stop or restart of the core goes through `POST /api/shutdown` first
and kills the process only after `drain_seconds + 5` seconds, the value the 202
answer carries; CLIProxyAPI is stopped after the core, with the OS signal or
`TerminateProcess`; in that order it holds no client stream the core is not
already draining. A kill takes the sidecar's whole process tree: a one-file
PyInstaller build is a bootloader whose child holds the port. A CLIProxyAPI
restart restarts the core after it, since the core reads its upstream address
at start.

### 6.2 Sidecars

`bundle.externalBin`: `lokki-core` built with PyInstaller (one file, per OS and
arch, with `REVISION` inside) and `cliproxy`, the official release archive for
the OS and arch, fetched at build time and verified against the release's
`checksums.txt`, pinned by version in `app/cliproxy.lock`. Sidecar files are
named with their version, dots written as underscores (`lokki-core-0_1_0`,
`cliproxy-8_0_23`: a dot in the name reads as an extension on Windows), the
rule living in `app/scripts/sidecars.py`, which also prints the `externalBin`
list and the version as the `--config` override `tauri build` takes, so the
tracked `tauri.conf.json` names neither. The shell deletes a previous
version's files at start where the directory is writable, which covers the
known NSIS reinstall case where a sidecar is not replaced.

### 6.3 First run

The shell runs `lokki-core init --data-dir DIR` (3.8), reads `lokki-core
shell-config` on a pipe, runs `lokki-core sync-cliproxy` and then starts both
sidecars; the shell itself writes no file. What `init` does, from the app's side:

1. Create the data directory: `~/Library/Application Support/lokki`,
   `%APPDATA%\lokki`, `~/.local/share/lokki`. Everything below lives under it:
   `config.yaml` (the core's), `cliproxy/config.yaml`, `cliproxy/auth/`,
   `secrets/` (0600), `logs/` (rotated), `runs.jsonl`.
2. Generate the four secrets (2).
3. Pick ports: 8381 and 8317 if free, otherwise the next free ones. lokki never
   adopts a CLIProxyAPI already running on the machine and never reads its auth
   directory: it runs its own, so a user who already has one logs in again. The
   Overview says which ports were chosen.
4. Write both config files (3.8). A port or panel change later goes through
   `PUT /api/settings`, then a CLIProxyAPI restart by the shell, which runs
   `sync-cliproxy` first.
5. Start both; open the window on Overview, which says Low Quota Mode is off
   and offers "Add an account", pointing at the CLIProxyAPI tab, where the
   account is added (4.7). When
   `init` or `shell-config` fails there is no window yet: the shell shows its
   error page with the subcommand's stderr.

### 6.4 The window

- The window loads the core's `/`, so the UI and the API are one origin and no
  CORS or preflight handling exists in the core. The shell injects the operator
  token with a webview initialization script that wraps `fetch` to add the
  header on calls to the core's origin (the UI never stores it in Tauri); the
  script keeps the token in a closure, does nothing on any other page origin,
  and the window may navigate nowhere but that origin. It exposes
  `window.lokki` with `restart(service)` (resolves after the named sidecar
  restarts, rejects on failure), `openExternal(url)` (the system browser,
  `https:` only), `openPanel()`, `copyManagementSecret()` and the updater's
  three, `updateStatus()`, `checkForUpdate()` and `installUpdate()`: seven hooks. The page's IPC
  grant is one capability for the main window and the core's exact origin,
  naming those commands and nothing else.
- CLIProxyAPI's panel (`/management.html` on its port) is off by default
  (`cliproxy.panel`) and, when on, opens in the system browser through
  `openPanel()`: it is third-party JavaScript downloaded from GitHub at run
  time, and in a shell window it would sit beside the Tauri bridge. Its login
  takes the management secret, which `copyManagementSecret()` puts on the
  clipboard on the user's click; the secret reaches no page and no URL.
  `docs/app/limits.md` names the download.
- Installers: `.dmg` (both arches), `.msi` and NSIS `.exe`, `.AppImage` and
  `.deb`; x64 from the first release, arm64 for Linux and Windows when a runner
  on this account builds them (macOS arm64 builds on the hosted runner).
  Unsigned: the README states the Gatekeeper (`xattr -d com.apple.quarantine`,
  or right-click Open) and SmartScreen steps.

## 7. Sub-project 5: update

- **Channel**: GitHub Releases on `thomasvan/lokki`, one release per version,
  assets per OS, plus the updater's `latest.json`.
- **One checker**: the Tauri updater, in the shell. The core makes no network
  call for updates (a server or Docker deployment of the core never phones
  GitHub).
- **Signature**: the updater requires one; the key pair comes from `tauri signer
  generate`, the private key is a CI secret, the public key is in
  `tauri.conf.json`. This is separate from OS code signing, so updates verify on
  every OS from the first release.
- **Endpoint and signed version**: the endpoint is
  `https://github.com/thomasvan/lokki/releases/latest/download/latest.json`, in
  `plugins.updater.endpoints`, with `requireSignedVersion: true` and downgrades
  refused. `LOKKI_UPDATE_ENDPOINT` in the shell's environment replaces the
  endpoint for a hand test (a release build accepts `https` only); the public
  key is never overridable, so a redirected endpoint can offer only packages the
  real key signed for the version it announces.
- **Install switch**: the shell decides `can_install`, never the page: on Linux
  only when `APPIMAGE` is set (a `.deb` gets the download), on Windows always,
  on macOS by `plugins.lokki.macInstall` in `tauri.conf.json` (`false` until
  signing exists).
- **Behaviour**: the shell checks daily and on "Check now"; the UI shows a
  banner with the notes link. On Windows (NSIS/MSI, passive mode) and Linux
  (AppImage) the app installs on the user's click and restarts. Before the
  install it runs the full stop sequence (6.1: the core drained through
  `POST /api/shutdown`, then CLIProxyAPI) and waits for both processes to be
  gone: on Windows the installer cannot replace a running sidecar. On macOS
  without a Developer ID the install step is off: the banner offers the
  download, and the README says a downloaded unsigned `.dmg` is quarantined
  again and needs the same step as the first install. When signing exists,
  enabling the install step is one config key.
- Linux: the install step exists only when the process runs from an AppImage
  (the `APPIMAGE` environment variable the runtime sets); a `.deb` install has
  no updater path, and its banner offers the download only.
- **Linux-only release**: `workflow_dispatch` takes `tag` and `platforms` (`all`
  or `linux`). A Linux-only release is the Human's deliberate dispatch with
  `platforms: linux`; its `latest.json` then names Linux alone, and a macOS or
  Windows updater reading it reports `up_to_date` with the reason "no build for
  this platform", never an error.
- **Release flow**: a tag `v<version>` runs `app.yml`'s release job, which
  refuses a tag that differs from `core/VERSION`, builds every installer into
  one draft release, and publishes the draft only once every asset and
  `latest.json` are uploaded, with the notes (the changelog since the previous
  tag, and CLIProxyAPI's release notes when the lock changed) patched into
  `latest.json`; the updater never sees a release missing an asset.
- **Settings across versions**: the core reads a settings file written by any
  earlier version: a key that moved is read under its old name and written
  under the new one once (`migrate(doc)` returns the renames it applied, tested per renamed key; the table
  is empty today, and `upstream` is a key of its own, not a rename),
  with the file replaced whole, never edited in place.
- **Sidecars**: the core ships inside the bundle and updates with it.
  CLIProxyAPI updates when `app/cliproxy.lock` changes in a release; the app
  never pulls a `latest`. A lock bump that changes CLIProxyAPI's major version
  is refused by CI unless the reader's route prefix (4.1) is updated in the
  same change.
- **Data**: an update never touches the data directory; a settings key the new
  version does not know is kept in the file and listed as `unknown_keys` in
  `/api/status`.

## 8. Repository structure and maintenance

```
lokki/
  README.md  LICENSE  WORKSPACE_PROTOCOL.md
  core/            lokki_core + tests + measurement tools   (Python)
  ui/              the web UI                                (TypeScript)
  app/             Tauri shell, sidecar build scripts, cliproxy.lock
  deploy/          the server/Docker deployment of the core (what exists today)
  docs/
    relay/         the relay's behaviour and measurements (as today)
    app/           the app: data directory, first run, update, limits, api
    superpowers/   specs and plans
  .github/workflows/
    core.yml       the suite: Linux on the self-hosted runner; macOS and Windows on dispatch
    ui.yml         lint, component tests, Playwright
    app.yml        sidecars and installers per OS; a draft release on a tag
```

- `core/` has no dependency on `ui/` or `app/`; `app/` depends on both only as
  build artifacts.
- `core/VERSION` is the one home of the product version: `pyproject.toml` reads
  it (`dynamic = ["version"]`, `file: VERSION`; setuptools cannot read outside
  the package directory, which is why it is not at the repository root), the
  Docker image copies it, the app build passes it to `tauri build` as a config
  override, and the UI build reads it into a constant. No tracked file holds a
  copy. `revision()` stays the commit.
- A change to the relay's cut behaviour comes with a measurement note in
  `docs/relay/`; a change to the control API comes with its test and the line in
  `docs/app/api.md`.
- `WORKSPACE_PROTOCOL.md`'s proof gates are updated in the PR that changes what
  they validate (the move, each new workflow).

## 9. Security

- Both services listen on loopback. The app writes loopback addresses (the
  core's `listen`, CLIProxyAPI's `server.host`, whose own default is every
  interface) and `PUT` accepts no other; `sync-cliproxy` rewrites the host on
  every CLIProxyAPI start; the OAuth callback listener (4.7) binds `127.0.0.1`;
  `allow_public_bind` stays never-writable through the API. The sidecars get an
  allow-listed environment from the shell, never the user's whole one. (The core's own rule for a hand-written file is unchanged: it refuses a
  wildcard and notes any other non-loopback address.)
- The operator token and the client key are different tokens; a copied client
  key cannot read settings or accounts (403).
- `/api/*` and `/version` check `Host` (loopback, or a host named in `listen`)
  and, on a request carrying `Origin`, that it is the core's own origin;
  otherwise 403 (3.4). This is the DNS-rebinding and CSRF guard for a local
  server with a token in a browser. `/healthz` and `/v1/*` are not covered: a
  client SDK sends whatever `Host` its base URL says.
- Emails are masked at the reader; the run log keeps counts, never content.
- The management secret and the CLIProxyAPI key are referenced by path or held
  in memory; `/api/settings` shows `"***"` for every secret.
- Update artifacts are verified by signature before install; a failed
  verification is shown, not retried silently.

## 10. Open items, for the plans

- PyInstaller on Windows arm64 and Linux arm64 is untested until a runner builds it.
- A device-code login: none in the pinned release's management API; it returns
  to the CLIProxyAPI view when a pinned release serves one.
- What the usage endpoint returns for plans other than the one measured (the
  window length, `limit_window_seconds`).
- The live pair on the owner's host keeps running the Docker deployment of the
  core; `deploy/` moves with `core/` in the move PR.
- The hosted macOS and Windows release jobs have not run on this account; the
  first release is Linux-only unless minutes exist.
- The macOS install step stays off until a Developer ID signs the build.
- The updater answers 404 while the repository is private.
