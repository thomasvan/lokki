# docs/ — index

This directory is the operational and measurement record of the relay, written for whoever runs or
reviews it.

The code lives in `core/` at the repository root; its operational
entry points are documented in [`relay/relay-8371.md`](relay/relay-8371.md). What the
relay is for, what it changes in a request and what it does not do is in the
repository's [`README.md`](../README.md). In these documents `<tailscale-addr>`, `<lan-addr>`, `<host>` and
`<user>` stand for this machine's Tailscale address, a LAN address, its host name and its user name; the address is
kept out of the tree (`deploy/.env`, see [`live-8381.md`](relay/live-8381.md)).

## relay/

- [`live-8381.md`](relay/live-8381.md) — the live pair (relay `:8381`, cliproxy `:8318`): what is in force, how it is kept
  running, how to change or roll it back, how to point a client at it.
- [`relay-8371.md`](relay/relay-8371.md) — the relay's behaviour: architecture, configuration, the modes, the billing
  mechanism, the run log and its guard, and what is not guaranteed.
- [`cost-of-a-cut.md`](relay/cost-of-a-cut.md) — what a cut costs and saves, arm by arm.
- [`latency.md`](relay/latency.md) — why a relayed turn is slower, and what was changed.
- [`usage-oracle.md`](relay/usage-oracle.md) — the token-side oracle and what it cannot see.
- [`docker.md`](relay/docker.md) — the relay as a container.
- [`images.md`](relay/images.md) — the image routes.

## app/

- [`first-run.md`](app/first-run.md) — first start, local data, accounts, the tray, errors and reset.
- [`api.md`](app/api.md) — core routes, roles, requests, responses and status codes.
- [`ui.md`](app/ui.md) — UI development, views, shell contract, build and checks.
- [`data-directory.md`](app/data-directory.md) — files created by `init` and their owners.
- [`limits.md`](app/limits.md) — what the app depends on that it does not control, and what it does when that changes.
- [`update.md`](app/update.md) — when the app checks for a release, what an install does, and when it offers a download.

## What a claim in these documents has to carry

`WORKSPACE_PROTOCOL.md` governs this repository, and the useful part for a reader
of these documents is its proof rule: every material claim names the shape of
proof that can falsify it. Concretely, in this directory that means:

- A **run number** is traceable to a committed record in
  `core/runs.jsonl` or `core/e2e_evidence.jsonl`, and names
  the revision that produced it. `e2e_evidence.jsonl` carries the revision per
  record; `runs.jsonl` does not, so it is cited alongside the doc's timestamp.
  `runs.jsonl` is the **frozen seed**: the running relay writes to
  `$XDG_STATE_HOME/cliproxy/runs.jsonl` instead, outside this repository, so a run
  recorded after the handover is not in the committed file by design.
- A **billing claim** names the meter reading it rests on. The relay's own
  `verdict: usage-discarded` field means "no finalized terminal frame arrived" — the
  precondition for the cut, not proof that the request was not charged. The
  observable is the upstream's used-percent, read before and after the traffic,
  with `core/billing_probe.py read --record`, which appends to
  `billing_reads.jsonl` under a label — that is how a delayed re-read is captured
  rather than transcribed by hand. `billing_ab.jsonl` holds the arms taken while
  the arm runner existed, with emails redacted to `acct-N`; a dashboard observation
  by the Human is still the independent check. State the resolution:
  `used_percent` is an integer, so a claim of "0%" is a claim of *less than one
  percent* over the number of requests that were sent, never a claim of literal
  zero. A delta is only attributable if the window is clean: cliproxy's
  `logs/main.log` shows every request on the pool, including other clients', and
  a foreign completion inside the bracket moves the meter just as this relay's
  would.
- A **behavioural claim about the engine** is backed by a test that fails when
  the behaviour is absent. The offline suites (`test_offline.py`,
  `test_engine.py`, `test_evidence.py`, `test_lifecycle.py`) reach no upstream and
  spend no quota.
