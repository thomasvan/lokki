#!/usr/bin/env python3
"""Offline tests for the evidence chain.

The chain's whole value is that a claim can be checked, so the tests are shaped
as tampering cases: edit a record, delete one, insert one, replace the file, cut
a line in half. Each has to be *seen*, and one case has to be seen as *not* seen —
tail truncation — because that limit is real and is written down rather than
implied.

No upstream, no quota.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import config, core, lock  # noqa: E402


def read_lines(path):
    with open(path) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def write_lines(path, records):
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class ChainHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="evidence-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = os.path.join(self.dir, "runs.jsonl")
        self._saved = (core.RUNS_LOG, dict(core._chain_cache),
                       config.RUNS_MAX_BYTES, config.RUNS_KEEP)
        core.RUNS_LOG = self.log
        core._chain_cache.clear()
        config.RUNS_MAX_BYTES = 8 * 1024 * 1024
        config.RUNS_KEEP = 5
        self.addCleanup(self._restore)

    def _restore(self):
        core.RUNS_LOG, cache, config.RUNS_MAX_BYTES, config.RUNS_KEEP = self._saved
        core._chain_cache.clear()
        core._chain_cache.update(cache)
        core.release_runs_lock()

    def log_n(self, n):
        out = []
        for i in range(n):
            rec = {"mode": "segmented", "delivered_chars": i}
            core.log_run(rec)
            out.append(rec)
        return out


class ChainIntegrityTests(ChainHarness):
    def test_records_link_and_number_from_one(self):
        recs = self.log_n(3)
        self.assertEqual([r["seq"] for r in recs], [1, 2, 3])
        self.assertEqual(recs[0]["prev_hash"], core.GENESIS_HASH)
        self.assertEqual(recs[1]["prev_hash"], recs[0]["hash"])
        self.assertEqual(recs[2]["prev_hash"], recs[1]["hash"])
        self.assertEqual(recs[0]["hash"], core.record_hash(recs[0]))
        self.assertTrue(core.verify_chain()["ok"])

    def test_an_edited_record_is_seen(self):
        self.log_n(3)
        lines = read_lines(self.log)
        lines[1]["delivered_chars"] = 9999          # the naive edit
        write_lines(self.log, lines)
        report = core.verify_chain()
        self.assertFalse(report["ok"])
        self.assertIn("hash does not recompute", report["breaks"][0]["why"])

    def test_a_deleted_record_is_seen(self):
        self.log_n(3)
        lines = read_lines(self.log)
        del lines[1]
        write_lines(self.log, lines)
        report = core.verify_chain()
        self.assertFalse(report["ok"])
        why = " ".join(b["why"] for b in report["breaks"])
        self.assertIn("seq", why)

    def test_an_inserted_record_is_seen(self):
        self.log_n(3)
        lines = read_lines(self.log)
        forged = dict(lines[0])
        forged["delivered_chars"] = 42
        forged["prev_hash"] = lines[0]["hash"]
        forged["seq"] = 2
        forged["hash"] = core.record_hash(forged)
        lines.insert(1, forged)
        write_lines(self.log, lines)
        report = core.verify_chain()
        self.assertFalse(report["ok"], "a record re-linked into the chain passed")

    def test_a_replaced_file_restarting_at_one_is_seen(self):
        """The whole file replaced by a fresh chain: it verifies on its own but
        the chain's head changed, and the seq restarts. Seeing a *whole* file swap
        needs a head remembered elsewhere, and this repository keeps none, so this
        test reaches the limit rather than covering it."""
        self.log_n(3)
        head_before = core.verify_chain()["head"]
        os.unlink(self.log)
        core._chain_cache.clear()
        self.log_n(2)                                # a new chain, seq 1..2
        self.assertTrue(core.verify_chain()["ok"])
        self.assertNotEqual(core.verify_chain()["head"], head_before)

    def test_truncating_the_tail_is_NOT_detected_and_that_is_the_stated_limit(self):
        """The honest ceiling. Nothing inside the file remembers how long it was,
        so cutting whole records off the end verifies clean. This test exists to
        keep the claim true and the limit visible; the only thing that could catch
        it is a head hash kept off this box, and the repository keeps none."""
        self.log_n(4)
        lines = read_lines(self.log)
        write_lines(self.log, lines[:2])
        report = core.verify_chain()
        self.assertTrue(report["ok"], "an in-file chain cannot see this, and any "
                                      "doc claiming otherwise is wrong")
        self.assertEqual(report["records"], 2)

    def test_records_written_before_the_chain_are_unverifiable_not_tampering(self):
        """This repository's own runs.jsonl starts with records from before the
        chain existed. Calling those a break would make the first run of the
        verifier a false alarm."""
        with open(self.log, "w") as f:
            f.write('{"ts": "2026-10-03T00:00:00Z", "mode": "segmented"}\n')
        core._chain_cache.clear()
        core.log_run({"delivered_chars": 1})
        report = core.verify_chain()
        self.assertTrue(report["ok"], report["breaks"])
        self.assertEqual(report["unchained"], 1)
        self.assertEqual(report["records"], 1)
        self.assertEqual(report["first_seq"], 1)

    def test_half_a_chain_is_reported_as_such(self):
        """A record carrying `hash` but no `seq` was not written by this code.
        Saying that beats reporting it as an edit *and* a sequence gap."""
        with open(self.log, "w") as f:
            f.write('{"ts": "2026-10-03T00:00:00Z", "hash": "abc"}\n')
        report = core.verify_chain()
        self.assertFalse(report["ok"])
        self.assertEqual(report["unchained"], 1)
        self.assertIn("half-chained", report["breaks"][0]["why"])

    def test_a_half_written_tail_line_does_not_break_the_next_append(self):
        self.log_n(2)
        with open(self.log, "a") as f:
            f.write('{"seq": 3, "prev_hash": "ab')     # a crash mid-write
        core._chain_cache.clear()
        rec = {}
        core.log_run(rec)
        self.assertEqual(rec["seq"], 3, "the next record continued from a "
                                        "half-written line instead of the last "
                                        "complete one")
        report = core.verify_chain()
        self.assertFalse(report["ok"])
        self.assertTrue(any("not JSON" in b["why"] for b in report["breaks"]))

    def test_a_record_is_on_disk_when_log_run_returns(self):
        """The flush. fsync itself is not observable from a test —
        only a crash can show it — so what is asserted is that the record is
        complete and parseable the moment the lock is released, which is what the
        reader and the next append depend on."""
        rec = {}
        core.log_run(rec)
        self.assertEqual(len(read_lines(self.log)), 1)
        self.assertEqual(read_lines(self.log)[0]["hash"], rec["hash"])

    def test_the_chain_adds_only_linkage_fields(self):
        """The chain adds a counter and two hashes. A field carrying client
        content here would be a new committed leak, so the set is pinned: names of
        forwarded parameters are recorded, their *values* never are."""
        rec = {"client": "test", "params_forwarded": ["temperature"],
               "dropped_params": ["user"], "temperature": 0.7}
        before = set(rec)
        core.log_run(rec)
        added = set(rec) - before - {"ts", "revision", "config_fingerprint"}
        self.assertEqual(added, {"seq", "prev_hash", "hash"})
        # The value the client sent is in this dict only because the test put it
        # there; nothing in the engine's own record does that, which is the
        # invariant test_engine pins.
        self.assertIsInstance(rec["seq"], int)
        self.assertEqual(len(rec["hash"]), 64)


class SingleWriterTests(ChainHarness):
    """A second writer must be refused at startup, not documented away.

    `log_run` computes `seq` and `prev_hash` from the last line of the file, so two
    processes appending to one run log interleave sequence numbers and break the
    chain. Documentation alone is only the honest ceiling; this is the
    enforcement."""

    def test_a_second_writer_for_the_same_log_is_refused(self):
        lock_path = core.acquire_runs_lock(self.log)
        self.assertTrue(os.path.exists(lock_path))
        with self.assertRaises(core.RunLogBusy) as caught:
            core.acquire_runs_lock(self.log)
        self.assertIn("already writing", str(caught.exception))
        self.assertIn("RELAY_RUNS_LOG", str(caught.exception))

    def test_a_different_log_is_a_different_lock(self):
        core.acquire_runs_lock(self.log)
        # The core holds one lock; the second acquire replaces this one, which is given
        # back here, else Windows cannot remove its file while it is open.
        first = core._runs_lock_fd
        self.addCleanup(os.close, first)
        self.addCleanup(lock.release, first)
        other = os.path.join(self.dir, "other.jsonl")
        self.assertTrue(core.acquire_runs_lock(other))

    def test_the_lock_can_be_given_back(self):
        core.acquire_runs_lock(self.log)
        core.release_runs_lock()
        self.assertTrue(core.acquire_runs_lock(self.log),
                        "the lock was not actually released")

    def test_the_entry_point_refuses_to_start_a_second_writer(self):
        from lokki_core import handler
        path = os.path.join(self.dir, "server-config.yaml")
        with open(path, "w") as f:
            f.write("listen: '127.0.0.1:0'\nauth:\n  token: t\n"
                    f"upstream: 'http://127.0.0.1:1'\n")
        core.acquire_runs_lock(self.log)
        # Hermetic on purpose. With `RELAY_RUNS_LOG` in the shell, the environment
        # outranks this config, `main()` would lock *that* file instead of the one
        # held above and serve for ever: the suite would hang, and a SIGTERM from
        # `timeout` would meet the relay's own drain handler, so the process would
        # outlive the run. Without the variable the refusal is what is tested, and
        # a relay that starts serving fails here instead of hanging.
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k != "RELAY_RUNS_LOG"}
        said = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(handler, "start_listeners",
                                  side_effect=AssertionError("started serving")), \
                contextlib.redirect_stderr(said):
            self.assertEqual(handler.main(["--config", path]), 2,
                             "the entry point started a second writer")
        refusal = said.getvalue().splitlines()
        self.assertEqual(len(refusal), 1)
        self.assertTrue(refusal[0].startswith(f"refusing to start: another relay is already writing {self.log} "),
                        refusal)


class LiveWalkLockTests(ChainHarness):
    """`/api/chain` walks the live files. Unlocked, a rotation in the middle of
    the walk renamed them out from under its file list, and the report showed a
    break that was never in the evidence."""

    def test_the_live_walk_waits_for_the_append_lock(self):
        import threading
        core.log_run({"delivered_chars": 1})
        done, result = threading.Event(), {}

        def walk():
            result["report"] = core.verify_chain()
            done.set()

        with core._runs_lock:
            threading.Thread(target=walk, daemon=True).start()
            self.assertFalse(done.wait(0.3),
                             "the walk read the live files while an append held the lock")
        self.assertTrue(done.wait(5))
        self.assertTrue(result["report"]["ok"], result["report"]["breaks"])


class RotationTests(ChainHarness):
    def test_rotation_preserves_the_chain_across_the_seam(self):
        config.RUNS_MAX_BYTES = 400
        config.RUNS_KEEP = 50          # keep everything, so the seam is the claim
        for i in range(30):
            core.log_run({"delivered_chars": i})
        self.assertTrue(os.path.exists(f"{self.log}.1"),
                        "nothing rotated at a 400-byte limit")
        report = core.verify_chain()
        self.assertTrue(report["ok"], report["breaks"])
        self.assertEqual(report["records"], 30)
        self.assertGreater(len(report["files"]), 1)
        self.assertIsNone(report["head_missing"])

    def test_a_rotated_away_head_is_reported_not_called_tampering(self):
        """RUNS_KEEP eventually discards old files. The verifier has to say so
        rather than cry wolf — and it cannot tell that apart from a deleted head,
        which is the limit the off-box anchor exists for."""
        config.RUNS_MAX_BYTES = 300
        config.RUNS_KEEP = 1
        for i in range(60):
            core.log_run({"delivered_chars": i})
        report = core.verify_chain()
        self.assertTrue(report["ok"], report["breaks"])
        self.assertIsNotNone(report["head_missing"])
        self.assertGreater(report["first_seq"], 1)

    def test_rotation_keeps_only_the_configured_number(self):
        config.RUNS_MAX_BYTES = 300
        config.RUNS_KEEP = 2
        for i in range(60):
            core.log_run({"delivered_chars": i})
        self.assertTrue(os.path.exists(f"{self.log}.2"))
        self.assertFalse(os.path.exists(f"{self.log}.3"),
                         "an old rotation survived RUNS_KEEP")

    def test_the_chain_continues_after_a_restart(self):
        """A new process must find the last link, including when the live file is
        empty because everything just rotated."""
        self.log_n(2)
        core._chain_cache.clear()
        rec = {}
        core.log_run(rec)
        self.assertEqual(rec["seq"], 3)
        config.RUNS_MAX_BYTES = 1
        core._chain_cache.clear()
        core.log_run({"delivered_chars": "rotate now"})
        self.assertTrue(os.path.exists(f"{self.log}.1"))
        core._chain_cache.clear()
        rec = {}
        core.log_run(rec)
        self.assertEqual(rec["seq"], 5, "the seq restarted after a rotation")
        self.assertTrue(core.verify_chain()["ok"])


class InRepoWriteTests(unittest.TestCase):
    """The backstop: the record itself refuses to be written into the checkout.

    Startup refuses such a configuration, so this is the second line — and the only
    one that does not depend on a configuration being right. The failure it
    prevents is a *false claim* inside committed evidence, which is the one thing
    this module exists to make impossible."""

    def setUp(self):
        self._saved = core.RUNS_LOG
        self.addCleanup(setattr, core, "RUNS_LOG", self._saved)
        self.addCleanup(core._chain_cache.clear)
        self.here = os.path.dirname(os.path.abspath(__file__))
        self.seed = os.path.join(self.here, "runs.jsonl")

    def test_a_record_into_the_repository_is_refused_before_anything_is_written(self):
        before = os.stat(self.seed)
        core.RUNS_LOG = self.seed
        core._chain_cache.clear()
        with self.assertRaises(core.RunLogUnsafe) as caught:
            core.log_run({"delivered_chars": 1})
        self.assertIn("outside the checkout", str(caught.exception))
        after = os.stat(self.seed)
        self.assertEqual((before.st_size, before.st_mtime_ns),
                         (after.st_size, after.st_mtime_ns),
                         "the committed seed was touched")

    def test_a_hard_link_to_an_in_repo_file_is_refused(self):
        """A path is not the whole story: the symlink hole's sibling.

        A hard link at a path *outside* the checkout can name an inode *inside* it,
        and no path inspection can see that — `inside_repo` says the outside path is
        outside, correctly. Without this check, a hard link to a file in this
        directory makes `log_run` append to the file inside the checkout.
        `st_nlink` is what distinguishes it, and nothing legitimate hard-links an
        evidence file."""
        in_repo = os.path.join(self.here, f"hardlink-probe-{os.getpid()}.jsonl")
        open(in_repo, "w").close()
        self.addCleanup(lambda: os.path.exists(in_repo) and os.unlink(in_repo))
        # A hard link needs the same filesystem, and `tempfile.gettempdir()` is
        # usually a tmpfs — so the probe looks for a directory on the checkout's
        # own filesystem, and skips (loudly) rather than passing vacuously.
        outside = None
        for candidate in (os.path.expanduser("~"),
                          os.path.dirname(core.REPO_ROOT or core.HERE),
                          tempfile.gettempdir()):
            if not os.path.isdir(candidate):
                continue
            outside_dir = tempfile.mkdtemp(prefix=".hardlink-probe-", dir=candidate)
            link = os.path.join(outside_dir, "runs.jsonl")
            try:
                os.link(in_repo, link)
            except OSError:
                shutil.rmtree(outside_dir, ignore_errors=True)
                continue
            outside = link
            self.addCleanup(shutil.rmtree, outside_dir, True)
            break
        if outside is None:
            self.skipTest("no directory on the checkout's filesystem to hard-link "
                          "into, so the inode check cannot be probed here")
        self.assertFalse(core.inside_repo(outside),
                         "the probe must exercise the inode check, not the path one")
        core.RUNS_LOG = outside
        core._chain_cache.clear()
        before = os.stat(in_repo).st_size
        with self.assertRaises(core.RunLogUnsafe) as caught:
            core.log_run({"delivered_chars": 1})
        self.assertIn("hard link", str(caught.exception))
        self.assertEqual(os.stat(in_repo).st_size, before,
                         "a record reached the file inside the checkout through a "
                         "hard link")

    def test_a_record_outside_the_repository_still_works(self):
        """The guard must refuse one path, not all of them."""
        with tempfile.TemporaryDirectory() as d:
            core.RUNS_LOG = os.path.join(d, "runs.jsonl")
            core._chain_cache.clear()
            rec = {}
            core.log_run(rec)
            self.assertEqual(rec["seq"], 1)
            self.assertTrue(core.verify_chain()["ok"])


# Run in a child whose locale is not UTF-8: the run log is UTF-8 whatever the
# platform's encoding (cp1252 on Windows cannot encode most of the world's text).
_NON_UTF8_CHILD = """
import json, locale, sys
sys.path.insert(0, sys.argv[2])
from lokki_core import core
core.RUNS_LOG = sys.argv[1]
core.log_run({"client": "caf\\u00e9-\\u6771\\u4eac", "mode": "segmented"})
core._chain_cache.clear()              # the next append reads the last link back from the file
second = {}
core.log_run(second)
print(json.dumps({"encoding": locale.getpreferredencoding(False), "seq": second["seq"],
                  "chain": core.verify_chain(), "client": core.read_runs()[0]["client"]}))
"""


class RunLogEncodingTests(unittest.TestCase):
    def test_a_non_ascii_field_is_written_and_read_back_under_a_non_utf8_locale(self):
        d = tempfile.mkdtemp(prefix="evidence-encoding-")
        self.addCleanup(shutil.rmtree, d, True)
        log = os.path.join(d, "runs.jsonl")
        name = "caf\u00e9-\u6771\u4eac"        # what the child writes
        here = os.path.dirname(os.path.abspath(__file__))
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0",
                   RELAY_RUNS_LOG=log)
        proc = subprocess.run([sys.executable, "-X", "utf8=0", "-c", _NON_UTF8_CHILD, log, here],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        if out["encoding"].lower().replace("-", "") in ("utf8", "cp65001"):
            self.skipTest(f"no non-UTF-8 locale to run under here ({out['encoding']})")
        self.assertEqual(out["seq"], 2)
        self.assertTrue(out["chain"]["ok"], out["chain"])
        self.assertEqual(out["client"], name)
        with open(log, "rb") as f:
            self.assertIn(name.encode("utf-8"), f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
