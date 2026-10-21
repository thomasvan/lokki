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
(`plugins.updater.endpoints`), and waits up to 30 seconds for it to answer. While the
repository is private, GitHub answers 404 and every check reports that error.

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
& Security opens it (README › macOS). Both were seen on an Apple silicon Mac with
`tauri` CLI 2.12.2; no one has run a sealed build on an Intel Mac.

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
