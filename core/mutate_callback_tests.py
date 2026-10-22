"""Temporary, run by core.yml on Windows and then removed: each Windows callback test
red on a mutated listener. Exits 1 when a test passes on its mutation."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MUTANTS = [
    ("test_on_windows_no_other_socket_binds_the_waiting_address",
     "SO_REUSEADDR on Windows, the listener before #2",
     "oauth._CallbackServer.allow_reuse_address = True"),
    ("test_on_windows_the_next_login_binds_the_port_at_once_after_a_callback",
     "close() leaves the socket bound, the server kept alive",
     "held = []; oauth._CallbackServer.server_close = lambda self: held.append(self)"),
]
RUN = """
import sys, unittest
sys.path.insert(0, {here!r})
from lokki_core.quota import oauth
{mutation}
import test_quota_reader
suite = unittest.defaultTestLoader.loadTestsFromName("test_quota_reader.OAuthTests.{test}")
result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
"""


def main():
    survived = 0
    for test, what, mutation in MUTANTS:
        for label, code in (("unmutated", "pass"), ("mutant: " + what, mutation)):
            run = subprocess.run([sys.executable, "-c", RUN.format(here=HERE, mutation=code, test=test)],
                                 cwd=HERE, capture_output=True, text=True, timeout=120)
            lines = [l for l in run.stdout.splitlines() if l.startswith(("test_", "AssertionError", "lokki_core", "OSError"))
                     or "Error:" in l or l.startswith(("OK", "FAILED"))]
            print(f"mutate {test} [{label}]: exit {run.returncode}: " + " | ".join(lines))
            if (run.returncode == 0) != (label == "unmutated"):
                survived += 1
    print(f"mutate: {survived} unexpected outcome(s)")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
