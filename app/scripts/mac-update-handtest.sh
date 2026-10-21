#!/usr/bin/env bash
# The hand test of an install in place on macOS: docs/app/update.md › "Testing an install on
# macOS" gives the steps and what each must show. It builds two versions of a test build
# (A = 99.0.0 and B = 99.0.1, which no release uses) with a throwaway key and a loopback
# endpoint, puts A's .dmg in Downloads as a browser leaves it, serves B, records the installed
# bundle before and after "Install and restart", makes the copies the refusals are checked
# from, and removes everything it made.
#
# The test build is a release build with one more --config, written into the scratch
# directory and never into the checkout: it names the throwaway key's public half, an
# http://127.0.0.1 endpoint and dangerousInsecureTransportProtocol, which the plugin needs for
# that endpoint. No build app.yml makes can carry it (test_release_workflow.py).
#
# Every subcommand but `overlay` runs on macOS only. Each check prints PASS or FAIL, and the
# script stops at the first FAIL. Sourced, it defines its functions and runs none, so
# test_mac_handtest.py can drive one with the Mac's tools stubbed.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: mac-update-handtest.sh build | serve | download | before | after | copy dmg|readonly|translocate | clean
       mac-update-handtest.sh overlay --out FILE --port N --pubkey FILE --version V
EOF
  exit 2
}

# Before anything that needs a tool: off macOS, only the overlay runs.
case "${1:-}" in
  "" | -h | --help | help) usage ;;
  overlay) ;;
  build | serve | download | before | after | copy | clean)
    [ "$(uname -s)" = Darwin ] || { echo "mac-update-handtest.sh: $1 runs on macOS only" >&2; exit 2; } ;;
  *) echo "mac-update-handtest.sh: $1 is not a subcommand" >&2; usage ;;
esac

case "${BASH_SOURCE[0]}" in */*) HERE="${BASH_SOURCE[0]%/*}" ;; *) HERE=. ;; esac
HERE="$(cd "$HERE" && pwd -P)"
APP="$(cd "$HERE/.." && pwd -P)"
REPO="$(cd "$APP/.." && pwd -P)"
SCRATCH="${LOKKI_HANDTEST_DIR:-$HOME/lokki-handtest}"
A=99.0.0
B=99.0.1
INSTALLED=/Applications/lokki.app
DATA="$HOME/Library/Application Support/lokki"
COPIES="$HOME/Downloads/lokki-handtest"
DOWNLOAD="$HOME/Downloads/lokki-handtest-download"

pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; exit 1; }
say() { echo "---- $*"; }

# The real directory a path is in, or fails when it does not exist.
real_dir() { (cd "$1" 2>/dev/null && pwd -P); }

inside_checkout() {
  case "$1/" in "$REPO"/*) return 0 ;; *) return 1 ;; esac
}

overlay() {
  local out="" port="" pubkey="" version=""
  while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || usage
    case "$1" in
      --out) out="$2" ;;
      --port) port="$2" ;;
      --pubkey) pubkey="$2" ;;
      --version) version="$2" ;;
      *) usage ;;
    esac
    shift 2
  done
  [ -n "$out" ] && [ -n "$port" ] && [ -n "$pubkey" ] && [ -n "$version" ] || usage
  local dir
  dir="$(real_dir "$(dirname "$out")")" || { echo "overlay: no directory for $out" >&2; exit 1; }
  if inside_checkout "$dir" || [ -L "$out" ]; then
    echo "overlay: $out must be outside the checkout ($REPO), and not a link" >&2
    exit 1
  fi
  python3 - "$dir/$(basename "$out")" "$port" "$pubkey" "$version" <<'PY'
import json, sys
out, port, pubkey, version = sys.argv[1:]
if not port.isdigit() or not 0 < int(port) < 65536:
    sys.exit(f"overlay: --port {port} is not a port")
with open(pubkey, encoding="utf-8") as f:
    key = f.read().strip()
if not key:
    sys.exit(f"overlay: {pubkey} is empty")
conf = {
    "version": version,
    "plugins": {"updater": {
        "pubkey": key,
        "endpoints": [f"http://127.0.0.1:{int(port)}/latest.json"],
        "dangerousInsecureTransportProtocol": True,
    }},
}
with open(out, "w", encoding="utf-8") as f:
    json.dump(conf, f, indent=2)
    f.write("\n")
PY
}

# ---- macOS from here on.

scratch_ok() {
  local parent
  parent="$(real_dir "$(dirname "$SCRATCH")")" || fail "the scratch directory's parent $(dirname "$SCRATCH") does not exist"
  ! inside_checkout "$parent/$(basename "$SCRATCH")" || fail "the scratch directory $SCRATCH is inside the checkout"
  [ "$parent/$(basename "$SCRATCH")" != "$(real_dir "$HOME")" ] || fail "the scratch directory is the home folder"
}

need_build() {
  [ -f "$SCRATCH/port" ] && [ -d "$SCRATCH/A/lokki.app" ] && [ -f "$SCRATCH/www/latest.json" ] \
    || fail "no test build in $SCRATCH: run build first"
}

# Pids whose arguments start with this executable.
pids_of() { pgrep -f "^$1( |\$)" || true; }

# The executable a process runs: lsof's first text file. Read whole, never cut by a pipe.
exe_of() {
  local out
  out="$(lsof -a -p "$1" -d txt -Fn 2>/dev/null || true)"
  out="$(sed -n 's/^n//p' <<<"$out")"
  printf '%s\n' "${out%%$'\n'*}"
}

plist_version() {
  /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$1/Contents/Info.plist" 2>/dev/null || true
}

# The Mach-O files `codesign` is compared on: the shell and both sidecars.
machos() {
  local app="$1" f
  for f in "$app"/Contents/MacOS/*; do echo "$f"; done
}

# Apple silicon refuses to run a Mach-O without a valid signature, and Intel runs it, so the
# core's /api/status is asked there only. A read that fails or finds no name is Intel's.
apple_silicon() { [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; }

# Every macOS build is sealed ad hoc, on either CPU: the bundle's signature binds its
# Info.plist, its resources and the shell, and each sidecar carries its own. `--deep --strict`
# verifies the bundle and the code in it; each sidecar is verified in place too, where codesign
# reads it as a file of its own. The shell is not verified apart from the bundle: a copy of it
# outside the bundle names sealed resources that are not beside it, and is refused.
verify_signatures() {
  local f out
  out="$(codesign --verify --deep --strict "$INSTALLED" 2>&1)" \
    && pass "codesign --verify --deep --strict: $INSTALLED" || fail "codesign refuses $INSTALLED: $out"
  while read -r f; do
    [ "$f" != "$INSTALLED/Contents/MacOS/lokki" ] || continue
    out="$(codesign -v "$f" 2>&1)" && pass "codesign -v: $(basename "$f")" || fail "codesign -v refuses $(basename "$f"): $out"
  done < <(machos "$INSTALLED")
}

# Every variable Tauri reads to sign or notarize a macOS build another way is named APPLE_…
# (its ENVIRONMENT_VARIABLES.md); one exported in this Terminal would change the build the test
# makes from the one a release makes, so build drops them.
drop_apple_signing() {
  local name
  for name in $(compgen -e); do
    case "$name" in APPLE_*) unset "$name"; echo "unset $name: the test build is signed as a release is" ;; esac
  done
}

# The names of the variables the shell does not give the core: every one outside its
# allow-list, whose home is for_sidecars in app/src-tauri/logic/src/env.rs. The core's loader
# reads RELAY_*, CLIPROXY_URL and more, so a name exported in this Terminal could make
# shell-config answer another port or token than the app's core has. Names alone, so no value
# reaches a process's arguments.
not_for_sidecars() {
  local name
  for name in $(compgen -e); do
    case "$name" in
      PATH | HOME | USER | LOGNAME | SHELL | LANG | TZ | TMPDIR | TMP | TEMP | LC_*) ;;
      HTTP_PROXY | HTTPS_PROXY | NO_PROXY | SSL_CERT_FILE | SSL_CERT_DIR) ;;
      http_proxy | https_proxy | no_proxy | ssl_cert_file | ssl_cert_dir) ;;
      *) echo "$name" ;;
    esac
  done
}

# Reads shell-config's line on stdin and asks /api/status with its port and operator token,
# waiting WAIT seconds; prints the port and the answer's status, or `nothing`.
STATUS_PY='
import json, sys, urllib.error, urllib.request
conf = json.load(sys.stdin)
port, wait = int(conf["core_port"]), float(sys.argv[1])
req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status",
                             headers={"Authorization": "Bearer " + conf["operator_token"]})
try:
    code = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=wait).status
except urllib.error.HTTPError as e:
    code = e.code
except OSError:
    code = "nothing"
print(port, code)
'

# On Apple silicon, the core, a PyInstaller one-file build that unpacks its libraries at start,
# answers /api/status. Its port and operator token are what the shell reads: the installed
# core's shell-config, run with the environment the shell gives the core. The token goes from
# shell-config to python3 through a pipe: no variable holds it, so no trace prints it, and it
# is in no process's arguments. A core that does not answer in 10 seconds
# (LOKKI_HANDTEST_STATUS_WAIT) fails the step.
core_answers_status() {
  apple_silicon || return 0
  local cores=("$INSTALLED"/Contents/MacOS/lokki-core-*) drop=() name answer
  [ "${#cores[@]}" = 1 ] && [ -x "${cores[0]}" ] || fail "no single lokki-core in $INSTALLED"
  while read -r name; do drop+=(-u "$name"); done < <(not_for_sidecars)
  answer="$(env ${drop[@]+"${drop[@]}"} "${cores[0]}" shell-config --data-dir "$DATA" 2>/dev/null \
    | python3 -c "$STATUS_PY" "${LOKKI_HANDTEST_STATUS_WAIT:-10}")" || fail "the core's shell-config gave no port and token"
  [ "${answer#* }" = 200 ] && pass "the core answers /api/status on 127.0.0.1:${answer%% *}" \
    || fail "/api/status answers ${answer#* } on 127.0.0.1:${answer%% *}"
}

# What step 6 compares: the identifier and Signature= lines, or that there is no signature.
signature_lines() {
  { codesign -dv "$1" 2>&1 || true; } | sed -n -e 's/^.*: \(code object is not signed at all\)$/\1/p' -e '/^Identifier=/p' -e '/^Signature=/p'
}

signatures() {
  local f
  echo "bundle: $(signature_lines "$INSTALLED" | tr '\n' ' ')"
  while read -r f; do
    echo "$(basename "$f"): $(signature_lines "$f" | tr '\n' ' ')"
  done < <(machos "$INSTALLED")
}

# Into FILE, a checksum per data file the update must not touch; the paths stay in the scratch
# file and are never printed, since an account's file name can carry its address.
# cliproxy/config.yaml is compared as its parsed document: every start rewrites it
# (sync-cliproxy's dump, and CLIProxyAPI's own writer), which can change its bytes, not what it says.
data_sums() {
  local p parsed
  for p in "$DATA/config.yaml" "$DATA/secrets" "$DATA/cliproxy/auth"; do
    [ -e "$p" ] || continue
    find "$p" -type f -print0 | sort -z | while IFS= read -r -d '' f; do shasum -a 256 "$f"; done
  done >"$1"
  p="$DATA/cliproxy/config.yaml"
  [ -f "$p" ] || return 0
  parsed="$(python3 -c 'import json, sys, yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1], encoding="utf-8")), sort_keys=True, default=str))' "$p")" \
    || fail "python3 cannot parse $p"
  echo "$(shasum -a 256 <<<"$parsed" | cut -d' ' -f1)  parsed $p" >>"$1"
}

# A Rust check of a minisign signature with the crate the updater plugin verifies with.
write_verifier() {
  mkdir -p "$1/src"
  cat >"$1/Cargo.toml" <<'EOF'
[package]
name = "handtest-verify"
version = "0.0.0"
edition = "2021"
publish = false

[dependencies]
minisign-verify = "=0.3.0"

[workspace]
EOF
  cat >"$1/src/main.rs" <<'EOF'
//! verify PUBKEY SIGNATURE FILE: both as minisign text; prints the trusted comment.
use minisign_verify::{PublicKey, Signature};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let read = |p: &str| std::fs::read_to_string(p).unwrap_or_else(|e| panic!("{p}: {e}"));
    let key = PublicKey::decode(&read(&args[0])).expect("a minisign public key");
    let sig = Signature::decode(&read(&args[1])).expect("a minisign signature");
    let data = std::fs::read(&args[2]).expect("the signed file");
    match key.verify(&data, &sig, true) {
        Ok(()) => println!("{}", sig.trusted_comment()),
        Err(e) => {
            eprintln!("the signature does not verify: {e}");
            std::process::exit(1);
        }
    }
}
EOF
}

b64_to() { python3 -c 'import base64, sys; open(sys.argv[2], "wb").write(base64.b64decode(open(sys.argv[1]).read()))' "$1" "$2"; }

build() {
  say "step 0: a Mac with no lokki on it"
  if apple_silicon; then
    # Under Rosetta uname -m says x86_64, and the build would be an x86_64 one.
    [ "$(uname -m)" = arm64 ] || fail "this Mac is Apple silicon, but this shell runs under Rosetta (uname -m says $(uname -m)): open a Terminal that runs natively, then run build again"
    echo "this Mac is Apple silicon: steps 4 and 6 verify the bundle's signature and each sidecar's, and step 6 also asks the core's /api/status"
  else
    echo "this Mac has an Intel CPU: steps 4 and 6 verify the bundle's signature and each sidecar's"
  fi
  [ -z "$(pgrep -x lokki || true)$(pgrep -f 'lokki\.app/Contents/MacOS/' || true)" ] && pass "no lokki process" || fail "a lokki process runs: quit lokki"
  [ ! -e "$DATA" ] && pass "no data directory" || fail "$DATA exists: move it aside"
  [ ! -e "$INSTALLED" ] && pass "no $INSTALLED" || fail "$INSTALLED exists: remove it, or run clean if it is the test's"
  scratch_ok
  [ ! -e "$SCRATCH" ] || fail "$SCRATCH exists: run clean first"
  local tool
  for tool in cargo node npm python3 codesign hdiutil lsof curl; do
    command -v "$tool" >/dev/null || fail "$tool is not on the PATH"
  done
  python3 -c 'import yaml, PyInstaller' 2>/dev/null || fail "python3 lacks pyyaml or PyInstaller (app.yml's PYTHON_BUILD_PACKAGES)"

  say "step 1: the throwaway key, the overlay, versions $A and $B"
  drop_apple_signing
  local triple platform
  case "$(uname -m)" in
    x86_64) triple=x86_64-apple-darwin platform=darwin-x86_64 ;;
    arm64) triple=aarch64-apple-darwin platform=darwin-aarch64 ;;
    *) fail "unknown machine $(uname -m)" ;;
  esac
  mkdir -m 700 "$SCRATCH"
  # clean removes only a directory carrying this mark.
  : >"$SCRATCH/.lokki-handtest"
  mkdir -p "$SCRATCH/key" "$SCRATCH/A" "$SCRATCH/B" "$SCRATCH/www"
  local port password
  port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
  echo "$port" >"$SCRATCH/port"
  password="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  (cd "$APP" && npm ci --silent)
  (cd "$APP" && npx --no-install tauri signer generate --ci -p "$password" -w "$SCRATCH/key/handtest.key") >/dev/null
  [ -s "$SCRATCH/key/handtest.key" ] && [ -s "$SCRATCH/key/handtest.key.pub" ] && pass "a throwaway key pair in $SCRATCH/key" || fail "no key pair"

  say "the UI and both sidecars for $triple"
  (cd "$REPO/ui" && npm ci --silent && npm run build)
  (cd "$APP" && python3 scripts/fetch_cliproxy.py --lock cliproxy.lock --target "$triple" --out src-tauri/binaries \
    && python3 scripts/fetch_cliproxy.py --lock cliproxy.lock --target "$triple" --out src-tauri/binaries --check \
    && python3 scripts/build_core.py --out src-tauri/binaries --triple "$triple" --run)

  local target="$SCRATCH/target" bundle="$SCRATCH/target/release/bundle" v
  (cd "$APP" && python3 scripts/sidecars.py --config-json --release --out "$SCRATCH/sidecars.json")
  for v in "$A" "$B"; do
    say "tauri build, version $v"
    overlay --out "$SCRATCH/overlay-$v.json" --port "$port" --pubkey "$SCRATCH/key/handtest.key.pub" --version "$v"
    # The overlay goes after sidecars.py's, so its version wins.
    (cd "$APP" && CARGO_TARGET_DIR="$target" TAURI_SIGNING_PRIVATE_KEY="$SCRATCH/key/handtest.key" \
      TAURI_SIGNING_PRIVATE_KEY_PASSWORD="$password" \
      npm run tauri build -- --bundles app,dmg --config "$SCRATCH/sidecars.json" --config "$SCRATCH/overlay-$v.json" -- --locked)
    [ "$(plist_version "$bundle/macos/lokki.app")" = "$v" ] || fail "the build's Info.plist does not say $v"
    if [ "$v" = "$A" ]; then
      local dmgs=("$bundle"/dmg/*.dmg)
      [ "${#dmgs[@]}" = 1 ] && [ -f "${dmgs[0]}" ] || fail "no single .dmg for $A"
      cp "${dmgs[0]}" "$SCRATCH/A/lokki-$A.dmg"
      ditto "$bundle/macos/lokki.app" "$SCRATCH/A/lokki.app"
    else
      cp "$bundle/macos/lokki.app.tar.gz" "$bundle/macos/lokki.app.tar.gz.sig" "$SCRATCH/www/"
      tar -xzf "$SCRATCH/www/lokki.app.tar.gz" -C "$SCRATCH/B"
    fi
  done
  [ -f "$SCRATCH/A/lokki-$A.dmg" ] && pass "$A: $SCRATCH/A/lokki-$A.dmg" || fail "no .dmg for $A"
  [ -d "$SCRATCH/B/lokki.app" ] && [ -s "$SCRATCH/www/lokki.app.tar.gz.sig" ] && pass "$B: lokki.app.tar.gz and its .sig" || fail "no archive for $B"

  write_verifier "$SCRATCH/verify"
  b64_to "$SCRATCH/key/handtest.key.pub" "$SCRATCH/verify/pubkey.txt"
  b64_to "$SCRATCH/www/lokki.app.tar.gz.sig" "$SCRATCH/verify/sig.txt"
  local trusted
  trusted="$(CARGO_TARGET_DIR="$SCRATCH/verify/target" cargo run --quiet --release --manifest-path "$SCRATCH/verify/Cargo.toml" -- \
    "$SCRATCH/verify/pubkey.txt" "$SCRATCH/verify/sig.txt" "$SCRATCH/www/lokki.app.tar.gz")" || fail "$B's signature does not verify with the throwaway public key"
  case $'\t'"$trusted"$'\t' in *$'\t'"version:$B"$'\t'*) true ;; *) false ;; esac \
    && pass "$B's signature verifies with the throwaway public key and signs version $B" \
    || fail "$B's signature carries no version:$B ($trusted)"

  python3 - "$SCRATCH/www" "$B" "$platform" "$port" <<'PY'
import datetime, json, os, sys
www, version, platform, port = sys.argv[1:]
with open(os.path.join(www, "lokki.app.tar.gz.sig"), encoding="utf-8") as f:
    signature = f.read().strip()
manifest = {
    "version": version,
    "notes": "the macOS hand test",
    "pub_date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "platforms": {platform: {"signature": signature, "url": f"http://127.0.0.1:{port}/lokki.app.tar.gz"}},
}
with open(os.path.join(www, "latest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
PY
  pass "latest.json names $B for $platform"
}

serve() {
  scratch_ok
  need_build
  local port pid
  port="$(cat "$SCRATCH/port")"
  if [ -f "$SCRATCH/server.pid" ] && kill -0 "$(cat "$SCRATCH/server.pid")" 2>/dev/null; then
    fail "the server already runs, pid $(cat "$SCRATCH/server.pid")"
  fi
  nohup python3 -m http.server "$port" --bind 127.0.0.1 --directory "$SCRATCH/www" >"$SCRATCH/server.log" 2>&1 &
  pid=$!
  echo "$pid" >"$SCRATCH/server.pid"
  echo "the server's pid: $pid"
  local code="" i
  for i in $(seq 1 50); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/latest.json" || true)"
    [ "$code" = 200 ] && break
    sleep 0.2
  done
  [ "$code" = 200 ] && pass "latest.json answers 200 on 127.0.0.1:$port" || fail "latest.json answers ${code:-nothing} on 127.0.0.1:$port"
}

# Step 3 opens A as the README says a download is opened: from a .dmg in Downloads that carries
# the quarantine attribute a browser writes.
download() {
  scratch_ok
  need_build
  [ -f "$SCRATCH/A/lokki-$A.dmg" ] || fail "no .dmg for $A: run build first"
  [ ! -e "$DOWNLOAD" ] || fail "$DOWNLOAD exists: run clean first"
  mkdir -p "$DOWNLOAD"
  # clean removes only a directory carrying this mark.
  : >"$DOWNLOAD/.lokki-handtest"
  cp "$SCRATCH/A/lokki-$A.dmg" "$DOWNLOAD/"
  xattr -w com.apple.quarantine "0081;$(printf %x "$(date +%s)");Safari;" "$DOWNLOAD/lokki-$A.dmg"
  pass "$DOWNLOAD/lokki-$A.dmg, quarantined as a browser leaves it: open it from Finder, drag lokki to Applications, eject the disk image, and open lokki from Applications"
}

before() {
  scratch_ok
  need_build
  [ "$(plist_version "$INSTALLED")" = "$A" ] || fail "$INSTALLED is not version $A"
  [ -n "$(pids_of "$INSTALLED/Contents/MacOS/lokki")" ] || fail "lokki is not running from $INSTALLED"
  mkdir -p "$SCRATCH/before"
  signatures >"$SCRATCH/before/signatures"
  xattr -lr "$INSTALLED" >"$SCRATCH/before/xattr" 2>&1 || true
  data_sums "$SCRATCH/before/data"
  cat "$SCRATCH/before/signatures"
  echo "the bundle's com.apple.quarantine: $(xattr -p com.apple.quarantine "$INSTALLED" 2>/dev/null || echo none)"
  verify_signatures
  [ -s "$SCRATCH/before/data" ] || fail "no data file to record in $DATA"
  pass "recorded the signatures, the attributes and $(wc -l <"$SCRATCH/before/data" | tr -d ' ') data checksums"
}

after() {
  scratch_ok
  need_build
  [ -f "$SCRATCH/before/signatures" ] || fail "nothing recorded: run before first"
  [ "$(plist_version "$INSTALLED")" = "$B" ] && pass "$INSTALLED is version $B" || fail "$INSTALLED is version $(plist_version "$INSTALLED")"

  # Each executable of B's bundle runs from the installed bundle, and nowhere else, as B's file.
  local f name pid exe running
  while read -r f; do
    name="$(basename "$f")"
    running=0
    for pid in $(pgrep -f "/$name( |\$)" || true); do
      exe="$(exe_of "$pid")"
      # Only a bundle's executable, so a `tail -f …/lokki` is not taken for lokki.
      case "$exe" in *.app/Contents/MacOS/*) ;; *) continue ;; esac
      [ "$exe" = "$INSTALLED/Contents/MacOS/$name" ] || fail "$name runs from $exe (pid $pid)"
      [ "$(shasum -a 256 <"$exe")" = "$(shasum -a 256 <"$f")" ] || fail "$name (pid $pid) is not $B's"
      running=1
    done
    [ "$running" = 1 ] && pass "$name runs from $INSTALLED as $B's file" || fail "$name is not running"
  done < <(machos "$SCRATCH/B/lokki.app")

  local attributes
  attributes="$(xattr -lr "$INSTALLED" 2>&1 || true)"
  if grep -q com.apple.quarantine <<<"$attributes"; then
    fail "the replaced bundle carries com.apple.quarantine"
  fi
  pass "no com.apple.quarantine on $INSTALLED"

  signatures >"$SCRATCH/after-signatures"
  cat "$SCRATCH/after-signatures"
  diff -u "$SCRATCH/before/signatures" "$SCRATCH/after-signatures" \
    && pass "codesign -dv reports what step 4 recorded" || fail "codesign -dv differs from step 4"
  verify_signatures

  data_sums "$SCRATCH/after-data"
  cmp -s "$SCRATCH/before/data" "$SCRATCH/after-data" \
    && pass "the data checksums are unchanged" || fail "the data checksums changed (compare $SCRATCH/before/data with $SCRATCH/after-data)"

  # The install's lines: from the last "update install asked" on.
  local log="$DATA/logs/app.log" run
  [ -f "$log" ] || fail "no $log"
  run="$(awk '/update install asked/ { buf = "" } { buf = buf $0 "\n" } END { printf "%s", buf }' "$log")"
  grep -q "update install asked" <<<"$run" || fail "app.log has no install"
  grep -Eq "core stopped, pid [0-9]+: drained=true killed=false" <<<"$run" && pass "app.log: the core drained and stopped" || fail "app.log: no drained stop of the core"
  grep -Eq "cliproxy stopped, pid [0-9]+: drained=true killed=false" <<<"$run" && pass "app.log: cliproxy stopped" || fail "app.log: no clean stop of cliproxy"
  grep -q "update installed; restarting" <<<"$run" && pass "app.log: both proved gone, then the install" || fail "app.log: no install after the stop"
  grep -Eq "ready: core on port" <<<"$run" && pass "app.log: the relaunch started both" || fail "app.log: no start after the install"
  if grep -Eiq "not installed|still alive|error|failed" <<<"$run"; then
    grep -Ei "not installed|still alive|error|failed" <<<"$run"
    fail "app.log: an error since the install was asked"
  fi
  pass "app.log: no error since the install was asked"
  core_answers_status
}

copy() {
  scratch_ok
  need_build
  [ -z "$(pids_of "$INSTALLED/Contents/MacOS/lokki")" ] || fail "quit lokki first: one lokki at a time"
  case "${1:-}" in
    dmg)
      local mount
      mount="$(hdiutil attach "$SCRATCH/A/lokki-$A.dmg" | awk -F'\t' 'END { print $NF }')"
      [ -d "$mount/lokki.app" ] || fail "the .dmg did not mount"
      echo "$mount" >"$SCRATCH/mount"
      pass "mounted at $mount: open lokki.app there from Finder; expect Download and the read-only reason"
      ;;
    readonly)
      mkdir -p "$SCRATCH/locked"
      ditto "$SCRATCH/A/lokki.app" "$SCRATCH/locked/lokki.app"
      chmod 555 "$SCRATCH/locked"
      pass "$SCRATCH/locked/lokki.app, its folder chmod 555: open it; expect Download and the not-writable reason, and no password dialog"
      ;;
    translocate)
      [ ! -e "$COPIES" ] || fail "$COPIES exists"
      mkdir -p "$COPIES"
      # clean removes only a directory carrying this mark.
      : >"$COPIES/.lokki-handtest"
      ditto "$SCRATCH/A/lokki.app" "$COPIES/lokki.app"
      xattr -w -r com.apple.quarantine "0081;$(printf %x "$(date +%s)");Safari;" "$COPIES/lokki.app"
      pass "$COPIES/lokki.app, quarantined: open it from Finder; expect macOS to say it could not verify lokki, then Open Anyway in System Settings › Privacy & Security, then Download and the translocation reason"
      ;;
    *) usage ;;
  esac
}

clean() {
  # Every refusal comes before the first stop or removal, so a refusal leaves all as it was.
  scratch_ok
  [ ! -e "$SCRATCH" ] || [ -f "$SCRATCH/.lokki-handtest" ] || fail "$SCRATCH is not the test's scratch directory: left in place"
  [ ! -e "$COPIES" ] || [ -f "$COPIES/.lokki-handtest" ] || fail "$COPIES is not the test's: left in place"
  [ ! -e "$DOWNLOAD" ] || [ -f "$DOWNLOAD/.lokki-handtest" ] || fail "$DOWNLOAD is not the test's: left in place"
  if [ -e "$INSTALLED" ]; then
    case "$(plist_version "$INSTALLED")" in
      "$A" | "$B") ;;
      *) fail "$INSTALLED is not the test's: left in place" ;;
    esac
    [ -z "$(pids_of "$INSTALLED/Contents/MacOS/lokki")" ] || fail "quit lokki first"
  fi
  local pid
  if [ -f "$SCRATCH/server.pid" ]; then
    pid="$(cat "$SCRATCH/server.pid")"
    # By its pid, and only when that pid is still this server.
    if grep -q "http.server" <<<"$(ps -o command= -p "$pid" 2>/dev/null || true)"; then
      kill "$pid"
      for _ in $(seq 1 25); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
    fi
    ! kill -0 "$pid" 2>/dev/null && pass "the server, pid $pid, is stopped" || fail "the server, pid $pid, still runs"
  fi
  if [ -f "$SCRATCH/mount" ] && [ -d "$(cat "$SCRATCH/mount")" ]; then
    hdiutil detach "$(cat "$SCRATCH/mount")" >/dev/null && pass "the .dmg is detached" || fail "the .dmg did not detach"
  fi
  rm -rf "$INSTALLED"
  [ -d "$SCRATCH/locked" ] && chmod 755 "$SCRATCH/locked"
  rm -rf "$COPIES" "$DOWNLOAD" "$SCRATCH"
  [ ! -e "$INSTALLED" ] && pass "no $INSTALLED" || fail "$INSTALLED is left"
  [ ! -e "$COPIES" ] && pass "no copy in Downloads" || fail "$COPIES is left"
  [ ! -e "$DOWNLOAD" ] && pass "no downloaded .dmg" || fail "$DOWNLOAD is left"
  [ ! -e "$SCRATCH" ] && pass "no scratch directory: the key, both builds and the copies are gone" || fail "$SCRATCH is left"
  echo "by hand: remove the test's data directory, $DATA, then move yours back"
}

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0
cmd="$1"
shift
case "$cmd" in
  overlay) overlay "$@" ;;
  build) build ;;
  serve) serve ;;
  download) download ;;
  before) before ;;
  after) after ;;
  copy) copy "$@" ;;
  clean) clean ;;
  *) usage ;;
esac
