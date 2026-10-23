#!/usr/bin/env bash
# Installs Debian packages on a GitHub-hosted Ubuntu runner, for app.yml's Linux jobs:
#
#   apt-install.sh [--debs DIR] PACKAGE...
#
# A normal fetch of the app's packages takes about a minute, but the image's first apt mirror,
# azure.archive.ubuntu.com, can crawl for half an hour. So each attempt gets 240 s and there is
# one retry, through archive.ubuntu.com: at most 570 s, counting the kill grace and the lock wait.
# Both running out fails the step with an ::error::.
#
# With --debs, DIR holds an earlier run's .deb files, restored by actions/cache: dpkg installs
# them and nothing is fetched. A fetch copies the .deb files it fetched into DIR for the cache to
# keep. dpkg checks no signature, so the release job passes no --debs: everything it installs is
# fetched by apt, which checks the archive's.
#
# `timeout` runs as root and signals its whole process group, apt's methods and dpkg included; the
# retry waits for apt and dpkg to exit, then finishes any half-done dpkg run, so a killed attempt
# cannot leave the lock held. apt resumes partial downloads.
set -euo pipefail

debs=""
if [ "${1:-}" = --debs ]; then debs="$2"; shift 2; fi
[ "$#" -gt 0 ] || { echo "usage: apt-install.sh [--debs DIR] PACKAGE..." >&2; exit 2; }

if [ -n "$debs" ] && ls "$debs"/*.deb >/dev/null 2>&1; then
  if sudo dpkg -i "$debs"/*.deb; then
    exit 0
  fi
  echo "::warning::The cached .deb files did not install; this run fetches them, and so will every run until the cache key changes or the entry is deleted."
fi

attempt() {
  local bound="$1"
  shift
  sudo timeout --kill-after=15 "$bound" sh -c 'apt-get update && apt-get install -y "$@"' apt-install "$@"
}
if ! attempt 240 "$@"; then
  echo "::warning::The first attempt failed or ran out of time; retrying once, through archive.ubuntu.com."
  for _ in $(seq 30); do pgrep -x 'apt|apt-get|dpkg' >/dev/null || break; sleep 2; done
  sudo pkill -x 'apt|apt-get' || true
  sudo dpkg --configure -a
  # Only this attempt: the image's mirror list is put back when the script ends, whatever way it ends.
  sudo cp /etc/apt/apt-mirrors.txt "$RUNNER_TEMP/apt-mirrors.txt"
  trap 'sudo cp "$RUNNER_TEMP/apt-mirrors.txt" /etc/apt/apt-mirrors.txt' EXIT
  printf 'https://archive.ubuntu.com/ubuntu/\tpriority:1\n' | sudo tee /etc/apt/apt-mirrors.txt >/dev/null
  attempt 240 "$@" || { echo "::error::The Linux packages did not install in two attempts of 240 s each."; exit 1; }
fi

if [ -n "$debs" ]; then
  mkdir -p "$debs"
  cp /var/cache/apt/archives/*.deb "$debs"/
fi
