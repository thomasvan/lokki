<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/design/ux/wordmark-dark.svg">
    <img src="docs/design/ux/wordmark-light.svg" alt="Lokki" width="265" height="100">
  </picture>
</h1>

*Lokki* is the product name; the relay inside it keeps its codename *midstream-abort* (the documents use the long name).
A desktop app around the relay is being designed:
`docs/superpowers/specs/2026-10-10-lokki-app-design.md`.

A relay that makes long Codex turns cost less. It sits between a Codex client and CLIProxyAPI (the
`eceasy/cli-proxy-api` image), and, where it can, ends each upstream request before the provider finalizes
it, once the part the client needs has arrived.

It does not make turns free. A request the relay cuts is still charged, for the cached prefix the provider
had already read. The measurements, with their limits, say how much cheaper that is than a request left to
finish: [`docs/relay/cost-of-a-cut.md`](docs/relay/cost-of-a-cut.md). The price is time and a visible
difference in what the client gets, below.

## How it saves, and what that depends on

A request the provider finalizes is charged for its uncached input and its output. The relay is built
around one observation: a request that is cut before anything in it finalizes is charged much less, for its
cached prefix and a small fraction of the rest ([`cost-of-a-cut.md`](docs/relay/cost-of-a-cut.md)).

So the relay keeps a request unfinished on purpose where it can. It adds an instruction asking the model to follow its
answer with a marker line, then an "appendix" of filler text, then one call to a placeholder tool whose argument
is padded to at least 2,000 characters. The answer streams first; the relay closes its read at the marker,
and the appendix and the call are never finished or delivered. The model is told that the filler and the placeholder tool
are discarded and that they only keep the request open until the relay ends it; the tool does nothing, and the relay
withholds the call from the client.

A turn that ends in a real tool call is the case this does not cover: the call finalizes before the relay
can cut, and that request is charged near a finalized one: the pelican run's relayed dispatches cost 1.0
point each, against 0.18 to 0.24 for a cut with nothing finalized
([`cost-of-a-cut.md`](docs/relay/cost-of-a-cut.md)). With `text_calls` (on unless a file or the environment turns it off;
[`live-8381.md`](docs/relay/live-8381.md) says whether the live pair runs it) the model writes the command
as text instead, and the relay builds the tool call itself and ends the request before the call finalizes.

That the provider charges less for an unfinished request is its accounting. It is observed on the meter,
not documented by the provider, and it can change. If the provider charged a cut request in full, the relay
would save nothing, and the meter readings kept in this repository are how that would show.

## What it changes in a request

- The instruction above, which also tells the model the sections are read by a program and not to mention
  them. In `text_calls` mode it goes in as a `developer` message.
- With `text_calls` (on unless a file or the environment turns it off), the written-command form above.
- With `session_pin` (off by default, and a decision, not a tuning knob), one fixed `session-id` per client,
  so the provider's prompt cache can be reused across that client's sessions; they then look to the
  provider like one session. The reasoning is in `core/lokki_core/config.py`.
- By default, in the modes that cut (`marker` and `segmented`, which `/v1/responses` always runs) and unless `text_calls_pad` is false, each of
  the client's own `function` and `custom` tools gets one more required property, `__pad9f3` ("filler text of any kind, at least 2,000
  characters"), and a call to such a tool reaches the client with it in the arguments; nothing strips it.
- Only the client's `openai-beta`, `originator`, `session_id` and `chatgpt-account-id` headers are
  forwarded, and the upstream `Authorization` is the operator's, never the client's. Request parameters
  outside an allow-list are dropped, and only their names go in the run record. `store: true` is refused.
- Upstream calls are paced and the simultaneous upstream streams capped (built in: one second apart, two at
  a time; `pacing_seconds` and `max_concurrent_upstream` change them). A 429, 502 or 503 from upstream is retried.

## What the client sees differently

- A slower turn: about 2.4 times as long at effort `high` on the measured task
  ([`docs/relay/latency.md`](docs/relay/latency.md)).
- The marker and the placeholder call are removed. The model has been seen to mention the added
  instruction, and the instruction now tells it not to.
- A long answer is continued in further requests and delivered as one message. When the relay ends an
  answer at its own length limit, the client is told in the text.
- On a cut turn, the closing `response.completed` is built by the relay, carries `"synthetic": true`, and
  has token counts estimated from character counts: output from the text delivered, input from the size of
  the last upstream request. The run record says which kind of count it holds (`usage_source`: `provider` or
  `synthetic-estimate`).
- When the model picks the provider's image tool in a `/v1/responses` turn, two settings decide what the
  client gets. `imagegen_cut` (built in: on) ends the run at the frame that names the image, before any
  image exists. `payload_cap` (built in: 65,536 characters) delivers an oversized field as a 1x1 placeholder.
  The live pair set both off on 2026-10-08; its current values are in
  [`docs/relay/live-8381.md`](docs/relay/live-8381.md).
- The `/v1/images` routes are passed through: an image is billed when it is generated, so there is
  nothing to cut ([`docs/relay/images.md`](docs/relay/images.md)).

## What it does not do

- It does not change what the provider records or meters, and it does not write to a usage meter.
  `billing_probe.py` only reads the account's meter.
- The cut reaches the provider. The relay closes its connection to CLIProxyAPI, which cancels the upstream
  request; in the arm that checked, CLIProxyAPI's own log shows each dispatch ending at or before the end
  of its run (`docs/relay/cost-of-a-cut.md`). The client is not told a turn was cut, apart from the length
  limit above.
- It does not choose, rotate or add accounts. It forwards the client's `chatgpt-account-id` header;
  CLIProxyAPI's configuration decides the rest.
- It does not try to push the charge of a cut request toward zero. One lever for that, defeating the
  provider's prompt cache so a cut request has nothing cached to be charged for, was measured on a
  development relay; it cost more, and it is not in the relay ([`docs/relay/latency.md`](docs/relay/latency.md)).

## Using it

Use accounts you are entitled to use, and check the provider's terms for what you do with them. This
repository makes no claim that any particular use complies with them.

The relay refuses to start without an auth token, answers only on addresses named in its config, and
refuses a wildcard address unless `allow_public_bind` is set. Two diagnostics are off by default and write
what the committed evidence never holds: `debug_log` (frames in both directions, request bodies sent
upstream, error bodies) and `capture_dir`; neither may be inside the repository. Start with
[`docs/relay/live-8381.md`](docs/relay/live-8381.md) for the running pair and
[`docs/relay/docker.md`](docs/relay/docker.md) for the container.

Run the core package with `python3 -m lokki_core --config FILE`, alongside
`python3 core/server.py --config FILE`. The core's control routes are documented
in [`docs/app/api.md`](docs/app/api.md).

### The UI

The core serves the UI at `/` (assets at `/static/`); it needs operator
authorization for control routes. Settings shows three open groups (Mode,
Connection, Updates and About) and an Advanced fold (CLIProxyAPI, Quota reading,
Cutting, Tool calls, Traffic, Set in the file) for a server operator, and writes
only keys in the API's allow-list. See [`docs/app/ui.md`](docs/app/ui.md) for development, build and
test commands.

### Relay, Auto and Direct

The `routing` setting is the mode. `relay` cuts every request (the relay path);
`direct` sends every request straight to CLIProxyAPI untouched (pass-through) at
full price; `auto` reads each codex account's remaining 5-hour quota and cuts
only when the watched accounts are low: an account is low below
`low_quota.threshold_percent` left (30 by default), `low_quota.accounts` names the
watched ones (empty: every enabled account), and with several watched,
`low_quota.rule` cuts when every one is low (`all`) or when one is (`any`).
`low_quota.models` names the models Auto cuts when it is low: empty is every model, and a
request for any other model, or for none, goes direct at full price (the Settings picker lists
what CLIProxyAPI offers; a request for `gpt-x(high)` counts as `gpt-x`, as CLIProxyAPI reads it). Relay and Direct ignore the list. The
quota is read in every mode. A quota the core cannot read counts as not low, so
under Auto the request goes direct at full price, with the reason saying which
read is missing. A file that names no mode is `relay`. The decision and its
reason are in `GET /api/status` and `GET /api/accounts`, and `/healthz` carries
them for a monitor; the routes are in [`docs/app/api.md`](docs/app/api.md) and the
dependence on an unofficial endpoint in [`docs/app/limits.md`](docs/app/limits.md).

**Updating from a release with Low Quota Mode.** The switch `low_quota.enabled`
is gone, and every file keeps the mode it chose: one with no `routing` keeps
cutting every request, or stays in Auto if it turned the switch on, and a copy
of the old `config.example.yaml` (Auto, no management secret file) becomes
`relay`. An installed app that was in Auto with Low Quota Mode off cut every
request; it now cuts only when its watched accounts are low. On a server, a file
that names `routing: auto` passes requests through when its quota is not low or
cannot be read; set `routing: relay` to keep cutting every request.

## Desktop app

Installers: macOS `.dmg` for arm64 and Intel; Windows `.msi` and NSIS `.exe` for
x64; Linux `.deb` and AppImage for x64. Linux arm64 and Windows arm64 wait for a
runner that builds them. `app.yml` builds every installer on GitHub's hosted
runners (Linux on Ubuntu 26.04) for a pull request, a push to `main` or a dispatch
without a tag, and keeps them as Actions artifacts; a tagged release puts them on
the Releases page. Installers carry no Apple Developer ID or Windows certificate:
the macOS app is signed ad hoc only, and is not notarized. A release's update
packages are signed with the project's release key, and the app checks that
signature before it installs an update.

### macOS

Lokki needs macOS 12.4 or later: its window draws its colours and the token
prompt with the WebKit of Safari 15.5, which macOS has built in from 12.4. On an
older Mac the disk image opens and the app can be dragged to Applications, but
macOS refuses to open it, saying it needs macOS 12.4 or later.

Open the `.dmg`, drag Lokki into Applications, and open it from there. macOS
says it could not verify Lokki and does not open it: press **Done**, then
**Open Anyway** in System Settings › Privacy & Security, and give your password.
Or, before you open it, run `xattr -cr /Applications/lokki.app`.

Updates then install in place when Lokki runs from a folder you can write to,
such as Applications.

### Windows

Run the `.msi` installer or the NSIS `.exe`. If SmartScreen blocks the unsigned
installer, choose **More info**, then **Run anyway**.

### Linux

Install the `.deb` with your distribution's package installer, or run the
`.AppImage` after marking it executable.

See [first run](docs/app/first-run.md) for local data, account setup and reset.

### Updates

The app checks for a new release a minute after it starts and then daily, and
**Check now** checks at once. A newer release shows a banner with its notes. The
core makes no network call for updates; only the desktop shell does.

**Install and restart** downloads the package and verifies its signature, so a
package that fails verification stops nothing. It then stops both services
cleanly, proves they are gone, installs, and restarts Lokki. If the install
fails, both services start again and the banner says why. Your data directory is
never touched.

Some installs offer **Download** instead, with the reason beside it: a `.deb`
install on Linux (only an AppImage updates itself), and a Lokki on macOS that
runs from somewhere it cannot replace itself, such as its disk image. A
downloaded `.dmg` is quarantined again, and Lokki from it is opened as at the
first install (see [macOS](#macos)).

The Linux installers are built on Ubuntu 26.04 (glibc 2.43) and run on
distributions at least that new.

A release is made by pushing the tag `v<version>` (the version in `core/VERSION`).
The run builds every platform into one draft and publishes it once all of them
are up. If the hosted macOS and Windows legs cannot run, dispatch the `app`
workflow with that tag and `platforms: linux`: it reuses the draft and publishes
a Linux-only release.

Releases live at <https://github.com/thomasvan/lokki/releases>. See
[updates](docs/app/update.md) for the states, the install order, and where an update has
been tested by hand.

## License

MIT, see [`LICENSE`](LICENSE).

## Names in the records

- **cut**: the relay closes the upstream read before the provider's terminal frame. **dispatch**: one
  upstream request; a client turn can take several.
- **leak**: a run the cut did not hold, which reached the provider's `response.completed` and was billed
  as a direct request would be.
- **`usage-discarded`** (a run record's `verdict`) means only that no terminal frame arrived. It is the
  precondition for the cut, not a claim that the request was not charged; `billed` means one did arrive.
- Until 2026-10-10 the placeholder tool was named `record_internal_notes`, the filler was called "implementation notes", and the
  model was told the tool was an "internal telemetry sink"; measurements and records before that date use those names.
- Committed run records spell the mode `stealth` and the cut `sentinel`; the modes are now called `segmented`
  and `marker`. The records are not rewritten. The code first took a cut request to be free of charge; the
  measurement that showed otherwise is in [`docs/relay/cost-of-a-cut.md`](docs/relay/cost-of-a-cut.md), which
  `billing_arms_2026-10-04.json` cites by its earlier name, `unbill-benefit.md`.

## History

This repository starts from one commit; its history is in a private development repository. The numbers of the
project's own pull requests and the hashes of its own commits, as the documents and the records cite them, belong to
that repository and do not resolve here. The hashes name the code that produced a measurement or ran on the live relay.

## Reading on

[`docs/README.md`](docs/README.md) indexes the measurements and what a claim in them has to carry.
[`WORKSPACE_PROTOCOL.md`](WORKSPACE_PROTOCOL.md) governs how this repository is changed and names
the writing principles it is written by.
