#!/usr/bin/env bash
# Restart the midstream-abort relay on the addresses named in config.yaml.
#
# Replaces whatever currently holds each configured port — typically a relay
# started from older code, which is exactly the case the address change has to
# cover: fixing the bind default does not re-secure a process already running on
# 0.0.0.0. Run it from the repository root.
#
# Usage:  core/restart-relay.sh [--config PATH] [--dry-run]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CONFIG="${RELAY_CONFIG:-$HERE/config.yaml}"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="${2:?--config needs a path}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ ! -f "$CONFIG" ]; then
  echo "no config at $CONFIG — copy config.example.yaml and fill it in" >&2
  exit 2
fi

mapfile -t PORTS < <(python3 - "$CONFIG" <<'PY'
import sys
import yaml

cfg = yaml.safe_load(open(sys.argv[1])) or {}
listen = cfg.get("listen") or []
if isinstance(listen, str):
    listen = [listen]
for entry in listen:
    _, _, port = str(entry).rpartition(":")
    print(port or entry)
PY
)

# How long the relay is allowed to finish its in-flight streams after SIGTERM
# (W3.7). The listener stays open for the whole drain, so waiting for the port to
# free is waiting for the drain: escalating to SIGKILL before it ends is exactly
# the cut the drain exists to prevent. Resolved in the relay's own order:
# RELAY_DRAIN_SECONDS, then drain_seconds, then the default. Reading only the file
# meant an environment that lengthened the drain was SIGKILLed partway through it.
#
# The environment read is *this shell's*: it is also the one the relay started
# below inherits, but not necessarily the one the relay being replaced was started
# with (a cron-launched relay has cron's). An environment value that is not a
# non-negative integer refuses here, as the relay itself refuses it.
DRAIN="$(python3 - "$CONFIG" <<'DRAINCFG'
import os, sys
value = (os.environ.get("RELAY_DRAIN_SECONDS") or "").strip()
if value:
    if not value.isdigit():
        sys.exit(f"RELAY_DRAIN_SECONDS={value!r} is not a non-negative integer")
    print(int(value))
    sys.exit(0)
try:
    import yaml
    cfg = yaml.safe_load(open(sys.argv[1])) or {}
    value = cfg.get("drain_seconds")
    print(int(value) if value is not None else 15)
except Exception:
    print(15)
DRAINCFG
)"

if [ "${#PORTS[@]}" -eq 0 ]; then
  echo "config has no listen entries" >&2
  exit 2
fi

echo "config: $CONFIG"
echo "ports:  ${PORTS[*]}"
echo "drain:  ${DRAIN}s allowed after SIGTERM before escalating"

for port in "${PORTS[@]}"; do
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [ -z "$pids" ] && continue
  echo "port $port held by pid(s): $pids"
  if [ "$DRY_RUN" -eq 1 ]; then
    continue
  fi
  # SIGTERM first so the holder can close listeners cleanly, then confirm.
  kill $pids 2>/dev/null || true
  echo "waiting up to $((DRAIN + 5))s for the drain (drain_seconds=$DRAIN)"
  for _ in $(seq 1 $(( (DRAIN + 5) * 2 ))); do
    still="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    [ -z "$still" ] && break
    sleep 0.5
  done
  still="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$still" ]; then
    echo "port $port still held by $still after SIGTERM; sending SIGKILL" >&2
    kill -9 $still 2>/dev/null || true
  fi
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo "dry run: not starting the relay"
  exit 0
fi

exec python3 "$HERE/server.py" --config "$CONFIG"
