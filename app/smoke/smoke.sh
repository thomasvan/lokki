#!/usr/bin/env bash
# The headless smoke: the release shell under Xvfb with a throwaway data directory and
# free ports. The shell starts both sidecars, restarts CLIProxyAPI through the page's hook,
# reads /api/status from the page, reports, stops everything and exits. This script then
# checks what that run left: its lines, the listeners, the data directory, the logs, that
# no secret reached a log or the output, and that no process it was shown is alive.
# It stops only the processes it started, by their pids: other services on this machine
# run binaries of the same names.
set -uo pipefail

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(dirname "$APP")"
TARGET="${CARGO_TARGET_DIR:-src-tauri/target}"
case "$TARGET" in /*) ;; *) TARGET="$APP/$TARGET" ;; esac
BIN="$TARGET/release/lokki"
RUN_LIMIT=240

[ -x "$BIN" ] || { echo "smoke: FAIL no release build at $BIN" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/lokki-smoke-XXXXXX")"
DATA="$WORK/data"
OUT="$WORK/stdout"
ERR="$WORK/stderr"
FAILED=0
RUN_PID=""

fail() {
  echo "smoke: FAIL $*" >&2
  FAILED=1
}

pids_seen() {
  sed -n 's/^smoke: pids=//p' "$OUT" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' || true
}

# Pids the run proved gone; cleanup leaves them alone, since such a pid may be reused.
GONE=" "
REAPED=0

cleanup() {
  # What the run did not prove gone: its own process group, then each sidecar's group and pid.
  if [ "$REAPED" = 0 ] && [ -n "$RUN_PID" ]; then
    kill -KILL -- "-$RUN_PID" 2>/dev/null
  fi
  for pid in $(pids_seen); do
    case "$GONE" in *" $pid "*) continue ;; esac
    kill -KILL -- "-$pid" 2>/dev/null
    kill -KILL "$pid" 2>/dev/null
  done
  if [ "$FAILED" = 0 ]; then
    rm -rf "$WORK"
  else
    echo "smoke: the run's files are kept in $WORK" >&2
  fi
}
trap cleanup EXIT

mkdir -m 700 "$DATA"
# Its own session and process group, so a run that hangs is stopped whole by its pid.
LOKKI_DATA_DIR="$DATA" LOKKI_PORTS=0,0 LOKKI_SMOKE=1 WEBKIT_DISABLE_DMABUF_RENDERER=1 \
  setsid -w xvfb-run -a dbus-run-session -- "$BIN" >"$OUT" 2>"$ERR" &
RUN_PID=$!

# Waits until `grep -c PATTERN` on the output reaches COUNT; false when the run ended first.
wait_for_line() {
  for _ in $(seq 1 $((RUN_LIMIT * 5))); do
    [ "$(grep -c "$1" "$OUT" 2>/dev/null || true)" -ge "$2" ] && return 0
    kill -0 "$RUN_PID" 2>/dev/null || return 1
    sleep 0.2
  done
  return 1
}

# Samples both ports' listeners into listeners.PHASE until each is seen on 127.0.0.1. The
# shell holds its exit for two seconds after the page's report, so the restart's listeners
# are there to be seen.
sample() {
  local file="$WORK/listeners.$1"
  : >"$file"
  for _ in $(seq 1 100); do
    ss -ltnH | awk -v a=":$CORE_PORT" -v b=":$CLIPROXY_PORT" \
      '{ n = length($4) } substr($4, n - length(a) + 1) == a || substr($4, n - length(b) + 1) == b' >>"$file"
    grep -q " 127\.0\.0\.1:$CLIPROXY_PORT " "$file" && grep -q " 127\.0\.0\.1:$CORE_PORT " "$file" && return
    kill -0 "$RUN_PID" 2>/dev/null || return
    sleep 0.1
  done
}

# The ports, once both sidecars are ready; then the listeners of the first start, and of
# the page's restart, which the second pids line marks.
CORE_PORT="" CLIPROXY_PORT=""
if wait_for_line '^smoke: core=' 1; then
  line="$(grep -m1 '^smoke: core=' "$OUT")"
  CORE_PORT="$(sed -n 's/^smoke: core=\([0-9]*\) cliproxy=\([0-9]*\)$/\1/p' <<<"$line")"
  CLIPROXY_PORT="$(sed -n 's/^smoke: core=\([0-9]*\) cliproxy=\([0-9]*\)$/\2/p' <<<"$line")"
fi
PHASES="start restart"
if [ -n "$CORE_PORT" ] && [ -n "$CLIPROXY_PORT" ]; then
  sample start
  wait_for_line '^smoke: pids=' 2 && sample restart
fi

STATUS=""
for _ in $(seq 1 $((RUN_LIMIT * 5))); do
  if ! kill -0 "$RUN_PID" 2>/dev/null; then
    wait "$RUN_PID"
    STATUS=$?
    REAPED=1
    break
  fi
  sleep 0.2
done
[ -n "$STATUS" ] || fail "the shell did not exit within $RUN_LIMIT s"

grep '^smoke: ' "$OUT"
for phase in $PHASES; do
  [ -f "$WORK/listeners.$phase" ] && sort -u "$WORK/listeners.$phase" | sed "s/^/smoke: listener $phase /"
done

[ "$STATUS" = 0 ] || fail "the shell exited with status ${STATUS:-none}"
[ -n "$CORE_PORT" ] && [ -n "$CLIPROXY_PORT" ] || fail "no 'smoke: core=<port> cliproxy=<port>' line"
grep -qx 'smoke: page status=200 restart=ok' "$OUT" || fail "the page did not report 'status=200 restart=ok'"
if ! grep -A1 '^smoke: core=' "$OUT" | tail -n1 | grep -Eqx 'smoke: revision=[0-9a-f]{7,64}'; then
  fail "the line after 'smoke: core=' is not 'smoke: revision=<hex>'"
fi
# The main window's grant names its seven hooks and smoke_done: the error page's command, a
# plugin's and the updater plugin's two are refused from it.
grep -qxF 'smoke: grant try_again=refused plugin:clipboard-manager|read_text=refused plugin:updater|check=refused plugin:updater|download_and_install=refused' "$OUT" ||
  fail "the page's grant did not refuse try_again, the clipboard plugin and the updater plugin"
# No check runs under LOKKI_SMOKE, and an install with nothing to install is refused before
# anything is downloaded or stopped.
grep -qx 'smoke: update state=idle' "$OUT" || fail "the update state was not 'idle'"
grep -qx 'smoke: install refused=no update is available to install' "$OUT" ||
  fail "the install was not refused with 'no update is available to install'"

# Both sidecars listen on loopback, and on no wildcard address, after each start.
if [ -n "$CORE_PORT" ] && [ -n "$CLIPROXY_PORT" ]; then
  for phase in $PHASES; do
    file="$WORK/listeners.$phase"
    [ -f "$file" ] || { fail "the listeners were not sampled after the $phase"; continue; }
    for name in core:$CORE_PORT CLIProxyAPI:$CLIPROXY_PORT; do
      port="${name#*:}" name="${name%%:*}"
      grep -q " 127\.0\.0\.1:$port " "$file" || fail "$name was not seen listening on 127.0.0.1:$port after the $phase"
      if grep -Eq " (0\.0\.0\.0|\[::\]|\*):$port " "$file"; then
        fail "$name listened on a wildcard address on port $port after the $phase"
      fi
    done
  done
fi

# Two starts: the shell's, then the page's restart, which starts both sidecars anew.
PIDS_LINES="$(grep -c '^smoke: pids=' "$OUT" || true)"
if [ "$PIDS_LINES" -ge 2 ]; then
  read -r first_core first_cli < <(grep '^smoke: pids=' "$OUT" | sed -n '1s/^smoke: pids=//p')
  read -r last_core last_cli < <(grep '^smoke: pids=' "$OUT" | sed -n '$s/^smoke: pids=//p')
  [ "$first_core" != "$last_core" ] && [ "$first_cli" != "$last_cli" ] || fail "the restart did not start new sidecar processes"
else
  fail "expected a 'smoke: pids=' line for the start and one for the restart, found $PIDS_LINES"
fi
for pid in $(pids_seen); do
  if kill -0 "$pid" 2>/dev/null || kill -0 -- "-$pid" 2>/dev/null; then
    fail "pid $pid (or its process group) is alive after the shell exited"
  else
    GONE="$GONE$pid "
  fi
done

# Every entry docs/app/data-directory.md names; CLIProxyAPI adds files of its own.
DOC="$REPO/docs/app/data-directory.md"
ENTRIES="$(sed -n 's/^| `\([^`]*\)` |.*/\1/p' "$DOC")"
[ -n "$ENTRIES" ] || fail "no entries read from $DOC"
for entry in $ENTRIES; do
  case "$entry" in
    */) [ -d "$DATA/$entry" ] || fail "the data directory has no directory $entry" ;;
    *) [ -f "$DATA/$entry" ] || fail "the data directory has no file $entry" ;;
  esac
done

grep -q 'relay listening on' "$DATA/logs/core.log" 2>/dev/null || fail "logs/core.log holds no 'relay listening on' banner"

# The four secrets, read here and compared here: none is printed or passed as an argument.
python3 -I - "$DATA" "$OUT" "$ERR" <<'PY' || FAILED=1
import os, sys, yaml
data, out, err = sys.argv[1:4]
with open(os.path.join(data, "config.yaml"), encoding="utf-8") as f:
    conf = yaml.safe_load(f)
with open(os.path.join(data, "secrets", "cliproxy-mgmt.secret"), encoding="utf-8") as f:
    secrets = {"the management secret": f.read().strip()}
for entry in conf["auth"]["tokens"]:
    secrets[f"the {entry['name']} token"] = entry["token"]
secrets["cli_proxy_api_key"] = conf["cli_proxy_api_key"]
if len(secrets) != 4 or not all(secrets.values()):
    print(f"smoke: FAIL expected four secrets, read {len(secrets)} names", file=sys.stderr)
    sys.exit(1)
logs = os.path.join(data, "logs")
files = {f"logs/{n}": os.path.join(logs, n) for n in sorted(os.listdir(logs)) if n.startswith(("app.log", "core.log", "cliproxy.log"))}
files.update({"the captured stdout": out, "the captured stderr": err})
bad = False
for label, path in files.items():
    with open(path, "rb") as f:
        text = f.read()
    for name, value in secrets.items():
        if value.encode() in text:
            print(f"smoke: FAIL {name} appears in {label}", file=sys.stderr)
            bad = True
for need in ("logs/app.log", "logs/core.log", "logs/cliproxy.log"):
    if need not in files:
        print(f"smoke: FAIL there is no {need} to check", file=sys.stderr)
        bad = True
sys.exit(1 if bad else 0)
PY

if [ "$FAILED" = 0 ]; then
  echo "smoke: ok"
fi
exit "$FAILED"
