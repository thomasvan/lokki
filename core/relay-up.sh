#!/usr/bin/env bash
# Idempotent start-or-verify for the relay, driven by cron for the earlier pair (:8371). The live pair
# (:8381, docs/relay/live-8381.md) has no cron entry since 2026-10-07: run it by hand there, with
# RELAY_LOG_DIR and RELAY_LOCK set so it does not use the earlier pair's log and lock.
#
# The Human owns the crontab (WORKSPACE_PROTOCOL: "Human must decide ... the cron
# entries that supervise the relay"), so this script is the
# deliverable and the entries below are for the Human to add. Two entries, the
# same shape as the other service on this host:
#
#   @reboot     sleep 30 && "$HOME/Sites/TOOL/cliproxy/midstream-abort/relay-up.sh" >> "$HOME/HARNESS/logs/relay-up.log" 2>&1
#   */5 * * * *  "$HOME/Sites/TOOL/cliproxy/midstream-abort/relay-up.sh" >> "$HOME/HARNESS/logs/relay-up.log" 2>&1
#
# The five-minute watchdog IS the restart mechanism: a crash is repaired up to
# five minutes late. That is the accepted cost of choosing cron over a systemd
# unit, written down so it is not
# discovered during an incident.
#
# Safe to run concurrently and safe when the relay is healthy: it takes a lock,
# and it starts the relay only when nothing is bound on its configured addresses.
#
# Usage: relay-up.sh [--check] [--config PATH]
#   --check   probe and report, never start. For a human or a test.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CONFIG="${RELAY_CONFIG:-$HERE/config.yaml}"
LOG_DIR="${RELAY_LOG_DIR:-$HOME/HARNESS/logs}"
LOG="$LOG_DIR/relay.log"
LOCK="${RELAY_LOCK:-/tmp/cliproxy-relay-up.lock}"
CHECK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --config) CONFIG="${2:?--config needs a path}"; shift 2 ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

if [ ! -f "$CONFIG" ]; then
  echo "$(stamp) no config at $CONFIG" >&2
  exit 2
fi

# The addresses to probe: each configured `host:port`, at that host. Probing
# 127.0.0.1 alone misread a relay configured only on its Tailscale address as
# "nothing bound", and the watchdog then started a second relay every five
# minutes. A bare port means loopback, as the relay reads it; a wildcard host is
# probed on loopback, where a wildcard bind also answers.
targets_raw="$(python3 - "$CONFIG" <<'TARGETS'
import sys
try:
    import yaml
except ImportError:
    sys.exit(0)
cfg = yaml.safe_load(open(sys.argv[1])) or {}
listen = cfg.get("listen") or []
if isinstance(listen, str):
    listen = [listen]
for entry in listen:
    entry = str(entry).strip()
    if not entry:
        continue
    host, _, port = entry.rpartition(":")
    if not port.isdigit():
        continue
    if host in ("", "0.0.0.0", "*"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    print(f"{host} {int(port)}")
TARGETS
)"
mapfile -t TARGETS < <(printf '%s\n' "$targets_raw" | sed '/^$/d')
ports="$(for t in "${TARGETS[@]}"; do printf '%s:%s ' "${t% *}" "${t#* }"; done)"

if [ "${#TARGETS[@]}" -eq 0 ]; then
  echo "$(stamp) config has no listen entries" >&2
  exit 2
fi

# The URL for one target: an IPv6 host needs brackets.
target_url() {
  local host="${1% *}" port="${1#* }"
  case "$host" in *:*) host="[$host]" ;; esac
  printf 'http://%s:%s/healthz' "$host" "$port"
}

healthz() { curl -fsS --max-time "$2" "$(target_url "$1")" >/dev/null 2>&1; }

# Is anything listening on this address? Used only to tell "nothing here" from
# "something here that is not answering".
port_bound() {
  python3 - "${1% *}" "${1#* }" <<'BOUND'
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=2).close()
except OSError:
    sys.exit(1)
BOUND
}

# Two different questions, deliberately asked separately.
#
#   healthy — /healthz answers 200: the relay is up, and it is a build that has it.
#   bound   — something is listening. It may be an older build with no /healthz
#             (that build answers 401), or a wedged process.
#
# Starting on "not healthy" alone is a spawn storm: the pre-Slice-1 build answers
# 401 on /healthz, so the watchdog would try to start a second relay every five
# minutes, the bind would fail every time, and nothing would say so. Starting on
# "not bound" alone would never repair a wedged process. So: start only when
# nothing is bound, and say loudly when something is bound but not healthy.
healthy=0
bound=0
total=0
for target in "${TARGETS[@]}"; do
  total=$((total + 1))
  if healthz "$target" 3; then
    healthy=$((healthy + 1))
    bound=$((bound + 1))
  elif port_bound "$target"; then
    bound=$((bound + 1))
  fi
done

if [ "$healthy" -gt 0 ]; then
  echo "$(stamp) relay healthy ($healthy/$total addresses answering /healthz)"
  exit 0
fi

if [ "$CHECK" -eq 1 ]; then
  echo "$(stamp) relay NOT answering /healthz on any of: $ports (bound: $bound/$total)"
  exit 1
fi

if [ "$bound" -gt 0 ]; then
  echo "$(stamp) WARNING: $bound/$total addresses are bound but none answers /healthz." \
       "Not starting a second relay. If this is a pre-/healthz build, upgrade it;" \
       "if it is wedged, stop it by hand." >&2
  exit 0
fi

mkdir -p "$LOG_DIR"
# Take the lock only for the start path: two watchdogs firing together must not
# both spawn a relay, and the second one finding the port taken would otherwise
# look like a bind failure.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(stamp) another relay-up holds the lock; nothing to do"
  exit 0
fi

# Re-probe now that we hold the lock: the other invocation may have just started it.
for target in "${TARGETS[@]}"; do
  if healthz "$target" 3; then
    echo "$(stamp) relay came up while waiting for the lock"
    exit 0
  fi
done

echo "$(stamp) nothing bound on $ports; starting"
cd "$REPO"
# `9>&-` matters: without it the spawned relay inherits the flock descriptor and
# holds the lock for its whole life, so a later invocation that genuinely needs to
# start — or a second instance on its own port — could never acquire it.
nohup python3 "$HERE/server.py" --config "$CONFIG" 9>&- >>"$LOG" 2>&1 &
child=$!
echo "$(stamp) started pid $child (log: $LOG)"

# Do not claim success from having forked: a bind failure exits inside the child,
# so wait for the server to answer before reporting anything.
for _ in $(seq 1 20); do
  for target in "${TARGETS[@]}"; do
    if healthz "$target" 2; then
      echo "$(stamp) relay is answering on ${target% *}:${target#* }"
      exit 0
    fi
  done
  if ! kill -0 "$child" 2>/dev/null; then
    echo "$(stamp) the relay exited immediately; refusing to report success" >&2
    tail -5 "$LOG" >&2 || true
    exit 2
  fi
  sleep 1
done

echo "$(stamp) the relay did not answer /healthz within 20s; it may still be" \
     "starting, or it may be wedged" >&2
tail -5 "$LOG" >&2 || true
exit 2
