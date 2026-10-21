#!/usr/bin/env bash
# Keep the live cliproxy container up: start it once the Tailscale address it binds exists.
#
# `cli-proxy-api-8318` publishes <tailscale-addr>:8318, and at boot Docker starts a container
# with `restart: unless-stopped` before tailscale0 has that address. The bind then fails, and
# (checked on this host) the container is poisoned: it stays `Created`, and every later
# `docker start` SUCCEEDS but leaves it running with no published port at all (`docker port`
# empty), which even a stop and start does not undo. The relay's /healthz does not look
# upstream, so that would be a healthy-looking relay in front of a dead cliproxy. Hence:
#
#   - the container's restart policy is `no`, so Docker never makes that first, failing start;
#   - this script starts it, and only when the address is up on an interface (it waits for it);
#   - "running" is not enough: the port must be published, or the script stops the container.
#     Docker erroring, or the port not showing for a moment, is not a verdict: it is read
#     again a few times, and a docker that will not answer ends the run without touching
#     anything;
#   - a container that has just exited is not "running without its port": it is re-read before
#     anything is stopped or marked, and one that exits right after a successful start is
#     reported (look at its logs), not marked;
#   - a failed `docker start` ends the script and leaves a marker file: the next start would
#     poison the container, so the only way back is to recreate it (the message says how), and
#     until then every later run says so and touches nothing. The marker goes when the
#     container is next seen running with its port published (by a plain run, not --check).
#
# The cost of policy `no`: a container that exits (a crash, a Docker restart) stays down, where Docker's
# own restart would take seconds, until this script runs again (at the next reboot, or by hand) or the
# harness's nightly :8318 step starts it (below). A paused container counts as up.
#
# Run it by hand after a Docker restart (docs/relay/live-8381.md). A reboot has a line for it: the harness
# (~/Sites/AI/harness; its PR #90, merged 2026-10-09) installed one line in the crontab (with the Human's
# approval, as its session reported): `@reboot sleep 30` and then this script, its output appended to
# $HOME/HARNESS/logs/<host>/cliproxy.log; `crontab -l` says what is installed now. A real reboot with
# it has not been tried. Nothing runs the script every five minutes: that watchdog is what the script was
# written to allow, not what is installed (adding it is the Human's, and it would start a container
# stopped on purpose again within five minutes). The script is idempotent: a container that is running
# with its port published is left alone.
#
# The lock (LOCK, /tmp/clisaving-container-up.lock) is shared with the harness's nightly :8318 step
# (bin/cliproxy-update.sh there, run by system-update.sh at 04:00 and once 5 s after boot). The step pulls
# the image (a failed pull is logged as ERROR and no longer stops it, harness PR #92) and then, only while the address is on an interface and it gets the lock without waiting
# (`flock -n`), runs `docker compose up -d` (which recreates the container if the image or the compose
# file changed, and, as that command is documented, starts one that is stopped: the harness measured the Docker daemon's restart leaving this container stopped on
# 2026-10-10; starting a stopped one with an unchanged image was not seen here) and
# `--check`. It skips all of that while this script holds the lock (its boot wait), so that a `docker
# start` here cannot race a compose recreate. A run of this script that finds the lock held exits 0 at
# once and starts nothing; the step runs this script itself, with its own lock released, once, when the
# container is not up and published after its `up -d` or the `up -d` failed. Do not rename or move the
# lock without telling the harness.
#
# Usage: deploy/container-up.sh            start it if it is not up
#        deploy/container-up.sh --check    only say whether it is up and published; change nothing
# Env:   CONTAINER, ADDR (else TAILSCALE_ADDR, in the environment or deploy/.env), TRIES, WAIT_S, RECHECK_S, MARKER, LOCK, DOCKER_T override the defaults
#        (the tests use them). Every docker call is capped at DOCKER_T seconds, and a second run
#        while one is waiting (the one at boot waits up to five minutes) exits at once.
# Exit:  0 up and published; 1 not up (--check), docker not answering, or gave up waiting;
#        2 no such container; 3 needs recreating; 64 a bad argument, or no usable address.
set -u

CONTAINER="${CONTAINER:-cli-proxy-api-8318}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# the address the container publishes on: ADDR, else TAILSCALE_ADDR in the environment, else TAILSCALE_ADDR in
# deploy/.env (untracked; the compose file reads the same file). A line may start with `export`, hold quotes, and end in a comment.
if [ -z "${ADDR:-}" ]; then ADDR="${TAILSCALE_ADDR:-}"; fi
if [ -z "${ADDR:-}" ] && [ -r "$HERE/.env" ]; then
  ADDR="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\{0,1\}TAILSCALE_ADDR=//p' "$HERE/.env" | tail -n 1 | sed 's/[[:space:]]*#.*$//' | tr -d "\"'\r ")"
fi
if ! [[ "${ADDR:-}" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
  echo "no usable address: set ADDR or TAILSCALE_ADDR, or put TAILSCALE_ADDR=<IPv4 address> in $HERE/.env" >&2
  exit 64
fi
TRIES="${TRIES:-20}"            # 20 tries 15 s apart: five minutes for tailscale0 to come up
WAIT_S="${WAIT_S:-15}"
RECHECK_S="${RECHECK_S:-2}"     # between the three reads of `docker port`
MARKER="${MARKER:-${HOME:-/tmp}/.local/state/cliproxy-8381/container-needs-recreate}"
LOCK="${LOCK:-/tmp/clisaving-container-up.lock}"
DOCKER_T="${DOCKER_T:-20}"      # a hung dockerd must not leave a script stuck on every run

CHECK=0
case "${1:-}" in
  "") ;;
  --check) CHECK=1 ;;
  *) echo "usage: $0 [--check]" >&2; exit 64 ;;
esac

if [ "$CHECK" -eq 0 ]; then
  exec 9>"$LOCK"
  flock -n 9 || { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) another run is in progress; nothing to do"; exit 0; }
fi

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
dk() { timeout "$DOCKER_T" docker "$@"; }
# an address on an interface that is down cannot be bound
has_addr() { ip -4 -o addr show up 2>/dev/null | grep -qF " ${ADDR}/"; }
# 0 published; 1 no port; 2 docker would not say (its error is not an empty answer)
port_state() {
  local out
  out="$(dk port "$CONTAINER" 2>/dev/null)" || return 2
  [ -n "$out" ]
}
# the same, read up to three times: a blip, or a start still settling, is not a verdict
settled_port_state() {
  local n s=1
  for n in 1 2 3; do
    port_state; s=$?
    [ "$s" -eq 0 ] && return 0
    [ "$n" -lt 3 ] && sleep "$RECHECK_S"
  done
  return "$s"
}
recreate_hint() {
  echo "$(stamp) recreate it: docker rm -f $CONTAINER && docker compose -f deploy/docker-compose.8318.yml up -d" \
       "(from ~/Sites/TOOL/lokki, the main checkout; the marker $MARKER goes by itself once it runs)" >&2
}
needs_recreate() {  # $1: why
  mkdir -p "$(dirname "$MARKER")" 2>/dev/null
  { echo "$(stamp) $1"; } 2>/dev/null > "$MARKER" 2>/dev/null \
    || echo "$(stamp) could not write the marker $MARKER: the next run will find this out again" >&2
  recreate_hint
  exit 3
}
docker_answers() { dk info >/dev/null 2>&1; }
# `docker inspect` prints an empty line before it fails, so the failure is read from its status.
running_state() { dk inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null; }
stop_it() { timeout 40 docker stop -t 10 "$CONTAINER" >/dev/null 2>&1; }

for i in $(seq 1 "$TRIES"); do
  state="$(running_state)" || state=missing

  if [ "$state" = missing ]; then
    if docker_answers; then
      echo "$(stamp) no container named $CONTAINER, attempt $i" >&2
      if [ "$CHECK" -eq 1 ] || [ "$i" -ge 3 ] || [ "$i" -ge "$TRIES" ]; then exit 2; fi
    else
      echo "$(stamp) docker is not answering yet, attempt $i" >&2
      if [ "$CHECK" -eq 1 ]; then exit 1; fi
    fi
    sleep "$WAIT_S"
    continue
  fi

  if [ "$state" = true ]; then
    settled_port_state; s=$?
    if [ "$s" -eq 0 ]; then
      [ "$CHECK" -eq 1 ] || rm -f "$MARKER" 2>/dev/null   # --check changes nothing
      echo "$(stamp) $CONTAINER is running with its port published"
      exit 0
    fi
    if [ "$s" -eq 2 ]; then
      echo "$(stamp) docker would not say whether $CONTAINER has its port published; leaving it alone" >&2
      exit 1
    fi
    # a container that exited while it was being read has no port either: look again first
    if [ "$(running_state)" != true ]; then
      echo "$(stamp) $CONTAINER stopped while it was being checked; looking again" >&2
      continue
    fi
    if [ "$CHECK" -eq 1 ]; then
      echo "$(stamp) $CONTAINER runs with no published port: it needs recreating" >&2
      exit 3
    fi
    echo "$(stamp) $CONTAINER runs with no published port: stopping it" >&2
    stop_it
    needs_recreate "$CONTAINER ran without its published port"
  fi

  # not running from here on
  if [ -e "$MARKER" ]; then
    echo "$(stamp) $CONTAINER needs recreating (since: $(cat "$MARKER" 2>/dev/null)); not touching it" >&2
    recreate_hint
    exit 3
  fi
  if [ "$CHECK" -eq 1 ]; then
    echo "$(stamp) $CONTAINER is not running" >&2
    exit 1
  fi
  if ! has_addr; then
    echo "$(stamp) attempt $i: $ADDR is not up on any interface yet; waiting ${WAIT_S}s"
    sleep "$WAIT_S"
    continue
  fi

  if dk start "$CONTAINER" >/dev/null 2>&1; then
    settled_port_state; s=$?
    if [ "$s" -eq 0 ]; then
      echo "$(stamp) started $CONTAINER, its port is published"
      exit 0
    fi
    if [ "$s" -eq 2 ]; then
      echo "$(stamp) started $CONTAINER, but docker would not say whether its port is published; leaving it alone" >&2
      exit 1
    fi
    if [ "$(running_state)" != true ]; then
      echo "$(stamp) $CONTAINER exited right after it started: look at its logs (docker logs $CONTAINER); not marking it" >&2
      exit 1
    fi
  elif ! docker_answers; then
    echo "$(stamp) docker stopped answering during the start; not marking anything, the next run tries again" >&2
    exit 1
  fi
  echo "$(stamp) $CONTAINER did not start with its port published; stopping it, and not trying again" >&2
  stop_it
  needs_recreate "$CONTAINER did not start with its port published"
done

echo "$(stamp) gave up on $CONTAINER after $TRIES attempts (docker not answering, or $ADDR never came up)" >&2
exit 1
