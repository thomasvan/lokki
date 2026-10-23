#!/usr/bin/env bash
# Installs Debian packages on a GitHub-hosted Ubuntu runner, for app.yml's Linux jobs:
#
#   apt-install.sh [--debs DIR] PACKAGE...
#
# A normal fetch of the app's packages takes about a minute, but the image's first apt mirror,
# azure.archive.ubuntu.com, can crawl for half an hour. So each attempt gets 240 s and there is
# one retry, through archive.ubuntu.com: at most 705 s, counting the kill grace, the lock wait and
# the bounded finish of a killed dpkg run. Both running out fails the step with an ::error::, and
# so does anything else that ends the script early. The image's other source,
# packages.microsoft.com, is not in the mirror list, so a stall there is retried through the
# same host.
#
# With --debs, DIR holds an earlier run's .deb files, restored by actions/cache. They go into
# apt's own archive directory before the first attempt, so apt fetches only what they lack and
# orders the install itself: `dpkg -i` of the whole set fails on systemd's pre-dependency and
# leaves the system broken. After the install, the archive directory is copied into DIR for the
# cache to keep. The release job passes no --debs, so everything it installs is fetched from the
# archive.
#
# `timeout` runs as root and signals its process group, which holds apt-get and its download
# methods. A killed attempt can leave apt or dpkg holding the lock the retry needs, so the retry
# waits 60 s for them to exit and then kills what is left, dpkg included: dpkg is built to be
# stopped at any point, and `dpkg --configure -a` and the retry's install finish what it left.
# apt resumes partial downloads.
set -euo pipefail
trap 'echo "::error::apt-install.sh, line $LINENO: \`$BASH_COMMAND\` failed with exit $?."' ERR

debs=""
if [ "${1:-}" = --debs ]; then
  [ "$#" -ge 2 ] || { echo "::error::apt-install.sh: --debs takes a directory."; exit 2; }
  debs="$2"
  shift 2
fi
[ "$#" -gt 0 ] || { echo "::error::usage: apt-install.sh [--debs DIR] PACKAGE..."; exit 2; }

if [ -n "$debs" ] && ls "$debs"/*.deb >/dev/null 2>&1; then
  sudo cp "$debs"/*.deb /var/cache/apt/archives/
fi

attempt() {
  local bound="$1"
  shift
  sudo timeout --kill-after=15 "$bound" sh -c 'apt-get update && apt-get install -y "$@"' apt-install "$@"
}
if ! attempt 240 "$@"; then
  echo "::warning::The first attempt failed or ran out of time; retrying once, through archive.ubuntu.com."
  for _ in $(seq 30); do pgrep -x 'apt|apt-get|dpkg' >/dev/null || break; sleep 2; done
  sudo pkill -KILL -x 'apt|apt-get|dpkg' || true
  sudo timeout --kill-after=15 120 dpkg --configure -a \
    || { echo "::error::dpkg did not finish the first attempt's half-done run within 120 s, so the retry cannot install."; exit 1; }
  [ -f /etc/apt/apt-mirrors.txt ] \
    || { echo "::error::The image has no /etc/apt/apt-mirrors.txt, so the retry has no mirror list to point at archive.ubuntu.com."; exit 1; }
  # Only this attempt: the image's mirror list is put back when the script ends, whatever way it ends.
  sudo cp /etc/apt/apt-mirrors.txt "$RUNNER_TEMP/apt-mirrors.txt"
  trap 'sudo cp "$RUNNER_TEMP/apt-mirrors.txt" /etc/apt/apt-mirrors.txt' EXIT
  printf 'https://archive.ubuntu.com/ubuntu/\tpriority:1\n' | sudo tee /etc/apt/apt-mirrors.txt >/dev/null
  attempt 240 "$@" || { echo "::error::The Linux packages did not install in two attempts of 240 s each."; exit 1; }
fi

if [ -n "$debs" ]; then
  mkdir -p "$debs"
  # An install that fetched nothing leaves no .deb to keep, and is a good install.
  find /var/cache/apt/archives -maxdepth 1 -name '*.deb' -exec cp -t "$debs" {} +
fi
