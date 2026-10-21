#!/usr/bin/env python3
"""The poller: a daemon thread that keeps the latest snapshot, wakes on reload
and refresh, never dies, and reads the secret from its file each time."""
import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import config  # noqa: E402
from lokki_core.quota import poller, reader  # noqa: E402
from test_offline import _restore_globals, _snapshot_globals  # noqa: E402


class FakeRead:
    def __init__(self):
        self.calls = []
        self.answer = {"read_at": 1.0, "accounts": {}}
        self.raise_once = None
        self.started = threading.Event()

    def __call__(self, management_url, secret, **kw):
        self.calls.append((management_url, secret, kw))
        self.started.set()
        if self.raise_once:
            exc, self.raise_once = self.raise_once, None
            raise exc
        return dict(self.answer, read_at=time.time())


class CountingCondition(threading.Condition):
    """Counts the waits entered. A wait is entered holding the lock, which it releases only
    once parked, so a notifier that takes the lock after the count moved cannot be missed."""
    def __init__(self):
        super().__init__()
        self.waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        return super().wait(timeout)


class GatedEvent(threading.Event):
    """An Event whose `wait` first blocks on a gate the test opens (bounded), so the test
    decides when an idle loop looks at its stop flag."""
    def __init__(self):
        super().__init__()
        self.gate = threading.Event()

    def wait(self, timeout=None):
        self.gate.wait(10)
        return super().wait(timeout)


class RecordingEvent(threading.Event):
    """Records the timeout of each wait: the sleep the loop chose after a read is what
    decides when it reads next, and an absence of reads proves nothing on a loaded host."""
    def __init__(self):
        super().__init__()
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        return super().wait(timeout)


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.secret_file = tempfile.NamedTemporaryFile("w", suffix=".secret", delete=False)
        self.secret_file.write("s3cr3t\n")
        self.secret_file.close()
        self.addCleanup(os.unlink, self.secret_file.name)
        self.addCleanup(_restore_globals, _snapshot_globals())   # `reload` applies every setting, not four
        config.LOW_QUOTA = {"threshold_percent": 30, "rule": "all", "accounts": [], "poll_seconds": 30}
        config.CLIPROXY = {"port": 8317, "url": "", "version": "8.0.23", "management_secret_file": self.secret_file.name}
        config.ROUTING = "auto"
        config.UPSTREAM = "http://user:pw@127.0.0.1:8317"       # the legacy form may carry credentials
        self.read = FakeRead()
        self.lines = []
        self.p = poller.Poller(read=self.read, log=self.lines.append)
        self.addCleanup(self.p.stop)

    def wait_for(self, cond, timeout=5.0):
        """Poll `cond` until true or `timeout`; the assertion that follows says what did not happen."""
        end = time.monotonic() + timeout
        while not cond() and time.monotonic() < end:
            time.sleep(0.02)

    def reload(self, **low_quota):
        """A reload as the core does it: new settings through `apply_settings`, which runs the hooks."""
        config.apply_settings(dict(config._settings, upstream=config.UPSTREAM, routing=config.ROUTING,
                                   cliproxy=config.CLIPROXY,
                                   low_quota=dict(config.LOW_QUOTA, **low_quota)), reload=True)

    def finished_reads(self):
        with self.p._cond:
            return self.p._finished_reads

    def passes(self):
        with self.p._cond:
            return self.p._passes

    def wait_for_pass_after(self, recorded):
        """Wait until the loop has finished a pass past `recorded`; an absence never moves
        the read counter, so a "no read" check rests on this one. A dead loop fails here."""
        self.wait_for(lambda: self.passes() > recorded)
        self.assertGreater(self.passes(), recorded, "the loop made no pass")

    def test_it_reads_at_start_with_the_secret_from_the_file_and_the_scope(self):
        self.p.start()
        self.assertTrue(self.read.started.wait(5))
        url, secret, kw = self.read.calls[0]
        self.assertEqual(url, "http://127.0.0.1:8317")           # config.UPSTREAM, credentials stripped
        self.assertEqual(secret, "s3cr3t")
        self.assertEqual(kw["accounts"], ())
        self.assertEqual(kw["cliproxy_version"], "8.0.23")
        self.assertEqual(kw["per_call_timeout"], 10.0)
        self.assertEqual(kw["deadline"], 15.0)
        for _ in range(50):
            if self.p.latest():
                break
            time.sleep(0.05)
        self.assertIsNotNone(self.p.latest())
        self.assertIsNone(self.p.status()["last_error"])

    def test_it_reads_under_relay_and_direct(self):
        for mode in ("relay", "direct"):
            with self.subTest(mode):
                config.ROUTING = mode
                read = FakeRead()
                p = poller.Poller(read=read, log=lambda line: None)
                p.start()
                try:
                    self.assertTrue(read.started.wait(5))
                    self.wait_for(lambda: p.latest() is not None)
                    self.assertIsNotNone(p.latest())
                    self.assertIsNone(p.status()["last_error"])
                finally:
                    p.stop()

    def test_a_raising_read_is_recorded_said_once_and_the_thread_lives_on(self):
        self.read.raise_once = reader.ManagementError("HTTP 401 on the credentials route")
        self.p.start()
        self.assertTrue(self.read.started.wait(5))
        for _ in range(50):
            if self.p.status()["last_error"]:
                break
            time.sleep(0.05)
        self.assertEqual(self.p.status()["last_error"], "HTTP 401 on the credentials route")
        self.assertIsNone(self.p.latest())
        self.wait_for(lambda: len(self.lines) >= 1)           # the line is written after the status, outside the lock
        self.assertEqual(len(self.lines), 1)
        self.assertIn("HTTP 401", self.lines[0])
        self.read.raise_once = reader.ManagementError("HTTP 401 on the credentials route")
        self.p.wake()                                        # the same error again: no new line
        self.wait_for(lambda: self.finished_reads() >= 2)
        self.assertEqual(len(self.lines), 1)
        self.p.wake()                                        # a good read: the recovery is said once
        self.wait_for(lambda: self.finished_reads() >= 3)
        self.assertGreaterEqual(len(self.read.calls), 3, "the thread died on the exception")
        self.assertIsNone(self.p.status()["last_error"])
        self.assertIsNotNone(self.p.latest())
        self.wait_for(lambda: len(self.lines) >= 2)
        self.assertEqual(len(self.lines), 2)
        self.assertIn("answering again", self.lines[1])

    def test_an_unexpected_exception_is_also_caught(self):
        self.read.raise_once = RuntimeError("bug")
        self.p.start()
        self.assertTrue(self.read.started.wait(5))
        for _ in range(50):
            if self.p.status()["last_error"]:
                break
            time.sleep(0.05)
        self.assertIn("RuntimeError", self.p.status()["last_error"])
        self.assertTrue(self.p.status()["running"])

    def test_a_reload_wakes_the_poller(self):
        self.p.start()                                       # start registers wake in config.ON_APPLY
        self.assertIn(self.p.wake, config.ON_APPLY)
        self.assertTrue(self.read.started.wait(5))
        self.wait_for_pass_after(0)                         # the first pass is over: the loop sleeps out its interval
        n = len(self.read.calls)
        recorded = self.passes()
        self.reload(accounts=["a1"])
        self.wait_for_pass_after(recorded)                  # woken, not waited out
        self.assertEqual(len(self.read.calls), n + 1)
        self.assertEqual(self.read.calls[-1][2]["accounts"], ("a1",))
        self.p.stop()
        self.assertNotIn(self.p.wake, config.ON_APPLY)      # stop unregisters it

    def test_a_refresh_under_relay_reads(self):
        config.ROUTING = "relay"
        self.p.start()
        self.wait_for_pass_after(0)
        n = len(self.read.calls)
        self.assertIsNotNone(self.p.refresh(timeout=5))
        self.assertEqual(len(self.read.calls), n + 1)

    def blocking_poller(self):
        """A poller whose read blocks until the test releases it (bounded, so nothing hangs)."""
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.cond = CountingCondition()
        calls = self.read.calls

        def blocked(*a, **kw):
            calls.append(a)
            self.read.started.set()
            self.release.wait(10)
            return {"read_at": time.time(), "accounts": {}}
        self.p = poller.Poller(read=blocked)
        self.p._cond = self.cond
        self.addCleanup(self.p.stop)

    def refresh_in_threads(self, count, **kw):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.p.refresh(**kw)), daemon=True)
                   for _ in range(count)]
        for th in threads:
            th.start()
        return threads, results

    def test_two_refreshes_during_one_read_share_that_read(self):
        self.blocking_poller()
        self.p.start()
        self.assertTrue(self.read.started.wait(5))
        threads, results = self.refresh_in_threads(2, timeout=10)
        self.wait_for(lambda: self.cond.waits >= 2)
        self.assertGreaterEqual(self.cond.waits, 2, "both refreshes should be waiting")
        self.assertFalse(self.p._wake.is_set(), "a refresh that joins a read in flight must not ask for another")
        self.release.set()
        for th in threads:
            th.join(10)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r is not None for r in results))
        self.wait_for_pass_after(0)
        self.assertEqual(len(self.read.calls), 1)

    def test_a_refresh_with_no_read_in_flight_starts_one(self):
        self.p.start()
        self.wait_for_pass_after(0)
        n = len(self.read.calls)
        self.assertIsNotNone(self.p.refresh(timeout=5))
        self.assertEqual(len(self.read.calls), n + 1)

    def test_refresh_returns_at_once_when_the_poller_was_never_started(self):
        t0 = time.monotonic()
        self.assertIsNone(self.p.refresh(timeout=15))
        self.assertLess(time.monotonic() - t0, 4.0)
        self.assertEqual(self.read.calls, [])

    def test_refresh_returns_when_the_thread_ends_while_it_waits(self):
        self.p._cond = self.cond = CountingCondition()
        self.p._wake = gated = GatedEvent()
        self.addCleanup(gated.gate.set)
        self.p.start()                                       # the gate is shut from the start
        self.wait_for_pass_after(0)                          # the first pass is over: the loop is held at the gate, short of its interval
        threads, results = self.refresh_in_threads(1, timeout=10)
        self.wait_for(lambda: self.cond.waits >= 1)
        self.assertGreaterEqual(self.cond.waits, 1, "the refresh should be waiting")
        self.p.stop()
        gated.gate.set()                                     # the loop leaves its wait, sees the stop flag and ends without a read
        threads[0].join(4)
        self.assertFalse(threads[0].is_alive(), "the refresh waited out its timeout")
        self.assertFalse(self.p.status()["running"])

    def test_refresh_answers_with_the_latest_when_its_wait_ends(self):
        def slow(*a, **kw):
            self.read.started.set()
            time.sleep(6)
            return {"read_at": time.time(), "accounts": {}}
        self.p = poller.Poller(read=slow)
        self.addCleanup(self.p.stop)
        self.p.start()
        self.assertTrue(self.read.started.wait(5))
        t0 = time.monotonic()
        self.assertIsNone(self.p.refresh(timeout=0.3))
        self.assertLess(time.monotonic() - t0, 4.0)   # under the read's 6 s, over a loaded host's scheduling

    def test_a_refused_secret_pauses_polling_until_a_wake(self):
        """About ten failed management logins earn the address a thirty-minute ban that
        refuses the right secret too: a 401 or a 403 holds the loop until a reload or a
        refresh, and any other failure keeps the interval."""
        held = "; polling paused until a reload or a refresh"
        for status, sleep in ((401, None), (403, None), (500, 30.0), (None, 30.0)):
            with self.subTest(status=status):
                read = FakeRead()
                read.raise_once = reader.ManagementError(f"HTTP {status} on the credentials route", status=status)
                p = poller.Poller(read=read, log=lambda line: None)
                p._wake = woke = RecordingEvent()
                p.start()
                try:
                    self.wait_for(lambda: woke.timeouts)
                    self.assertEqual(woke.timeouts[:1], [sleep])
                    self.assertEqual(len(read.calls), 1)
                    self.assertEqual(p.status()["last_error"],
                                     f"HTTP {status} on the credentials route" + (held if sleep is None else ""))
                    p.wake()                                 # what a reload or a refresh does
                    self.wait_for(lambda: len(woke.timeouts) >= 2)
                    self.assertEqual(len(read.calls), 2)
                    self.assertEqual(woke.timeouts[1], 30.0)   # a good read: the interval again
                    self.assertIsNone(p.status()["last_error"])
                finally:
                    p.stop()

    def test_an_empty_secret_path_is_said_and_calls_nothing(self):
        config.CLIPROXY = dict(config.CLIPROXY, management_secret_file="")
        self.p._wake = woke = RecordingEvent()
        self.p.start()
        self.wait_for(lambda: woke.timeouts)
        self.assertEqual(self.p.status()["last_error"], "no management secret file is set")
        self.assertEqual(self.read.calls, [])
        self.assertEqual(woke.timeouts[:1], [30.0])          # not held: no secret was refused

    def test_a_missing_secret_file_is_the_error(self):
        config.CLIPROXY = dict(config.CLIPROXY, management_secret_file="/nonexistent/secret")
        self.p.start()
        for _ in range(50):
            if self.p.status()["last_error"]:
                break
            time.sleep(0.05)
        self.assertIn("secret", self.p.status()["last_error"])
        self.assertEqual(self.read.calls, [])


class OnApplyTests(unittest.TestCase):
    def test_apply_settings_calls_every_hook_and_survives_one_that_raises(self):
        seen = []
        hooks = [lambda: 1 / 0, lambda: seen.append(1)]   # the raiser first: stopping at it loses the second
        config.ON_APPLY.extend(hooks)
        self.addCleanup(lambda: [config.ON_APPLY.remove(h) for h in hooks])   # only what this test added
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            config.apply_settings(dict(config._settings))
        self.assertEqual(seen, [1])
        self.assertRegex(said.getvalue(), r"\Asettings hook <.*> failed: ZeroDivisionError: division by zero\n\Z")


if __name__ == "__main__":
    unittest.main()
