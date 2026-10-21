#!/usr/bin/env python3
"""The run-log lock on either OS family: one holder, a second acquire refused,
release frees it."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import lock  # noqa: E402

PROBE = ("import os, sys; sys.path.insert(0, sys.argv[1]); from lokki_core import lock; "
         "fd = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT, 0o600); print(lock.acquire(fd))")


class LockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lock-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "runs.jsonl.lock")
        self.here = os.path.dirname(os.path.abspath(__file__))

    def probe(self):
        out = subprocess.run([sys.executable, "-c", PROBE, self.here, self.path],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip(), out.stderr

    def test_a_second_process_is_refused_while_the_first_holds_it(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.assertTrue(lock.acquire(fd))
        self.assertEqual(self.probe()[0], "False", self.probe()[1])
        lock.release(fd)
        os.close(fd)
        self.assertEqual(self.probe()[0], "True")


if __name__ == "__main__":
    unittest.main()
