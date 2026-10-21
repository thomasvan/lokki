# midstream-abort relay (127.0.0.1:8371 + Tailscale)

> This page is the behaviour reference for the relay's code. The pair that runs it, the relay on `:8381` in front of cliproxy
> `:8318`, is in [`live-8381.md`](live-8381.md); ports and paths below that name `8371`/`8317` describe the earlier pair, which this
> repository no longer maintains.

`core/server.py` is the entry point for the midstream-abort relay: a
client-facing proxy that cuts its upstream read mid-generation. It sits between
the client and cliproxy:

```
client (curl / Codex CLI)
  -> relay :8371              (injects markers, scans SSE, hard-closes the
                               socket to cliproxy mid-generation, issues
                               continuation segments, serves relay.summary)
  -> cliproxy :8317           (Go: client disconnect cancels the upstream
                               request context)
  -> chatgpt.com/backend-api/codex
```

The abort works because cliproxy treats a downstream disconnect as a request
cancellation and kills the upstream call immediately. Usage items that never
reach a terminal frame (`output_item.done`, `function_call_arguments.done`,
`response.completed`) are never *finalized*: the provider sends no usage block for
them, so nothing reaches the usage collector. **It does not follow that they are
never billed.** The 2026-10-05 relay-charge arms measured 12 such turns against a
settled baseline and the meter moved +4, against +8 for the same 12 turns direct;
the provider charges for the cached prefix it had already read when the
connection dropped, and it does so without a usage block — the charge follows the
cache reads, not the output (measured 2026-10-05), and on an agentic turn most of
that prefix is the reasoning and the tool call the cut still lets the model
finish. On the owner's own Codex command the same pair is **+3 relayed against +9
direct**, where the relayed run also ends before the file the direct run wrote
exists; the size of the cut's saving is a property of the body and of where the
cut can land, not a constant. Both pairs are in *The cut removes
the usage event, not the charge* in `docs/relay/cost-of-a-cut.md`.

## Layout

Path names below are relative to the repository root.

```
core/
  server.py            entry point and re-export surface (~110 lines)
  lokki_core/
    core.py            paths, run log, input flattening, SSE writing, errors
    config.py          config.yaml + environment overrides, resolved settings
    renamer.py         frame identity, request-body building, synthetic frame
    stream_view.py     what the client was shown, and the frames that close it
    trace.py           the opt-in diagnostic trace (off unless debug_log is set)
    sse.py             server-sent-events framing
    engine.py          one upstream request per segment, and the segment loop
    handler.py         request guards, listeners, main()
    textcalls.py       `text_calls`: an exec call written as text, found and built into a call
    images.py          the image routes, passed through to the cliproxy (see images.md)
  config.example.yaml  every key with its default and meaning, generated from lokki_core/schema.py (no secrets)
  config.yaml          local, gitignored: token and cliproxy key
  restart-relay.sh     replace whatever holds the configured ports
  relay-up.sh          idempotent cron supervisor; starts only when nothing is bound
  runs.jsonl           one record per engine run — the frozen seed; the running
                       relay writes to the state directory
  e2e_evidence.jsonl   one record per live test of the retired live suite, with
                       the revision that produced it
  billing_probe.py     billing oracle: read the upstream meter, and the plugin's
                       per-request token record
  billing_ab.jsonl     one record per A/B arm, emails redacted to acct-N
  billing_reads.jsonl  one record per labelled `read`, for delayed re-reads
  test_offline.py      config, auth, bind, input guards (no quota)
  test_engine.py       SSE framing, tail, cut modes, Renamer (no quota)
  test_images.py       the image routes against a fake cliproxy (no quota)
```

## Run

```bash
cp core/config.example.yaml core/config.yaml  # once
$EDITOR core/config.yaml   # set auth.token and cli_proxy_api_key
python3 core/server.py     # listens on every address in `listen`
# requires cliproxy running on :8317 (docker compose up -d)
```

Configuration lives in `core/config.yaml` (gitignored), mirroring how
cliproxy is configured: no secret on the command line and none in the
repository. Environment variables override it (`RELAY_TOKEN`, `RELAY_LISTEN`,
`CLIPROXY_URL`, `CLI_PROXY_API_KEY`, `RELAY_CONFIG`), and
`--config <path>` selects another file.

`listen` takes one address or a list. The shipped example binds loopback plus
this machine's Tailscale address — the same posture `docker-compose.yml` uses
for cliproxy, never `0.0.0.0`. An address that cannot be bound is reported and
skipped; startup only fails when none can be bound.

The relay refuses to start without `auth.token`, and every request must present
it as `X-Relay-Token` or `Authorization: Bearer`, because it forwards the
operator's upstream credential. It answers with no CORS headers at all, so no
page can drive it from a browser.

Trigger with a token from the config:

```bash
TOKEN=$(python3 -c "import yaml;print(yaml.safe_load(open('core/config.yaml'))['auth']['token'])")
curl -sN http://127.0.0.1:8371/api/run -H 'Content-Type: application/json' \
  -H "X-Relay-Token: $TOKEN" \
  -d '{"mode":"segmented","prompt":"Write a 300-word essay about metering APIs.",
       "model":"gpt-6-astra","seg_budget":800,"max_segments":8,"pad_tools":true}'
```

## Modes

| mode | behavior |
|---|---|
| `control` | pass-through, no injection, no cut |
| `marker` | inject format rule, abort when `TASK_COMPLETE_9F3` arrives |
| `after-deltas` | abort after N output_text deltas (`abort_after`) |
| `segmented` | marker + `__pad9f3` tool padding + segmented continuation |

The committed evidence predates these names and is never rewritten: its run
records spell the mode `stealth` and the cut `sentinel`, and no committed record
carries `abortn`.

`/v1/responses` always runs `segmented`. A caller can pick another mode, `control`
included, only on the operator path or with `allow_client_tuning` on. So there is
no per-client or per-request switch, and the choice below is made by which
endpoint a client is pointed at.

## When to point a client at the relay

On the account of [cost-of-a-cut.md](cost-of-a-cut.md), which fits the arms measured and is not an observed rule, a request the
relay cuts before any of its items has finalized is charged about its cached prefix and little else: neither its uncached input nor
its output (0.00–0.045 points a dispatch on a cold prefix, 0.16–0.21 on a warm one). A request that has finalized an item first is
charged as a finalized one. A text answer cut at the marker is the first kind. A turn that ends in a tool call the client has to run
is the second, because the call finalizes before the `toolcall` cut. In text answers most of what is saved is input: in the
300-word direct block 221,904 of 242,640 input tokens were uncached. Measured on the test pair:

| workload | what the meter says | what the client gets |
| --- | --- | --- |
| text-only answers, 300 words, `gpt-6.1-sol`, 15 per arm | relay under 1 point against 7–9 direct, so under about one seventh. Direct spends 0.53 points an answer | every answer in two messages at the built-in `seg_budget` of 1,500 (now one: `merge_text`), and 1.8× slower (median 57 s against 32 s) |
| text-only answers, 900 words, `gpt-6.1-sol`, 10 per arm | +4 against +6 in the first of two rounds (the second round's reading was void), both under the 5 points a ratio needs. Direct spends 0.6 points an answer | one message; 1.2 to 2× slower; two blind judges picked direct in 6 of 9 pairs each |
| a whole Codex task that writes a file, pelican SVG on `gpt-6-astra` | 0.78× the direct spend over both block orders (0.91× and 0.67× in the two), between 0.64 and 0.95 on the meter's rounding; the two orders overlap only between about 0.75 and 0.82, and six runs per arm do not separate them | the relay's median wall time was 21–23 s shorter in both block orders, but only one gap clears the harness's rule; drawings preferred on the direct side in 5 of 6 blind pairs, not separated from chance |
| one replayed turn, 12 per arm ([cost-of-a-cut.md](cost-of-a-cut.md)) | about half | not a client task |

So, as a default:

1. **Text-only consumers: the relay saves most on short answers, and costs time.** At 300 words (one segment) it saved about 0.5
   points an answer, under a seventh of the direct spend, on one prompt on `gpt-6.1-sol` at `medium`. At 900 words, which the relay
   has to continue, the meter did not separate the arms, because the direct cost is mostly the 16,000-token prefix and the relay
   pays a cached prefix for every request it makes; the saving shrinks with every extra segment. The answer arrives as **one
   message** whatever the number of segments (`merge_text`, below), and a continuation starts a new word. The relay is 1.2 to 2×
   slower on a 900-word answer. The model used to announce the relay's instructions in a lead-in line; every block of injected
   instructions now ends with an order not to mention them. Two settings are the ones to look at for a long answer: `seg_budget` at
   least the answer's length in one request, and `tail_chars` at least `seg_budget` (with `tail_chars` below `seg_budget`, 4 of 10
   900-word answers ran past their length and were cut at `total_cap`; with 8,000, none; the tail is the likely reason, not a settled one). An answer past `total_cap` ends with the
   relay's notice (see "One item per answer" in "Serving a real client"). A client that reads the output text as one stream of
   deltas and never shows a message boundary is a case not measured, and `codex exec -o`, which keeps only the last message, gets
   the whole answer.
2. **Point agentic Codex runs at cliproxy directly**: tools, files written, many turns. The relay's saving there was about a
   fifth in the two rounds measured, between 5% and 36% on the meter's rounding, and the runs' own spread is not separated from it. It is not shown to leave
   the work as good. The one cut it does well, the final short answer, is about 8% of such a run.
3. **If the cost of a run is the problem, change the model before the endpoint.** On the pelican task `gpt-5.6-terra` and
   `gpt-6.1-sol` each cost about 0.2 of `gpt-6-astra` (sol: 0.21, between 0.10 and 0.31 for the meter's rounding of one point) and
   `gpt-6-luna` under about 0.12 on that task (below 0.04 over the day's traffic); blind judges picked astra in 8 of 9 pairs, 6 of 6 against luna and terra and 2 of 3 against sol (the sol comparison is
   three pairs from one judge: not a result).
   The per-model factors for the spend meter are in [cost-of-a-cut.md](cost-of-a-cut.md). Whether that is worth it is a question
   about what a usable result is.
4. **The first request of each session is not the relay's to cut**, and it is 47% of a pelican run on astra: a cold 16k-token
   prefix every time. On the test clone it was read from the provider's cache when a proxy made all of a session's per-session HTTP
   headers (`session-id`, `thread-id`, the window and turn headers) constant across sessions; with the headers left alone it was not.
   That is small N, one model, one account, one task, and it was not tried with the relay in front. Only `session_pin` (off) does
   anything like it: it sends a `session-id` of its own, which makes several sessions look to the provider like one, so it is the
   operator's decision, not a setting to flip.

`merge_text` delivers a continued text answer as one message whatever `seg_budget`, at the price of a second upstream request. With
`seg_budget: 4000` a 300-word answer (about 2,000 characters) arrives as one message from one request, in 27 s a run (direct runs
took 24 s), and the meter read +0 against the direct +8: one prompt, one model, one order. That +0 was read in a window the relay's own
requests had not anchored, so the 300-word block's reading, taken in an anchored one, carries the bound more firmly. The built-in is 1,500 with `tail_chars`
1,500. What a larger budget gives up is the earlier cut for a turn whose model never writes the marker: such a turn runs longer
before the cut, and one that finishes by itself first is billed in full. In the 55 relayed text runs measured, the run always ended
at the marker or at the cap.

Blind judges preferred the direct answer in 11 of 15 pairs at 300 words (one judge) and in 6 of 9 at 900 words (two judges);
neither is resolved from chance, but both lean the way the pelican pairs did, so the saving is not shown to come free of quality.
Whether to set a larger budget on a live relay is the operator's decision.

### Which endpoint a Codex command reaches

A provider name in a project's `.codex/config.toml` does not define that provider: Codex ignores `model_provider` and
`model_providers` there and says so on every such run. `-c model_provider=relay` in a project directory resolves `relay` from
`~/.codex/config.toml`. To reach the test pair, pass the provider on the command line with `--ignore-user-config`, or add
differently named providers to `~/.codex/config.toml`. The clone listens on its Tailscale address only, so a `cliproxy` entry
pointing at loopback cannot work for it.

## Traceability of the claims

A claim in this document is traceable to a committed artifact: the raw per-run records
in `core/runs.jsonl` and the per-test records in
`core/e2e_evidence.jsonl`. The test records carry the git revision that
produced them; `runs.jsonl` has no revision field, so cite `e2e_evidence.jsonl` when
attribution matters, and the document's own timestamp otherwise. The dated live-run
results live in those two files, which is their home.

## Billing measurement (upstream meter)

The meter says what a window cost, in whole percent. The per-request token side is the cpa-usage plugin: what it records, what it
cannot record, and the cliproxy ABI mismatch that has to be worked around for it to run at all are in
[`usage-oracle.md`](usage-oracle.md). What a cut costs and saves, arm by arm, is in [`cost-of-a-cut.md`](cost-of-a-cut.md): a cut
removes the usage event, not the charge. A relayed turn of a real body costs about half of a direct one, and a whole Codex task
0.78 of the direct spend (pelican on `gpt-6-astra`).

The relay can read the upstream's own usage meter, so a billing claim is an
operator-checkable measurement:

```sh
export CLIPROXY_MGMT_SECRET=<management password>
python3 core/billing_probe.py read                    # meter now
python3 core/billing_probe.py tokens --bucket hour    # the token side
```

`requests/api-call` performs a plain authenticated `GET` of
`chatgpt.com/backend-api/wham/usage` against one named credential; cliproxy
substitutes `$TOKEN$`, so no access token passes through the tool and the read
spends no quota. A bracketed measurement is two of these reads around your own
traffic, each recorded with `read --record LABEL`, which appends to
`core/billing_reads.jsonl`; `billing_ab.jsonl` holds the arms taken
while the arm runner existed, with emails redacted to `acct-N`. Note the cached
copy in `/v8/management/credentials` lags the upstream by one to two percent —
cite the `live` values.

### The event-level mechanism: what a cut removes

Usage reaches the client, and the usage collector, in the *terminal frame* and
nowhere else — `response.completed`, or the `output_item.done` /
`function_call_arguments.done` that closes the turn (the head of this document
lists the full set). A delta carries no billing information, so a read aborted
before the terminal frame leaves the provider with no usage block to finalize for
that turn. The run record keeps the two cases apart: `usage: null` with
`real_completed: false` means no finalized terminal frame arrived, and `verdict:
billed` is the relay's assertion that one did.
`provider_terminal` names the terminal frame that ended the run, if one did:
`response.completed`; `response.incomplete` (a `max_output_tokens` limit or a
filter ended it — finalized, so `real_completed: true`, with the provider's own
usage when it sent one); or `response.failed`, which is not a finalized answer:
`real_completed` stays false and its usage, if any, is kept as the provider's
count. None of these is a billing measurement; that is the meter's. The field is
`null` for a run the relay cut or that broke.
`carry_dropped_chars` counts characters the client never received: a trailing
marker prefix is held back when a budget or toolpad cut continues into another
`segmented` segment, so the next segment's scan sees it with the marker's other
half, and this field counts what no later segment in the run had text to attach
it to. It is the price of never sending a fragment on a closed item or a tool
call, and it is at most the marker's length less one.

That is the whole of what a cut removes — the usage event, not the charge. The
provider still bills the cached prefix it had already read when the connection
dropped, so a cut turn is cheaper than a finalized one and never free. What the
difference is worth is measured in `cost-of-a-cut.md`.

### The `itemdone` cut: a structured output

A client that asks for a **structured output** (`text.format.type` of `json_schema` or `json_object`, `strict` or not; Codex CLI's
task-title and rollout-summary requests are such) makes both early triggers unreachable: the model cannot write the marker line or
call the padding tool without breaking the schema, so it answers, closes the text item, and the next frame upstream is
`response.completed`. `engine.py`'s `itemdone` cut takes that window: when upstream closes a **text** item on such a request and no
early trigger fired, the run ends there (a segment that delivered nothing continues into another segment instead,
`itemdone-empty`). It is gated on the structured output and not on "the caller offered no tool": Codex declares its tools inside the
`input` items, and a tool call may open only after a preamble message has closed, which the cut would otherwise end at the preamble.
A text answer split across two items ends at the first; that is the same bias the `cap` cut carries, and an item boundary is a
cleaner place to truncate than mid-character at `total_cap`. The run log shows it: `cut: itemdone` should be the dominant cut on
structured-output traffic, and `verdict: billed` should fall to the prose residual; nothing watches that automatically.

### Which guards run on which path

The guards that protect the CLIProxyAPI pool and the endpoint run on both paths. The refusal of
`previous_response_id` and `store: true` runs only on the relay path: it exists because the relay cuts
before the provider finalizes a response, and a request sent straight through is handled by the
provider. The list is in [`docs/app/api.md`](../app/api.md#pass-through).

### Before believing a meter delta

cliproxy's `main.log` records every request on these credentials, including other clients'. Two preconditions: a settled
baseline (two reads with no relay traffic in between returning the same values) and a clean window (no `POST /v1/responses` on the
pool during the arm that this relay did not make). Skipping either produces a number that looks like a result and is not one.
The address in `main.log` does not say which process made a request: `172.27.0.1` is the docker gateway, so a relay, a scratch
relay and a direct Codex session look the same. The container's log stamps are UTC+8 while the host is UTC+7.

### What the relay's own verdict does not say

`verdict: usage-discarded` in `runs.jsonl` is the relay's own state: it is derived as `"billed" if real_completed else
"usage-discarded"`, so it records that no finalized terminal frame arrived, which is the precondition for the cut, not a billing
measurement: the relay cannot see whether the provider finalized the request anyway. The billing claim rests on the meter reading,
never on that field.

## Offline suites

The offline suites spend no quota and reach no upstream, so they are safe to run
any time, and the runner is where their number lives:

```bash
python3 -m unittest discover -s core -p 'test_*.py'
```

- `test_offline.py` — config loading (token/listen/key, env overrides, CRLF
  rejection, and an explicit `0` for pacing or concurrency not being
  confused with "unset"), multi-bind, auth, body guards, input edge cases,
  run-log parsing, sequence numbering, the client-facing cut policy (a stub
  upstream records the body the relay actually sent, so a caller's `mode` and
  `pad_tools` cannot quietly survive), and the billing oracle's evidence guards
  (no account email may reach the committed evidence file, and a failed meter
  read must not be reported as "no change").
- `test_engine.py` — SSE framing per spec, the bounded continuation tail, the
  `after-deltas` and `cap` cut paths, and the real cross-segment `Renamer` behaviour.
- `test_evidence.py` — the record chain as a set of tampering cases: edited, deleted,
  inserted, replaced, half-written. Plus the one case that must *not* be detected
  (truncating the tail) and the rotation seam.
- `test_lifecycle.py` — a real relay in a real subprocess: SIGTERM mid-stream
  finishes the stream, a stuck stream still exits inside the window, and SIGHUP
  adds a client token without dropping the process. It points the child at a temp
  `RELAY_RUNS_LOG`, because the relay derives `runs.jsonl` from its own module path
  rather than from the config path — a test relay would otherwise append synthetic
  runs to the committed evidence, which is a false claim in the record.

## Upstream overload and pacing

The Codex backend answers a burst with `503 service_unavailable_error`
(`code: server_is_overloaded`). cliproxy relays that status, so the relay sees it
as its own upstream failure. When the whole credential pool is cooling down it
answers something more specific instead:

```
503 {"error":{"message":"auth_unavailable: no auth available (providers=codex,
     model=gpt-6-astra; last upstream error: server_is_overloaded: ...)"}}
Retry-After: 1
```

That second body is the one the relay was actually hitting, and it names the real
cause: cliproxy had **no credential to route to**. So the question "what pacing
does the upstream tolerate" was the wrong question — the right one is "how much
traffic does this pool of accounts absorb before every one of them is cooling
down at once".

Measured 2026-10-02 on a **two-account** pool, same prompt and mode:

| gap between upstream calls | result |
|---|---|
| back-to-back (relay floor was 0.25 s) | **30/30 aborted runs failed** with 503 |
| 1 s | 8/8 succeeded, no retry needed |
| 2 s | 10/10 succeeded, no retry needed |

That set the original 1 s floor and the two-slot cap. **It does not generalise to
a larger pool**, so it was re-measured on 2026-10-03 against **three** accounts,
with aborted runs. Another client's traffic can only add load, so a clean result
understates the margin rather than overstating it.

| pacing | concurrent streams | runs | hard failures | runs that retried | seconds/run |
|---|---|---|---|---|---|
| 0.1 / 0.25 / 0.5 / 1.0 | 2 | 15 each | 0 | 0 | 2.8 |
| 1.0 | 3 | 24 | 0 | 0 | 2.11 |
| 1.0 | 4 | 24 | 0 | 0 | 1.54 |
| 1.0 | **6** | 24 | 0 | 0 | **1.23** |
| 1.0 | 8 | 24 | 0 | 0 | 1.23 |
| 0.5 / 0.25 / 0.1 | 6 | 24 each | 0 | 0 | 0.98 / 1.17 / 1.02 |
| 0.1 | 12 | 24 | 0 | 0 | 0.56 |
| 0.05 | 24 | 32 | 0 | 0 | 0.45 |
| 0.1 | 32 | 32 | 0 | 0 | 0.34 |
| **0.5** | **6** | **12 multi-segment** | **0** | **0** | 16.1 |

345 aborted runs, zero hard failures, zero retries — **no boundary was found**,
so the defaults moved to a documented margin rather than a measured floor. Two
things worth reading off the table:

- **A pacing floor only binds once `run_duration / concurrent_streams` drops
  below it.** At two concurrent streams every pacing value gave the same 2.8 s
  per run, including 0.1 s, because the slot was the constraint and the real
  inter-call gap was already ~2.8 s. The pacing sweep only became informative
  once concurrency was raised. A floor set by sweeping pacing at low concurrency
  measures nothing.
- **Throughput plateaus at six streams** (1.23 s per run, the same as eight),
  which is where six comes from — not from a failure threshold, which was never
  reached. Twelve and thirty-two concurrent streams were clean too.

The last row is the one that matters for the relay's real workload: twelve runs
on the engine's default multi-segment budget, each holding an upstream slot for
most of a minute, rather than five-second single-segment runs.

The relay therefore does three things, all configurable:

- **Pacing.** `pacing_seconds` is a per-process minimum gap between upstream calls,
  serialised across concurrent clients. Built in: **1.0**; **0.5** is the documented
  margin for a three-account pool (the calibration above). `0` disables it (an explicit `0` is a setting, not
  "unset": `lokki_core/config.py::_pick` tells the two apart); `RELAY_PACING` overrides it.
- **Concurrency cap.** `max_concurrent_upstream` is how many streams may be open at
  once; extra clients wait for a slot. Built in: **2**; **6** is where the measured
  throughput levels off (the calibration above). Pacing alone does not bound this, because several clients can each hold a
  live continuation.
  `RELAY_MAX_CONCURRENT` overrides it. Set it to the number of accounts to
  guarantee that each simultaneous stream goes to a distinct one. A client that reads
  slowly holds its slot for the whole stream: with the cap at 2, four slow readers
  stopped a test sweep from completing a single run in ninety minutes, and at 6 the
  same readers added about half again to each run. The 6 is the margin that keeps a
  few slow clients from taking the endpoint, not slack; a relay with no setting runs
  with the built-in 2.
- **Bounded retry.** `retries` (default 6) retries only a request that has
  produced **no output yet**, and only on 429/502/503, honouring `Retry-After`
  when the provider sends one and otherwise backing off exponentially (0.5 s,
  1 s, 2 s, capped at 8 s). Once any frame has reached the client the request is
  not retried, because a second attempt would deliver the answer twice.
  `retries: 1` disables retrying, and so does a per-request `retries: 0` where
  the knobs are honoured. Each run
  record carries `attempts`, `retries` and `last_status`, so a failed run says
  whether the budget was spent or the status was not retryable.

A retry budget of a few seconds does not outlast a cliproxy credential cooldown;
pacing is what prevents the cooldown in the first place. cliproxy's own
credential retry (`request-retry: 3`, `max-retry-interval: 30`) sits underneath
this and is not modified by the relay.

The numbers above are the record: the sweep that produced them is gone with the
rest of the research tooling and its raw dump was git-ignored by design, so
re-derive the table before citing it.

## Serving surface

The relay is the endpoint clients talk to; cliproxy is the upstream credential
manager and the management panel.

| path | auth | notes |
|---|---|---|
| `/healthz` | **none** | `{"status","uptime_s"}` — no upstream call, no config, no account, no client data, so a supervisor can probe cheaply. It is the liveness probe the five-minute watchdog uses |
| `/version` | operator token | revision, config fingerprint (including `allow_client_tuning`, so a record's `mode` can be read as operator policy or caller's request), the configured client names, uptime |
| `/v1/models` | token | passthrough |
| `/api/runs` | operator token | evidence tail, every client's records |
| `/api/chain` | operator token | the record chain walked and verified: head, `seq` range, and the first break if there is one |
| `POST /api/run`, `POST /v1/responses` | token | the two request shapes |
| `POST /v1/images/generations`, `POST /v1/images/edits` | token | passed through to the cliproxy's image routes, with the relay's credential, slots and a run record; no cut, no retry ([`images.md`](images.md)) |

The three views marked *operator token* answer 403 to a client token: they name
every client and carry every client's records, and a client token is one tenant's
credential. The legacy single token (`auth.token`, `RELAY_TOKEN`) is an operator.

**Cut policy is the operator's.** `/api/run` is the operator's own path and
honours the relay's own knobs (`mode`, `abort_after`, `seg_budget`, `total_cap`,
`max_segments`, `payload_cap`, `imagegen_cut`, `pad_tools`, `retries`). `/v1/responses` is the
client-facing path and **ignores** them: `mode` is forced to `segmented` and the
padding stays on, because a caller choosing `mode: control` bills the account and
`pad_tools: false` removes the abort surface the relay exists for. An
unrecognised `mode` falls back to `segmented` rather than erroring. The flag
`allow_client_tuning` (default **false**, `RELAY_ALLOW_CLIENT_TUNING=1` overrides)
re-opens them deliberately. Asserted at the upstream boundary — on the body the
relay actually sent — rather than only in `resolve_knobs`, and again against the
recorded run on the live build. Two of the knobs, `payload_cap` and `imagegen_cut`, are
also settings of `config.yaml` (and `RELAY_PAYLOAD_CAP`, `RELAY_IMAGEGEN_CUT`) since
2026-10-08: the operator's way to set them for every client without opening the others
(`images.md`, "Images in a Responses session").
Each record's `knobs` holds the values the run actually ran with (`seg_budget`,
`total_cap`, `max_segments`, `abort_after`, `payload_cap`, `imagegen_cut`,
`retries`). `config_fingerprint` names only the configured defaults, so a record
taken with per-request knobs is read against `knobs`, not against the fingerprint.
Where the knobs are honoured (`/api/run` with an operator token, or any path with
`allow_client_tuning`), a negative value for any of them is a 400: none has a
meaning below zero, and a negative budget used to be accepted and cut every
segment on its first delta. Where they are not honoured they are ignored, negative
or not, and the run uses the operator's policy.
The `relay.summary` frame a client receives is this record without the
operator's fields — `revision`, `config_fingerprint`, `knobs`, the trace path and
counters, and the chain's `seq`, `prev_hash` and `hash` (`seq` also counts every
other client's runs). They stay in the record and on `/api/runs`.

**The payload cap bounds what the client is given, not what it asked for.**
`seg_budget` and `total_cap` count answer *text*. An image result is a base64
field on its own item and counts toward neither, so on 2026-10-05 a
`image_generation_call` of 1.9–2.1M characters was delivered whole: the client
stored it, sent it back on every later turn, and 18.2M of that run's 22.2M upstream
characters were two such items. (The client's own rollout of a later session is
0.42MB with the cap against 4.6MB without it.) Above `payload_cap` (default 65536,
`0` disables) the field is replaced with a 1×1 PNG placeholder and the run record
carries `payload_capped` and `payload_chars_dropped`. Three properties are
deliberate:

- **it is not a cut.** The run continues, so the client still gets the answer that
  was being produced. Ending the turn would have cost the very output the cut
  policy exists to protect;
- **the item's lifecycle stays complete.** An item a strict client cannot commit
  makes it drop the turn's answer silently — the failure `stream_view` exists to
  prevent. The honest record is the run log and the item's untouched
  `revised_prompt`, not a status invented for it;
- **the whitelist is one entry** (`image_generation_call.result`).
  `custom_tool_call_output` is large and is also re-sent, but it is referenced by
  the call that produced it, and rewriting one is the bug that made the
  rebuild-and-flatten path uninhabitable (see `renamer.build_body`). A generated
  image references nothing.

**What the cap is worth, corrected the same day.** The first version of this
section priced the capped characters with the spend model and read ~36 of that
run's ~44 points out of them. That was wrong, and the measurement that shows it is
in the arms below: an **aborted dispatch is charged ~0.21 points whatever it
carries**. Growing a body from 137k to 201k cost 0 points over 3 dispatches; an
800k body sent twice more as pure cache reads (`/tmp/rate_big.py`, 3.0M cached
characters) moved the meter by 0; the warm arms measured 0.158–0.21 for a 136k
body; and R1's *nine* 2.1M-character dispatches leave no room for more than ~0.2
each once its images are priced. So capping 84% of a run's characters is worth
**~1–2 points, not ~36**. The cap is still right — it bounds what crosses the wire
and what the client stores — but the reason is not the meter.

Both delivery sites are capped, and there are two: the `output_item.done` frame
and `response.completed.response.output`. The second is built from `view.output()`,
which is why the cap is applied *before* the view observes the frame — capping only
the wire would hand the client back, in the terminal frame, exactly what the item
frame withheld.

Verified offline against that run's own frames, at zero quota: the upstream SSE was
replayed from the trace (`pid 243795`, `run 35`, the 11:25:46Z dispatch) with the
1,946,276-character result spliced back in from the item Codex's rollout recorded
for the same item id. The replay reproduces the real run's ending exactly
(`delivered_chars 106`, `cut toolcall`), and the cap turns **5,993,384 bytes
delivered into 154,838** — `payload_capped 2` (both sites), `payload_chars_dropped
3,892,368`, which is `2 × (1,946,276 - 92)`. The script is not committed: it reads
the trace and a rollout, and both are client content the repository does not carry.

The client side of the same question was tested with the real client, also at zero
quota: Codex CLI 0.160.0 against a second relay instance whose upstream was a stub
streaming a 200,000-character result. With the cap on, Codex's own rollout stores
the item with `result` at 92 characters and `status: completed`, exits 0 and
delivers the answer; with `DEFAULT_PAYLOAD_CAP` temporarily `0`, the same run
stores the full 200,000. The item's lifecycle being *complete* is what makes the
first column possible, and it is the reason the status is not rewritten.

**The image cut, and what the meter would not settle.** `imagegen_cut` (default
**true**; `false` for a client whose images matter, with `payload_cap: 0`, both in
`config.yaml` since 2026-10-08) ends the run at
`response.output_item.added` when the item is an `image_generation_call` — before
the image exists. Upstream's sequence for one is slow and fixed: `added` (the item,
no `result` yet) -> `in_progress` -> `generating` -> 60-140 seconds -> `completed`
-> `done` with a ~2M-character base64 `result`. The frame is delivered before the
cut, so the client is shown the item and `close_stream` closes it `incomplete`, the
same shape a cut client tool call gets; text produced before the image choice stays
delivered. The run record says `cut: imagegen`.

Measured live, on a second relay instance with the real provider behind it: the cut
fires (`cut: imagegen` in the record), the run ends in **31 seconds** rather than
the ~5 minutes the same prompt took when the image was generated, and the client
exits 0.

**Exiting 0 is not the same as delivering.** Two runs of the pelican command
through the production relay (2026-10-05, 94s and 48s, both `cut: imagegen`, both
exit 0) left the directory exactly as they found it, while the direct run of the
same command wrote `pelican-bicycle.svg` in 179 seconds; the relayed run's own
trace is `reasoning` and `custom_tool_call:exec` items and then the image item it
dies on, so the write never happens. A completed synthetic frame is enough to stop
the client, and the client does not know the turn was cut. A command whose
artifact has to land is therefore on `false`, and priced like the direct arm
(`cost-of-a-cut.md`).

**It is not priced in points, and this document will not pretend otherwise.** A
first version of this section claimed ~10 points a generated image, from a model
(`points ~= 0.21 x dispatch + ~10 x image`) that fitted nine measurements. The tenth
was a pair of pelican runs half an hour apart, each with one generated image, that
moved the meter **+12** and **+1** — and a cross-read of that pair found the reason
the pair proved nothing, which is worth recording because the mistake is easy to
repeat:

| | run A (14:51Z) | run C (15:50Z) |
| --- | --- | --- |
| dispatches / upstream requests | 7 / 7 | 4 / 9 |
| `gpt-6-astra` at `effort: xhigh` | **7 requests, 1,148,970 chars** | **3 requests, 347,213 chars** |
| `gpt-5.6-luna` at `effort: low` | 0 | **6 requests, 384,004 chars** |
| output deltas / encrypted reasoning | 11,792 / 29,804 chars | 10,298 / 34,664 chars |
| generated images | 1 | 1 |
| meter movement | **+12** | **+1** |

The two runs were compared on **total** characters (1.15M against 731k, "within
1.5x") while the spend model at issue prices uncached input and reasoning — and run
A put roughly **3.3x the `astra`/`xhigh` volume** through the account. Run C's bulk
was `luna` at `low`. So the +12/+1 split is explained by the *composition* of the
work, not by the image, and this pair was never the controlled experiment it was
presented as. The same review found the `+12` bracket is not even run A's own
number: there is no meter read between the 14:36Z turn (three dispatches, ~292k
characters, all `astra`) and run A's first dispatch at 14:51:51Z.

The withdrawal stands, and it stands on the stronger of the two grounds: not "two
identical runs disagreed" but "the pair was never equivalent, so it carried no
weight either way". No cost model for a real session is established, and the image
cut is justified by what it demonstrably does — it neither produces nor delivers a
2M-character artifact, and the run is ten times shorter — not by a points saving.

What *is* solid, because it was measured repeatedly and at two sizes: an aborted
dispatch is cheap and roughly flat. `rate_grow` (one body grown 137k -> 201k over
three dispatches, 510,054 characters) cost **0** and was still 0 after a 15-minute
settle; `rate_big` (a 750k body built in 150k steps, then sent twice more as pure
cache reads — 3,952,741 characters over seven dispatches) cost **0**, against the
~5.9 the per-token spend model predicts. Whatever a session costs, it is not its
characters: capping 84% of R1's characters is worth ~1-2 points, and the ~36 an
earlier reading attributed to them is withdrawn.

**One limit, stated rather than left to be found.** The cap covers an item's
payload field. It does not cover `response.image_generation_call.partial_image`
events, which carry their own base64 chunk when a client asks for partial images
(`partial_images` in the tool declaration). Codex does not ask — the run above
declares `n: 1`, `output_format: png` and no partial count — and a partial image is
not what the transcript keeps and re-sends, so it is not the loop this fixes. A
client that did ask would get those chunks uncapped.

**How the cap is set, and how it is turned off.** It has no `config.yaml` key (the
three cut budgets `seg_budget`, `total_cap` and `max_segments` have one, since
2026-10-07): the production value is `DEFAULT_PAYLOAD_CAP` in
`lokki_core/config.py`, and a request sets the knob on `/api/run` — or on
`/v1/responses` only under `allow_client_tuning`, since it is part of the cut
policy. An explicit `0` turns it off, and that `0` now survives `resolve_knobs`:
`int(raw or default)` used to collapse an integer zero into the default, so the
documented escape hatch was unreachable through the only path that sets the knob —
the same silent no-op as a config key `load_settings` does not read. The *budgets*
keep the opposite reading deliberately (`seg_budget: 0` resolves to the default),
because their zero means "no bound", which is the direction that leaks the answer
the relay exists to cut.

**The client is not told, deliberately.** No marker field on the item, no extra
frame: the item arrives complete with a 1x1 image, and only the run record and the
item's untouched `revised_prompt` carry the truth. The alternative — an extra
frame or field — would put a shape into a typed item that no client asked for, and
this relay's whole `stream_view` apparatus exists because a client that meets an
item it cannot commit drops the turn's answer. An operator
reads `payload_capped` in the record; a client is left with a blank image rather
than a broken turn.

**Limits**, each a property with a test rather than a claim about robustness:
listen backlog 128 (the stdlib default of 5 refuses clients under any real
concurrency), at most 64 concurrent client connections with an explicit 503 past
the cap, a 30-second per-connection socket timeout so an idle client cannot hold
a thread, and an 8 MB body cap answered 413 **before** the body is buffered. A
chunked upload (`Transfer-Encoding: chunked`, no `Content-Length`) is read up to
the same cap; it used to be answered "empty request body". A request carrying
both framings is refused 400, since two parsers can disagree on where its body
ends.

**Binding.** Every configured address is required: one that cannot be bound stops
startup and is named. Binding every interface (`0.0.0.0`, `::`, `:port`) is
refused unless `allow_public_bind: true` is set — this process forwards the
operator's upstream credential, and the protocol reserves that exposure to the
Human. The old code only warned, which is how a typo could publish it.

**Two different questions at the supervisor.** `/healthz` answering 200 means
healthy; *something listening* means bound. `relay-up.sh` starts only when
nothing is bound, and warns when something is bound but not healthy — the
pre-Slice-1 build answers 401 on `/healthz`, so a probe that started on "not
healthy" would have spawned a second relay every five minutes into a failing
bind with nothing said. The path is compared after `urlsplit` and trailing-slash
normalisation, because a monitor that appends `?ts=…` must not get a 401 that
reads as "down".

**Errors.** Non-stream failures return the OpenAI envelope
`{"error": {message, type, param, code}}`; `type` is a value clients actually
switch on. A failure **after** the first byte cannot carry a status, so the
engine emits an SSE `error` event in the same shape before closing; its own
`relay.error` frame stays for its tooling. The payload carries a top-level `type`
alongside the nested envelope: the Responses API dispatches stream events on
`type`, so a bare `{"error": …}` is dropped by a generated SDK. It also carries a
`sequence_number` like every frame around it, so a client that validates
monotonicity does not drop the frame that says what went wrong; the handler's
last-resort in-band error, which fires where no stream was ever numbered, is the
one place without one, deliberately. Error responses
send `Connection: close`, because on a 413 the body was never read and leaving
those bytes on a keep-alive socket makes the next request be parsed as if they
were its request line.

**The client going away is not an upstream fault.** A disconnect is recorded as
`cut: client_gone` with `error: null`, and the slot is released. It must not be recorded as `error`: that
makes every abandoned request look like a relay failure.

**Evidence carries counts, not content.** `attribution.items[*].content[*]` is not
copied into the committed record: it is upstream-controlled free text that holds only
token counts today, so one provider change would otherwise put a client's prompt or
tool arguments into git — and the negative test over the request body cannot catch
that.
`core.summarise_usage` whitelists the counts and item ids instead. Records written
before this keep the old shape.

**Supervision of the earlier pair (`:8371`) is cron**, and the Human owns the entries (protocol: "Human must
decide ... the cron entries that supervise the relay"; the live pair on `:8381` has none, `live-8381.md`):
`relay-up.sh` takes a lock, probes `/healthz` at each configured `host:port` (a
wildcard host on loopback), and starts the relay only if it is not answering.
Probing 127.0.0.1 alone read a relay bound only to its Tailscale address as
"nothing bound". It is idempotent and safe to run concurrently. The five-minute
watchdog **is** the restart mechanism — a crash is repaired up to five minutes
late, which is the accepted cost of choosing cron over a systemd unit.

## Client identity and the reload

One shared token meant no attribution and no revocation that did not mean
restarting every client, so `auth.tokens` is now a list of `{name, token}`. The
token that matched decides the `client` field in the run record. The older
`auth.token` still works and is named `default`. Two names may **not** share one
token: that is one identity wearing two labels, and the relay refuses to start on
it. `RELAY_TOKENS` (a JSON list) carries the same thing from the environment.

`SIGHUP` re-reads the configuration in place. A reload that does not parse, or
that would leave *no* client at all, is refused and the previous list stays live —
a serving process must not be able to revoke everyone by accident. The token list
is swapped in a single assignment, so no request can authorise against a
half-updated set. `/version` lists the live names, which is how "it took effect"
is checked on the running process. It needs an operator token; a client checking
its own new token reads the status instead — 403 from `/version` means the token
is live, 401 means it is not:

    kill -HUP $(pgrep -f 'server.py --config .*core/config.yaml')

## Serving a real client: the Codex CLI contract

A Codex CLI 0.160.0 client was pointed at this relay in 2026-10, and its diagnostic
report (four findings: the stream shape, a resumed turn, dropped fields, and the
usage object) is what these rules answer. The mechanism is unchanged — the abort is
still the product — but the bytes a strict client needs are now the bytes it gets.

**The stream is closed, not just ended.** Upstream never closes what it opened,
because it never gets to `response.completed`; the relay used to forward the open
items and synthesize a terminal frame that described nothing, and Codex exited 0
with no answer. Now `lokki_core/stream_view.py` holds what the client was actually
shown — delivered frames only, never upstream's held-back buffer — and closes it:
`response.output_text.done`, `response.content_part.done` and
`response.output_item.done` per open item, at every segment boundary and on every
terminal path. `response.created` is forwarded exactly once, and
`response.completed.response.output` always lists the items the client saw. The
`sequence_number` and `output_index` a client sees are contiguous, because a frame
that is withheld takes no number with it.

**An item id is the provider's.** The relay does not renumber item ids; it renumbers
`output_index` and `sequence_number`, which nothing outside the stream is bound to.
A reasoning item's `encrypted_content` is verified by the provider against the id it
is replayed under, so an invented id (`item_0`) is rejected on the next turn as
`thinking_signature_invalid`. That was the resumed-turn failure.

**A client's tool call is delivered whole, and the abort lands after it.** A call to
the relay's own padding tool (`discarded_placeholder`) exists only because the relay
injected it — the client never offered that tool — so it is withheld and counted (`hidden_pad_calls`), and
*it* is where the `toolpad` cut fires: at `pad_abort_chars` characters of its arguments.
The injected instruction names that tool unconditionally, so a request that carries its
own function tools (`pads_with_dummy` false, no dummy injected) is told to call a tool
it was given no definition for. The model ignores it in practice and the abort falls
back to the marker; it is stated here because the instruction text assumes the dummy
path.
The tool is the relay's only when the relay *injected* it, which happens only for a
request that carries no function tool of its own; a client that defines a tool by that
name keeps it.

A call to a **client's** tool used to be treated the same way, and that was the defect
that made a tool-using client impossible: counting every call's arguments toward
`toolpad` truncated a real `exec` at 50 characters in every segment, so the model
re-issued the call, all six segments were burned, no text was delivered, and the tool
never ran. The relay now delivers a client's call whole — arguments, `*.done`,
`output_item.done` — and aborts the moment it is complete, with `cut: toolcall`. The
client gets a call it can run, and `usage` still never finalizes because the provider's
`response.completed` is never read. A client call that *is* cut mid-arguments (the
segment budget, the total cap, a marker) is closed `status: "incomplete"` and counted
(`cut_tool_calls`), never presented as a finished call.

**Synthetic usage is an estimate and says so.** Upstream reports no usage before
`response.completed`, and the relay never reaches it, so there is nothing to measure.
`input_tokens` is estimated from the size of the last request the relay sent upstream
and `output_tokens` from the characters delivered — the same `characters // 4` rule
for both. On a real Codex-shaped request the estimate ran +22% against the provider's
own count. The response carries `synthetic: true`, and the run record carries
`usage_source` — `"synthetic-estimate"` for a synthesized completion, `"provider"`
when one of the provider's own terminal frames carried usage (a `response.failed`
included, with `real_completed: false`), and `null` otherwise — a provider terminal
without usage, or no completion at all — so the label never describes a usage
object that does not exist. It also carries
`upstream_input_chars` (the estimate's basis: the last request the relay sent) and
`upstream_input_chars_total` (what the account was asked to read across the whole
run), which are two different facts and so are two different fields. The
`*_tokens_details` objects are omitted because the relay does not know the
cached/reasoning split and must not assert a zero for it. A client that needs
authoritative accounting must use a lane that does not abort.

**A provider rejection keeps its reason.** On an upstream `HTTPError` the relay reads
a bounded part of the response body: `error.code` and `error.type` are recorded when
they are identifier-shaped, and the provider's message goes to the *client* in the
`error` event. The record keeps identifiers, the client gets the diagnosis, and
`rec["error"]` reads `HTTPError 400: thinking_signature_invalid` instead of
`HTTP Error 400: Bad Request`. Before this, finding the cause of a rejected request
needed a proxy between the relay and cliproxy.

**One item per answer, since 2026-10-07 (`merge_text`, on by default).** A text answer
that a segment budget continues used to arrive as one output item per segment, the later
ones starting mid-word. Now the message a budget cut leaves open stays open. The
continuation's own message, and the reasoning item a provider that reasons sends before
every answer, are withheld; its deltas go out on the first message's id and index; and
the message is closed once, by `stream_view`, with the text of every segment. Anything
else that opens while it is held (a client's tool call, or a message once the
continuation's own has closed) closes it first, so the client never holds two open items. If the provider opens another item while
the continuation's message is still open (none has been seen to), the merge cannot follow it,
so the run ends there with an error the client is told: what was received is delivered, a
trailing marker prefix of two characters or more is not, and nothing after it is read, so
the provider's terminal frame is not. If that error ever appears in a real run record
(`the provider opened an item while a continuation's message was still open`), reopen the
question with the observed ordering in hand: ending the run is the choice for an order
nobody has seen, not a verdict on one that occurs. With `merge_text: false` the old shape
returns. A budget cut also lands after the last whitespace the relay holds, so a
continuation starts a new word; the partial word is dropped from what the client gets
and from the tail the continuation is given, and the model writes it again. Text with
no whitespace to cut at (CJK, base64) is cut where it stands after `SEAM_LOOKAHEAD` (200)
more characters. Measured against real Codex 0.160.1 and the real provider, one command
at `seg_budget` 300: one message from three upstream requests, `merged_segments` 2.

**An answer the relay ends at its limit says so.** The run ends at whichever comes first
of `total_cap` (8,000 characters by default) and `max_segments` × `seg_budget`, whatever
the model has left to say. The message is then closed `status: incomplete` and ends with
`[relay: this answer was cut at its length limit (N characters)]`, and the run record has
`truncated: true`. The response itself stays `completed` with `synthetic: true`: a
`response.incomplete` was tried against Codex and is read as a stream error (five
reconnects, nine relay runs for one command, then failure).
The notice is in the answer text, so a client that keeps the message is expected to send
it back with its next turn, and whether it does, and whether Codex or the provider
accepts a replayed `incomplete` message, no run has looked at. The recorded offline simulation results used a fake provider that
continues perfectly (`core/experiments/longanswer_sim.py`, removed in `21f14bc9d33c4966831f6fdac6348ac831008518`, no quota);
at `seg_budget` 4000:
5,000 characters is one message and complete; 9,000 is one message that stops at about
8,160 characters and says so (that figure comes from the simulation's 40-character deltas
and the 128-character hold-back window, so real deltas would stop a little nearer 8,130).
At `seg_budget` 1000 it stops at about 5,999, on `max_segments`. While a message is held
open the client is sent nothing between segments, where the old closing frames wrote
something, and the continuation's annotation frames go with the rest of its message.

## Debugging: the diagnostic trace

The run record is the evidence file — counts, identifiers, timings, and nothing the
client or the provider wrote. That shape is right for evidence and wrong for
diagnosis: establishing why a resumed Codex turn was rejected needed a hand-built
proxy between this relay and cliproxy, and the investigation that found the
tool-call defect ran on a scratch port no client used.

`lokki_core/trace.py` is that proxy, built in.

* **Off unless `debug_log:` names a path** (or `RELAY_DEBUG_LOG` does — the
  environment wins, so an operator can trace *this* deployment without editing the
  config). With no path every call is a no-op. It is a startup setting like
  `runs_log`, never applied on `SIGHUP`: a trace that moved mid-stream would split
  one run's frames across two files.
* **What it writes** is one JSON object per line: every SSE frame in both
  directions (`dir: up|down`), the frames the relay deliberately did *not* send
  (`dir: skipped`, with the reason), the body it sent upstream (`dir: request`),
  the provider's error body (`dir: provider_error`), and a `run_start`/`run_end`
  pair. Every line carries a `run` id, which is what turns a few hundred lines into
  an answer.
* **It is not an evidence file.** Nothing reads it back: the chain verifier and
  the tests read `runs.jsonl`. It carries what local law bars from
  committed files, so the path is refused inside the repository by the same guard
  as `runs_log` and `capture_dir`, the file is created 0600, and it is a
  measurement taken while investigating and deleted afterwards.
* **It can never break the run.** Every write is best-effort; a failure is counted
  (`trace_errors`) rather than raised, and a path that cannot be opened refuses
  startup rather than serving silently untraced.

Read one run's frames:

    grep '"run": 7,' ~/.local/state/cliproxy/debug.jsonl | python3 -m json.tool --json-lines

## What reaches upstream, and what does not

- **Parameters (W1.3).** `temperature`, `top_p`, `max_output_tokens`, `reasoning`,
  `parallel_tool_calls`, `metadata`, `truncation`, `prompt_cache_key`, `include`,
  `text`, `client_metadata` and `tool_choice` are forwarded. The last four are what
  a real Codex CLI client sends and this relay used to drop: recording a drop in
  `dropped_params` is evidence, not compatibility. Everything else the caller sent
  is named there — names only, never values. `model` and `stream` are the relay's:
  they are set *after* the caller's parameters, so they cannot be overridden even
  by a name that slipped into the list. `client_metadata` carries session and
  installation ids, so forwarding it is a deliberate, named crossing: the client
  identifies itself upstream, exactly as it does when it talks to the provider
  directly.
- **`store` is the relay's, and `true` is refused.** The relay sends
  `store: false` because it cuts the stream before the response is finalized, so
  there is nothing to store. A caller sending `false` agrees with that; a caller
  sending `true` gets a 400 naming the reason, because sending `false` instead
  would give it the opposite of what it asked for and let it discover that by
  chaining onto a response that does not exist.
- **`previous_response_id` is refused (W1.4)** with `invalid_request_error` and a
  message naming the fix. Two independent reasons it cannot work here: the relay
  sets `store: false` upstream, and it cuts the stream before the response is
  finalized — so the id points at an aborted response or at the relay's own
  synthetic one, neither of which exists upstream. Accepting it would give the
  model a single turn with no context and return a confidently wrong answer.
  **This forces inline history, which is a client-side change.**
  The refusal is a setting (`reject_previous_response_id`, default **true**), and
  `false` is the *dangerous* state rather than a safe one — it restores the silent
  wrong answer. It exists so an operator can back out during an incident with a
  `SIGHUP` instead of a redeploy. Whichever way it is set, the record says whether
  a request carried the field (`previous_response_id_seen`), and a refusal is
  logged to the relay's stderr as `refused previous_response_id from client=<name>`
  — a refused request never reaches the run log, so that line and the client's own
  400 are the only places the source can be found.
- **Client headers (W1.5).** `OpenAI-Beta`, `originator`, `session_id` and
  `chatgpt-account-id` are forwarded from an allow-list. `Authorization` never is:
  that header carries the *relay's* upstream credential. The list is the plan's
  named set. A capture of Codex 0.160.1 shows what it
  sends: `originator`, `session-id`, `thread-id`, `x-client-request-id`,
  `x-codex-window-id`, `x-codex-turn-metadata`, `x-codex-beta-features` and
  `x-openai-internal-codex-responses-lite`, with hyphens. Of the four named here only
  `originator` ever arrives from it, and the relay's requests carry no session id at
  all unless `session_pin` is on.
- **`session_pin`** (default **false**; `session_pin:` or `RELAY_SESSION_PIN`; never a
  request knob) makes the relay send a `session-id` header of its own on every request
  it makes upstream: one constant per client, a UUIDv5 of the client's name, so the same
  for a client's every session and different between clients. Of Codex's per-session
  headers the provider's prompt cache followed that one and no other (one
  model, one account, one prefix measured), so with it a client's new session can read the prefix an
  earlier one wrote, for as long as the entry lasts (60 minutes was the longest measured),
  and without it the relay sends none. The same name on two relays in front of one account
  gets the same id. The price is the one the experiment names:
  one client's sessions then look to the provider like one session, and share cache
  entries with each other. It is a flag in every run record's knobs (`session_pin`) and
  in the config fingerprint; the id is never recorded. Turning it on is the operator's
  decision: nothing in the repository sets it. Real pelican sessions through the live relay with it on cost about
  a third less on the meter and ran about 5 seconds longer, in one order of blocks.
- **The continuation tail is a setting (W1.6).** `tail_chars` (default 1500) is
  what a continuation carries of the answer so far; it rides in the config
  fingerprint, so two records taken with different tails are comparable.
- **The cut budgets are settings.** `seg_budget` (default 1500), `total_cap` (8000)
  and `max_segments` (6) can be named in `config.yaml` or in `RELAY_SEG_BUDGET`,
  `RELAY_TOTAL_CAP` and `RELAY_MAX_SEGMENTS`. Each is at least 1, `total_cap` may not
  be below `seg_budget`, and a `SIGHUP` re-reads them for the runs that start after
  it. They ride in `/version` and in every run record's knobs, so a record says
  what it ran with. Naming none changes nothing.
- **`merge_text`** (default true; `merge_text:` or `RELAY_MERGE_TEXT`, and a request
  knob under `allow_client_tuning`) keeps a budget-continued text answer one message
  for the client; false returns the one-message-per-segment shape. It is a setting
  because it changes the stream a strict client reads, and it is in the knobs of every
  record, with `merged_segments` and `truncated` beside them. See "One item per answer"
  in "Serving a real client".
- **`text_calls`** (default **true**; `text_calls: false` or `RELAY_TEXT_CALLS=0` turns it off, a typo reads as off, and it is
  a request knob under `allow_client_tuning`). On, the first request of a turn asks the model to write a call to
  `exec` as text between a line `<<<CALL exec>>>` and a line `<<<END>>>` (any other reply keeps the marker
  shape); the relay reads the text as it streams, closes the message the call came in, hands the client the
  `exec` call (a `custom_tool_call` the relay builds, with ids of its own) and ends the request there, in
  an unfinished message, with the cut `textcall`. The provider does not finalize that request, which is the
  point; like any cut request it is billed for the prefix it had read.
  Its limits:
  - the call is always built as a custom tool call (what `exec` is in Codex 0.161.0), without looking at the
    client's tool list, so a client whose `exec` is another kind of tool gets a call it cannot match;
  - the first line `<<<END>>>` in the input ends the call, so an input with such a line of its own is cut short;
  - the first answer at high effort comes later ([`latency.md`](latency.md));
  - with `pad_tools` off the model is not asked for the format and the setting does nothing.

  What looked like a call but was not (the marker first, a
  message that ended without the closing line, a call over 250,000 characters) is delivered as text and
  counted in `text_call_fallbacks`. Only `exec` is read; the model's calls to any other tool pass through
  and bill as before. Off, nothing about the relay changes. It is in the knobs of every record, with
  `text_call` (the tool) and `text_call_chars` (the length of its input).
  It is on unless a file or the environment turns it off. A deployment that never named it sends the instruction, and its
  `config_fingerprint`, which holds `text_calls`, differs from that of a deployment that turns it off;
  a deployment that sets it to `true`, as the live pair does, has the fingerprint of one that never named it.
- **`text_calls_pad`** (default **true**; `text_calls_pad:` or `RELAY_TEXT_CALLS_PAD`, strict like `imagegen_cut`: only a clear
  true or false, a typo is an error; and a request knob under `allow_client_tuning`) applies only with `text_calls` on. True is the request
  as it was: the relay's padding tool, and the filler section after the marker. False sends neither: no `discarded_placeholder`
  tool, the second reply shape asks for the answer and the marker alone, and the caller's own tools go through as they are.
  Text calls, the marker cut, the cut at a finished native call and the cut of a closed structured answer (`itemdone`) are all
  unchanged. What goes is the abort window for an answer without the marker (the `toolpad` cut): such an answer reaches the
  provider's `response.completed` and is billed. Why one would turn it off, and what was measured: `latency.md`. It is in the
  knobs of every record (`text_calls_pad`), in the configuration fingerprint, and the record's `pad_tools` says whether the
  tool was in the request. In this mode a call the model makes to a tool named `discarded_placeholder` is an ordinary call (the
  tool is not offered, so only an invented one), and the `itemdone` cut stays off a request whose caller sent its own tools.

## The evidence chain, crash-safety and rotation

Every record now carries `seq`, `prev_hash` and `hash`. `GET /api/chain` walks the
chain and reports the head and the first break.

What it detects, stated for what it is: an edited record, an inserted record, a
deleted record, a replaced file, and a half-written line. What it does **not**
detect: truncation of the tail. Nothing inside the file remembers how long it was,
and a verifier cannot tell a rotated-away head from a deleted one — that is what
an off-box anchor is for. This repository keeps **no off-box anchor**: the head is
verified (`/api/chain`, `core.verify_chain`) and named in this host's log, which is
on the same disk, so a writer who rewrites the whole file can recompute the whole
chain and leave nothing behind that contradicts it. A signed or append-only medium
would be stronger and remains a decision.

Appends are flushed and `fsync`ed before the lock is released, so a crash leaves
complete records or none. `runs.jsonl` rotates at 8 MB keeping 5 files, and the
first record of the new file links to the last hash of the old one, so the chain
is continuous across the seam (`verify_chain` reads `<path>.N … .1, path` in
order).

## Restart and drain

`SIGTERM` and `SIGINT` now refuse new work (503 with `Retry-After: 5`), let the
streams already running finish inside `drain_seconds` (default 15), then exit. A
second signal exits immediately. The bound matters as much as the drain: an
unbounded graceful shutdown hangs a deploy on one stuck client, so a stream that
outlives the window is logged and cut with the count of what was cut.
`restart-relay.sh` waits longer than the drain before it escalates to `SIGKILL`.

## Token roles and knob rights

`allow_client_tuning: false` protected `/v1/responses` while the operator's own
`/api/run` honoured the relay's knobs **for any authenticated token** — so a client
could set `mode: control` (which bills) and `pad_tools: false` (which removes the
abort surface) by posting to the operator's path. Tokens carry a role now:

    auth:
      tokens:
        - {name: jevchat, token: "...", operator: false}
        - {name: me,      token: "...", operator: true}

A named client is a client unless it says otherwise. The legacy single
`auth.token` stays an **operator**, because it is the operator's own secret and
demoting it on upgrade would lock them out of their own path. `/version` lists
`clients` and `operators`, the meter oracle prefers an operator token so an arm
driven through `/api/run` keeps the budgets it asks for (and says which role it
got, so a silent downgrade is visible), and `allow_client_tuning: true` still
re-opens the knobs to everyone.

## Request handling details

- `/api/runs` reads the run log's **tail** rather than parsing the whole history
  per call, capped at 4 MB; `_last_read_lines` makes the bound assertable, because
  the result is identical either way and only the work distinguishes them.
- A **string `input` stays a string** across a continuation; wrapping it in a message list would make
  segment 2 a different kind of request from segment 1 for anyone comparing shapes.
- **POST paths are normalised** the way GET already was, so `?api-version=` and a
  trailing slash answer instead of 404ing.
- A **disconnect between segments is already classified correctly** — the review
  named that window, and the guard test pins both the classification and that the
  reset really lands at the boundary, so it cannot quietly stop testing it.

## Capturing a real client

A round trip on a real client body needs a real request, and a real request is
client content. The relay
writes it only when `capture_dir` is configured — off by default, mode 0600, one
file per request, path logged — and **refuses a directory inside this repository**,
which is structural rather than a note in a doc: a captured body must never be
`git add`-able. Use it for a measurement, then delete the files.

    capture_dir: ~/.local/state/cliproxy/captures

## Staged rollout

A second instance runs on its own port, with its own config (0600, outside the
repo) and its own run log. It is a second relay, started and stopped the way the
first one is.

**The second instance gets its own run log**, and that is not a detail: a record's
`seq` and `prev_hash` are computed from the last line of the run log, so two relays
sharing one would interleave sequence numbers and break the chain they exist to
make verifiable. The relay **refuses to start** if another process holds the log's lock
(`<run log>.lock`, taken at startup and held for life): two writers would corrupt the
chain silently, and it would be detected only afterwards. A staged rollout therefore produces two evidence populations — the
honest shape anyway, since you can tell which port a record came from by which file
it is in.

## Where the run log lives, and the guard

The relay must not append to a tracked file: a `checkout`, `stash` or `clean` then
drops records behind a running process with nothing said, and a `git add` commits
client-run evidence.

`runs_log` is a resolved setting, defaulting to
`$XDG_STATE_HOME/cliproxy/runs.jsonl` (`~/.local/state/cliproxy/runs.jsonl`). The
precedence is `RELAY_RUNS_LOG`, then `runs_log:` in the config, then that default —
outside the checkout in all three cases, because a path *inside* it is refused:

| Where the path can come from | Refused if it is in the checkout? |
|---|---|
| `runs_log:` in config.yaml | yes, at startup |
| `RELAY_RUNS_LOG` in the environment | yes, at startup — a guard that only covers the config file is a guard the next deployment walks around |
| the default | not applicable; it is outside by construction |
| `core.RUNS_LOG` set directly | yes, **at the write**: `log_run` refuses and the request answers 500 naming the reason |

The last row is the backstop, and the only one that does not depend on a
configuration being right. It is loud rather than silent by design: a record that
cannot be written honestly must not be written somewhere else instead. The check is
one shared function (`core.refuse_inside_repo`), used by `capture_dir` too — the
swe-2 reader had found the symlink hole in the `capture_dir` copy of that logic,
and two copies is where the next hole would hide.

Two smaller consequences, both stated because they were choices:

- **`runs_log` is a startup setting, not a reload setting.** `apply_settings` runs
  again on every SIGHUP, and moving a *serving* process's run log mid-stream would
  split one chain across two files — the records before the reload in one, after it
  in another — while each file still looks continuous from the inside. A reload
  that changes it says `takes effect at the next start` rather than obeying.
- **A config that names no run log does not move the process's log.** Otherwise
  `apply_settings` would reach into a caller that chose its own path on purpose:
  the test suite passes `RELAY_RUNS_LOG`.

`config.RUNS_LOG` is the running path rather than a copy taken at import (a module
`__getattr__`): two answers to "which file?" is the shape this codebase treats as a
defect.

The startup banner names the file:

```
relay listening on http://127.0.0.1:8371 ... clients=default runs=/home/<user>/.local/state/cliproxy/runs.jsonl config=...
```

## The committed seed

`core/runs.jsonl` stays committed at the byte-for-byte state it had when
the relay stopped writing to it. Its own chain still verifies, and a reader may
read it, which is why the guard is about *writing*
inside the checkout rather than about reading there.

## What the run-log guard does not cover

- **A bind mount** of the checkout elsewhere is invisible to path inspection and to
  `st_nlink`. It needs root, and with root the checkout is not the boundary that
  matters.
- **A TOCTOU swap** between the check and the `open` would need `O_NOFOLLOW` and
  fd-relative checks; out of scope for a self-hosted relay.
- **A hard link** is refused at the write (`st_nlink > 1`): a link at a path outside
  the checkout reaches an inode inside it, which no path check can see. Nothing
  legitimate hard-links an evidence file.
- **A refusal is a sentence and exit 2**, from the config file or the environment —
  including when it is found while the module is imported, where the import
  remembers it and `ensure_settings` raises it so `main()` can print
  `refusing to start: …`.

## Exposure, and what is open

**The publish as it stands.** `docker-compose.yml` publishes cliproxy on `0.0.0.0:8317`,
so its management panel, management API and client API answer on every interface, sharing
one port behind one password. Measured after the change: `<lan-addr>:8317` answers HTTP
200 where it was connection-refused, so the direct path is reachable from the LAN and not
only the tailnet. The relay is unaffected: it calls `127.0.0.1:8317`.

**Narrowing it**, without changing the publish, is one `DOCKER-USER` rule — a `ufw`/INPUT
rule never sees the traffic, because Docker DNATs and it traverses `FORWARD`. That rule
does not survive a reboot here. Dropping the Tailscale publish instead costs a few seconds
of upstream downtime on `docker compose up -d` and moves the panel behind
`ssh -N -L 8317:127.0.0.1:8317`.

**Open by decision, not oversight:**

- **The earlier pair's cliproxy access key has never been rotated.** It is the only secret value ever
  committed to the repository this one was copied from (17 historical versions of the since-untracked
  `config.yaml`; this repository starts from one commit and does not carry them, the old repository and
  its backup do), it is still live, and it is the sole entry in cliproxy's `access.api-keys`. With the
  publish above, anyone who has that history *and* can reach the LAN uses the accounts. Rotating it takes
  minutes and needs no history rewrite.
- **The direct path itself is accepted and widened**, not closed.
- **The evidence chain has no off-box anchor.** The head appears only in this host's
  log, so a rewrite of the whole file is not visible against anything off-box. A signed
  or append-only medium would be stronger, and upgrading is a decision.
- **`allow_public_bind` stays false** unless the Human says otherwise: a wildcard bind is
  refused at startup, not warned about.
- **The real-client work is open** — whether the client sends `previous_response_id`, one
  captured request to finalise the forwarded-header list, and the client-by-client move.

## What is not guaranteed

State, not a to-do list: these are true of the relay as it runs, and each is here
because nothing else in the repository states it.

- **No request log.** `Handler.log_message` is suppressed, so the relay keeps no
  record of who called it. The run record attributes a **token name**, not a caller
  address or a request line; cliproxy's own log is the only place a request appears.
- **`/api/run` takes a plain prompt.** Its `prompt` field goes through
  `extract_prompt`, which flattens the conversation to text, so roles and non-text
  items are lost there. The client-facing `/v1/responses` path forwards `input`
  verbatim — `/api/run` is a measurement shape, not the production one.
- **Only allow-listed parameters reach upstream**, from the caller's body:
  `renamer.FORWARDED_PARAMS` is the list, and it is the home of that fact.
- **A continuation carries a bounded tail** (`tail_chars`), not the whole answer. Since
  2026-10-07 a text answer a budget continues is one message item (`merge_text`; see "One
  item per answer" in "Serving a real client"); with `merge_text: false`, or when anything
  other than the continuation's message opens, each segment's item is closed before the next
  opens one, so the split is well-formed — not, as it used to be, an item left open for
  ever behind a terminal frame that described no output.

If this code is ever repurposed as a general-purpose relay — rather than this
operator's endpoint — the audit trail and `/api/run`'s flattening are the two
things to re-assess first.

## Notes for review

- The last 128 chars of streamed text are never flushed to the client until
  safe; the marker line and the padded appendix never leak.
- Continuations carry only the last `TAIL_CHARS` (1500) of the transcript, not
  the whole accumulated answer; `tail_chars` in each run record shows what was
  actually sent per segment.
- cliproxy logs every aborted request as a normal `200`.
