"""Run the core suite and fail on anything a test did not ask for.

Discovery is the one the CI runs. unittest's own report goes to a saved copy of
stderr; fds 1 and 2 go to files, so threads and subprocesses that write to them
directly are caught too. Any captured byte (a ResourceWarning included) or any
file left in the run's private TMPDIR fails the run, and so does a run that
finds no tests, as the bare discover command does (exit status 5).

Both private directories are made in the same place, side by side, before TMPDIR
moves, so the capture is never an entry of the directory it checks. They are
removed once the check is done.
"""
import gc
import os
import shutil
import sys
import tempfile
import unittest
import warnings


def main():
    base = os.environ.get("RUNNER_TEMP")
    tmp = tempfile.mkdtemp(prefix="suite-tmp-", dir=base)
    out = tempfile.mkdtemp(prefix="suite-out-", dir=base)
    for name in ("TMPDIR", "TMP", "TEMP"):
        os.environ[name] = tmp
    tempfile.tempdir = None
    warnings.simplefilter("always")

    report = os.fdopen(os.dup(2), "w", buffering=1)
    captured = {}
    for fd, name in ((1, "stdout"), (2, "stderr")):
        path = captured[name] = os.path.join(out, name)
        with open(path, "wb") as f:
            os.dup2(f.fileno(), fd)

    runner = unittest.TextTestRunner(stream=report, warnings="always")
    program = unittest.main(
        module=None,
        argv=["unittest", "discover", "-s", ".", "-p", "test_*.py"],
        testRunner=runner,
        exit=False,
    )
    gc.collect()
    sys.stdout.flush()
    sys.stderr.flush()

    problems = []
    for name, path in captured.items():
        with open(path, "rb") as f:
            data = f.read()
        if data:
            problems.append("the suite wrote %d bytes to %s:\n%s" % (len(data), name, data.decode("utf-8", "replace")))
    left = sorted(os.path.relpath(os.path.join(d, n), tmp) for d, dirs, files in os.walk(tmp) for n in dirs + files)
    if left:
        problems.append("the suite left %d entries in TMPDIR:\n%s" % (len(left), "\n".join(left)))

    for p in problems:
        print("NOISE: " + p, file=report)
    ran = program.result.testsRun
    if not ran:
        print("NO TESTS RAN: discovery in %s found no tests" % os.getcwd(), file=report)
    report.close()
    for d in (tmp, out):
        shutil.rmtree(d, ignore_errors=True)
    if not ran:
        return 5
    return 0 if program.result.wasSuccessful() and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
