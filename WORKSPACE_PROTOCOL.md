# WORKSPACE_PROTOCOL.md

If you are not a room seat, only two parts of this file bind you: the proof
gates (how this repository is validated) and the local law (what its owner
requires of anyone changing it). Nothing else here does. The front door to the
room is `~/.paseo/orchestration/bin/room guide`. If `~/.paseo/orchestration` is
absent, this host runs no room and only the project's own part applies.

## Project-owned fields

Keep this project-owned part under two screens: durable repository policy only,
never task state. A committed protocol is fully resolved only when it contains
zero unresolved markers, required or optional.

Each airoom-managed repository carries one committed `WORKSPACE_PROTOCOL.md` at
its repository root, and every worktree inherits it. The Human/project owner
resolves and commits the first version before Lead selection; the owning Lead
maintains it afterward.

### Protocol metadata

- **owner:** Human/project owner at bootstrap; owning Lead afterward.
- **audience:** Lead and Supervisor read the full file; Peers receive only the bounded rules their brief needs.
- **applies_to:** this repository and its worktree checkouts; the running relay process, its cron entries, the docker container and the local ports it describes.
- **review_when:** authority or risk changes, topology changes, the release mechanism changes, or accepted recurrence evidence shows this protocol is wrong.

This is briefing policy, not access control. The committed repository-root file
is present in every worktree and can be read there; the rule governs what the
Lead includes in a bounded Peer brief.

### Identity

- **Repo:** `~/Sites/TOOL/lokki`, the canonical checkout, which moved here from the `clisaving` directory on 2026-10-21. It runs the live pair from 2026-10-07: relay `:8381` in front of cliproxy `:8318` (`docs/relay/live-8381.md`). The earlier pair (`:8371`/`:8317`, the `~/Sites/TOOL/cliproxy` checkout) is not maintained from here.
- **Purpose:** Hosts the cliproxy relay deployment (docker compose, config) and `core/`, the relay client traffic goes through. Operational: clients depend on it, and it exists to cut an over-long turn mid-stream so
  that it costs less than a finalized one — never nothing; the measurement and its limits
  are in `docs/relay/cost-of-a-cut.md`.
- **Primary branch:** `main`, with PR branches for review.
- **Lead seat:** the generated Room seats table is authoritative; name no provider, model, effort, or mode here. Read it before opening a seat for this repository and copy provider, model, effort and, where the row names one, mode into the launch call, never back into this file: `~/.paseo/orchestration/bin/room-seat-table <staffing role>`, whose argument is the role that *staffs* the seat, so a Lead's rows print under `supervisor`. Nothing else substitutes for it. A provider's own model menu is wider than the table permits, and a session Paseo did not launch has no `list_profiles` tool configured unless someone wires one; a model id taken from either, or from memory, is how a seat lands on a spelling the table does not carry.

### Project characteristics

- **Criticality:** Operational endpoint on live ChatGPT OAuth credentials, and the single point of failure for client traffic. If the cut stops holding, every request spends real quota while looking healthy.
- **Dominant risks:** a trigger failing silently (the turn finalizes anyway and bills in full, clients keep being served, nobody is told); client prompt and tool payloads reaching committed evidence; credential leakage (`auths/` OAuth tokens); port/service exposure beyond loopback+Tailscale; false billing claims in the evidence docs.
- **Expensive-to-reverse decisions:** content leaving the workspace; changing what clients point at; anything touching `auths/`; changing what `config.yaml` binds or exposes.
- **External side effects:** upstream API calls bill quota whether or not the cut holds — a cut request is charged for the cached prefix the provider had already read. That came to roughly half a direct turn for one replayed turn (`docs/relay/cost-of-a-cut.md`), and to 0.78× the direct spend for a whole Codex task, pelican on `gpt-6-astra` (about 0.64–0.95 on the meter's rounding alone; `docs/relay/relay-8371.md`). The cut reduces the spend and never zeroes it; the relay ports answer on loopback and the Tailscale address; client prompts, tool arguments and tool results transit the host.

### Authority

- **Lead may decide:** relay code, docs, and harness details; which branch/PR carries work; rerunning relay modes at the quota cost a single run entails.
- **Human must decide:** publishing anything outside the workspace, account credential changes, exposing any port publicly, merging PRs, the cron entries that supervise the relay (the Lead added four for `:8381` and `:8318` on 2026-10-07 while going live, without the Human naming cron, and the Human had them removed the same day; the relay on `:8381` is supervised by Docker; the crontab lines that act on the live pair are the harness's: an `@reboot` line that starts the `:8318` cliproxy, approved by the Human on 2026-10-09, and its nightly update, whose `:8318` step the Human delegated to the harness the same day (both as the harness session reported it): `docs/relay/live-8381.md`; adding any other is the Human's), and any change to what the endpoint does when it is under load.
- **Prohibited without explicit authority:** committing or pushing `auths/`, `logs/`, `static/`, or any OAuth/API secret; pushing to `main` directly; changing container port bindings; disabling cliproxy or stopping the relay outside a stated task; editing the Human's crontab.
- **Commit:** Lead may commit on a feature branch.
- **Push:** Lead may push feature branches to `origin`.
- **Merge:** Human merges PRs.
- **Release/deploy:** Human decides; there is no release train, and the deliverable is the running endpoint plus its reviewable evidence docs.
- **Spending:** relay runs spend upstream quota — a cut turn is cheaper than a finalized one, never free; a Lead may run them as the task requires but must report runs to the Human when confirming meter observations.
- **External communication:** none by seats; publishing anything outside the workspace is the Human's alone.
- **Data mutation:** Lead may write repo files and scratch under `/tmp`; never mutate `auths/`, wipe `logs/`, or rewrite committed evidence as a shortcut.
- **Irreversible decisions:** deleting auths, force-pushing, dropping git history — Human only.

### Peer task classes

| Task class | Scope | Staff when |
| --- | --- | --- |
| Implementation | `core/` (including `relay-up.sh`, the cron supervisor) and `docs/` | a relay change is authorized |
| Review | a PR's diff and evidence | before the Human merges |
| Read-only investigation | repo + docs + public web | a question needs evidence, not a change |

### Capacity escalation triggers

- **Stronger Peer trigger:** upstream behavior changes observed mid-task, or a mechanism conclusion needs adversarial re-derivation.
- Provider, model, effort, mode, and skill routing remain owned by the generated Room seats table and skill catalog; this protocol does not reproduce or replace either.

### Proof gates

- **Typecheck:** `cd core && python3 -c "import server"` checks that the core imports; `cd ui && npx svelte-check --tsconfig ./tsconfig.app.json && npx tsc -p tsconfig.node.json --noEmit` checks Svelte and TypeScript types.
- **Lint:** `cd ui && npx eslint .` checks UI lint rules.
- **Targeted test:** `cd core && python3 -m unittest <file>` runs the named core test module; `cd ui && npx vitest run <file>` runs the named UI test file.
- **Browser:** `cd ui && npm run e2e` builds the UI and runs Playwright against a real core with a fake CLIProxyAPI, checking the browser path through token entry, quota, settings and schema.
- **CI:** every job runs on a GitHub-hosted runner. `.github/workflows/core.yml` runs the core suite on Linux, macOS and Windows on every push and pull request that touches the core; `.github/workflows/ui.yml` runs UI checks, component tests and the browser run. A PR is not merged red.
- **App script tests:** `cd app/scripts && python3 -m unittest` proves the sidecar scripts and pinned CLIProxyAPI asset handling satisfy their tests, and that `release_manifest.py` writes `latest.json` from the assets that are there, names a missing platform, reuses a tag's draft and refuses a released tag, and refuses a tag that is not `core/VERSION` or a public key that cannot have signed a release, `release_notes.py` writes the notes, `check_lock_major.py` refuses a CLIProxyAPI major the reader does not speak, and `test_release_workflow.py` holds `app.yml`'s release matrix and `--require` list equal to the platform table and keeps workflow inputs out of `run:` lines and the signing secrets in the two signing steps, holds every workflow to hosted runners, actions pinned by commit, no `pull_request_target` and secrets read by the release jobs only, and that no config a build reads names a `dangerous…` updater key and every `tauri build` takes one `--config`, the `sidecars.py` overlay, which names no plugin, and its `MacSigningTests` hold `tauri.conf.json`'s `bundle.macOS` at `"signingIdentity": "-"` and `"hardenedRuntime": false`, no overlay naming either, and no workflow naming an `APPLE_…` variable, `--no-sign` or `TAURI_CONFIG`, and `test_mac_handtest.py` holds that the macOS hand test's overlay names only its loopback endpoint, its key and its version, and is never written inside the checkout, and, with a Mac's tools stubbed on Linux, that its build unsets every `APPLE_…` variable before `tauri build`, `download` leaves A's `.dmg` in a marked folder in Downloads with a browser's quarantine attribute and `clean` removes it, and `before` and `after` run `codesign --verify --deep --strict` on the installed bundle and `codesign -v` on each sidecar in place on either CPU, and `after` asks the core for `/api/status` with the token `shell-config` prints on an Apple silicon Mac only, not on an Intel Mac or when `sysctl` fails; `cd app && node --test 'inject/*.test.mjs'` proves the injected script adds the operator header only on the core origin and exposes the declared shell hooks.
- **App Rust gates:** Before tests, `cd app/src-tauri && cargo fmt --check` proves Rust formatting is clean and `cargo clippy --locked --workspace -- -D warnings` proves the complete workspace passes Clippy without warnings. `cd app/src-tauri && cargo test --locked --workspace` proves the complete Tauri workspace compiles and its tests pass. On Linux the workspace and smoke gates require `libwebkit2gtk-4.1-dev`, `libgtk-3-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`, `libssl-dev`, `pkg-config`, `patchelf`, `xvfb` and `dbus`.
- **App smoke:** `cd app && bash smoke/smoke.sh` proves the built shell starts both sidecars, serves its UI, restarts CLIProxyAPI, and stops both process trees as specified.
- **App CI:** `.github/workflows/app.yml` runs the app script, injected-script, Rust, installer and headless smoke gates. The Linux job runs every gate above. Wherever it runs (a pull request that touches the app or a workflow, a push to `main`, a dispatch with an empty tag), the `rust` job runs `cargo clippy --locked --workspace -- -D warnings` and `cargo test --locked --workspace` on `windows-latest` and `macos-latest`, the only gate of the shell's Windows and macOS code, and the macOS and Windows installers are built; the Windows installer leg also refuses a `lokki.exe` that is not a GUI-subsystem program (`app/scripts/check_pe_subsystem.py`). Only Linux runs `cargo fmt --check` and the smoke. A pushed tag `v<core/VERSION>` builds, signs and publishes one release once every required platform's asset is up, and refuses the development key.
- **Round validation:** a live run against cliproxy (`POST /v1/responses` on `core/server.py`) whose `relay.summary`/runs entry shows the claimed cut chain — that is the exact claim each run proves and no more; a quota assertion additionally names the meter reading it rests on (account, window, and the settled baseline it was taken against), read with `core/billing_probe.py`, which spends no quota.
- **Unavailable protocol:** what a Lead does when this section yields no command with its claim is the room's, not this project's: see the missing-validation procedure in `~/.paseo/orchestration/docs/routing-and-ownership.md`. A project may not soften it here.

### Review flow

- **Reviewer pass:** relay mechanics changes and every billing claim in the evidence docs.
- **Ultra-review:** a reader pass on the branch, recorded in the pull request and its commits, when a finding changes a billing claim or a measured number.

### Escalation tripwires

- Any output suggesting credentials left the machine or `auths/` contents got logged.
- **Nothing watches the billing on a schedule.** There is no check to go silent and no
  alert to miss: a turn that finalized where a trigger should have cut it is found by
  reading the run log, and whether the cut is worth anything is measured in
  `docs/relay/cost-of-a-cut.md`.

### Local law

- Writing principles for text and tests: `~/.paseo/orchestration/docs/philosophy.md`.
- OAuth tokens and API keys never enter commits, logs destined for the repo, or conversation-bound artifacts.
- Client request and response bodies — prompts, tool arguments, tool results — never enter commits, evidence files, or repo-destined logs. Evidence records keep counts, identifiers and timings, not content.
- Evidence docs keep the codename `midstream-abort` and measured timestamps in UTC.
- `docs/` is the handoff surface; keep it free of dead-end narrative.

### Decisions

The rules this project runs on, each with the reason that keeps it. A rule whose reason
is gone goes with it; the record of when each was taken is in git.

- **The relay is an external Python process, not a cliproxy plugin.** cliproxy's
  `response_stream_interceptor` cannot abort an upstream connection mid-stream, and that
  abort is the mechanism. Lifecycle lives in `core/relay-up.sh` and
  `restart-relay.sh`; behaviour lives in `docs/relay/relay-8371.md`.
- **`midstream-abort` is the endpoint clients use**; cliproxy stays the upstream
  credential manager and management panel. The relay never stops or changes mode by
  itself; for the earlier pair the Human's crontab supervises it, and a watchdog that has gone silent
  is an incident, not a pass (the live pair on `:8381` has no watchdog: Docker restarts the relay, and the harness's `@reboot` line and nightly step start the cliproxy; `docs/relay/live-8381.md`).
- **The earlier pair's cliproxy is published on every interface** (`0.0.0.0:8317` in `docker-compose.yml`; the live one, `:8318`, only on the Tailscale address).
  Its management panel, management API and client API therefore share one port on the LAN
  behind one password; the relay is unaffected because it calls `127.0.0.1:8317`.
  Narrowing that is one `DOCKER-USER` rule — a `ufw`/INPUT rule cannot work, because
  Docker DNATs and the traffic traverses `FORWARD` — and it does not survive a reboot
  here. The exposure itself, and what is left open about it, are in
  `docs/relay/relay-8371.md`.
- **The relay's evidence file lives outside this repository**, at
  `$XDG_STATE_HOME/cliproxy/runs.jsonl`. A live process must not append to a tracked
  file: a `checkout`, `stash` or `clean` then drops records behind it with nothing said,
  and a `git add` commits client-run evidence. The code refuses an in-repo path at
  startup and again at the write; the committed `runs.jsonl` is the frozen seed; a
  `runs_log:` change takes effect at the next start and never on SIGHUP; and a reader may
  still anchor the seed.

## Room law

The room's law is indexed at `~/.paseo/orchestration/docs/README.md`; this file restates none of it, and a project protocol cannot loosen it.
