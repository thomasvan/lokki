# The token oracle: the cpa-usage plugin

What it is, what it can and cannot see, and how to read it. The meter
(`billing_probe.py`, the integer percent in the upstream's 5h window) says what a
window cost; this says which tokens were involved. Nothing here replaces the
meter, and nothing here makes the meter finer.

## The plugin

`usage-report` v0.3.3, from <https://github.com/andyWang1688/cpa-usage>, enabled in `config.yaml` under
`plugins.configs.usage-report`. It is a cliproxy plugin (a `.so` under
`plugins/linux/amd64/`), it collects through the `usage.handle` callback, and it
keeps its own SQLite database. It is not part of the relay.

- **Database:** `logs/usage-report/.cli-proxy-api/usage-report.sqlite` on the host
  (`/CLIProxyAPI/logs/usage-report/.cli-proxy-api/usage-report.sqlite` in the
  container), table `usage_events(ts, dedup, payload)`, one JSON payload per
  request. `logs/` is git-ignored, and this database must stay out of the repo: it
  holds account emails and hashed client keys.
- **API:** `GET /v0/management/plugins/usage-report/api/usage?start=YYYY-MM-DD&end=YYYY-MM-DD&bucket=day|hour|month`
  with `Authorization: Bearer <management key>`. It returns `events[]` and
  `buckets[]`, plus `collect_err`, which is the only field that says whether the
  plugin is collecting at all. `python3 core/billing_probe.py tokens
  [--start --end --bucket --json]` prints it as per-key, per-model and total
  token counts, uncached input included, and spends no quota. It raises on a
  non-null `collect_err` rather than printing zeros: an empty event list *with*
  an error is exactly what a broken oracle looks like.
- **One event carries:** `timestamp`, `model`, `source`, `failed`, `latency_ms`,
  `ttft_ms`, and `tokens{input_tokens, output_tokens, reasoning_tokens,
  cached_tokens, cache_read_tokens, cache_creation_tokens, total_tokens}`.

`source` is the **OAuth account email** for codex traffic, so token volume is
attributable per account — the meter is too, but the meter cannot say which
tokens bought which percent.

## What it does and does not see

- A **finalized** request through cliproxy appears with exact token counts. A
  minimal `gpt-6-astra` request recorded `input_tokens: 309, output_tokens: 5,
  cache_read_tokens: 0, source: <account email>`.
- A **cut** request appears **not at all**. A relay run that produced two upstream
  requests and a `budget`→`marker` cut chain left the database with the one
  earlier event and nothing else. The relay kills the stream before cliproxy is
  handed usage, so cliproxy never runs its usage callback for it.
  - A cut request can still appear once in a while, as a **zero-token** event with `failed: false` (1 of 29 in an A/B
    round). Its absence or its zero is not a statement about what the provider charged.
- A request that **fails before reaching a provider** (unknown model) also
  records nothing.

The second point is the load-bearing one, in both directions. It is direct
evidence for what the relay's `verdict: usage-discarded` means — *cliproxy's*
ignorance, not a statement about the provider — and it means the plugin can never
report what an aborted request cost. Its use for the abort question is the
*other* side of the pair: for an identical body, a finalized request measures the
input in tokens, which is then the input the aborted twin carried.

## `db_path` does not reach the plugin

`plugins.configs.usage-report.db_path` is inert on this cliproxy build. The host
sends the plugin's config as `rpcLifecycleRequest.ConfigYAML`, a `[]byte`
(`internal/pluginhost/rpc_schema.go`), and `json.Marshal` encodes a `[]byte` as
**base64**; the plugin decodes that field into a `string` and scans it for a line
beginning with `db_path:`, which base64 text never contains
(`plugin/main.go`, `applyConfig`). The plugin therefore ignores the setting and
falls back to `defaultDBPath()`.

That fallback then fails too: this image has no `/etc/passwd` entry for uid 1000,
so `user.Current()` errors and `defaultDBPath()` returns the **relative** path
`usage.sqlite`, resolved against the CWD `/CLIProxyAPI` — a root-owned directory
uid 1000 cannot write. The plugin opened nothing and every API call answered
`sqlite migrate: unable to open database file (14)` with no rows, which looks
exactly like "no traffic yet".

The deployment fix is in `docker-compose.yml`: `./deploy/cpa-passwd` is mounted
over `/etc/passwd` so uid 1000 has a home inside the already-mounted logs
directory, and the database lands in
`/CLIProxyAPI/logs/usage-report/.cli-proxy-api/usage-report.sqlite`. The directory
`logs/usage-report/.cli-proxy-api/` must exist and be owned by uid 1000 (the
plugin does not create it), and `db_path` in `config.yaml` names the same file so
that both routes agree if cliproxy ever starts forwarding the config.
`GET /v0/management/plugins/usage-report/api/usage` → `collect_err: null` is the
check that any of this is still true.

## The meter stays coarse

The upstream reports quota as an integer percent in two places, and both are
integers: `/backend-api/wham/usage` (`rate_limit.primary_window.used_percent`,
what `billing_probe.py read` prints) and the `X-Codex-Primary-Used-Percent`
response header, which cliproxy captures per account into
`/v8/management/credentials` → `quota.signals` with an `observed_at` timestamp.
`credits` carries no usable resolution (`balance: "0"`, message counts `[0, 0]`).

So a single request's quota cost below one point of the 5h window is invisible,
and an arm has to move the meter by a few points before its cost can be read at
all. The oracle supplies the token side of that comparison exactly; it does not
supply a finer meter.

## Reading "zero events"

"Zero events" is evidence only if the collector could have written one. Put one tiny finalized request inside the
window as a canary: the oracle must show it. The proof that the plugin was alive is the `plugin loaded` line in
`docker logs` (the plugin's lines go to stdout, not to `logs/main.log`) and a health check made inside the window
(`collect_err: null` from the usage API). An arm also needs on the order of 100,000 characters of input before the meter can read it at all.

## What it is used for

The meter says what a window cost; the oracle says which tokens were involved. For an identical body, a finalized request measures
the input in tokens, which is then the input its cut twin carried. The costs measured that way are in
[`cost-of-a-cut.md`](cost-of-a-cut.md).
