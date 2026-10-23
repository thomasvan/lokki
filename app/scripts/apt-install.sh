#!/usr/bin/env bash
# Installs Debian packages on a GitHub-hosted Ubuntu runner, for app.yml's Linux jobs:
#
#   apt-install.sh [--debs DIR] PACKAGE...
#
# A normal fetch of the app's packages takes about a minute, but the image's first apt mirror,
# azure.archive.ubuntu.com, can crawl for half an hour. So each attempt gets 240 s and there is
# one retry, through archive.ubuntu.com: at most 715 s, counting the kill grace, the lock wait, the
# wait after a kill and the bounded finish of a killed dpkg run. Both running out fails the step with an ::error::, and
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
# `timeout`, uutils coreutils' on this image, runs as root and leads a process group of its own,
# which holds apt-get. On a hosted runner, a first attempt cut at its bound returned at the bound,
# its 15 s kill grace unused, with apt-get still exiting; 2 s later apt-get was gone. The log
# shows no signal, so it does not show which processes `timeout` signalled. apt starts dpkg in a
# session of its own, outside that group, and dpkg's maintainer scripts run in that session: at
# the return dpkg and two of its `sh` children were alive, and the terminal apt gives dpkg had
# hung up as apt-get exited. 2 s later dpkg was gone; what ended it, the log does not show. Then
# `dpkg --configure -a` configured what it had unpacked, and the retry installed the rest.
# The retry waits 60 s for apt, apt-get and dpkg to exit. If any is left, it kills dpkg's session
# with what runs in it, then every process named apt, apt-get or dpkg, and waits up to 10 s for
# them to be gone, so that `dpkg --configure -a` finds dpkg's lock free. No run has shown what a
# killed dpkg leaves. apt resumes partial downloads.
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
# TEMPORARY: a 10 s first attempt, no lock wait, and the processes and dpkg states around the kill.
if ! attempt 10 "$@"; then
  echo "== TEMPORARY the first attempt returned"; ps -e -o pid,ppid,pgid,sid,stat,comm | awk 'NR == 1 || $6 ~ /^(sudo|timeout|sh|apt|apt-get|dpkg|dpkg-deb|http|https|store|gpgv|frontend|ldconfig|update-alternat|systemctl|deb-systemd-hel|install-info|mandb)$/'
  echo "::warning::The first attempt failed or ran out of time; retrying once, through archive.ubuntu.com."
  # apt, apt-get and dpkg gone within $1 checks 2 s apart
  gone() { for _ in $(seq "$1"); do pgrep -x 'apt|apt-get|dpkg' >/dev/null || return 0; sleep 2; done; return 1; }
  if ! gone 0; then
    # `pkill -s` kills the sessions these pids lead; a dpkg that leads none kills nothing more.
    if sessions=$(pgrep -d, -x dpkg); then sudo pkill -KILL -s "$sessions" || true; fi
    sudo pkill -KILL -x 'apt|apt-get|dpkg' || true
    echo "== TEMPORARY just after the kill"; ps -e -o pid,ppid,pgid,sid,stat,comm | awk 'NR == 1 || $6 ~ /^(sudo|timeout|sh|apt|apt-get|dpkg|dpkg-deb|http|https|store|gpgv|frontend|ldconfig|update-alternat|systemctl|deb-systemd-hel|install-info|mandb)$/'
    gone 5 || { echo "::error::apt or dpkg was still running 10 s after SIGKILL, so the retry cannot install."; exit 1; }
  fi
  echo "== TEMPORARY after the wait after the kill"; ps -e -o pid,ppid,pgid,sid,stat,comm | awk 'NR == 1 || $6 ~ /^(sudo|timeout|sh|apt|apt-get|dpkg|dpkg-deb|http|https|store|gpgv|frontend|ldconfig|update-alternat|systemctl|deb-systemd-hel|install-info|mandb)$/'
  { echo "== TEMPORARY dpkg --audit"; sudo dpkg --audit || true; echo "== TEMPORARY dpkg -l, not ii"; dpkg -l | awk 'NR > 5 && $1 != "ii"'; }
  finish=0
  sudo timeout --kill-after=15 120 dpkg --configure -a || finish=$?
  echo "== TEMPORARY after dpkg --configure -a, exit $finish"; { echo "== TEMPORARY dpkg --audit"; sudo dpkg --audit || true; echo "== TEMPORARY dpkg -l, not ii"; dpkg -l | awk 'NR > 5 && $1 != "ii"'; }
  case "$finish" in
    0) ;;
    124|137) echo "::error::dpkg --configure -a did not finish the first attempt's half-done run within 120 s, so the retry cannot install."; exit 1 ;;
    *) echo "::error::dpkg --configure -a failed with exit $finish, so the retry cannot install."; exit 1 ;;
  esac
  [ -f /etc/apt/apt-mirrors.txt ] \
    || { echo "::error::The image has no /etc/apt/apt-mirrors.txt, so the retry has no mirror list to point at archive.ubuntu.com."; exit 1; }
  # Only this attempt: the image's mirror list is put back when the script ends, whatever way it ends.
  sudo cp /etc/apt/apt-mirrors.txt "$RUNNER_TEMP/apt-mirrors.txt"
  # A function: the ERR trap does not reach into one, so a restore that fails is named once, by its own line.
  restore() {
    sudo cp "$RUNNER_TEMP/apt-mirrors.txt" /etc/apt/apt-mirrors.txt \
      || { echo "::error::apt-install.sh could not put the image's mirror list back into /etc/apt/apt-mirrors.txt."; exit 1; }
  }
  trap restore EXIT
  printf 'https://archive.ubuntu.com/ubuntu/\tpriority:1\n' | sudo tee /etc/apt/apt-mirrors.txt >/dev/null
  attempt 240 "$@" || { echo "::error::The Linux packages did not install in two attempts of 240 s each."; exit 1; }
  echo "== TEMPORARY after the retry"; { echo "== TEMPORARY dpkg --audit"; sudo dpkg --audit || true; echo "== TEMPORARY dpkg -l, not ii"; dpkg -l | awk 'NR > 5 && $1 != "ii"'; }
fi

if [ -n "$debs" ]; then
  mkdir -p "$debs"
  # An install that fetched nothing leaves no .deb to keep, and is a good install.
  find /var/cache/apt/archives -maxdepth 1 -name '*.deb' -exec cp -t "$debs" {} +
fi
