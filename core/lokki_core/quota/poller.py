"""A daemon thread that keeps the latest quota snapshot, read in every mode, so an
account's numbers show and a switch to Auto has a snapshot at once. It stores only
the snapshot; the decision is computed per request (`policy.decide`). It wakes on
`wake()` (a reload or a refresh) and otherwise sleeps `poll_seconds`; after a
read the management API refused (401, 403) it sleeps until a wake, because about
ten failed management logins earn this address a thirty-minute ban that refuses
the right secret too. Every exception in the loop is recorded as `last_error`
and said once per change (a dead management API makes every request a
full-price one while the endpoint looks healthy, so silence here is the costly
failure); the thread never dies."""
import sys
import threading
import time

from .. import config, control
from . import reader

PER_CALL_TIMEOUT = 10.0
_HELD = "; polling paused until a reload or a refresh"


def current_settings():
    """One consistent read of the three settings the loop uses: each is swapped in
    one assignment by `apply_settings`, and the apply hook runs after all three."""
    return config.LOW_QUOTA, config.CLIPROXY, config.ROUTING


def management_url():
    """Where CLIProxyAPI is: the running upstream, the one address pass-through
    uses, with any credentials of the legacy forms taken out."""
    return control.without_credentials(config.UPSTREAM).rstrip("/")


def _say(line):
    print(f"[quota] {line}", file=sys.stderr, flush=True)


def management_secret(cliproxy):
    path = cliproxy.get("management_secret_file") or ""
    if not path:
        raise reader.ManagementError("no management secret file is set")
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
    except OSError as e:
        raise reader.ManagementError(f"management secret file unreadable: {type(e).__name__}") from None
    if not value:
        raise reader.ManagementError("management secret file is empty")
    return value


class Poller:
    def __init__(self, read=reader.read, clock=time.time, log=None):
        self._read, self._clock, self._log = read, clock, log or _say
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._latest = None
        self._status = {"last_ok_at": None, "last_error": None, "reads": 0, "running": False}
        self._finished_reads = 0           # reads ended, under _cond
        # Loop passes ended, under _cond. Production code keeps it because it is the positive
        # control the "no read happened" tests rest on: an absence never moves a read counter,
        # and a fixed sleep proves nothing on a loaded host.
        self._passes = 0
        self._reading = False              # a read is in flight, under _cond
        self._held = False                 # the last read was refused: sleep until a wake
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._status["running"] = True
        config.ON_APPLY.append(self.wake)
        self._thread = threading.Thread(target=self._loop, name="lokki-quota-poller", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.wake in config.ON_APPLY:
            config.ON_APPLY.remove(self.wake)

    def wake(self):
        self._wake.set()

    def latest(self):
        with self._cond:
            return self._latest

    def status(self):
        with self._cond:
            return dict(self._status)

    def refresh(self, timeout=15.0):
        """One read now: joins the read in flight or wakes the loop to start one,
        waits for that read at most `timeout`, and returns the latest snapshot.
        Two refreshes during one read wait for the same read. With no running
        thread there is nothing to wait for: the latest snapshot, at once, and the
        same if the thread ends while it waits."""
        with self._cond:
            if not self._status["running"]:
                return self._latest
            wanted = self._finished_reads + 1      # the next read to finish, in flight or not
            join = self._reading
        if not join:
            self._wake.set()
        with self._cond:
            self._cond.wait_for(lambda: self._finished_reads >= wanted or not self._status["running"], timeout)
            return self._latest

    def _loop(self):
        while not self._stop.is_set():
            low_quota, cliproxy, _ = current_settings()
            self._one_read(low_quota, cliproxy)
            with self._cond:
                self._passes += 1
                self._cond.notify_all()
            interval = float(low_quota.get("poll_seconds") or 60)
            with self._cond:
                held = self._held
            self._wake.wait(None if held else interval)
            self._wake.clear()
        with self._cond:
            self._status["running"] = False
            self._cond.notify_all()        # a refresh waiting for a read that will not come

    def _one_read(self, low_quota, cliproxy):
        error, held = None, False
        with self._cond:
            self._reading = True
        try:
            snapshot = self._read(management_url(), management_secret(cliproxy),
                                  accounts=tuple(low_quota.get("accounts") or ()),
                                  per_call_timeout=PER_CALL_TIMEOUT,
                                  deadline=float(low_quota.get("poll_seconds") or 60) / 2,
                                  cliproxy_version=cliproxy.get("version") or "")
        except reader.ManagementError as e:
            held = e.status in (401, 403)
            error = str(e) + (_HELD if held else "")
        except Exception as e:  # noqa: BLE001 - the thread must outlive any bug
            error = f"{type(e).__name__}: {e}"
        with self._cond:
            previous = self._status["last_error"]
            if error is None:
                self._latest = snapshot
                self._status.update(last_ok_at=self._clock(), last_error=None, reads=self._status["reads"] + 1)
            else:
                self._status["last_error"] = error
            self._held = held
            self._finished_reads += 1
            self._reading = False
            self._cond.notify_all()
        if error != previous:                 # once per change, the recovery included
            self._log(f"quota reads failed: {error}" if error else "management API answering again")


POLLER = Poller()
