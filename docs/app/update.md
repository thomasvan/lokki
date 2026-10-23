# Updates

The desktop app's shell checks for, verifies and installs new releases of lokki. The
core never calls the network for updates, so a server or Docker deployment of the
core never contacts GitHub.

## When lokki checks

The shell checks a minute after it starts, then a day after its last check, and
whenever the page asks through `checkForUpdate()`. Two checks never overlap, and no
check runs during an install. A check that fails reports `error` with its sentence and
is not retried until the next daily check or the next `checkForUpdate()`.

A release with nothing newer than the running version is `up_to_date`. So is a release
with no build for this platform, with the reason "no build for this platform in the
latest release".

## What the page sees

The page reaches the updater only through three hooks, each a shell command its
window's grant names:

| Hook | Answer |
| --- | --- |
| `updateStatus()` | the state the shell holds |
| `checkForUpdate()` | a check now, then the state |
| `installUpdate()` | on success lokki restarts and the call never answers; otherwise it rejects with the sentence that says why not |

The state carries `state` (`idle`, `checking`, `up_to_date`, `available`,
`downloading`, `installing` or `error`), `current_version`, the new `version` and its
`notes_url`, `can_install` with the `reason` when it is false, and the `error`. The page
holds no permission of the updater plugin itself.

## What an install does

1. It downloads the package, allowing 15 minutes for the whole download, and verifies
   its signature. A package that fails to download or to verify stops nothing and
   installs nothing: the sidecars keep running and the state says why.
2. It runs the stop sequence that quitting runs: the core is drained through
   `POST /api/shutdown`, then CLIProxyAPI is stopped.
3. It probes each stopped sidecar's process group for up to 2 seconds. A sidecar still
   alive after that blocks the install, and the error names it with its pid.
4. It installs the package and restarts lokki.

From the stop until the sidecars run again, restarting a sidecar from the page or the
error page is refused with "an update is being installed", so nothing starts one
between the stop and the proof.

When step 3 or step 4 fails, the shell starts both sidecars again, keeps the window
open, and reports the failure in the state; when starting them again fails too, the
state and `logs/app.log` say that as well. An install never touches the data
directory.

On Windows the installer runs in passive mode.

On macOS the package is the `.app.tar.gz` the release publishes, and the whole
`lokki.app`, sidecars included, is replaced with it. The shell checks that the bundle
can be replaced at start, again when "Install and restart" is pressed, before the
download, and again after step 3, just before the install; a refusal after the stop
starts both sidecars again and is the install's error. Between that last check and the
swap the updater extracts the archive, a few seconds. A permission change inside that
window, or a refusal the permission bits do not show, still reaches macOS's own
administrator dialog: cancelling it keeps the previous app, and the banner shows the
error. One such refusal: in `/Applications`, whose sticky bit lets only an item's owner
rename it, a group-writable `lokki.app` owned by another user passes the check, and its
rename then fails with `EPERM`.

## When it offers a download instead

`can_install` is false, and `reason` says why:

- On Linux, when lokki is not running from an AppImage (the runtime sets `APPIMAGE`).
  A `.deb` is never updated in place; download the new package instead.
- On macOS, when the shell cannot replace the bundle it runs from. The first that holds,
  in this order, gives the reason:

| When | `reason` |
| --- | --- |
| lokki is not running from a `.app` | `this lokki is not running from lokki.app, so it cannot replace itself; download the new .dmg` |
| macOS runs a translocated copy (a path with `AppTranslocation`) | `macOS is running lokki from a temporary copy, which cannot be updated: quit lokki, move lokki.app into Applications with Finder, open it from there, then install` |
| the bundle's file system is read-only, such as its disk image | `lokki is running from a read-only disk, such as its disk image: move lokki.app into Applications, open it from there, then install` |
| the user cannot write the bundle or the folder that holds it | `your macOS user cannot change lokki.app or the folder it is in, and lokki does not ask for an administrator's password; download the new .dmg, or move lokki.app to a folder you can write to` |

The translocation row names a path Apple calls an implementation detail; it is there for
the words it gives the user. The read-only and writable checks are the guard.

## The endpoint and the signature

The shell reads `https://github.com/thomasvan/lokki/releases/latest/download/latest.json`
(`plugins.updater.endpoints`), and waits up to 30 seconds for it to answer.

Every package must be signed by the key whose public half is
`plugins.updater.pubkey`. With `requireSignedVersion`, the signature must also carry
the version the manifest announces, so an older signed package cannot be offered under
a newer version number. Downgrades are refused.

## How a macOS build is signed

Every macOS build, a release's or the hand test's, on either CPU, is sealed ad hoc
without hardened runtime: `tauri.conf.json`'s `bundle.macOS` sets `"signingIdentity":
"-"` and `"hardenedRuntime": false`. `test_release_workflow.py` holds both values, and
holds that no overlay names them and no workflow sets a variable Tauri signs or
notarizes with (each is named `APPLE_…`). lokki has no Apple Developer ID and is not
notarized, so an ad hoc seal is the signature it can have.

The seal is what lets macOS ask before it opens a download. Without it, the bundle's only
signature is the one the linker gives the shell, which expects sealed resources that no
bundle has; a copy a browser downloaded is refused as "damaged", and Privacy & Security
offers no Open Anyway. A sealed copy gets macOS's ordinary prompt: lokki was "Not
Opened" because Apple could not verify it, and Open Anyway in System Settings › Privacy
& Security opens it (README › macOS). The sealed copy's prompt and Open Anyway were
seen on an Apple silicon Mac with `tauri` CLI 2.12.2 and on an Intel Mac (The hand
test on an Intel Mac, below); the "damaged" refusal of an unsealed copy, on Apple
silicon only: the Intel run built only sealed bundles.

Hardened runtime gives an app that is not notarized nothing. A build with it, on the same
Mac, never logged `ready` in 60 seconds. Why was not checked; one guess is that library
validation refuses the libraries the core, a PyInstaller one-file build, unpacks at start.

## Testing against another endpoint

`LOKKI_UPDATE_ENDPOINT`, set in the shell's environment, replaces the endpoint list:
point it at a loopback or staging server for a hand test. A release build accepts only
`https` endpoints. The public key cannot be overridden, so another endpoint can offer
only packages the configured key signed. The headless smoke (`LOKKI_SMOKE`) never
checks.

## Testing an install on macOS

`app/scripts/mac-update-handtest.sh` runs this test with the steps below. It builds a
release build with one more `--config`, written into its scratch directory: a throwaway
key's public half, the endpoint `http://127.0.0.1:<port>/latest.json`, and
`dangerousInsecureTransportProtocol`, which the updater needs for that endpoint. That is
a different build, not an override of a release build: "a release build accepts only
`https` endpoints" and "the public key cannot be overridden" stay true of every build
`app.yml` makes, and `test_release_workflow.py` holds them. The endpoint is in the build,
not in `LOKKI_UPDATE_ENDPOINT`, because the test opens lokki from Finder, which passes
it no environment.

What the Mac needs:

- an Intel or Apple silicon CPU, and macOS 12.4 or later; on Apple silicon, a Terminal
  that runs natively, not under Rosetta;
- an administrator account, so the user can write `/Applications` without a password;
- the checkout at the change's head, with what `app.yml`'s macOS job has: Rust (`cargo`
  on the `PATH`), Node with npm, the Xcode command line tools, and a `python3` 3.13 with
  `app.yml`'s `PYTHON_BUILD_PACKAGES` (pyyaml and PyInstaller);
- the network, for npm, cargo and the CLIProxyAPI download;
- no system proxy for `127.0.0.1`: the test build asks its endpoint through the system
  proxy settings;
- a logged-in desktop session to run the script from: the `.dmg` build may ask that
  Terminal be allowed to control Finder.

The build leaves the build's ignored outputs in the checkout, which `git status --ignored`
lists; `clean` does not remove them.

The script works in `~/lokki-handtest` (`LOKKI_HANDTEST_DIR` moves it; never inside the
checkout). It makes the throwaway key there, at run time, and `clean` removes it. Its
versions are A = `99.0.0` and B = `99.0.1`, which no release uses. It prints `PASS` or
`FAIL` for each check it makes and stops at the first `FAIL`. Every subcommand but
`overlay` runs on macOS only.

0. Quit any lokki; move `~/Library/Application Support/lokki` aside if it exists; no
   `lokki.app` in `/Applications`. *Pass:* `build` first finds no lokki process, no data
   directory and no bundle.
1. `mac-update-handtest.sh build`: makes the scratch directory, a throwaway key pair
   (`tauri signer generate`), the overlay, and builds version A (`.dmg`) and version B
   (`.app.tar.gz` and `.sig`), with every `APPLE_…` variable of this Terminal unset, so
   they are sealed as a release is; writes `latest.json` naming B for this Mac's platform.
   *Pass:* both builds exist, and B's signature verifies with the throwaway public key
   and signs version B.
2. `mac-update-handtest.sh serve`: serves the scratch directory's `www` on
   `127.0.0.1:<port>` and prints its pid. *Pass:* `latest.json` answers 200 there.
3. `mac-update-handtest.sh download` puts a copy of A's `.dmg` in
   `~/Downloads/lokki-handtest-download`, with the quarantine attribute a browser writes
   (`0081;<time>;Safari;`). Then, by hand, install it as the README says: open that
   `.dmg` from Finder, drag lokki to Applications, eject the disk image, and open lokki
   from Applications. macOS says lokki was not opened because Apple could not verify it;
   press Done. In System Settings › Privacy & Security, press Open Anyway and give the
   password. Let first run finish, then quit lokki and open it again once, so what step 4
   records has been through a restart, and wait for Home. Then "Check now" (or wait the
   minute before the first check). *Pass:* macOS gives that prompt, not "damaged", and
   Privacy & Security offers Open Anyway; Home shows; the banner says "lokki 99.0.1 is
   available." with "Install and restart".
4. `mac-update-handtest.sh before`: records the bundle's `codesign -dv` and `xattr -lr`,
   the sidecars' `codesign -dv`, a checksum of each file of the data directory's
   `config.yaml`, `secrets/` and `cliproxy/auth/`, and a checksum of
   `cliproxy/config.yaml`'s parsed document, since every start rewrites that file's bytes;
   prints the `com.apple.quarantine` attribute the approved bundle carries; and verifies
   the bundle's signature and each sidecar's. *Pass:* recorded, and each signature
   verifies.
5. By hand: "Install and restart". *Pass:* no password dialog, no Gatekeeper dialog;
   lokki relaunches; the banner names lokki 99.0.1 ("lokki 99.0.1", or "lokki 99.0.1 is
   up to date." after a check).
6. `mac-update-handtest.sh after`. *Pass:* `/Applications/lokki.app` is version B; the
   shell and both sidecars run from `/Applications/lokki.app` and are the files of B's
   archive; `xattr -lr /Applications/lokki.app` shows no `com.apple.quarantine`;
   `codesign -dv` on the bundle and on each of its executables reports what step 4
   recorded (the identifier and `Signature=` lines, or that it is not signed), and each
   signature verifies again; the data checksums are unchanged, `cliproxy/config.yaml` as
   its parsed document; `logs/app.log`, from the install on, shows the core drained and
   stopped, CLIProxyAPI stopped, the install after the proof, both started again, and no
   error.
7. The refusals, by hand, each from a copy of A the script makes on request; quit lokki
   before each, quit each copy before the next, and in each copy wait for Home, then
   "Check now", so the banner offers 99.0.1:
   - `mac-update-handtest.sh copy dmg` mounts A's `.dmg`; open lokki from it. *Pass:*
     "Download" and the read-only reason;
   - `mac-update-handtest.sh copy readonly` puts a copy in a folder it `chmod 555`s; open
     it. *Pass:* "Download" and the not-writable reason; pressing nothing else, no
     password dialog appears;
   - `mac-update-handtest.sh copy translocate` puts a copy in `~/Downloads/lokki-handtest`
     with a quarantine attribute; open it from Finder. *Pass:* macOS says lokki was not
     opened because Apple could not verify it; after Open Anyway in System Settings ›
     Privacy & Security, lokki runs from a temporary copy, and offers "Download" with the
     translocation reason.
8. `mac-update-handtest.sh clean`: stops the server by its pid, detaches the `.dmg`,
   removes the scratch directory with the throwaway key, both builds and every copy, the
   downloaded `.dmg` and its folder, and `/Applications/lokki.app` when it is version A
   or B. By hand: remove the test's data directory, then move yours back. *Pass:*
   nothing of the test is left but the checkout's build outputs.

The build in step 1 is the first macOS compile of a change no hosted run built: a
compile failure there sends the change back to its implementation, as any other `FAIL`
keeps it from merging.

Every macOS build is sealed (see "How a macOS build is signed"), so on either CPU step 4
runs `codesign --verify --deep --strict` on `/Applications/lokki.app`, which checks the
bundle's seal and the code inside it, and `codesign -v` on each sidecar where it is; step
6 runs both again on the replaced bundle. *Pass:* each passes. The shell is checked only
as part of the bundle: its signature binds the bundle's `Info.plist` and sealed
resources, so a copy of it outside the bundle is refused.

An Apple silicon Mac refuses to run a Mach-O without a valid signature, and Intel runs
it, so there step 6 also asks the core sidecar, a PyInstaller one-file build that unpacks
its libraries at start, for `/api/status` on the port and with the operator token that
the installed core's `shell-config` prints. *Pass:* it answers 200. The script takes a
Mac for Apple silicon when `sysctl -n hw.optional.arm64` reads 1, and step 0 says which
it found. Step 5's relaunch and the sidecars' start prove the kernel ran them.

## The hand test on an Intel Mac

On 2026-10-22, from 04:41 UTC, `app/scripts/mac-update-handtest.sh` ran steps 0 to 8
of Testing an install on macOS, above, at 8236d37, on an Intel Core i9-9880H Mac with
macOS 26.7.1. The build venv had Python 3.14.9, not 3.13. The real lokki (0.1.0) and its data
directory were moved aside first and put back afterwards.

- Step 1 built A and B in about 8 minutes.
- Step 3, by hand, through the README's path: A's quarantined `.dmg`
  (`0081;…;Safari;`) opened from Finder, lokki dragged to Applications and
  opened. macOS gave its "could not verify" prompt, not "damaged". (The person at
  the Mac did not quote the dialog. It is known for this by three things: step 3 finished
  through Open Anyway, which a copy refused as "damaged" does not offer, as above;
  the approved bundle's quarantine read `01c1`; and step 7c went through the same
  prompt.) Privacy & Security offered Open Anyway, which asked for the password. The first run
  reached Home, and so did the restart. "Check now" offered 99.0.1 with "Install
  and restart".
- Step 4: the bundle read `Identifier=dev.lokki.desktop Signature=adhoc` and both
  sidecars `Signature=adhoc`. `codesign --verify --deep --strict` passed on
  `/Applications/lokki.app`, and `codesign -v` passed on `cliproxy-8_0_23` and on
  `lokki-core-0_1_0`. The approved bundle's quarantine read `01c1;…;Safari;`.
- Step 5, by hand: "Install and restart" gave no password dialog and no
  Gatekeeper dialog, and lokki relaunched as 99.0.1. Its `app.log` has the install
  asked at 05:20:02Z, both sidecars stopped drained at 05:20:03Z, "update
  installed; restarting" at 05:20:03Z, init saying "already initialised" at
  05:20:26Z, and ready at 05:21:11Z.
- Step 6: every check passed. The version is 99.0.1, and the shell and both
  sidecars run as B's files from `/Applications/lokki.app`. The bundle carries no
  quarantine. `codesign -dv` matches step 4, and `--deep --strict` and each
  sidecar's `-v` pass again. The data checksums are unchanged. `app.log` shows the
  drain, the stop, the install after the proof, both sidecars started again, and
  no error.
- Step 7, by hand, the banner's reason word for word:
  - 7a: "lokki is running from a read-only disk, such as its disk image: move
    lokki.app into Applications, open it from there, then install"
  - 7b: "your macOS user cannot change lokki.app or the folder it is in, and lokki
    does not ask for an administrator's password; download the new .dmg, or move
    lokki.app to a folder you can write to"
  - 7c, after the "could not verify" prompt and Open Anyway: "macOS is running
    lokki from a temporary copy, which cannot be updated: quit lokki, move
    lokki.app into Applications with Finder, open it from there, then install"
- Step 8: every `clean` check passed. The test's data directory was removed, the
  real install was put back, and nothing of the test was left.

## The hand test on Windows

The times in this section and the ones under it are the machine's own clock, as its
scripts and `app.log` print them, with a `Z`, and they are not converted. That clock
was about 50 minutes behind UTC when it was measured at 13:39 that day (main's build at
909fd89, below); it was not measured earlier.

On 2026-10-23 lokki 0.1.0 was run by hand on a Windows 11 machine. Measured over ssh
before the install and read again after the run: Windows 11 Education 10.0.26300, build
26300, 64-bit; WebView2 156.0.4314.42 (the EdgeUpdate client key); an uninstall entry
for the user reading "lokki 0.1.0"; and the release's `lokki_0.1.0_x64-setup.exe`,
fetched again there, 30,618,427 bytes, whose SHA-256 equals the release asset's digest.
Its core reported revision 17a0316, and `app.log`, which records an install that is
asked for and never an offer, has no update line: no install was asked for.

What was measured from outside lokki: its processes and listeners, sampled about every
5 seconds from 05:27, a line for each change; and at 05:55:30 the install, the data
directory, its ACLs, a count of the secrets found in the logs, and the tail of
`logs/app.log`.

- The first start was first seen at 05:33:23, and `app.log` has ready at 05:34:00,
  about 37 seconds later.
- Both sidecars listened on `127.0.0.1` only: CLIProxyAPI on 8317, the core's Python
  child on 8381.
- The data directory, `secrets/`, `logs/`, `cliproxy/` and `cliproxy/auth/` each had a
  protected DACL of two entries, the user and SYSTEM, full control, not inherited.
- None of the four secrets (the two tokens, the CLIProxyAPI key and the management
  secret) was in any log.
- Each of two restarts of CLIProxyAPI started both sidecars as new processes. Each stop
  of CLIProxyAPI, in the two restarts and in the quit, logged `drained=false
  killed=true` 5 to 6 seconds after it began.
- The quit was asked at 05:37:13, and no lokki process was left at 05:37:23.
- The next start ran `init` at 05:37:32 and was ready at 05:37:41.
- At 05:51:20 that shell, pid 13988, was gone, and CLIProxyAPI (2780) and the core's
  bootloader (12816) with its Python child (15744) still ran and listened. A shell
  started at 05:51:23 showed "lokki could not start its services" at 05:51:31. The
  three were gone at 05:55:04; the record does not say what ended them.

What the person at the machine reported:

- They installed v0.1.0. How it was downloaded, and what Edge or SmartScreen showed,
  was not reported.
- `lokki.exe` opened a console window whenever it ran.
- Closing that window ended the shell at 05:51:20.
- The start ready at 05:37:41 was opened from the Start menu.

### #7's build over a running v0.1.0

Later that day the same machine was given the Windows installer of app run 39927837172
(7469b5d), whose SHA-256 is `df465b7edcf713c9882c73b69e6f6cf96304eeb7bab2d235fa31859e6bac6463`,
over a running lokki 0.1.0. Its times are the machine's clock, which was about 50
minutes behind UTC later that day (above). Measured over ssh, the sampler as before:

- At 09:34:38 the 0.1.0 shell, pid 14916, was ended with `TerminateProcess`. CLIProxyAPI
  (7772, on 8317) and the core's bootloader (9816) with its Python child (14604, on
  8381) still ran and listened, at every sample until 10:05:03.
- The installer ran silently from 09:34:44 and exited 0 at 09:34:56. The install
  directory then held `cliproxy-8_0_23.exe` and `lokki-core-0_1_0.exe` with their
  times of 2026-10-22 07:58, `lokki.exe` of 2026-10-23 09:01:42, and `uninstall.exe` of
  09:34:55.
- `app.log` has three `init` lines at 10:10:31 and 10:10:32, and "lokki is already
  running" twice at 10:10:41.
- At 10:10:56: `port 8381: a core an earlier run left, pid 14604, took this data
  directory's token and did not stop within 30 s`, and `port 8317: a CLIProxyAPI an
  earlier run left, pid 7772, stopped`.
- The new CLIProxyAPI, 13200, started at 10:10:59; at 10:11:00 it logged `stopped …:
  drained=true killed=false`, and the error page "lokki could not start its services"
  opened. After a quit at 10:15:27 the next start did the same at 10:16:03 and
  10:16:07, with CLIProxyAPI 9132.
- At 10:19:18 core 14604 still listened on 8381 and answered `/healthz` with 200. Its
  `core.log` was last written at 09:34:31.

What the person at the machine reported: the new build opened with no console window,
and showed a port-conflict error.

What is inferred:

- The two "already running" lines are two of three starts made within a second, which
  found the lock held by the first.
- The new CLIProxyAPI stopped on the `CTRL_BREAK_EVENT` the shell sends: `drained=true`
  is a stop it heard, which the build sends no other way on Windows.
- The leftover core was 0.1.0's, whose drain dies on a write no one reads ([Limits](limits.md),
  When the shell ends without its stop): it took the token and never exited, so every
  start failed on its port. The next start now kills such a core.

### main's build at 909fd89 over a running v0.1.0

On 2026-10-23 the same Windows 11 machine retested #10's fix as merged: the Windows
installer of app run 39952841108 (the push of 909fd89 to `main`), whose SHA-256 is
`67462d1c258e55060108789ae6fe353146713a5112c83aae13cfd9cbba478eee`. Every install was
silent and run over ssh, and so were both `TerminateProcess` ends of a shell. Every
open, from the Start menu, and every restart and Quit was done by the person at the
machine. The machine's clock read 13:39:32 at 14:29:24 UTC. Measured over ssh:

- 0.1.0's installer, over the installed #7 build with nothing running, exited 0 at
  13:39:22.
- 0.1.0 was opened, and once it was ready its shell, pid 15244, was ended with
  `TerminateProcess` at 13:50:40. CLIProxyAPI (8736, on 8317) and the core's bootloader
  (13128) with its Python child (13360, on 8381) still ran and listened.
- 909fd89's installer ran from 13:50:46 and exited 0 at 13:50:57, the three still
  running. `lokki.exe` was replaced (16,266,752 bytes, dated 12:32:30);
  `cliproxy-8_0_23.exe` and `lokki-core-0_1_0.exe` kept their times of 07:58:34 and
  07:58:50 of the day before, and the core its size, 10,539,519 bytes.
- The new shell, pid 15316, started at 13:52:37. Its `app.log`:
  - 13:53:03 `port 8381: a core an earlier run left, pid 13360, took this data
    directory's token and did not stop within 20 s; its tree, pids 13360, 13128:
    killed=true`;
  - 13:53:03 `port 8317: a CLIProxyAPI an earlier run left, pid 8736, stopped`;
  - 13:53:09 `ready`, 32 s after the shell started.
- At 13:53:12 none of the three leftovers was alive. The core then running was the
  0.1.0 file the install had left, started by the new shell.
- `app.log` has `restart of cliproxy asked` at 13:54:56 and 13:55:13. Each was followed
  by `core stopped …: drained=true killed=false` and `cliproxy stopped …: drained=true
  killed=false`, and by `ready` 10 s after the ask, at 13:55:06 and 13:55:23.
- The shell was ended with `TerminateProcess` at 13:55:30, with sidecars 6060, 14748 and
  13584. At 1 s, 2 s and 5 s after, none of the three was alive and nothing listened on
  8317 or 8381.
- 909fd89's installer, with nothing running, exited 0 at 13:56:02 and replaced every
  file: `cliproxy-8_0_23.exe` (71,834,624 bytes) dated 2026-10-23 12:24:20,
  `lokki-core-0_1_0.exe` (10,539,913 bytes) 12:24:40, `lokki.exe` 12:32:30.
- The next start ran `init` at 13:56:55 and was `ready` at 13:57:07, its listeners on
  `127.0.0.1` only, and its `core.log` has
  `revision=909fd890d2e6c7569acaaa83af63ca922e683038`.
- `app.log` has `quitting` at 13:57:25, and at 13:57:26 `core stopped, pid 9372:
  drained=true killed=false` and `cliproxy stopped, pid 16044: drained=true
  killed=false`. At 13:57:33 no lokki process ran and nothing listened on 8317 or 8381.

What the person at the machine reported, after the run: no terminal window appeared.
They were connected to the machine over Remote Desktop when they reported it; the record
does not say whether they were for the run itself. Measured beside it: at 13:53:12 and
again at 13:55:29 the sidecars had two `conhost.exe` children between them (`conhost
children: 2`); whether either had a window was not measured.

Not tried: a leftover of a fixed core on Windows, an interactive install, and the MSI.
Ending the shell should not leave a fixed core: each sidecar runs in a job of its own
that ends its processes when its last handle closes, and only the shell holds that
handle (`spawn` and `Job` in `app/src-tauri/src/process.rs`). What was measured is that
the sidecars were gone within 1 s of the shell's end; that the job ended them is
inferred.

### The in-app update from 0.1.0 to 0.1.1

On 2026-10-23 the same Windows 11 machine was updated from 0.1.0 to 0.1.1 from inside
lokki, through the release `v0.1.1`: a lightweight tag at e9767ae, built by release run
39982744404 and published at 16:07:22 UTC. Every updater signature in the release
verifies against `tauri.conf.json`'s key (428AD592CE9CF454) for `version:0.1.1` and
matches `latest.json`, the Windows package's among them: `lokki_0.1.1_x64-setup.exe`,
SHA-256 `8929673250396dd6545074c9b4a147ba0d1e4730a59b2a9c499390fad4aa9d9d`. The endpoint
0.1.0 reads answered `version 0.1.1`. The times are the machine's clock (above); file
times are the files' own, so they come from the build.

Before the update, 0.1.0 was installed silently with nothing running: the release's
NSIS installer (SHA-256 `a6f51e6c…`) exited 0 at 15:08:41, leaving `lokki.exe` of
16,260,608 bytes and `lokki-core-0_1_0.exe` of 10,539,519. The release's signer reports
running that install over ssh; the record does not measure who ran it. Measured from
`app.log` and process samples every 2 s:

- 15:21:10 `ready`: the core, pid 700, with its listener 17208 on `127.0.0.1:8381`, and
  CLIProxyAPI, pid 10260, on `127.0.0.1:8317`. The person at the machine had opened
  0.1.0.
- 15:21:33 `update install asked`, asked by the person inside lokki.
- 15:21:36 `core stopped, pid 700: drained=true killed=false`, and 15:21:42 `cliproxy
  stopped, pid 10260: drained=false killed=true`. That is 0.1.0's own stop, which ended
  CLIProxyAPI this way on every Windows run of 0.1.0 (above); the stop 0.1.1 carries
  drained it in the retest of main's build at 909fd89 (above).
- 15:21:50 `lokki-0.1.1-installer.exe`, pid 15188, a child of the 0.1.0 shell, pid
  13748.
- 15:22:01 `removed an old sidecar file: …\AppData\Local\lokki\lokki-core-0_1_0.exe`.
- 15:22:03 a new `lokki.exe`, pid 18324, a child of the installer. From 15:22:06 to
  15:22:10 three short-lived `lokki-core-0_1_1.exe` children of it ran, one with a child
  of its own, and `app.log` has `lokki-core init` at 15:22:06.
- 15:22:19 `ready: core on port 8381 pid 5284, cliproxy on port 8317 pid 15728`, 46 s
  after the ask.
- At 15:22:25 the processes were `lokki.exe` 18324, CLIProxyAPI 15728, and the core 5284
  with its listener 7736, on `127.0.0.1:8381` and `127.0.0.1:8317`. No 0.1.0 process
  was left.
- The install directory then held `lokki.exe` (16,270,848 bytes, 2026-10-23 16:04:44),
  `lokki-core-0_1_1.exe` (10,543,069 bytes, 15:59:02) and `cliproxy-8_0_23.exe`
  (71,834,624 bytes, 15:58:48), and no `lokki-core-0_1_0.exe`. `core.log` has
  `revision=e9767aee6fdf65f357942b38987dffe578a35da3`, the tag's commit.

What the person at the machine reported after the update: no terminal window appeared,
and lokki shows version 0.1.1.

What is inferred:

- The new shell removed `lokki-core-0_1_0.exe`. 0.1.0's prune keeps that file, and
  0.1.1's runs first in `run()`, before the first core start; the line comes 5 s before
  `init`.
- The installer ran in the updater's passive mode. No installer window was recorded.
- The three short-lived `lokki-core-0_1_1.exe` children were the shell's core
  subcommands.

Not tried: an update from one release to the next on macOS (the Intel hand test above
updated its own test builds) or on Linux, an update through the MSI, and an update while
a client was streaming through the core.

## The leftover hand test on an M4 Mac mini

On 2026-10-23 two builds were run by hand on an M4 Mac mini, macOS 27.0.1, on a new
default data directory, with the real install and its data moved aside and put back
after: the release's `lokki_0.1.0_aarch64.dmg` (SHA-256
`40eac00e8d289c4c9640ff80833812f8a3d5f7ce5bc164df3b3a5d9eb8c6debb`), and the
`lokki-aarch64-apple-darwin` disk image of app run 39943969755 (#10's head ca1db14,
SHA-256 `2e23def2a3aa7374be0e6482c453f6e17e799f49b0a4bd13d32ef756cdfb6fae`). Each was copied
out of its disk image, with no quarantine, and passed `codesign --verify --deep
--strict`. Measured: process listings, the times of each step, and `logs/app.log`.

- **A 0.1.0 leftover, met by #10's build.** At 12:13:26 the 0.1.0 shell was ended with
  `kill -9`; CLIProxyAPI and the core's bootloader with its Python child, which held
  8381, still ran. #10's build was opened at 12:13:34. At 12:14:00 `app.log` has `port
  8381: a core an earlier run left, pid 22743, took this data directory's token and did
  not stop within 20 s; its tree, pids 22743, 22742: killed=true`, and `ready` at
  12:14:06, 32 s after the open.
- **#10's own leftover, met by #10's build.** At 12:14:28 that shell was ended with
  `kill -9`, and the build opened again at 12:14:31. At 12:14:37 `app.log` has `port
  8381: a core an earlier run left, pid 24539, took this data directory's token,
  drained and stopped`, 3 s after `init`, and `ready` at 12:14:43, 12 s after the open.
  It did so on both such runs; the second has the same line for pid 25924 at 12:15:34.
  Nothing was killed.
- **The leftover CLIProxyAPI.** At 12:14:00 and 12:14:37 `app.log` has `… (pid …) is
  not this data directory's CLIProxyAPI; left alone`, and those CLIProxyAPIs were seen
  gone at 12:14:08 and 12:14:44. At 12:15:15, 5 s after a `kill -9` of the shell, CLIProxyAPI 25922
  still ran; one `GET /` on its port at 12:15:16 got no answer (curl code 000); at
  12:15:19 it was gone, and the next start's `app.log` has no line for its port.
- A SIGTERM of the last shell at 12:15:41 left no lokki process at 12:15:42.

What is inferred, not proven: a CLIProxyAPI whose shell has ended ends on its first
request without answering it, because it writes a line for the request to the output
the dead shell read, and Go ends a program on SIGPIPE when a write to a broken pipe on
fd 1 or 2 fails. The "left alone" lines are that: the check's own request ended it.
CLIProxyAPI alone was measured doing the same on Linux ([Limits](limits.md), When the
shell ends without its stop).
