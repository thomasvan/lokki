# lokki Update Implementation Plan

> **For agentic workers:** execute this plan task by task, in order, with the `superpowers:executing-plans` skill when the seat has the superpowers plugin, otherwise as written. Steps use checkbox (`- [ ]`) syntax for tracking. Each task names its gate. The gates that exist today stay green throughout: `cd core && python ci_quiet_suite.py`, `cd ui && npm run check && npm test && npm run e2e`, `cd app/scripts && python3 -m unittest`, `cd app && node --test 'inject/*.test.mjs'`, `cd app/src-tauri && cargo fmt --check && cargo clippy --locked --workspace -- -D warnings && cargo test --locked --workspace`, `cd app && bash smoke/smoke.sh`, and `app.yml`'s Linux job on every PR. A pull request that adds a test carries, in its body, one sentence per new test naming the behaviour that test binds and no existing test does; a behaviour an existing table can hold is a row there, not a new test function. No task's PR is red at any step: every test a task adds passes in that task.

**Goal:** The installed app learns about a new release, verifies it, stops both sidecars cleanly and proves them gone, installs it and restarts; a tag `v<version>` produces that release with every installer and the updater's manifest; and a settings file written by an earlier version keeps working.

**Architecture:** One checker, in the shell: `tauri-plugin-updater` called from Rust only (the page gets no plugin permission; it gets three shell commands, granted like the four it has). The shell keeps one `Machine` with the update state it reports to the page on request; it checks 60 s after boot, daily, and on "Check now"; an install downloads and verifies first, then runs the stop sequence the shell already has (`Shell::stop_all`, the core drained first), probes each sidecar's process group and refuses to go on while one is alive, installs, and restarts; an install that fails after the stop starts both sidecars again and shows why. The release is `app.yml`'s work on a tag: a `prepare` job refuses a tag that is not `core/VERSION`, a tag that does not exist, or a config still carrying the development public key, and creates one draft release with the notes; the Linux job on the workstation runner and the hosted jobs build and sign their installers (a release-only config overlay turns `createUpdaterArtifacts` on; the CI builds never need the key) and upload them to the draft; a `publish` job assembles `latest.json` from the assets actually uploaded (one home for the platform table, `app/scripts/release_manifest.py`, held equal to the workflow's matrix by a test that lives beside the matrix), refuses to publish while a required platform is missing, and only then turns the draft into a release. The core reads old key names through `migrate(doc)`, whose table starts with the one rename the tree already has (`upstream` → `cliproxy.url`), and `init` rewrites the file whole once; nothing here makes the core call the network.

**Tech Stack:** `tauri-plugin-updater` 2.13.3 (Rust only; `default-features = false, features = ["rustls-tls", "system-proxy"]`: no OpenSSL at run time on Linux, the user's proxy honoured) and `tauri` 2.12.3 with the `test` feature as a dev-dependency (`tauri::test::mock_builder` for the one test that runs the real plugin against a loopback manifest server); the Tauri CLI's `signer` for the key pairs (`@tauri-apps/cli` 2.12.2, already pinned); Python 3.13 and `unittest` for the three release scripts (standard library only); `gh` on every runner for the draft release; Node 24 `node --test` for the injected script; vitest for the UI banner. Every version pinned exactly where a lock file exists; the Lead re-reads the plugin's version at Task 4 with the major given (`cargo info tauri-plugin-updater@2`; a bare `cargo info` names the 3.0 alpha).

**Spec:** `docs/superpowers/specs/2026-10-10-lokki-app-design.md` section 7 (the whole of it), 6.4 (unsigned installs, the macOS re-quarantine, the page's hooks), 8 (the layout, `core/VERSION` the one home of the version, `app.yml`'s draft release on a tag), 9 (signature verified before install; a failed verification shown, not retried silently), 10 (open items).

**Facts the plan rests on, read on 2026-10-14 from the tree at `main` e759278:** the shell's stop path is `Shell::quit` over `Shell::stop_all` over `Sidecars::stop(cx, conf, token, service)` for the core then CLIProxyAPI (`app/src-tauri/src/lib.rs`), and `Sidecars::stop` takes the child out of its slot and drops it, so after `stop_all` nothing in `Sidecars` can be asked whether the process is alive: the proof after a stop must come from the pid itself (Task 4's `group_alive`); `Shell::stop_all` takes the `side` lock only inside, so a command's thread may call it; `MONITOR_TICK` is 250 ms; the smoke's page sequence is the `SMOKE_PAGE` constant in `lib.rs`, which calls `smoke_done` with `status`, `restart` and `grant`; the page's hooks are four, in `app/inject/inject.js`, typed in `ui/src/lib/lokki.d.ts`, each a `window.__TAURI__.core.invoke` of a command `build.rs`'s `COMMANDS` declares and `capability.rs`'s `grant_main` lists, and `inject.test.mjs` (8 tests) reads `build.rs` to hold the hooks and the commands equal; `ui/src/lib/state.svelte.ts` polls the status every 10 s through `polled(...)` and `startPolling(tab)`, tested by individual `it` blocks with fake timers in `state.test.ts`; the core's tests are flat files `core/test_*.py` (`load_settings` is tested in `test_schema.py`, `init` in `test_init.py`'s `InitTests`); `init._write` lands a file in a sibling temp file and `os.replace`s it whole, `init._read`/`_dump` read and dump YAML, and `init.run` on an existing file answers `created: False` with `_existing(core_config)`, which returns a dict with `None` ports when only CLIProxyAPI's file exists and `None` when the core's file was not made by `init`; `config.load_settings` reads the raw document with `read_config` and hands it to `resolve_settings`, which lists `unknown_keys`; no settings key has been renamed: `upstream` is a key of its own with its own rule (a server deployment's full upstream URL, path and credentials allowed, which `test_the_old_upstream_key_and_cliproxy_url_load_what_they_loaded` in `test_schema.py` keeps for the live relay), while `cliproxy.url` is held to scheme, host and port by `_upstream_url`; `schema.py`'s description of `upstream` and `settings_api.py`'s message about it call it the old name of `cliproxy.url`, which Task 1 corrects in words only; `settings_api.py`, `control.py` and `docs/app/api.md` read `upstream` as that value; so the rename table starts empty and `upstream`'s behaviour is not touched; `default_settings()` and the resolved settings carry a key `upstream` that is the resolved upstream URL; `core/config.example.yaml` is the generated listing (`python3 -m lokki_core schema --example`, held equal by `test_schema_example_is_the_generated_listing_byte_for_byte`); the reader's management route major is the one constant `MANAGEMENT_MAJOR = "8"` in `core/lokki_core/quota/reader.py`; `app.yml` builds `deb,appimage` on the workstation runner for pushes to `main` and PRs (`pull_request` paths: `app/**` and the workflow), and `dmg` / `nsis,msi` on hosted runners in a `hosted` job on `workflow_dispatch` only; `app/scripts/sidecars.py --config-json` is how `version` and `bundle.externalBin` reach `tauri build`; the repository `thomasvan/lokki` is private today.

**Facts the plan rests on, read on 2026-10-14 from the published `tauri-plugin-updater` 2.13.3 crate (`src/updater.rs`, `config.rs`, `error.rs`):** the plugin config is `plugins.updater` with `pubkey` (required at plugin init as a string, parsed only when a signature is verified), `endpoints`, `windows.installMode`, `requireSignedVersion`, `allowDowngrades` (default false) and `dangerousInsecureTransportProtocol` (which this plan never sets); `UpdaterBuilder::endpoints(..)` runs `validate_endpoints`, which refuses a non-`https` endpoint on a release build with `Error::InsecureTransportProtocol` and only warns on a debug build; `Updater::check()` fetches the endpoints, resolves the download URL and signature for `<os>-<arch>` (and `<os>-<arch>-<installer>`) before comparing versions, so a manifest without an entry for this platform is `Err(Error::TargetsNotFound(Vec<String>))` whatever its version (`TargetNotFound(String)` only when `.target()` was set, which this plan does not do), and otherwise answers `Ok(None)` when the manifest's version is not greater than the app's; `Update::download(on_chunk: FnMut(usize, Option<u64>), on_finish: FnOnce()) -> Result<Vec<u8>>` verifies the minisign signature against the public key before returning, and with `requireSignedVersion` checks the signature's trusted comment carries the announced version (closing the pairing of a new version number with an older signed package, since the manifest itself is not signed); `Update::install(&self, bytes)` on Linux picks `install_deb`, `install_rpm` or `install_appimage` by the running bundle's type (it does not require `APPIMAGE`; the shell's own policy is the guard that keeps a `.deb` install on the download path), on Windows runs the NSIS or MSI installer in the configured mode and exits the app itself, on macOS replaces the `.app`; `AppHandle::restart()` requests an exit with the restart code and the shell's `RunEvent::Exit` arm runs its idempotent `stop_all` on the way out.

**Facts the Lead confirms at Task 4 and Task 6, against the Tauri CLI it runs:** with `bundle.createUpdaterArtifacts: true` and no `TAURI_SIGNING_PRIVATE_KEY`, `tauri build` fails (so the setting lives in the release-only overlay, never in `tauri.conf.json`); the macOS updater artifact is `<productName>.app.tar.gz` with no version or arch in its name (so the hosted job renames it before the upload); `--bundles app,dmg` is what produces it beside the `.dmg`; the `.sig` file holds the base64 of the minisign signature text, and that base64 is what `latest.json`'s `signature` carries; the signature's trusted comment carries `version:<the app version>` (checked at Task 4 with the development key before it is deleted: `npx tauri signer sign -k /tmp/lokki-dev.key <any file>` and `base64 -d` of the `.sig`; if this CLI writes no `version:`, `requireSignedVersion` stays off and the body and spec 7 say so, since the plugin would otherwise refuse every update with `MissingSignedVersion`); `tauri::AppHandle` without a runtime parameter is `AppHandle<Wry>`, so anything a test drives with `tauri::test::MockRuntime` is generic over `tauri::Runtime`.

**Prerequisites (not in a task; named so the Lead starts with them in place):**

1. Everything sub-project 4 needed (the Tauri Linux packages, `pkg-config`, `xvfb`, `dbus`, `~/.cargo/bin` on the PATH) is installed on the workstation and its runner.
2. The release signing key is the Human's act, done once, at any time before the first tag: `cd app && npm ci && npx tauri signer generate -w ~/.tauri/lokki.key` (it asks for a password), then the repository secrets `TAURI_SIGNING_PRIVATE_KEY` (the file's content) and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, then the public key the command printed replaces the development key in `app/src-tauri/tauri.conf.json`, in a PR of its own. Until then the tree carries a development public key the Lead generates at Task 4 and whose private half it deletes the same minute (decision 1); the release job refuses to run with it. No task waits on the Human for this.
3. The repository is private today: GitHub answers 404 to an anonymous request for `releases/latest/download/latest.json`, for an asset and for a release page. The updater therefore reports `error: the update check failed: … 404` on every check until the repository is public, which is the Human's act (already pending from the publish-readiness work). The plan builds and tests everything else now; the first real check is the Human's, after the first tag, on a public repository.
4. Hosted macOS and Windows runners are refused on this account today. The release job's hosted matrix fails on such an account, and the draft then stays a draft (the spec's rule: the updater never sees a release missing an asset). A Linux-only release is the Human's deliberate dispatch (decision 7).
5. The release build host for Linux is the workstation (Ubuntu 26.04, glibc 2.43): the `.deb` and the AppImage run on distributions at least that new, and the README says so.

**Decisions the plan takes (where the spec was open, or where it departs and the spec is amended in the same change):**

1. **One public key in the tree, two in time.** `tauri.conf.json` carries `plugins.updater.pubkey`. Until the Human's release key exists it is a development key the Lead generated and whose private half is gone, so nothing signed with it will ever exist and no agent seat holds a key that can sign a release; `release_manifest.py` knows that key as `DEV_PUBKEY`, and the `prepare` job refuses to release with it or with a key that does not decode to a minisign public key. The Human's key replaces it in a PR, and the secret holds the private half.
2. **The updater is Rust-only.** The page gets no `updater:` permission. It calls three shell commands through hooks: `updateStatus()` (the state the shell holds), `checkForUpdate()` (a check now, then the state), `installUpdate()` (download and verify, the stop sequence with its proof, install, restart). `build.rs`'s `COMMANDS` grows by those three names and `grant_main` lists them for the core's origin; `inject.test.mjs` already holds hooks and commands equal, and gains an assertion that holds `ui/src/lib/lokki.d.ts` equal to the hooks (one test reading two files, so the four places cannot drift). Spec 6.4's "four hooks" becomes seven in Task 6.
3. **The install switch is decided by the shell, not the page:** `can_install` is true on Linux only when `APPIMAGE` is set in the shell's own environment (the plugin would also install a `.deb`, with `sudo`, which this app never asks for: a `.deb` gets the download path), true on Windows, and on macOS equal to `plugins.lokki.macInstall` in `tauri.conf.json` (`false` until signing exists: "enabling the install step is one config key"). `reason` carries the sentence the banner shows beside "Download" when `can_install` is false.
4. **The check runs on a thread of its own** 60 s after boot, then every 24 h, and on `checkForUpdate()`; never from the core, never from the page's timers. A check or a download that fails leaves `state: "error"` with the sentence, and nothing retries until the next daily tick or "Check now" (spec 9: shown, not retried silently). Two checks never overlap, a check never runs while an install runs, and two installs never overlap: `Machine` holds `checking` and `installing` flags and a second caller gets the running state or "an install is already running".
5. **The install reuses the stop sequence and proves it.** `installUpdate()` downloads and verifies first (nothing stops for a package that fails verification), then runs `Shell::stop_all`, then probes each stopped sidecar's process group (`group_alive(pid)`: `kill(-pid, 0)` on Unix, `tasklist /FI "PID eq <pid>"` on Windows); a live group is an error, and so is a failed `install`; after either, the shell starts both sidecars again (`Sidecars::start_all`, the window stays) and the state says why. Only a clean install reaches `AppHandle::restart()`. The data directory is never touched.
6. **The endpoint** is `https://github.com/thomasvan/lokki/releases/latest/download/latest.json`, in `plugins.updater.endpoints`, with `requireSignedVersion: true` and `allowDowngrades` left false. `LOKKI_UPDATE_ENDPOINT` in the shell's environment replaces the endpoint list (a developer's hand test points it at a loopback or a staging server; on a release build the plugin accepts `https` only, and the plan never sets `dangerousInsecureTransportProtocol`); the public key is never overridable, so a redirected endpoint can only offer packages the real key signed for the version it announces.
7. **The release is one draft, published whole.** On a tag `v<version>`: `prepare` (the tag exists, equals `core/VERSION` through `sidecars.py --versions`, the config's key is a minisign key and not `DEV_PUBKEY`; the notes from `release_notes.py`; `gh release create --draft`), then the Linux job and the hosted matrix build with the release overlay and the signing secrets scoped to their build step and upload each installer and its `.sig` to the draft, then `publish` runs `release_manifest.py` over the draft's asset names with `--require` every platform in its table, uploads `latest.json`, and `gh release edit --draft=false`. A missing platform fails `publish` and the draft stays. `workflow_dispatch` takes `tag` and `platforms` (`all` or `linux`): the Human's deliberate Linux-only release passes `--require linux-x86_64` only, and `latest.json` then names Linux alone; a macOS or Windows updater reading it reports "no build for this platform" as `up_to_date` with a reason, never as an error. The CI jobs (`linux`, `hosted`) do not run on a tag push or on a dispatch with `tag` set.
8. **The platform table has one home,** `release_manifest.py`'s `PLATFORMS` (triple → updater key → the asset's name pattern with the version in it); the workflow's release matrix and `publish`'s `--require` list are deliberate copies held equal by `test_the_workflow_names_every_platform_once`, written in Task 6 beside the matrix.
9. **The notes** are the commit subjects since the previous tag, and, when `app/cliproxy.lock` changed between the tags, a line "CLIProxyAPI <old> -> <new>" followed by that release's body, which the workflow fetches with `gh release view` and hands to `release_notes.py` in a file (the script makes no network call).
10. **A CLIProxyAPI major that the reader does not speak is refused on every push and PR,** by `check_lock_major.py` in `app.yml`'s Linux job on the head alone: `app/cliproxy.lock`'s major must equal `MANAGEMENT_MAJOR` in `core/lokki_core/quota/reader.py`. (A base comparison adds nothing: a head where the lock's major moved and the reader's did not already fails this invariant.) The PR trigger's paths gain `core/lokki_core/quota/reader.py` so a reader-only change runs it too.
11. **Settings migration lives in the core,** `core/lokki_core/migrate.py`: `RENAMES` (old dotted key → new dotted key), empty today, since no key has moved; `migrate(doc, renames=RENAMES)` moves each value that is under an old name and not under the new one (a value under both names keeps the new one), deletes the old, and returns the list applied (the spec's `-> changed` is that list; Task 6 amends the sentence). `load_settings` applies it in memory before `resolve_settings` (a server deployment reads old names without `init`); `init` on an existing file it made applies it and rewrites the file whole with `_write` when the list is not empty, reporting `migrated` in its JSON line (a hand-written comment in that file is not kept: `init` never wrote one). The mechanism is bound by rows that pass their own table in; a row added to `RENAMES` later carries a row with the real names in that test. `upstream` is not a rename (the facts above) and stays as it is; the one-line description in `schema.py` says what it is ("a server deployment's upstream URL, any path or credentials; the app writes cliproxy.url instead"), a wording change the generated `core/config.example.yaml` follows. A key the new version does not know stays in the file and in `unknown_keys` (already the case).

## Global Constraints

- The core makes no network call for updates (spec 7: "a server or Docker deployment of the core never phones GitHub"); only the shell's updater thread reaches the endpoint.
- Loopback only, as before: nothing here changes the core's `listen`, CLIProxyAPI's `server.host`, or the OAuth listener; the smoke's loopback assertions stay.
- No secret in a log, a URL, a PR body or a test's output; the signing private key exists only as the Human's file and the repository secret; the workflow passes it to one step and never echoes it (`set -x` is forbidden in release steps).
- The signature is verified before anything is stopped or installed (decision 5); a package that fails verification is reported in `state: "error"` with the plugin's sentence.
- Sidecar names keep their versions (`lokki-core-<v>`, `cliproxy-<v>`, dots as underscores, no triple); the updater installs the bundle as built, so `sidecar_files::prune` at the next start removes the previous version's files.
- One capability per window and origin: the three new commands join the main window's grant and nothing else; no static capability file; no plugin permission reaches the page (the smoke probes `plugin:updater|check` and `plugin:updater|download_and_install` and expects both refused).
- `--locked` on every Cargo command; fmt and clippy with `-D warnings`; every action pinned by a 40-hex SHA with its tag in a comment; `permissions: contents: read` at the workflow level and `contents: write` only on the release jobs; a workflow input never reaches a `run:` line except through `env`.
- Nothing in CI or a test binds `:1455`, `:8381` or `:8318`; the one test that serves a manifest listens on an ephemeral loopback port and stops its thread when the test ends.
- Commands chained in a gate use `&&` only; no `| tail` hides a status. Commit messages end with the attribution lines the session carries; `git add` names paths.

## Review Focus

1. A manifest whose version is not above the running one must leave `state: "up_to_date"`, and one with no entry for this platform must too, with a reason, never an error (Task 3 `a_manifest_without_this_platform_is_up_to_date_with_a_reason`; Task 4 `the_plugin_answers_from_a_loopback_manifest`, rows "not newer" and "no platform").
2. A package whose signature does not verify must stop nothing and install nothing: the sidecars keep running and the state says why (Task 3 `a_package_that_fails_verification_stops_nothing`).
3. A sidecar still alive after the stop sequence must block the install, and the sidecars must be running again afterwards (Task 3 `an_alive_sidecar_blocks_the_install_and_the_sidecars_come_back`; Task 4 `a_process_group_is_alive_until_its_members_are_gone`).
4. A settings file holding a key under both its old and its new name must keep the new one and drop the old (Task 1, the row "a key present under both names keeps the new one").
5. A release with every asset but one must stay a draft, and the publish step must say which platform is missing (Task 2 `a_missing_required_platform_is_refused_by_name`).

---

### Task 1: The core reads old settings names and `init` rewrites the file once

**Files:**
- Create: `core/lokki_core/migrate.py`, `core/test_migrate.py`
- Modify: `core/lokki_core/config.py` (`load_settings`, after `read_config`), `core/lokki_core/schema.py` (the `upstream` field's one-line description), `core/lokki_core/settings_api.py` (its one message about `upstream`), `core/test_settings_api.py` (that message's words, if asserted), `core/config.example.yaml` (regenerated with `python3 -m lokki_core schema --example > config.example.yaml` from `core/`; `test_schema_example_is_the_generated_listing_byte_for_byte` holds them equal), `core/lokki_core/init.py` (`run`, the existing-file branch), `core/test_init.py` (one new test), `core/test_schema.py` (one new row), `docs/app/api.md` (one sentence under `init`), `docs/app/data-directory.md` (one sentence on `config.yaml`)

**Interfaces:**
- Consumes: `init._write(path, text, mode)`, `init._dump(doc)`, `init._read(path)`, `init._existing(core_config)`, `config.read_config(path) -> (text, raw)`.
- Produces: `migrate.RENAMES: list[tuple[str, str]]` (empty) and `migrate.migrate(doc: dict, renames=None) -> list[tuple[str, str]]` (`None` means the module's `RENAMES`, read at call time) (the renames applied, in table order; a dotted name addresses a key inside a section; a section that becomes empty is removed). `init.run(...)` on an existing file it made answers `"migrated": ["old -> new", ...]` (empty when nothing moved).

- [ ] **Step 1: Failing tests** in `core/test_migrate.py` (the core's tests are flat files under `core/`, run as `python -m unittest test_migrate` from `core/`; the table is empty today, so every row passes its own renames in):

```python
import unittest

from lokki_core import migrate


class MigrateTests(unittest.TestCase):
    def test_each_rename_moves_the_value_once(self):
        # (label, renames, before, after, applied)
        rows = [
            ("a top-level key moves",
             [("retries_old", "retries")], {"retries_old": 3}, {"retries": 3}, [("retries_old", "retries")]),
            ("a key moves into a section",
             [("poll_seconds", "low_quota.poll_seconds")], {"poll_seconds": 30},
             {"low_quota": {"poll_seconds": 30}}, [("poll_seconds", "low_quota.poll_seconds")]),
            ("a key moves out of a section, and the empty section goes",
             [("cliproxy.panel_on", "panel")], {"cliproxy": {"panel_on": True}}, {"panel": True},
             [("cliproxy.panel_on", "panel")]),
            ("a key moves between sections",
             [("a.x", "b.x")], {"a": {"x": 1, "y": 2}}, {"a": {"y": 2}, "b": {"x": 1}}, [("a.x", "b.x")]),
            ("nothing to move",
             [("old", "new")], {"new": 1}, {"new": 1}, []),
            ("a key present under both names keeps the new one",
             [("old", "new")], {"old": 1, "new": 2}, {"new": 2}, [("old", "new")]),
            ("an old name whose section is not a mapping is left alone",
             [("a.x", "b")], {"a": "text"}, {"a": "text"}, []),
            ("two renames apply in table order",
             [("a", "b"), ("b", "c")], {"a": 1}, {"c": 1}, [("a", "b"), ("b", "c")]),
        ]
        for label, renames, before, after, applied in rows:
            with self.subTest(label):
                doc = dict(before)
                self.assertEqual(migrate.migrate(doc, renames=renames), applied)
                self.assertEqual(doc, after)
```

- [ ] **Step 2: Run** `cd core && python -m unittest test_migrate -v`
Expected: FAIL with "No module named 'lokki_core.migrate'".

- [ ] **Step 3: Implement** `core/lokki_core/migrate.py`:

```python
"""Settings keys that moved between versions: read under the old name, written under the
new one once. `RENAMES` is the one home of the list; each row is (old, new) with dotted
names for keys inside a section."""

RENAMES: list[tuple[str, str]] = []


def _get(doc, dotted):
    section, _, key = dotted.rpartition(".")
    holder = doc.get(section) if section else doc
    if not isinstance(holder, dict) or key not in holder:
        return None, None
    return holder, key


def _put(doc, dotted, value):
    section, _, key = dotted.rpartition(".")
    if section:
        holder = doc.get(section)
        if not isinstance(holder, dict):
            holder = doc[section] = {}
    else:
        holder = doc
    holder[key] = value


def migrate(doc, renames=None):
    """Move each value found under an old name to its new name, unless the new name is
    already set (then the old one is dropped). Returns the renames applied, in order.
    `renames` defaults to the module's table at call time, so a test may patch `RENAMES`."""
    applied = []
    for old, new in (RENAMES if renames is None else renames):
        holder, key = _get(doc, old)
        if holder is None:
            continue
        value = holder.pop(key)
        new_holder, _ = _get(doc, new)
        if new_holder is None:
            _put(doc, new, value)
        section, _, _ = old.rpartition(".")
        if section and isinstance(doc.get(section), dict) and not doc[section]:
            del doc[section]
        applied.append((old, new))
    return applied
```
- [ ] **Step 4: `load_settings` reads old names.** In `core/lokki_core/config.py`, `load_settings`, right after `raw = read_config(path)[1]`:

```python
        migrate.migrate(raw)
```
with `from lokki_core import migrate` beside the module's other imports (`migrate` imports nothing from `config`, so there is no cycle). The test first, a new function in `test_schema.py`, `test_a_moved_key_in_a_file_is_read_under_its_new_name`: with `unittest.mock.patch.object(migrate, "RENAMES", [("retries_old", "retries")])`, a temp file holding `retries_old: 2` loaded through `config.load_settings(config_path=path, env={})` resolves `retries == 2` and lists no unknown key (that is the path a server deployment takes). Nothing else in `config.py` changes: `upstream` keeps its own read and its own rule.

- [ ] **Step 5: `upstream` says what it is.** `schema.py`'s `Field("upstream", ...)` description becomes "a server deployment's upstream URL, any path or credentials; the app writes cliproxy.url instead"; `settings_api.py`'s message about `upstream` (its line 79 today, "cliproxy.url is its new name") says the same thing in its own words ("the app's setting is cliproxy.url; upstream is a server deployment's"); regenerate `core/config.example.yaml` from `core/` with `python3 -m lokki_core schema --example > config.example.yaml`, which `test_schema_example_is_the_generated_listing_byte_for_byte` holds equal; the test in `test_settings_api.py` that asserts that message, if one does, gets the new words. No behaviour changes; `test_the_old_upstream_key_and_cliproxy_url_load_what_they_loaded` stays as it is.

- [ ] **Step 6: `init` rewrites once.** In `core/lokki_core/init.py`, `run`, in the `if present:` branch, after `ports = _existing(core_config)`:

```python
        out["migrated"] = []
        if ports is None:
            out["note"] = (f"{core_config} was not made by init (it lacks the keys init "
                           "writes), nothing changed")
        else:
            out.update(ports)
            out["note"] = f"already initialised: {present[0]} exists, nothing changed"
            if os.path.exists(core_config):
                doc = _read(core_config)
                applied = migrate.migrate(doc)
                if applied:
                    _write(core_config, _dump(doc), 0o600)
                    out["migrated"] = [f"{old} -> {new}" for old, new in applied]
                    out["note"] = f"already initialised: {present[0]} exists; settings renamed: " + ", ".join(out["migrated"])
        return out
```
(`_existing` answers a dict with `None` ports when only CLIProxyAPI's file exists; the `os.path.exists` guard keeps that case as it is.) The failing test first, a new function in `test_init.py`'s `InitTests`, `test_a_second_init_renames_a_moved_key_once`: with `unittest.mock.patch.object(migrate, "RENAMES", [("retries_old", "retries")])`, after a first `init.run`, append `retries_old: 2` to the core file; a second `run` answers `created: False`, `migrated == ["retries_old -> retries"]`, the file holds `retries: 2` and no `retries_old`, its mode is still `0o600`, and every other key `init` wrote is unchanged; a third `run` answers `migrated == []` and leaves the file's mtime alone. The body's sentence: "a moved key in a file `init` made is rewritten under its new name once, whole, and reported".

- [ ] **Step 7: Docs.** `docs/app/api.md`, the `init` paragraph: "On an existing file `init` made, it applies the renames in `lokki_core/migrate.py` and rewrites the file whole once; the JSON line lists them as `migrated`." `docs/app/data-directory.md`, the `config.yaml` row: "A key an older version wrote under a name that moved is read under the old name and rewritten under the new one by the next `init`; a key this version does not know stays and is listed in `unknown_keys`."

- [ ] **Step 8: Gate and commit**

Run: `cd core && python ci_quiet_suite.py`
Expected: every test passes, nothing written by a test.

```bash
git add core/lokki_core/migrate.py core/test_migrate.py core/lokki_core/config.py core/lokki_core/schema.py core/lokki_core/settings_api.py core/test_settings_api.py core/config.example.yaml core/lokki_core/init.py core/test_init.py core/test_schema.py docs/app/api.md docs/app/data-directory.md
git commit -m "core: settings keys that move are read under the old name and rewritten once by init"
```

---

### Task 2: The release scripts: the manifest, the notes, the lock-major check

**Files:**
- Create: `app/scripts/release_manifest.py`, `app/scripts/release_notes.py`, `app/scripts/check_lock_major.py`, `app/scripts/test_release_manifest.py`, `app/scripts/test_release_notes.py`, `app/scripts/test_check_lock_major.py`

**Interfaces:**
- Consumes: `sidecars.read_versions()`.
- Produces, in `release_manifest.py`: `PLATFORMS: dict[str, tuple[str, str]]` = triple → (updater key, the asset's name with `{version}`): `{"x86_64-unknown-linux-gnu": ("linux-x86_64", "lokki_{version}_amd64.AppImage"), "aarch64-apple-darwin": ("darwin-aarch64", "lokki_{version}_aarch64.app.tar.gz"), "x86_64-apple-darwin": ("darwin-x86_64", "lokki_{version}_x64.app.tar.gz"), "x86_64-pc-windows-msvc": ("windows-x86_64", "lokki_{version}_x64-setup.exe")}`; `DEV_PUBKEY: str` (the development key's text, set at Task 4; `""` until then); `class Missing(Exception)` with `.platforms: list[str]`; `asset_url(repo, tag, name) -> str` (`https://github.com/<repo>/releases/download/<tag>/<name>`); `manifest(version, tag, repo, names: list[str], signatures: dict[str, str], require: set[str], pub_date: str, notes: str) -> dict` (`signatures` is asset name → the `.sig` file's text, stripped; raises `Missing` naming every required platform without its asset or its signature; ignores names of no platform, such as the `.deb`); `check_tag(tag, version) -> str | None` (`None` when `tag == f"v{version}"`); `check_pubkey(config: dict) -> str | None` (`None` only when `config["plugins"]["updater"]["pubkey"]` is not `DEV_PUBKEY` and decodes as base64 to two lines whose second decodes as base64 to 42 bytes starting with `Ed`, which is a minisign public key). CLI: `release_manifest.py --version V --tag TAG --repo OWNER/NAME --names FILE --signatures-dir DIR --require KEY[,KEY] --pub-date DATE --notes FILE --out latest.json`, `--check-tag TAG`, `--check-pubkey CONFIG`.
- `release_notes.py`: `notes(subjects: list[str], lock_before: dict | None, lock_after: dict, cliproxy_body: str) -> str`; CLI reads subjects on stdin (one per line) and `--lock-before FILE` (optional), `--lock-after FILE`, `--cliproxy-notes FILE` (optional).
- `check_lock_major.py`: `refuse(lock: dict, reader: str) -> str | None`; CLI `check_lock_major.py --lock FILE --reader FILE` exits 1 with the sentence.

- [ ] **Step 1: Failing tests**, `app/scripts/test_release_manifest.py` (the `unittest` style of `test_sidecars.py`):

```python
import base64
import json
import unittest

import release_manifest as rm

SIG = base64.b64encode(b"untrusted comment: signature from tauri secret key\nRWQabc\ntrusted comment: timestamp:1\tfile:x\tversion:0.2.0\nZZZ\n").decode()
NAMES = ["lokki_0.2.0_amd64.AppImage", "lokki_0.2.0_amd64.deb", "lokki_0.2.0_x64-setup.exe", "lokki_0.2.0_x64_en-US.msi",
         "lokki_0.2.0_aarch64.app.tar.gz", "lokki_0.2.0_x64.app.tar.gz", "lokki_0.2.0_aarch64.dmg", "lokki_0.2.0_x64.dmg"]
ALL = {key for key, _ in rm.PLATFORMS.values()}


def sigs_for(*names):
    return {f"{n}.sig": SIG + "\n" for n in names}


def minisign_pubkey(key_id=b"\x01" * 8, key=b"\x02" * 32):
    line = base64.b64encode(b"Ed" + key_id + key).decode()
    return base64.b64encode(f"untrusted comment: minisign public key\n{line}\n".encode()).decode()


class ManifestTests(unittest.TestCase):
    def test_the_manifest_names_each_required_platform_with_its_signature_and_url(self):
        out = rm.manifest("0.2.0", "v0.2.0", "thomasvan/lokki", NAMES, sigs_for(*NAMES), ALL, "2026-10-14T00:00:00Z", "notes")
        self.assertEqual(out["version"], "0.2.0")
        self.assertEqual(out["notes"], "notes")
        self.assertEqual(out["pub_date"], "2026-10-14T00:00:00Z")
        self.assertEqual(set(out["platforms"]), ALL)
        self.assertEqual(out["platforms"]["linux-x86_64"]["url"],
                         "https://github.com/thomasvan/lokki/releases/download/v0.2.0/lokki_0.2.0_amd64.AppImage")
        self.assertEqual(out["platforms"]["darwin-aarch64"]["url"],
                         "https://github.com/thomasvan/lokki/releases/download/v0.2.0/lokki_0.2.0_aarch64.app.tar.gz")
        self.assertEqual(out["platforms"]["linux-x86_64"]["signature"], SIG, "the signature is the .sig's text, stripped")
        self.assertNotIn(".deb", json.dumps(out))
        self.assertNotIn(".msi", json.dumps(out))

    def test_a_missing_required_platform_is_refused_by_name(self):
        # (label, names, signatures, require, missing)
        rows = [
            ("no windows asset", ["lokki_0.2.0_amd64.AppImage"], sigs_for("lokki_0.2.0_amd64.AppImage"), {"linux-x86_64", "windows-x86_64"}, ["windows-x86_64"]),
            ("an asset without its signature", ["lokki_0.2.0_amd64.AppImage"], {}, {"linux-x86_64"}, ["linux-x86_64"]),
            ("an asset of another version does not count", ["lokki_0.1.0_amd64.AppImage"], sigs_for("lokki_0.1.0_amd64.AppImage"), {"linux-x86_64"}, ["linux-x86_64"]),
            ("linux only is enough when linux only is required", ["lokki_0.2.0_amd64.AppImage"], sigs_for("lokki_0.2.0_amd64.AppImage"), {"linux-x86_64"}, []),
            ("every platform missing, named in table order", [], {}, ALL, ["linux-x86_64", "darwin-aarch64", "darwin-x86_64", "windows-x86_64"]),
        ]
        for label, names, sigs, require, missing in rows:
            with self.subTest(label):
                if missing:
                    with self.assertRaises(rm.Missing) as cm:
                        rm.manifest("0.2.0", "v0.2.0", "thomasvan/lokki", names, sigs, require, "2026-10-14T00:00:00Z", "")
                    self.assertEqual(cm.exception.platforms, missing)
                else:
                    rm.manifest("0.2.0", "v0.2.0", "thomasvan/lokki", names, sigs, require, "2026-10-14T00:00:00Z", "")

    def test_the_tag_must_be_the_version(self):
        self.assertIsNone(rm.check_tag("v0.2.0", "0.2.0"))
        self.assertIn("v0.2.0", rm.check_tag("v0.2.1", "0.2.0"))
        self.assertIn("v0.2.0", rm.check_tag("0.2.0", "0.2.0"))

    def test_only_a_minisign_release_key_can_release(self):
        # (label, pubkey, refused)
        rows = [
            ("the development key", rm.DEV_PUBKEY, True),
            ("no key", None, True),
            ("not base64", "not a key", True),
            ("base64 of one line", base64.b64encode(b"just one line").decode(), True),
            ("the wrong algorithm", base64.b64encode(b"untrusted comment: x\n" + base64.b64encode(b"XX" + b"\x01" * 40) + b"\n").decode(), True),
            ("the wrong length", base64.b64encode(b"untrusted comment: x\n" + base64.b64encode(b"Ed" + b"\x01" * 30) + b"\n").decode(), True),
            ("a minisign key", minisign_pubkey(), False),
        ]
        for label, key, refused in rows:
            with self.subTest(label):
                config = {"plugins": {"updater": ({"pubkey": key} if key is not None else {})}}
                self.assertEqual(rm.check_pubkey(config) is not None, refused)
```

- [ ] **Step 2: Run** `cd app/scripts && python3 -m unittest test_release_manifest -v`. Expected: FAIL, "No module named 'release_manifest'".

- [ ] **Step 3: Implement** `app/scripts/release_manifest.py`:

```python
#!/usr/bin/env python3
"""The updater's manifest (`latest.json`) from a draft release's asset names, and the two
refusals the release job makes before building: a tag that is not core/VERSION, and a
public key that cannot have signed a release. PLATFORMS is the one home of the platform
table; app.yml's release matrix and its --require list are deliberate copies, held equal
by a test beside the matrix."""
import argparse
import base64
import binascii
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# triple -> (the updater's platform key, the asset the updater downloads)
PLATFORMS = {
    "x86_64-unknown-linux-gnu": ("linux-x86_64", "lokki_{version}_amd64.AppImage"),
    "aarch64-apple-darwin": ("darwin-aarch64", "lokki_{version}_aarch64.app.tar.gz"),
    "x86_64-apple-darwin": ("darwin-x86_64", "lokki_{version}_x64.app.tar.gz"),
    "x86_64-pc-windows-msvc": ("windows-x86_64", "lokki_{version}_x64-setup.exe"),
}

# The key the tree carries until the Human's release key replaces it (plan decision 1).
DEV_PUBKEY = ""


class Missing(Exception):
    def __init__(self, platforms):
        super().__init__("no signed asset for: " + ", ".join(platforms))
        self.platforms = platforms


def asset_url(repo, tag, name):
    return f"https://github.com/{repo}/releases/download/{tag}/{name}"


def manifest(version, tag, repo, names, signatures, require, pub_date, notes):
    present = set(names)
    platforms = {}
    missing = []
    for key, pattern in PLATFORMS.values():
        name = pattern.format(version=version)
        sig = signatures.get(f"{name}.sig")
        if name in present and sig and sig.strip():
            platforms[key] = {"signature": sig.strip(), "url": asset_url(repo, tag, name)}
        elif key in require:
            missing.append(key)
    if missing:
        raise Missing(missing)
    return {"version": version, "notes": notes, "pub_date": pub_date, "platforms": platforms}


def check_tag(tag, version):
    want = f"v{version}"
    return None if tag == want else f"the tag is {tag!r} but core/VERSION says {want!r}"


def _is_minisign_pubkey(text):
    try:
        lines = base64.b64decode(text, validate=True).decode("utf-8").splitlines()
        raw = base64.b64decode(lines[1], validate=True)
    except (binascii.Error, UnicodeDecodeError, IndexError, ValueError):
        return False
    return len(lines) >= 2 and len(raw) == 42 and raw[:2] == b"Ed"


def check_pubkey(config):
    key = ((config.get("plugins") or {}).get("updater") or {}).get("pubkey")
    if not key or key == DEV_PUBKEY:
        return "tauri.conf.json carries no release public key (plugins.updater.pubkey is unset or the development key)"
    if not _is_minisign_pubkey(key):
        return "plugins.updater.pubkey is not a minisign public key as `tauri signer generate` prints it"
    return None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check-tag")
    p.add_argument("--check-pubkey")
    p.add_argument("--version")
    p.add_argument("--tag")
    p.add_argument("--repo")
    p.add_argument("--names", help="a file with one asset name per line")
    p.add_argument("--signatures-dir")
    p.add_argument("--require")
    p.add_argument("--pub-date")
    p.add_argument("--notes")
    p.add_argument("--out")
    a = p.parse_args(argv)
    if a.check_tag:
        import sidecars
        core, _ = sidecars.read_versions()
        problem = check_tag(a.check_tag, core)
    elif a.check_pubkey:
        with open(a.check_pubkey, encoding="utf-8") as f:
            problem = check_pubkey(json.load(f))
    else:
        for name in ("version", "tag", "repo", "names", "signatures_dir", "require", "pub_date", "notes", "out"):
            if getattr(a, name) is None:
                p.error(f"--{name.replace('_', '-')} is required to write the manifest")
        with open(a.names, encoding="utf-8") as f:
            names = [line.strip() for line in f if line.strip()]
        signatures = {}
        for name in os.listdir(a.signatures_dir):
            if name.endswith(".sig"):
                with open(os.path.join(a.signatures_dir, name), encoding="utf-8") as f:
                    signatures[name] = f.read()
        with open(a.notes, encoding="utf-8") as f:
            notes = f.read()
        try:
            out = manifest(a.version, a.tag, a.repo, names, signatures, set(a.require.split(",")), a.pub_date, notes)
        except Missing as e:
            print(f"release_manifest: {e}", file=sys.stderr)
            return 1
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
            f.write("\n")
        return 0
    if problem:
        print(f"release_manifest: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```
A CLI round trip is one more test in the same file:

```python
    def test_the_cli_writes_the_manifest_from_files(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            names = os.path.join(d, "names.txt")
            with open(names, "w", encoding="utf-8") as f:
                f.write("lokki_0.2.0_amd64.AppImage\nlokki_0.2.0_amd64.deb\n")
            os.mkdir(os.path.join(d, "sigs"))
            with open(os.path.join(d, "sigs", "lokki_0.2.0_amd64.AppImage.sig"), "w", encoding="utf-8") as f:
                f.write(SIG + "\n")
            notes = os.path.join(d, "notes.md")
            with open(notes, "w", encoding="utf-8") as f:
                f.write("notes\n")
            out = os.path.join(d, "latest.json")
            common = ["--version", "0.2.0", "--tag", "v0.2.0", "--repo", "thomasvan/lokki", "--names", names,
                      "--signatures-dir", os.path.join(d, "sigs"), "--pub-date", "2026-10-14T00:00:00Z", "--notes", notes, "--out", out]
            self.assertEqual(rm.main(common + ["--require", "linux-x86_64"]), 0)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(json.load(f), rm.manifest("0.2.0", "v0.2.0", "thomasvan/lokki", ["lokki_0.2.0_amd64.AppImage", "lokki_0.2.0_amd64.deb"],
                                                           {"lokki_0.2.0_amd64.AppImage.sig": SIG + "\n"}, {"linux-x86_64"}, "2026-10-14T00:00:00Z", "notes\n"))
            os.unlink(out)
            self.assertEqual(rm.main(common + ["--require", "linux-x86_64,windows-x86_64"]), 1)
            self.assertFalse(os.path.exists(out))
```

- [ ] **Step 4: Run** the tests. Expected: PASS, 5 tests.

- [ ] **Step 5: `release_notes.py`**, test first in `app/scripts/test_release_notes.py`:

```python
import unittest

import release_notes as rn


class NotesTests(unittest.TestCase):
    def test_notes_list_the_subjects_and_name_a_cliproxy_change(self):
        # (label, subjects, lock_before, lock_after, body, expected)
        rows = [
            ("subjects only, lock unchanged", ["app: a", "core: b"], {"version": "8.0.23"}, {"version": "8.0.23"}, "",
             "## Changes\n\n- app: a\n- core: b\n"),
            ("lock changed: the versions and the upstream body", ["app: bump"], {"version": "8.0.23"}, {"version": "8.1.0"},
             "fixed things\n", "## Changes\n\n- app: bump\n\n## CLIProxyAPI 8.0.23 -> 8.1.0\n\nfixed things\n"),
            ("first release: no previous lock", ["core: first"], None, {"version": "8.0.23"}, "",
             "## Changes\n\n- core: first\n\n## CLIProxyAPI 8.0.23\n"),
            ("no subjects", [], {"version": "8.0.23"}, {"version": "8.0.23"}, "", "## Changes\n\n(no commits since the previous tag)\n"),
        ]
        for label, subjects, before, after, body, expected in rows:
            with self.subTest(label):
                self.assertEqual(rn.notes(subjects, before, after, body), expected)
```
Implementation:

```python
#!/usr/bin/env python3
"""The release notes: the commit subjects since the previous tag and, when app/cliproxy.lock
changed, CLIProxyAPI's versions and that release's body (handed in; no network here)."""
import argparse
import json
import sys


def notes(subjects, lock_before, lock_after, cliproxy_body):
    lines = ["## Changes", ""]
    lines += [f"- {s}" for s in subjects] or ["(no commits since the previous tag)"]
    after = lock_after["version"]
    before = lock_before["version"] if lock_before else None
    if before != after:
        lines += ["", f"## CLIProxyAPI {before} -> {after}" if before else f"## CLIProxyAPI {after}"]
        if cliproxy_body.strip():
            lines += ["", cliproxy_body.rstrip("\n")]
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lock-before")
    p.add_argument("--lock-after", required=True)
    p.add_argument("--cliproxy-notes")
    a = p.parse_args(argv)
    subjects = [line.rstrip("\n") for line in sys.stdin if line.strip()]
    before = None
    if a.lock_before:
        with open(a.lock_before, encoding="utf-8") as f:
            before = json.load(f)
    with open(a.lock_after, encoding="utf-8") as f:
        after = json.load(f)
    body = ""
    if a.cliproxy_notes:
        with open(a.cliproxy_notes, encoding="utf-8") as f:
            body = f.read()
    sys.stdout.write(notes(subjects, before, after, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: `check_lock_major.py`**, test first in `app/scripts/test_check_lock_major.py`:

```python
import unittest

import check_lock_major as clm

READER_8 = 'MANAGEMENT_MAJOR = "8"          # the `/v8/` route prefix this module uses\n'
READER_9 = 'MANAGEMENT_MAJOR = "9"\n'


class LockMajorTests(unittest.TestCase):
    def test_the_lock_and_the_reader_speak_the_same_major(self):
        # (label, lock, reader, refused)
        rows = [
            ("the same major", {"version": "8.0.23"}, READER_8, False),
            ("a minor bump", {"version": "8.1.0"}, READER_8, False),
            ("a major bump the reader does not speak", {"version": "9.0.0"}, READER_8, True),
            ("a major bump with the reader moved", {"version": "9.0.0"}, READER_9, False),
            ("a reader ahead of the lock", {"version": "8.0.23"}, READER_9, True),
            ("a reader without the constant", {"version": "8.0.23"}, "# nothing here\n", True),
        ]
        for label, lock, reader, refused in rows:
            with self.subTest(label):
                problem = clm.refuse(lock, reader)
                self.assertEqual(problem is not None, refused, problem)
```
Implementation:

```python
#!/usr/bin/env python3
"""Refuse a tree whose CLIProxyAPI lock pins a major version the reader's route prefix
(MANAGEMENT_MAJOR in core/lokki_core/quota/reader.py) does not speak (spec 7). Run on the
head alone: a lock bump across a major without the reader fails here on that PR."""
import argparse
import json
import re
import sys

_MAJOR = re.compile(r'^MANAGEMENT_MAJOR\s*=\s*"(\d+)"', re.M)


def refuse(lock, reader):
    lock_major = int(lock["version"].split(".")[0])
    m = _MAJOR.search(reader)
    if not m:
        return "core/lokki_core/quota/reader.py has no MANAGEMENT_MAJOR constant"
    reader_major = int(m.group(1))
    if reader_major != lock_major:
        return (f"the reader's MANAGEMENT_MAJOR is {reader_major} but app/cliproxy.lock pins "
                f"{lock['version']}: change both in one PR")
    return None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lock", required=True)
    p.add_argument("--reader", required=True)
    a = p.parse_args(argv)
    with open(a.lock, encoding="utf-8") as f:
        lock = json.load(f)
    with open(a.reader, encoding="utf-8") as f:
        reader = f.read()
    problem = refuse(lock, reader)
    if problem:
        print(f"check_lock_major: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Gate and commit**

Run: `cd app/scripts && python3 -m unittest`
Expected: every test passes (the three new files add 5 + 1 + 1 tests).

```bash
git add app/scripts/release_manifest.py app/scripts/release_notes.py app/scripts/check_lock_major.py app/scripts/test_release_manifest.py app/scripts/test_release_notes.py app/scripts/test_check_lock_major.py
git commit -m "app: the release manifest, the notes and the lock-major check as tested scripts"
```

---

### Task 3: The update machine, pure: states, the policy, the order of an install

**Files:**
- Create: `app/src-tauri/src/update.rs`
- Modify: `app/src-tauri/src/lib.rs` (`mod update;` and a `#[cfg(test)] pub(crate) mod testing` holding the `FakeClock` that `sidecars.rs`'s tests define today, moved there so both modules use one)

**Interfaces:**
- Consumes: `lokki_logic::supervise::Clock` (`now() -> Duration`, `sleep(Duration)`).
- Produces: `update::Status` serialized as `{"state": "idle"|"checking"|"up_to_date"|"available"|"downloading"|"installing"|"error", "current_version": "<v>", "version": "<v>"|null, "notes_url": "<url>"|null, "can_install": bool, "reason": "<sentence>"|null, "error": "<sentence>"|null}`; `update::Found { version: String, notes_url: String }`; `update::Package<P>` (what `download` verified; only `install` takes it); `update::Source { type Package; fn check(&self) -> Result<Option<Found>, String>; fn download(&self, found: &Found) -> Result<Self::Package, String>; fn install(&self, package: Self::Package) -> Result<(), String>; }`; `update::Os { Linux, Macos, Windows }` with `Os::current()`; `update::Policy { can_install, reason }` with `Policy::detect(os, env: impl Fn(&str) -> Option<String>, mac_install: bool)`; `update::Machine<S: Source, C: Clock>` with `new(source, clock, current_version, policy)`, `status()`, `due() -> bool`, `check() -> Status`, `install(stop: impl FnOnce() -> Result<(), String>, resume: impl FnOnce()) -> Result<(), String>`; the constants `FIRST_CHECK` (60 s), `DAILY` (24 h), `NO_PLATFORM` (the sentence "no build for this platform in the latest release").

- [ ] **Step 1: Failing tests** in `update.rs`'s `mod tests`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::FakeClock;
    use std::cell::RefCell;
    use std::rc::Rc;

    /// Every call the fake and the closures make, in order.
    type Log = Rc<RefCell<Vec<&'static str>>>;

    struct FakeSource {
        log: Log,
        check: RefCell<Vec<Result<Option<Found>, String>>>,
        download: RefCell<Result<(), String>>,
        install: RefCell<Result<(), String>>,
    }
    impl FakeSource {
        fn new(log: &Log) -> Self {
            FakeSource { log: Rc::clone(log), check: RefCell::new(Vec::new()), download: RefCell::new(Ok(())), install: RefCell::new(Ok(())) }
        }
        fn answers(self, answer: Result<Option<Found>, String>) -> Self {
            self.check.borrow_mut().push(answer);
            self
        }
    }
    impl Source for FakeSource {
        type Package = ();
        fn check(&self) -> Result<Option<Found>, String> {
            self.log.borrow_mut().push("check");
            self.check.borrow_mut().remove(0)
        }
        fn download(&self, _: &Found) -> Result<(), String> {
            self.log.borrow_mut().push("download");
            self.download.borrow().clone()
        }
        fn install(&self, _: ()) -> Result<(), String> {
            self.log.borrow_mut().push("install");
            self.install.borrow().clone()
        }
    }
    fn found(v: &str) -> Found {
        Found { version: v.into(), notes_url: format!("https://github.com/thomasvan/lokki/releases/tag/v{v}") }
    }
    fn installs() -> Policy {
        Policy { can_install: true, reason: None }
    }
    fn downloads_only() -> Policy {
        Policy { can_install: false, reason: Some("this lokki is not an AppImage".into()) }
    }
    fn machine(source: FakeSource, policy: Policy) -> Machine<FakeSource, FakeClock> {
        Machine::new(source, FakeClock::default(), "0.1.0", policy)
    }
    fn stop_ok(log: &Log) -> impl FnOnce() -> Result<(), String> {
        let log = Rc::clone(log);
        move || { log.borrow_mut().push("stop"); Ok(()) }
    }
    fn resume(log: &Log) -> impl FnOnce() {
        let log = Rc::clone(log);
        move || log.borrow_mut().push("resume")
    }

    #[test]
    fn a_check_reports_available_up_to_date_or_the_error() {
        // (label, the source's answer, state, version, error contains)
        let rows: Vec<(&str, Result<Option<Found>, String>, &str, Option<&str>, Option<&str>)> = vec![
            ("a newer release", Ok(Some(found("0.2.0"))), "available", Some("0.2.0"), None),
            ("nothing newer", Ok(None), "up_to_date", None, None),
            ("the endpoint failed", Err("the update check failed: 404".into()), "error", None, Some("404")),
        ];
        for (label, answer, state, version, error) in rows {
            let log = Log::default();
            let m = machine(FakeSource::new(&log).answers(answer), installs());
            let status = m.check();
            assert_eq!(status.state, state, "{label}");
            assert_eq!(status.version.as_deref(), version, "{label}");
            assert_eq!(status.error.is_some(), error.is_some(), "{label}");
            if let Some(e) = error {
                assert!(status.error.unwrap().contains(e), "{label}");
            }
            assert_eq!(status.current_version, "0.1.0");
            assert_eq!(status.notes_url.is_some(), version.is_some(), "{label}");
        }
    }

    #[test]
    fn a_manifest_without_this_platform_is_up_to_date_with_a_reason() {
        let log = Log::default();
        let m = machine(FakeSource::new(&log).answers(Err(NO_PLATFORM.into())), installs());
        let status = m.check();
        assert_eq!(status.state, "up_to_date");
        assert_eq!(status.reason.as_deref(), Some(NO_PLATFORM));
        assert!(status.error.is_none());
    }

    #[test]
    fn the_policy_decides_can_install() {
        // (label, os, APPIMAGE set, macInstall, can_install, reason contains)
        let rows = [
            ("an AppImage on Linux", Os::Linux, true, false, true, None),
            ("not an AppImage on Linux", Os::Linux, false, false, false, Some("AppImage")),
            ("Windows", Os::Windows, false, false, true, None),
            ("macOS without a Developer ID", Os::Macos, false, false, false, Some("signed")),
            ("macOS with the key turned on", Os::Macos, false, true, true, None),
        ];
        for (label, os, appimage, mac, want, reason) in rows {
            let env = |name: &str| (appimage && name == "APPIMAGE").then(|| "/opt/lokki.AppImage".to_string());
            let p = Policy::detect(os, env, mac);
            assert_eq!(p.can_install, want, "{label}");
            assert_eq!(p.reason.is_some(), reason.is_some(), "{label}");
            if let Some(r) = reason {
                assert!(p.reason.unwrap().contains(r), "{label}");
            }
        }
    }

    #[test]
    fn a_package_that_fails_verification_stops_nothing() {
        let log = Log::default();
        let source = FakeSource::new(&log).answers(Ok(Some(found("0.2.0"))));
        *source.download.borrow_mut() = Err("the download or its signature failed: signature verification failed".into());
        let m = machine(source, installs());
        m.check();
        let r = m.install(stop_ok(&log), resume(&log));
        assert!(r.unwrap_err().contains("signature"));
        assert_eq!(*log.borrow(), vec!["check", "download"], "neither stop nor resume ran");
        assert_eq!(m.status().state, "error");
        assert_eq!(m.status().version.as_deref(), Some("0.2.0"), "the update stays known");
    }

    #[test]
    fn an_alive_sidecar_blocks_the_install_and_the_sidecars_come_back() {
        let log = Log::default();
        let m = machine(FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))), installs());
        m.check();
        let stop = { let log = Rc::clone(&log); move || { log.borrow_mut().push("stop"); Err("the core (pid 4242) is still alive after the stop sequence".to_string()) } };
        let r = m.install(stop, resume(&log));
        assert!(r.unwrap_err().contains("4242"));
        assert_eq!(*log.borrow(), vec!["check", "download", "stop", "resume"]);
        assert_eq!(m.status().state, "error");
    }

    #[test]
    fn a_failed_install_restarts_the_sidecars() {
        let log = Log::default();
        let source = FakeSource::new(&log).answers(Ok(Some(found("0.2.0"))));
        *source.install.borrow_mut() = Err("the install failed: permission denied".into());
        let m = machine(source, installs());
        m.check();
        let r = m.install(stop_ok(&log), resume(&log));
        assert!(r.unwrap_err().contains("permission"));
        assert_eq!(*log.borrow(), vec!["check", "download", "stop", "install", "resume"]);
        assert_eq!(m.status().state, "error");
    }

    #[test]
    fn an_install_runs_download_then_stop_then_install_once() {
        let log = Log::default();
        let m = machine(FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))), installs());
        m.check();
        m.install(stop_ok(&log), resume(&log)).unwrap();
        assert_eq!(*log.borrow(), vec!["check", "download", "stop", "install"]);
        assert_eq!(m.status().state, "installing");
    }

    #[test]
    fn install_is_refused_without_an_available_update_or_the_policy_or_while_one_runs() {
        // (label, policy, check first, error contains)
        let rows = [
            ("nothing available", installs(), false, "no update"),
            ("not an AppImage", downloads_only(), true, "AppImage"),
        ];
        for (label, policy, check, err) in rows {
            let log = Log::default();
            let m = machine(FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))), policy);
            if check { m.check(); }
            let r = m.install(stop_ok(&log), resume(&log));
            assert!(r.unwrap_err().contains(err), "{label}");
            assert!(!log.borrow().contains(&"download"), "{label}");
        }
        let log = Log::default();
        let m = machine(FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))), installs());
        m.check();
        m.begin_install_for_test();
        assert!(m.install(stop_ok(&log), resume(&log)).unwrap_err().contains("already running"));
        assert_eq!(m.check().state, "installing", "no check runs during an install");
    }

    #[test]
    fn a_check_while_one_runs_returns_the_running_state() {
        let log = Log::default();
        let m = machine(FakeSource::new(&log), installs());
        m.begin_check_for_test();
        assert_eq!(m.check().state, "checking");
        assert!(log.borrow().is_empty(), "the source was not asked");
    }

    #[test]
    fn checks_are_due_a_minute_after_boot_then_daily_and_an_error_waits_for_the_tick() {
        let log = Log::default();
        let m = machine(FakeSource::new(&log).answers(Err("boom".into())).answers(Ok(None)), installs());
        assert!(!m.due(), "not before the first minute");
        m.clock.sleep(FIRST_CHECK);
        assert!(m.due());
        m.check();
        assert_eq!(m.status().state, "error");
        m.clock.sleep(Duration::from_secs(3600));
        assert!(!m.due(), "nothing retries inside the day");
        m.clock.sleep(DAILY);
        assert!(m.due());
        assert_eq!(m.check().state, "up_to_date");
    }
}
```

- [ ] **Step 2: Run** `cd app/src-tauri && cargo test --locked -p lokki_app update`. Expected: FAIL, "unresolved import `update`" (then, with the module stub, the missing items).

- [ ] **Step 3: Implement** `update.rs`:

```rust
//! The updater's state and the order of an install (spec 7): one state the page reads,
//! a check that never overlaps, an install that verifies, stops, proves, installs.
use lokki_logic::supervise::Clock;
use serde::Serialize;
use std::sync::Mutex;
use std::time::Duration;

pub const FIRST_CHECK: Duration = Duration::from_secs(60);
pub const DAILY: Duration = Duration::from_secs(24 * 3600);
pub const NO_PLATFORM: &str = "no build for this platform in the latest release";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Found {
    pub version: String,
    pub notes_url: String,
}

pub trait Source {
    /// What `download` verified; only `install` takes it.
    type Package;
    fn check(&self) -> Result<Option<Found>, String>;
    fn download(&self, found: &Found) -> Result<Self::Package, String>;
    fn install(&self, package: Self::Package) -> Result<(), String>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Os {
    Linux,
    Macos,
    Windows,
}

impl Os {
    pub fn current() -> Self {
        if cfg!(target_os = "windows") {
            Os::Windows
        } else if cfg!(target_os = "macos") {
            Os::Macos
        } else {
            Os::Linux
        }
    }
}

#[derive(Clone, Debug)]
pub struct Policy {
    pub can_install: bool,
    pub reason: Option<String>,
}

impl Policy {
    /// Linux installs only from an AppImage; Windows always; macOS when the build is signed
    /// (`plugins.lokki.macInstall`).
    pub fn detect(os: Os, env: impl Fn(&str) -> Option<String>, mac_install: bool) -> Self {
        let no = |reason: &str| Policy { can_install: false, reason: Some(reason.into()) };
        let yes = Policy { can_install: true, reason: None };
        match os {
            Os::Windows => yes,
            Os::Linux if env("APPIMAGE").is_some() => yes,
            Os::Linux => no("this lokki is not an AppImage; the updater replaces AppImages only, so download the new package"),
            Os::Macos if mac_install => yes,
            Os::Macos => no("installing is off on macOS until the build is signed; download the new .dmg"),
        }
    }
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct Status {
    pub state: String,
    pub current_version: String,
    pub version: Option<String>,
    pub notes_url: Option<String>,
    pub can_install: bool,
    pub reason: Option<String>,
    pub error: Option<String>,
}

struct Inner {
    state: &'static str,
    found: Option<Found>,
    error: Option<String>,
    reason: Option<String>,
    checked_at: Option<Duration>,
    checking: bool,
    installing: bool,
}

pub struct Machine<S: Source, C: Clock> {
    pub source: S,
    pub clock: C,
    current: String,
    policy: Policy,
    started: Duration,
    inner: Mutex<Inner>,
}

impl<S: Source, C: Clock> Machine<S, C> {
    pub fn new(source: S, clock: C, current: &str, policy: Policy) -> Self {
        let started = clock.now();
        Machine {
            source,
            clock,
            current: current.into(),
            policy,
            started,
            inner: Mutex::new(Inner {
                state: "idle",
                found: None,
                error: None,
                reason: None,
                checked_at: None,
                checking: false,
                installing: false,
            }),
        }
    }

    pub fn status(&self) -> Status {
        let i = self.inner.lock().unwrap();
        Status {
            state: i.state.into(),
            current_version: self.current.clone(),
            version: i.found.as_ref().map(|f| f.version.clone()),
            notes_url: i.found.as_ref().map(|f| f.notes_url.clone()),
            can_install: self.policy.can_install,
            reason: i.reason.clone().or_else(|| self.policy.reason.clone()),
            error: i.error.clone(),
        }
    }

    /// A check is due a minute after boot and then a day after the last one.
    pub fn due(&self) -> bool {
        let now = self.clock.now();
        let i = self.inner.lock().unwrap();
        match i.checked_at {
            None => now.saturating_sub(self.started) >= FIRST_CHECK,
            Some(at) => now.saturating_sub(at) >= DAILY,
        }
    }

    pub fn check(&self) -> Status {
        let busy = {
            let mut i = self.inner.lock().unwrap();
            let busy = i.checking || i.installing;
            if !busy {
                i.checking = true;
                i.state = "checking";
            }
            busy
        };
        if busy {
            return self.status();
        }
        let answer = self.source.check();
        {
            let mut i = self.inner.lock().unwrap();
            i.checking = false;
            i.checked_at = Some(self.clock.now());
            i.error = None;
            i.reason = None;
            match answer {
                Ok(Some(found)) => {
                    i.found = Some(found);
                    i.state = "available";
                }
                Ok(None) => {
                    i.found = None;
                    i.state = "up_to_date";
                }
                Err(e) if e == NO_PLATFORM => {
                    i.found = None;
                    i.state = "up_to_date";
                    i.reason = Some(e);
                }
                Err(e) => {
                    i.state = "error";
                    i.error = Some(e);
                }
            }
        }
        self.status()
    }

    /// Download and verify, then `stop` (the shell's stop sequence, which proves both
    /// sidecars gone or answers why not), then install. After a failure past the stop,
    /// `resume` starts the sidecars again. Any failure leaves `error` with the sentence.
    pub fn install(&self, stop: impl FnOnce() -> Result<(), String>, resume: impl FnOnce()) -> Result<(), String> {
        let found = {
            let mut i = self.inner.lock().unwrap();
            if i.installing {
                return Err("an install is already running".into());
            }
            let found = i.found.clone().ok_or_else(|| "no update is available to install".to_string())?;
            if !self.policy.can_install {
                return Err(self.policy.reason.clone().unwrap_or_else(|| "installing is off".into()));
            }
            i.installing = true;
            i.state = "downloading";
            found
        };
        let result = match self.source.download(&found) {
            Err(e) => Err(e),
            Ok(package) => match stop() {
                Err(e) => {
                    resume();
                    Err(e)
                }
                Ok(()) => {
                    self.set("installing");
                    match self.source.install(package) {
                        Ok(()) => Ok(()),
                        Err(e) => {
                            resume();
                            Err(e)
                        }
                    }
                }
            },
        };
        let mut i = self.inner.lock().unwrap();
        if let Err(e) = &result {
            i.installing = false;
            i.state = "error";
            i.error = Some(e.clone());
        }
        result
    }

    fn set(&self, state: &'static str) {
        self.inner.lock().unwrap().state = state;
    }

    #[cfg(test)]
    fn begin_check_for_test(&self) {
        let mut i = self.inner.lock().unwrap();
        i.checking = true;
        i.state = "checking";
    }

    #[cfg(test)]
    fn begin_install_for_test(&self) {
        let mut i = self.inner.lock().unwrap();
        i.installing = true;
        i.state = "installing";
    }
}
```
(`installing` stays set after a clean install: the app is about to restart, and a second click must not start a second install.)

- [ ] **Step 4: `FakeClock` has one home.** Move the `FakeClock` from `sidecars.rs`'s tests into `lib.rs`'s `#[cfg(test)] pub(crate) mod testing` and use it from both test modules; nothing else in `sidecars.rs` changes.

- [ ] **Step 5: Run** the tests. Expected: PASS, 10 tests, and `sidecars.rs`'s tests still green on the shared fake.

- [ ] **Step 6: Gate and commit**

Run: `cd app/src-tauri && cargo fmt --check && cargo clippy --locked --workspace -- -D warnings && cargo test --locked --workspace`
Expected: green; `lokki_app` reports its 7 tests plus the 10 new ones.

```bash
git add app/src-tauri/src/update.rs app/src-tauri/src/lib.rs app/src-tauri/src/sidecars.rs
git commit -m "app: the update machine: states, the install policy, and the verified-stop-proved-install order, on fakes"
```

---

### Task 4: The plugin in the shell: the source, the commands, the proof after the stop, the smoke

**Files:**
- Modify: `app/src-tauri/Cargo.toml` (`tauri-plugin-updater = { version = "=2.13.3", default-features = false, features = ["rustls-tls", "system-proxy"] }`; `[dev-dependencies] tauri = { version = "=2.12.3", features = ["test"] }`), `app/src-tauri/Cargo.lock`, `app/src-tauri/tauri.conf.json` (`plugins.updater.{pubkey, endpoints, requireSignedVersion, windows.installMode}`, `plugins.lokki.macInstall`), `app/src-tauri/build.rs` (`COMMANDS` + 3), `app/src-tauri/src/capability.rs` (`grant_main` + 3 permissions; the docstring says seven hooks), `app/src-tauri/src/commands.rs` (3 commands; the module comment's count), `app/src-tauri/src/update.rs` (`PluginSource`, `is_no_platform`, the loopback-manifest test), `app/src-tauri/src/sidecars.rs` (`group_alive`, `Sidecars::stopped_pids`, `Sidecars::alive_after_stop`), `app/src-tauri/src/lib.rs` (`Shell.update`, the thread, `update_status`/`check_for_update`/`install_update`, `SMOKE_PAGE` and `smoke_done`), `app/inject/inject.js` (3 hooks), `app/inject/inject.test.mjs` (rows and the `lokki.d.ts` assertion), `app/smoke/smoke.sh` (the two probes, the two lines; the comment's count), `app/scripts/release_manifest.py` (`DEV_PUBKEY`'s value), `docs/app/update.md` (new), `docs/app/first-run.md` and `docs/README.md` (a link)

**Interfaces:**
- Consumes: Task 3's `Machine`, `Policy`, `Source`, `Found`, `NO_PLATFORM`; `Shell::stop_all`; `Sidecars::start_all(cx, conf)`; `Context.clock` (`RealClock`); `SMOKE_PAGE`; `capability::grant_main`.
- Produces: `update::PluginSource<R: tauri::Runtime> { app: AppHandle<R>, endpoint: Result<Option<Url>, String> }` implementing `Source` with `type Package = (tauri_plugin_updater::Update, Vec<u8>)`; `update::is_no_platform(&tauri_plugin_updater::Error) -> bool`; `sidecars::group_alive(pid: u32) -> bool`; `sidecars::GONE_WAIT` (2 s); `Sidecars::stopped_pids() -> Vec<(Service, u32)>` (the pids of the children the last `stop` calls took) and `Sidecars::alive_after_stop(clock: &impl Clock, limit: Duration) -> Vec<(Service, u32)>` (those of them whose group is still alive once `limit` has passed); the commands `update_status`, `check_for_update`, `install_update`; the hooks `window.lokki.updateStatus(): Promise<Status>`, `checkForUpdate(): Promise<Status>`, `installUpdate(): Promise<void>` (rejects with the sentence); the smoke lines `smoke: update state=<state>` and `smoke: install refused=<sentence>`.

- [ ] **Step 1: The proof after the stop, test first** in `sidecars.rs`'s tests (Unix only; the Windows probe is confirmed by the hosted run, which the body says):

```rust
    #[cfg(unix)]
    #[test]
    fn a_process_group_is_alive_until_its_members_are_gone() {
        use std::os::unix::process::CommandExt;
        let mut child = std::process::Command::new("sleep").arg("30").process_group(0).spawn().unwrap();
        let pid = child.id();
        assert!(group_alive(pid), "a group with a live member");
        unsafe { libc::kill(-(pid as i32), libc::SIGKILL) };
        child.wait().unwrap();
        assert!(!group_alive(pid), "a group whose only member is reaped");
    }
```
`group_alive`: on Unix `unsafe { libc::kill(-(pid as libc::pid_t), 0) == 0 }` (ESRCH means no member; EPERM would mean a member of another user, which cannot be ours: treat it as alive); on Windows `tasklist /FI "PID eq <pid>" /NH` printed by `std::process::Command` with `CREATE_NO_WINDOW`, alive when the output names the pid. The Windows probe runs nowhere in CI (the hosted jobs build installers and never run `cargo test`): the body says so, and the Human's first Windows run is its test. `Sidecars::stop` first removes the service's entry from `stopped: Vec<(Service, u32)>` (a service with no child leaves none, so a reused pid is never probed), then records `(service, pid)` of the child it takes; `stopped_pids()` returns the list; `alive_after_stop(clock, limit)` polls `group_alive` for each entry every 100 ms of the clock until none answers alive or `limit` (`GONE_WAIT`, 2 s) has passed, and returns those still alive (a reaped leader can leave a dying group member for a few ms; the wait keeps a clean stop from reading as a live one). The recording has its own test, `stop_records_the_pid_it_took_and_nothing_for_a_service_without_one`, on a real short-lived child, since `Slot.proc` wraps a real `std::process::Child` and `stop` takes a `Context` (Unix only, like the probe test): a `sleep 30` spawned in its own process group and wrapped the way `start` wraps a child (`Proc::new`, or whatever the slot holds), put into `Service::Cliproxy`'s slot (no HTTP on that path), a test `Context` with a scratch log directory; after `stop`, `stopped_pids()` is `[(Service::Cliproxy, <that child's pid>)]` and `group_alive` of it is false; after `stop` on the empty `Service::Core` slot the list still holds only the CLIProxyAPI entry; a second child in the slot and a second `stop` replace the entry. The test removes its scratch directory.

- [ ] **Step 2: The real source and its test.** In `update.rs`:

```rust
/// The plugin, from Rust only. The endpoint list is `LOKKI_UPDATE_ENDPOINT` when set, else
/// the configured one; the public key is always the configured one. Generic over the
/// runtime: the app runs on `Wry`, the loopback test on `tauri::test::MockRuntime`.
pub struct PluginSource<R: tauri::Runtime> {
    app: tauri::AppHandle<R>,
    endpoint: Result<Option<tauri::Url>, String>,
}

impl<R: tauri::Runtime> PluginSource<R> {
    pub fn new(app: tauri::AppHandle<R>, endpoint_override: Option<String>) -> Self {
        let endpoint = endpoint_override
            .map(|s| s.parse::<tauri::Url>().map_err(|e| format!("LOKKI_UPDATE_ENDPOINT is not a URL: {e}")))
            .transpose();
        PluginSource { app, endpoint }
    }

    fn updater(&self) -> Result<tauri_plugin_updater::Updater, String> {
        use tauri_plugin_updater::UpdaterExt;
        let mut b = self.app.updater_builder();
        if let Some(url) = self.endpoint.clone()? {
            // A loopback endpoint (a hand test, the test below) never goes through a proxy.
            if matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]")) {
                b = b.no_proxy();
            }
            b = b.endpoints(vec![url]).map_err(|e| format!("the update endpoint is refused: {e}"))?;
        }
        b.build().map_err(|e| format!("the updater could not start: {e}"))
    }

    fn check_raw(&self) -> Result<Option<tauri_plugin_updater::Update>, String> {
        let updater = self.updater()?;
        match tauri::async_runtime::block_on(updater.check()) {
            Ok(u) => Ok(u),
            Err(e) if is_no_platform(&e) => Err(NO_PLATFORM.into()),
            Err(e) => Err(format!("the update check failed: {e}")),
        }
    }
}

pub fn is_no_platform(e: &tauri_plugin_updater::Error) -> bool {
    use tauri_plugin_updater::Error;
    matches!(e, Error::TargetNotFound(_) | Error::TargetsNotFound(_))
}

impl<R: tauri::Runtime> Source for PluginSource<R> {
    type Package = (tauri_plugin_updater::Update, Vec<u8>);

    fn check(&self) -> Result<Option<Found>, String> {
        Ok(self.check_raw()?.map(|u| Found {
            version: u.version.clone(),
            notes_url: format!("https://github.com/thomasvan/lokki/releases/tag/v{}", u.version),
        }))
    }

    fn download(&self, found: &Found) -> Result<Self::Package, String> {
        let update = self
            .check_raw()?
            .filter(|u| u.version == found.version)
            .ok_or_else(|| "the release changed since the check; check again".to_string())?;
        let bytes = tauri::async_runtime::block_on(update.download(|_, _| {}, || {}))
            .map_err(|e| format!("the download or its signature failed: {e}"))?;
        Ok((update, bytes))
    }

    fn install(&self, (update, bytes): Self::Package) -> Result<(), String> {
        update.install(bytes).map_err(|e| format!("the install failed: {e}"))
    }
}
```
The test, in the same `mod tests`, runs the real plugin against a loopback server (debug builds accept `http`, so this is `cargo test`'s path and never a release binary's):

```rust
    /// One HTTP/1.1 answer on an ephemeral loopback port; the thread ends after that answer,
    /// or with the test process if nothing ever connects.
    fn serve_once(body: String) -> String {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://127.0.0.1:{}/latest.json", listener.local_addr().unwrap().port());
        std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = [0u8; 4096];
            let _ = s.read(&mut buf);
            let _ = write!(s, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body);
        });
        url
    }

    fn mock_app() -> tauri::App<tauri::test::MockRuntime> {
        // `mock_context` already names the version 0.1.0.
        let mut context = tauri::test::mock_context(tauri::test::noop_assets());
        context.config_mut().plugins.0.insert(
            "updater".into(),
            serde_json::json!({ "pubkey": DEV_PUBKEY_FOR_TESTS, "endpoints": ["https://example.invalid/latest.json"] }),
        );
        tauri::test::mock_builder()
            .plugin(tauri_plugin_updater::Builder::new().build())
            .build(context)
            .unwrap()
    }

    #[test]
    fn the_plugin_answers_from_a_loopback_manifest() {
        // (label, the manifest's version and platforms, the answer)
        let here = format!("{}-{}", if cfg!(target_os = "macos") { "darwin" } else if cfg!(windows) { "windows" } else { "linux" }, std::env::consts::ARCH);
        let platform = |key: &str| format!(r#""{key}": {{"signature": "AAAA", "url": "http://127.0.0.1:1/none"}}"#);
        let rows = [
            ("newer, for this platform", "9.9.9", platform(&here), Ok(Some("9.9.9"))),
            ("not newer", "0.1.0", platform(&here), Ok(None)),
            ("no platform", "9.9.9", platform("plan9-mips"), Err(NO_PLATFORM)),
        ];
        let app = mock_app();
        for (label, version, platforms, want) in rows {
            let url = serve_once(format!(r#"{{"version": "{version}", "notes": "", "pub_date": "2026-01-01T00:00:00Z", "platforms": {{{platforms}}}}}"#));
            let source = PluginSource::new(app.handle().clone(), Some(url));
            let got = source.check().map(|f| f.map(|f| f.version));
            match want {
                Ok(v) => assert_eq!(got.unwrap().as_deref(), v, "{label}"),
                Err(e) => assert_eq!(got.unwrap_err(), e, "{label}"),
            }
        }
        let bad = PluginSource::new(app.handle().clone(), Some("not a url".into()));
        assert!(bad.check().unwrap_err().contains("LOKKI_UPDATE_ENDPOINT"));
    }

    #[test]
    fn the_no_platform_errors_are_the_plugins_two() {
        use tauri_plugin_updater::Error;
        assert!(is_no_platform(&Error::TargetsNotFound(vec!["linux-x86_64".into()])));
        assert!(is_no_platform(&Error::TargetNotFound("linux-x86_64".into())));
        assert!(!is_no_platform(&Error::ReleaseNotFound));
    }
```
`DEV_PUBKEY_FOR_TESTS` is the development public key (Step 4), spelt in the test as a constant read from `tauri.conf.json` at compile time (`include_str!` of the file and a `serde_json` read in the test), so the test and the app carry one key. `mock_app()` answers an `App<MockRuntime>`, whose handle is an `AppHandle<MockRuntime>`: that is why `PluginSource` is generic. The Lead confirms `tauri::test::mock_context`, `mock_builder`, `noop_assets`, `config_mut()` and `MockRuntime` as named in `tauri` 2.12.3 and adjusts, saying so in the body.

- [ ] **Step 3: `Shell` and the commands.** In `lib.rs`: a field `update: OnceLock<Machine<PluginSource<tauri::Wry>, RealClock>>` set in `setup` from `Policy::detect(Os::current(), |n| std::env::var(n).ok(), mac_install)` where `mac_install` is `app.config().plugins.0.get("lokki").and_then(|v| v.get("macInstall")).and_then(|v| v.as_bool()).unwrap_or(false)`, `PluginSource::new(app.handle().clone(), std::env::var("LOKKI_UPDATE_ENDPOINT").ok())`, the current version `app.package_info().version.to_string()` (the `version` the config override set from `core/VERSION`), and the shell's `RealClock`; a thread, not started under `LOKKI_SMOKE` (the smoke makes no network call), `loop { thread::sleep(MONITOR_TICK * 4); if !shell.quitting.load(..) && shell.update().due() { shell.update().check(); } }`. `Shell::update_status()`, `Shell::check_for_update()` (the machine's), and:

```rust
    /// Download and verify, stop both sidecars and prove them gone, install, restart. A
    /// failure after the stop starts the sidecars again and is reported.
    fn install_update(self: &Arc<Self>) -> Result<(), String> {
        let stop = || {
            self.stop_all();
            let alive = self.side.lock().unwrap().alive_after_stop(&self.cx.clock, GONE_WAIT);
            if alive.is_empty() {
                Ok(())
            } else {
                Err(alive
                    .iter()
                    .map(|(s, pid)| format!("{} (pid {pid}) is still alive after the stop sequence", s.title()))
                    .collect::<Vec<_>>()
                    .join("; "))
            }
        };
        let resume = || {
            if let Some(conf) = self.conf() {
                let mut side = self.side.lock().unwrap();
                if let Err((service, e)) = side.start_all(&self.cx, &conf) {
                    self.cx.log.line(format_args!("after the failed install, {} did not start: {e}", service.name()));
                }
            }
        };
        self.update().install(stop, resume)?;
        self.cx.log.line("update installed; restarting");
        self.app.restart()
    }
```
(`AppHandle::restart()` returns `!`; the function's last expression is it.) In `commands.rs`:

```rust
#[tauri::command]
pub async fn update_status(shell: State<'_, Arc<Shell>>) -> Result<update::Status, String> {
    Ok(shell.update_status())
}

#[tauri::command]
pub async fn check_for_update(shell: State<'_, Arc<Shell>>) -> Result<update::Status, String> {
    let shell = Arc::clone(&shell);
    tauri::async_runtime::spawn_blocking(move || shell.check_for_update())
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn install_update(shell: State<'_, Arc<Shell>>) -> Answer {
    let shell = Arc::clone(&shell);
    tauri::async_runtime::spawn_blocking(move || shell.install_update())
        .await
        .map_err(|e| e.to_string())?
}
```
`build.rs`: `COMMANDS` gains `"update_status", "check_for_update", "install_update"`. `capability.rs`: `grant_main` gains `allow-update-status`, `allow-check-for-update`, `allow-install-update`, and its docstring says seven hooks; `commands.rs`'s module comment says nine commands. `lib.rs`: `.plugin(tauri_plugin_updater::Builder::new().build())` and the three commands in `generate_handler!`.

- [ ] **Step 4: The config and the development key.** `cd app && npx tauri signer generate -w /tmp/lokki-dev.key` with an empty password; copy the printed public key into `tauri.conf.json` and into `release_manifest.py`'s `DEV_PUBKEY`; delete `/tmp/lokki-dev.key` and say so in the body. `tauri.conf.json` gains:

```json
  "plugins": {
    "updater": {
      "pubkey": "<the development public key>",
      "endpoints": ["https://github.com/thomasvan/lokki/releases/latest/download/latest.json"],
      "requireSignedVersion": true,
      "windows": { "installMode": "passive" }
    },
    "lokki": { "macInstall": false }
  }
```
`bundle.createUpdaterArtifacts` is not set here (a build without the private key would fail); Task 6's release overlay sets it. `requireSignedVersion: true` stays only if the signature the development key made carries `version:` in its trusted comment (the Lead-confirms fact above); otherwise the key is left out, and the body and spec 7 say the downgrade pairing stays open until a CLI that records the version.

- [ ] **Step 5: The hooks.** `inject.js`'s `window.lokki` gains:

```js
    updateStatus: () => call("update_status"),
    checkForUpdate: () => call("check_for_update"),
    installUpdate: () => call("install_update"),
```
`inject.test.mjs`: three rows in `lokki_hooks_invoke_their_commands` (hook → command, no arguments), and one more assertion in that test: the property names in `ui/src/lib/lokki.d.ts` (read the file, match `/^\s+(\w+)\?:\s*\(/gm`) equal the hooks `inject.js` defines, both ways. The test count stays 8; the body's sentence for the assertion: "`lokki.d.ts` names exactly the hooks `inject.js` defines, so the UI's types cannot drift from the shell". `lokki.d.ts` gains the three hook types in this task (Task 5 adds the `UpdateStatus` type's use; the type itself lands here):

```ts
interface UpdateStatus {
  state: "idle" | "checking" | "up_to_date" | "available" | "downloading" | "installing" | "error";
  current_version: string;
  version: string | null;
  notes_url: string | null;
  can_install: boolean;
  reason: string | null;
  error: string | null;
}
interface Window {
  lokki?: {
    restart?: (service: "core" | "cliproxy") => Promise<void>;
    openExternal?: (url: string) => Promise<void>;
    openPanel?: () => Promise<void>;
    copyManagementSecret?: () => Promise<void>;
    updateStatus?: () => Promise<UpdateStatus>;
    checkForUpdate?: () => Promise<UpdateStatus>;
    installUpdate?: () => Promise<void>;
  };
}
```
(`cd ui && npm run check` stays green: the UI does not use them yet.)

- [ ] **Step 6: The smoke.** `SMOKE_PAGE` in `lib.rs` gains, after the grant probes and before `restart("cliproxy")`, two more probes in the `grant` string, `plugin:updater|check` and `plugin:updater|download_and_install` (both must be refused), and two reads it passes to `smoke_done` as `update: String` and `install: String`: `const update = await window.lokki.updateStatus().then((s) => s.state, (e) => "rejected: " + e)` and `const install = await window.lokki.installUpdate().then(() => "accepted", (e) => String(e))`; `smoke_done` prints `smoke: update state=<update>` and `smoke: install refused=<install>`. The script asserts `smoke: update state=idle` (no check runs under `LOKKI_SMOKE`: the daily thread is not started, so the smoke makes no network call), `smoke: install refused=no update is available to install` (the machine's first refusal, before the policy, before any download or stop; `installUpdate()` runs before `restart("cliproxy")` in the page sequence, and the sentence is the evidence that nothing was stopped), and that the grant line now holds `plugin:updater|check=refused plugin:updater|download_and_install=refused`. The secrets grep and the loopback checks stay; the comment at `smoke.sh:126` says seven hooks.

- [ ] **Step 7: `docs/app/update.md`** (new): how the app learns of a release (a minute after start, daily, "Check now"), the banner, what "Install and restart" does (verify, stop both sidecars cleanly and prove them gone, install, restart; a failure after the stop starts them again and says why), when it is "Download" instead (not an AppImage; macOS until signing, with the re-quarantine sentence from spec 6.4), that the data directory is never touched, the `latest.json` endpoint and `requireSignedVersion`, that the repository must be public for the check to answer, and `LOKKI_UPDATE_ENDPOINT` for a hand test (`https` on a release build). `docs/app/first-run.md` and `docs/README.md` link it.

- [ ] **Step 8: Gate and commit**

Run: `cd ui && npm run check && cd ../app && node --test 'inject/*.test.mjs' && cd src-tauri && cargo fmt --check && cargo clippy --locked --workspace -- -D warnings && cargo test --locked --workspace && cd .. && python3 scripts/sidecars.py --config-json --out /tmp/lokki-tauri.json && npm run tauri build -- --no-bundle --config /tmp/lokki-tauri.json -- --locked && bash smoke/smoke.sh`
Expected: `npm run check` 0 errors (the types grew, nothing uses them yet), `tests 8` (inject) green, `lokki_app`'s tests green including `the_plugin_answers_from_a_loopback_manifest`, `the_no_platform_errors_are_the_plugins_two`, `a_process_group_is_alive_until_its_members_are_gone` and `stop_records_the_pid_it_took_and_nothing_for_a_service_without_one`, the smoke's lines including `smoke: update state=idle` and `smoke: install refused=no update is available to install`, exit 0. (The sidecars are built as in sub-project 4's Task 4 Step 3 before the `tauri build`.)

```bash
git add app/src-tauri/Cargo.toml app/src-tauri/Cargo.lock app/src-tauri/tauri.conf.json app/src-tauri/build.rs app/src-tauri/src app/inject app/smoke/smoke.sh app/scripts/release_manifest.py ui/src/lib/lokki.d.ts docs/app/update.md docs/app/first-run.md docs/README.md
git commit -m "app: the updater plugin behind three page commands, the proof after the stop, the smoke's probes"
```

---

### Task 5: The UI banner

**Files:**
- Create: `ui/src/views/Update.svelte`, `ui/src/views/Update.test.ts`
- Modify: `ui/src/lib/state.svelte.ts` (`app.update`, `loadUpdate`, `checkNow`, `installUpdate`, the status poll), `ui/src/lib/state.test.ts` (one new test), `ui/src/App.svelte` (the banner above the tabs), `docs/app/ui.md` (the hooks and the banner)

**Interfaces:**
- Consumes: `window.lokki.updateStatus()`, `checkForUpdate()`, `installUpdate()`, `openExternal(url)` with the `UpdateStatus` type of Task 4.
- Produces: `app.update: UpdateStatus | null` in the state (`null` in a plain browser); `loadUpdate()` (reads `updateStatus()` when the hook exists, else leaves `null`), polled with the status every 10 s; `checkNow()`; `installUpdate()`; the banner renders whenever `app.update` is not `null`: one line with the version and "Check now" in `idle` and `up_to_date`, the full banner in `available`, a sentence in `checking`, `downloading` and `installing`, the error and "Check now" in `error`.

- [ ] **Step 1: Failing tests**, `ui/src/views/Update.test.ts` (vitest and testing-library as the other view tests):

```ts
import { fireEvent, render, screen } from "@testing-library/svelte";
import { tick } from "svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Update from "./Update.svelte";
import { app } from "../lib/state.svelte";

type S = NonNullable<typeof app.update>;
const status = (over: Partial<S>): S => ({
  state: "available", current_version: "0.1.0", version: "0.2.0",
  notes_url: "https://github.com/thomasvan/lokki/releases/tag/v0.2.0", can_install: true,
  reason: null, error: null, ...over,
});

describe("the update banner", () => {
  beforeEach(() => { app.update = null; delete (window as { lokki?: unknown }).lokki; });

  it("renders per state", () => {
    // (label, status, text shown, text absent)
    const rows: Array<[string, Partial<S> | null, string[], string[]]> = [
      ["a plain browser", null, [], ["Check now", "available"]],
      ["idle", { state: "idle", version: null, notes_url: null }, ["lokki 0.1.0", "Check now"], ["available"]],
      ["up to date", { state: "up_to_date", version: null, notes_url: null }, ["lokki 0.1.0 is up to date", "Check now"], ["Install"]],
      ["available and installable", {}, ["lokki 0.2.0 is available", "Release notes", "Install and restart", "Check now"], ["Download"]],
      ["available, download only", { can_install: false, reason: "this lokki is not an AppImage" }, ["lokki 0.2.0 is available", "Download", "this lokki is not an AppImage"], ["Install and restart"]],
      ["checking", { state: "checking", version: null, notes_url: null }, ["Checking"], ["Check now"]],
      ["downloading", { state: "downloading" }, ["Downloading"], ["Install and restart"]],
      ["installing", { state: "installing" }, ["Installing"], ["Install and restart"]],
      ["error", { state: "error", version: null, notes_url: null, error: "the update check failed: 404" }, ["the update check failed: 404", "Check now"], ["Install"]],
      ["no build for this platform", { state: "up_to_date", version: null, notes_url: null, reason: "no build for this platform in the latest release" }, ["no build for this platform"], ["Install"]],
      ["an install that failed keeps the update", { state: "error", error: "the core (pid 4242) is still alive" }, ["4242", "Install and restart"], []],
    ];
    for (const [label, over, shown, absent] of rows) {
      app.update = over === null ? null : status(over);
      const { unmount } = render(Update);
      for (const t of shown) expect(screen.queryByText(t, { exact: false }), label).not.toBeNull();
      for (const t of absent) expect(screen.queryByText(t, { exact: false }), label).toBeNull();
      unmount();
    }
  });

  it("the buttons call the hooks and the state follows", async () => {
    const lokki = {
      installUpdate: vi.fn(async () => {}),
      checkForUpdate: vi.fn(async () => status({ state: "up_to_date", version: null, notes_url: null })),
      openExternal: vi.fn(async () => {}),
    };
    (window as { lokki?: typeof lokki }).lokki = lokki;
    app.update = status({});
    render(Update);
    await fireEvent.click(screen.getByText("Release notes"));
    expect(lokki.openExternal).toHaveBeenCalledWith("https://github.com/thomasvan/lokki/releases/tag/v0.2.0");
    await fireEvent.click(screen.getByText("Install and restart"));
    expect(lokki.installUpdate).toHaveBeenCalledTimes(1);
    expect(app.update?.state).toBe("downloading");
    app.update = status({});
    await tick();
    await fireEvent.click(screen.getByText("Check now"));
    expect(lokki.checkForUpdate).toHaveBeenCalledTimes(1);
    expect(app.update?.state).toBe("up_to_date");
  });

  it("an install that rejects shows the sentence and keeps the button", async () => {
    (window as { lokki?: unknown }).lokki = { installUpdate: vi.fn(async () => { throw new Error("the core (pid 4242) is still alive"); }) };
    app.update = status({});
    render(Update);
    await fireEvent.click(screen.getByText("Install and restart"));
    expect(await screen.findByText("the core (pid 4242) is still alive", { exact: false })).toBeTruthy();
    expect(screen.queryByText("Install and restart")).not.toBeNull();
  });
});
```
and a new test in `ui/src/lib/state.test.ts` beside the polling ones, `it("reads the update state with every status poll when the shell provides the hook", ...)`: with fake timers and no `window.lokki`, `startPolling("overview")` leaves `app.update` `null` after 20 s; with `window.lokki.updateStatus` a `vi.fn` answering a status, it is called at once and again after 10 s and `app.update` holds the answer.

- [ ] **Step 2: Run** `cd ui && npm test -- Update`. Expected: FAIL, cannot resolve `./Update.svelte`.

- [ ] **Step 3: Implement.** `state.svelte.ts`:

```ts
import { VERSION } from "./version";
export const emptyUpdate = (): UpdateStatus => ({
  state: "idle", current_version: VERSION, version: null, notes_url: null, can_install: false, reason: null, error: null,
});
// `app.update` is null in a plain browser; the banner renders only when the shell answered.
export const loadUpdate = async () => {
  const read = window.lokki?.updateStatus;
  if (!read) return;
  try {
    app.update = await read();
  } catch (e) {
    app.update = { ...(app.update ?? emptyUpdate()), state: "error", error: errorText(e) };
  }
};
export const checkNow = async () => {
  const check = window.lokki?.checkForUpdate;
  if (!check) return;
  app.update = { ...(app.update ?? emptyUpdate()), state: "checking", error: null };
  try { app.update = await check(); } catch (e) { app.update = { ...(app.update ?? emptyUpdate()), state: "error", error: errorText(e) }; }
};
export const installUpdate = async () => {
  const install = window.lokki?.installUpdate;
  if (!install || !app.update) return;
  app.update = { ...app.update, state: "downloading", error: null };
  try { await install(); } catch (e) { app.update = { ...(app.update ?? emptyUpdate()), state: "error", error: errorText(e) }; }
};
```
with `update: null as UpdateStatus | null` in `app`, and `loadUpdate` added to the status poll's loads in `startPolling` for every tab (`polled(loadStatus, loadUpdate, ...)`; the shell answers from memory, so this costs one IPC call per poll). `Update.svelte`:

```svelte
<script lang="ts">
  import { app, checkNow, installUpdate } from "../lib/state.svelte";
  const open = (url: string) => (window.lokki?.openExternal ? window.lokki.openExternal(url) : Promise.resolve(window.open(url)));
  const latest = "https://github.com/thomasvan/lokki/releases/latest";
</script>

{#if app.update}
  {@const u = app.update}
  <aside class="update" role="status">
    {#if u.state === "available" || (u.state === "error" && u.version)}
      <strong>lokki {u.version} is available.</strong>
      <button onclick={() => open(u.notes_url ?? latest)}>Release notes</button>
      {#if u.can_install}
        <button onclick={installUpdate}>Install and restart</button>
      {:else}
        <button onclick={() => open(u.notes_url ?? latest)}>Download</button>
        {#if u.reason}<span>{u.reason}</span>{/if}
      {/if}
      <button onclick={checkNow}>Check now</button>
    {:else if u.state === "checking"}
      <span>Checking for an update…</span>
    {:else if u.state === "downloading"}
      <span>Downloading the update…</span>
    {:else if u.state === "installing"}
      <span>Installing the update; lokki restarts when it is done.</span>
    {:else if u.state === "up_to_date"}
      <span>lokki {u.current_version} is up to date.</span>
      {#if u.reason}<span>{u.reason}</span>{/if}
      <button onclick={checkNow}>Check now</button>
    {:else}
      <span>lokki {u.current_version}</span>
      <button onclick={checkNow}>Check now</button>
    {/if}
    {#if u.error}<span class="error">{u.error}</span>{/if}
  </aside>
{/if}
```
(An `error` without a version lands in the last branch: the version line, one "Check now", and the error sentence under it as small text, so an offline machine shows a quiet line, not a banner. `emptyUpdate` takes the version from `ui/src/lib/version.ts`'s `VERSION`, the build-time constant from `core/VERSION`. If `svelte-check` does not narrow `app.update` through `{@const u = app.update}`, bind `const u = $derived(app.update)` in the script and guard on `u` instead; `npm run check` decides.) `App.svelte` renders `<Update />` above the tab bar. `docs/app/ui.md`: the three hooks (optional, like the others), the banner's states and buttons, and that a plain browser shows no banner.

- [ ] **Step 4: Gate and commit**

Run: `cd ui && npm run check && npm test && npm run e2e`
Expected: 0 errors; the suite green with the three new view tests and the one state test; the e2e unchanged (a plain browser shows no banner).

```bash
git add ui/src/views/Update.svelte ui/src/views/Update.test.ts ui/src/lib/state.svelte.ts ui/src/lib/state.test.ts ui/src/App.svelte docs/app/ui.md
git commit -m "ui: the update banner over the three shell hooks"
```

---

### Task 6: The release job, the lock-major check, the protocol's gates, the README, the spec

**Files:**
- Create: `app/scripts/test_release_workflow.py` (the test beside the matrix)
- Modify: `.github/workflows/app.yml` (the triggers, the CI guards, the lock-major step, the four release jobs), `app/scripts/sidecars.py` (`--config-json --release` adds `bundle.createUpdaterArtifacts`), `app/scripts/test_sidecars.py` (one row), `WORKSPACE_PROTOCOL.md` (proof gates), `README.md` (an "Updates" subsection under "Desktop app"), `docs/superpowers/specs/2026-10-10-lokki-app-design.md` (sections 6.4, 7 and 10)

**Interfaces:**
- Consumes: `release_manifest.py`, `release_notes.py`, `check_lock_major.py`, `sidecars.py --versions`, the signing secrets, `gh`.
- Produces: `sidecars.py --config-json --release --out FILE` writing `{"version": ..., "bundle": {"externalBin": [...], "createUpdaterArtifacts": true}}`; the workflow's `prepare`, `release-linux`, `release-hosted` and `publish` jobs; `workflow_dispatch` inputs `tag` (default empty: the pushed tag) and `platforms` (`all` | `linux`, default `all`).

- [ ] **Step 1: The release overlay.** One row in `test_sidecars.py`'s config-json test: with `--release` the written JSON also holds `bundle.createUpdaterArtifacts: true`; without it the key is absent. `sidecars.py`'s `--config-json` branch: `if args.release: conf["bundle"]["createUpdaterArtifacts"] = True`.

- [ ] **Step 2: The test beside the matrix**, `app/scripts/test_release_workflow.py`:

```python
import os
import re
import unittest

import release_manifest as rm

WORKFLOW = os.path.join(rm.REPO, ".github", "workflows", "app.yml")


class WorkflowTests(unittest.TestCase):
    def test_the_workflow_names_every_platform_once(self):
        # app.yml's release matrix and its --require list are deliberate copies of PLATFORMS.
        with open(WORKFLOW, encoding="utf-8") as f:
            text = f.read()
        linux = text[text.index("  release-linux:"):text.index("  release-hosted:")]
        hosted = text[text.index("  release-hosted:"):text.index("  publish:")]
        publish = text[text.index("  publish:"):]
        self.assertIn("--target x86_64-unknown-linux-gnu", linux)
        self.assertEqual(sorted(re.findall(r"triple: ([^\s,}]+)", hosted)), sorted(t for t in rm.PLATFORMS if "linux" not in t))
        require = max(re.findall(r"REQ=([a-z0-9_,-]+)", publish), key=len)
        self.assertEqual(set(require.split(",")), {key for key, _ in rm.PLATFORMS.values()})
        self.assertNotIn("set -x", text)
```

- [ ] **Step 3: The triggers, the guards and the lock-major step.** `app.yml`:

```yaml
on:
  push:
    branches: [main]
    tags: ["v*"]
    paths: ["app/**", ".github/workflows/app.yml", "core/**", "ui/**"]
  pull_request:
    paths: ["app/**", ".github/workflows/app.yml", "core/lokki_core/quota/reader.py"]
  workflow_dispatch:
    inputs:
      tag:
        description: "The release tag to build (v<core/VERSION>, already pushed); empty runs the CI jobs only"
        default: ""
      platforms:
        description: "all, or linux for a deliberate Linux-only release"
        default: "all"
```
(`paths` does not filter a tag push, so the CI jobs carry a guard.) The `linux` job gets `if: ${{ !startsWith(github.ref, 'refs/tags/') && (github.event_name != 'workflow_dispatch' || inputs.tag == '') }}` and the `hosted` job `if: ${{ github.event_name == 'workflow_dispatch' && inputs.tag == '' }}`; a step after "Script tests":

```yaml
      - name: The lock's CLIProxyAPI major is the reader's
        working-directory: app/scripts
        run: python3 check_lock_major.py --lock ../cliproxy.lock --reader ../../core/lokki_core/quota/reader.py
```

- [ ] **Step 4: The release jobs.**

```yaml
  prepare:
    name: The draft release
    if: ${{ startsWith(github.ref, 'refs/tags/v') || (github.event_name == 'workflow_dispatch' && inputs.tag != '') }}
    runs-on: [self-hosted, Linux, X64]
    timeout-minutes: 15
    permissions:
      contents: write
    outputs:
      tag: ${{ steps.tag.outputs.tag }}
      version: ${{ steps.tag.outputs.version }}
    env:
      TAG: ${{ inputs.tag || github.ref_name }}
    steps:
      # The tag's own tree, whatever branch a dispatch came from; a tag that is not pushed
      # fails here, before anything is created.
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false, fetch-depth: 0, ref: "refs/tags/${{ inputs.tag || github.ref_name }}" }
      - id: tag
        name: The tag is core/VERSION and the key is the release key
        working-directory: app/scripts
        run: |
          python3 release_manifest.py --check-tag "$TAG"
          python3 release_manifest.py --check-pubkey ../src-tauri/tauri.conf.json
          echo "tag=$TAG" >> "$GITHUB_OUTPUT"
          echo "version=${TAG#v}" >> "$GITHUB_OUTPUT"
      - name: The notes and the draft
        working-directory: app/scripts
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          PREV="$(git describe --tags --abbrev=0 "$TAG^" 2>/dev/null || true)"
          BEFORE=""
          if [ -n "$PREV" ]; then git show "$PREV:app/cliproxy.lock" > "$RUNNER_TEMP/lock.before"; BEFORE="--lock-before $RUNNER_TEMP/lock.before"; fi
          NEW="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' ../cliproxy.lock)"
          gh release view "v$NEW" -R router-for-me/CLIProxyAPI --json body --jq .body > "$RUNNER_TEMP/cliproxy.md" || : > "$RUNNER_TEMP/cliproxy.md"
          git log --format=%s "${PREV:+$PREV..}$TAG" | python3 release_notes.py $BEFORE --lock-after ../cliproxy.lock --cliproxy-notes "$RUNNER_TEMP/cliproxy.md" > "$RUNNER_TEMP/notes.md"
          gh release create "$TAG" --draft --verify-tag --title "lokki ${TAG#v}" --notes-file "$RUNNER_TEMP/notes.md" -R "$GITHUB_REPOSITORY"

  release-linux:
    name: The Linux installers, signed
    needs: prepare
    runs-on: [self-hosted, Linux, X64]
    timeout-minutes: 60
    permissions:
      contents: write
    env:
      CARGO_TERM_COLOR: never
      APPIMAGE_EXTRACT_AND_RUN: "1"
      TAG: ${{ needs.prepare.outputs.tag }}
      VERSION: ${{ needs.prepare.outputs.version }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false, ref: "refs/tags/${{ needs.prepare.outputs.tag }}" }
      - name: Toolchain on the PATH
        run: |
          echo "CARGO_TARGET_DIR=$(dirname "$GITHUB_WORKSPACE")/app-target" >> "$GITHUB_ENV"
          echo "$HOME/.cargo/bin" >> "$GITHUB_PATH"
          echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - name: Python for the core sidecar
        run: |
          uv venv --python 3.13 "$RUNNER_TEMP/venv"
          uv pip install --python "$RUNNER_TEMP/venv/bin/python" ${{ env.PYTHON_BUILD_PACKAGES }}
          echo "$RUNNER_TEMP/venv/bin" >> "$GITHUB_PATH"
      - name: UI build
        working-directory: ui
        run: npm ci && npm run build
      - name: Sidecars
        working-directory: app
        run: |
          python scripts/fetch_cliproxy.py --lock cliproxy.lock --target x86_64-unknown-linux-gnu --out src-tauri/binaries
          python scripts/fetch_cliproxy.py --lock cliproxy.lock --target x86_64-unknown-linux-gnu --out src-tauri/binaries --check
          python scripts/build_core.py --out src-tauri/binaries --triple x86_64-unknown-linux-gnu --run
      - name: Build and sign the installers
        working-directory: app
        env:
          TAURI_SIGNING_PRIVATE_KEY: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}
          TAURI_SIGNING_PRIVATE_KEY_PASSWORD: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY_PASSWORD }}
        run: |
          python scripts/sidecars.py --config-json --release --out "$RUNNER_TEMP/tauri.json"
          npm ci && npm run tauri build -- --bundles deb,appimage --config "$RUNNER_TEMP/tauri.json" -- --locked
      - name: Upload to the draft
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          B="$CARGO_TARGET_DIR/release/bundle"
          gh release upload "$TAG" "$B/deb/lokki_${VERSION}_amd64.deb" "$B/appimage/lokki_${VERSION}_amd64.AppImage" "$B/appimage/lokki_${VERSION}_amd64.AppImage.sig" -R "$GITHUB_REPOSITORY" --clobber

  release-hosted:
    name: The ${{ matrix.triple }} installers, signed
    needs: prepare
    if: ${{ inputs.platforms != 'linux' }}
    strategy:
      fail-fast: false
      matrix:
        include:
          - { os: macos-latest, triple: aarch64-apple-darwin, bundles: "app,dmg", arch: aarch64 }
          - { os: macos-15-intel, triple: x86_64-apple-darwin, bundles: "app,dmg", arch: x64 }
          - { os: windows-latest, triple: x86_64-pc-windows-msvc, bundles: "nsis,msi", arch: x64 }
    runs-on: ${{ matrix.os }}
    timeout-minutes: 60
    permissions:
      contents: write
    env:
      TAG: ${{ needs.prepare.outputs.tag }}
      VERSION: ${{ needs.prepare.outputs.version }}
      ARCH: ${{ matrix.arch }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false, ref: "refs/tags/${{ needs.prepare.outputs.tag }}" }
      - uses: dtolnay/rust-toolchain@686976e191b89faba57d3206551f0f330d8cb249 # stable branch
        with: { toolchain: stable }
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5
        with: { python-version: "3.13" }
      - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0
        with: { node-version: 24 }
      - run: pip install ${{ env.PYTHON_BUILD_PACKAGES }}
        shell: bash
      - run: cd ui && npm ci && npm run build
        shell: bash
      - shell: bash
        run: cd app && python scripts/fetch_cliproxy.py --lock cliproxy.lock --target ${{ matrix.triple }} --out src-tauri/binaries && python scripts/fetch_cliproxy.py --lock cliproxy.lock --target ${{ matrix.triple }} --out src-tauri/binaries --check && python scripts/build_core.py --out src-tauri/binaries --triple ${{ matrix.triple }} --run
      - name: Build and sign the installers
        shell: bash
        env:
          TAURI_SIGNING_PRIVATE_KEY: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}
          TAURI_SIGNING_PRIVATE_KEY_PASSWORD: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY_PASSWORD }}
        run: cd app && python scripts/sidecars.py --config-json --release --out "$RUNNER_TEMP/tauri.json" && npm ci && npm run tauri build -- --bundles ${{ matrix.bundles }} --config "$RUNNER_TEMP/tauri.json" -- --locked
      - name: Upload to the draft, the updater archive named by version and arch
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          cd app/src-tauri/target/release/bundle
          if [ -d macos ]; then
            mv macos/lokki.app.tar.gz "macos/lokki_${VERSION}_${ARCH}.app.tar.gz"
            mv macos/lokki.app.tar.gz.sig "macos/lokki_${VERSION}_${ARCH}.app.tar.gz.sig"
            gh release upload "$TAG" dmg/*.dmg "macos/lokki_${VERSION}_${ARCH}.app.tar.gz" "macos/lokki_${VERSION}_${ARCH}.app.tar.gz.sig" -R "$GITHUB_REPOSITORY" --clobber
          else
            gh release upload "$TAG" "nsis/lokki_${VERSION}_${ARCH}-setup.exe" "nsis/lokki_${VERSION}_${ARCH}-setup.exe.sig" msi/*.msi -R "$GITHUB_REPOSITORY" --clobber
          fi

  publish:
    name: latest.json, then the release
    needs: [prepare, release-linux, release-hosted]
    if: ${{ always() && needs.prepare.result == 'success' && needs.release-linux.result == 'success' && (needs.release-hosted.result == 'success' || inputs.platforms == 'linux') }}
    runs-on: [self-hosted, Linux, X64]
    timeout-minutes: 15
    permissions:
      contents: write
    env:
      TAG: ${{ needs.prepare.outputs.tag }}
      VERSION: ${{ needs.prepare.outputs.version }}
      PLATFORMS: ${{ inputs.platforms || 'all' }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false, ref: "refs/tags/${{ needs.prepare.outputs.tag }}" }
      - name: The manifest from the assets that are there
        working-directory: app/scripts
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release view "$TAG" -R "$GITHUB_REPOSITORY" --json assets --jq '.assets[].name' > "$RUNNER_TEMP/names.txt"
          mkdir -p "$RUNNER_TEMP/sigs" && gh release download "$TAG" -R "$GITHUB_REPOSITORY" -p '*.sig' -D "$RUNNER_TEMP/sigs"
          gh release view "$TAG" -R "$GITHUB_REPOSITORY" --json body --jq .body > "$RUNNER_TEMP/notes.md"
          if [ "$PLATFORMS" = linux ]; then REQ=linux-x86_64; else REQ=linux-x86_64,darwin-aarch64,darwin-x86_64,windows-x86_64; fi
          python3 release_manifest.py --version "$VERSION" --tag "$TAG" --repo "$GITHUB_REPOSITORY" --names "$RUNNER_TEMP/names.txt" --signatures-dir "$RUNNER_TEMP/sigs" --require "$REQ" --pub-date "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --notes "$RUNNER_TEMP/notes.md" --out "$RUNNER_TEMP/latest.json"
          gh release upload "$TAG" "$RUNNER_TEMP/latest.json" -R "$GITHUB_REPOSITORY" --clobber
          gh release edit "$TAG" -R "$GITHUB_REPOSITORY" --draft=false
```
Every input reaches a `run:` line through `env` only. `--verify-tag` makes `gh release create` refuse to invent a tag. The upload names carry the version, so an older build left in the shared `app-target` is never uploaded. The Lead confirms, against the Tauri CLI, the NSIS installer's name (`lokki_<version>_x64-setup.exe`) and the macOS archive's (`lokki.app.tar.gz`), and adjusts the `mv` and the globs, saying so in the body.

- [ ] **Step 5: What a Lead verifies before the first tag.** `actionlint` and the YAML parse; `test_release_workflow.py` green; the four scripts run by hand with the arguments the YAML passes (a names file, a `sigs` directory with one real-format signature text, `--require linux-x86_64`); and `gh`'s handling of a draft, read from `gh`'s source at the installed version (`gh --version`; `pkg/cmd/release/shared/fetch.go` at that tag, read on GitHub: `FetchRelease` falls back to its draft lookup, `fetchDraftRelease` in `gh` 2.102, which lists the releases and matches the tag name, when `releases/tags/<tag>` answers 404; and that `view`, `download`, `upload` and `edit` all resolve the release through `FetchRelease`); the body quotes the function names and the tag of the source read. No draft is created on the repository before the Human's first tag: a release is the Human's act. A release run is not possible without a tag, and a tag is the Human's: the body says the release jobs are unrun until the first tag, and that the first tag proves the signing step, the artifact names per OS, the real `.sig` content, the hosted legs and an install from a real AppImage. The CI jobs' guard is proven by this branch's own `app.yml` run (push and PR events run `linux`, not `prepare`).

- [ ] **Step 6: The protocol, the README, the spec.** `WORKSPACE_PROTOCOL.md` proof gates: the app script tests line names `release_manifest.py`, `release_notes.py`, `check_lock_major.py` and `test_release_workflow.py` with what each proves; the App CI line adds "a pushed tag `v<core/VERSION>` builds, signs and publishes one release once every required platform's asset is up, and refuses the development key"; nothing else in the protocol changes. `README.md`, under "Desktop app", a subsection "Updates": the banner, "Install and restart" (what it does, that both services are stopped cleanly and proved gone first), the `.deb` and the macOS cases (the re-quarantine sentence: a downloaded unsigned `.dmg` is quarantined again and needs the same step as the first install), the glibc floor of the Linux build (2.43, Ubuntu 26.04), that releases live at `https://github.com/thomasvan/lokki/releases` and that the updater answers only once the repository is public. The spec: 6.4's "four hooks" becomes the seven with the three named; 7 gains decisions 3 (the install switch per OS, `plugins.lokki.macInstall`), 6 (the endpoint, `requireSignedVersion`, `LOKKI_UPDATE_ENDPOINT`), 7 (the Linux-only release as the Human's dispatch), 11 (`migrate` returns the renames applied; the table is empty today, and `upstream` is a key of its own, not a rename); 10 adds "the hosted macOS and Windows release jobs have not run on this account; the first release is Linux-only unless minutes exist", "the macOS install step stays off until a Developer ID signs the build", and "the updater answers 404 while the repository is private".

- [ ] **Step 7: Gate and commit**

Run: `cd app/scripts && python3 -m unittest && cd ../.. && actionlint .github/workflows/app.yml && git diff --check`
Expected: every script test green, actionlint silent.

```bash
git add .github/workflows/app.yml app/scripts/sidecars.py app/scripts/test_sidecars.py app/scripts/test_release_workflow.py WORKSPACE_PROTOCOL.md README.md docs/superpowers/specs/2026-10-10-lokki-app-design.md
git commit -m "app: the release job on a tag, the lock-major gate, the protocol's gates, the README's updates section"
```

The PR body names the checks CI cannot make: the first tagged release (the Human pushes `v0.1.0` once the key is in place and the repository is public), the install on an AppImage from a real release, the hosted installers.
