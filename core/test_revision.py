#!/usr/bin/env python3
"""Offline tests for `core.revision()`: what a record and `/version` say the code is.

A container image has no checkout and no `git`, so a build writes the revision down
(`RELAY_REVISION`, or the file `REVISION` beside `server.py`); a checkout still answers from its
own `HEAD`. Nothing here sends a request.

Run:  python3 core/test_revision.py
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lokki_core import core  # noqa: E402

SHA = "94712a7899c9fecef386f0ae18f362cda505f67c"
OTHER = "0123456789abcdef0123456789abcdef01234567"


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self._saved = (core._revision_cache, core.HERE, os.environ.get("RELAY_REVISION"))
        os.environ.pop("RELAY_REVISION", None)
        core._revision_cache = None
        self.here = tempfile.mkdtemp()
        core.HERE = self.here
        self.addCleanup(self.restore)

    def restore(self):
        core._revision_cache, core.HERE, env = self._saved
        if env is None:
            os.environ.pop("RELAY_REVISION", None)
        else:
            os.environ["RELAY_REVISION"] = env
        for name in os.listdir(self.here):
            os.unlink(os.path.join(self.here, name))
        os.rmdir(self.here)

    def write_file(self, text):
        with open(os.path.join(self.here, "REVISION"), "w") as f:
            f.write(text)

    def no_git(self):
        return mock.patch("subprocess.check_output", side_effect=FileNotFoundError("git"))

    def test_the_environment_names_it(self):
        os.environ["RELAY_REVISION"] = SHA
        with self.no_git():
            self.assertEqual(core.revision(), SHA)

    def test_the_file_names_it_when_the_environment_does_not(self):
        self.write_file(SHA + "\n")
        with self.no_git():
            self.assertEqual(core.revision(), SHA)

    def test_the_environment_wins_over_the_file(self):
        os.environ["RELAY_REVISION"] = SHA
        self.write_file(OTHER)
        with self.no_git():
            self.assertEqual(core.revision(), SHA)

    def test_what_is_not_a_commit_id_is_not_taken_for_one(self):
        for bad in ("latest", "not a sha", SHA + "\nx", "xyz1234", "abc", "G" * 40, SHA + "0" * 40):
            with self.subTest(bad=bad):
                core._revision_cache = None
                os.environ["RELAY_REVISION"] = bad
                with self.no_git():
                    self.assertEqual(core.revision(), "unknown")

    def test_a_bad_environment_value_falls_to_the_file(self):
        os.environ["RELAY_REVISION"] = "latest"
        self.write_file(SHA)
        with self.no_git():
            self.assertEqual(core.revision(), SHA)

    def test_a_bad_file_is_ignored(self):
        self.write_file("<<<<<<< HEAD")
        with self.no_git():
            self.assertEqual(core.revision(), "unknown")

    def test_a_file_that_is_not_text_is_ignored_and_does_not_raise(self):
        with open(os.path.join(self.here, "REVISION"), "wb") as f:
            f.write(b"\xff\xfe\x00")
        with self.no_git():
            self.assertEqual(core.revision(), "unknown")

    def test_the_longest_id_is_taken_and_one_digit_more_is_not(self):
        for text, want in (("a" * 64, "a" * 64), ("a" * 65, "unknown"), ("a" * 7, "a" * 7), ("a" * 6, "unknown")):
            with self.subTest(length=len(text)):
                core._revision_cache = None
                os.environ["RELAY_REVISION"] = text
                with self.no_git():
                    self.assertEqual(core.revision(), want)

    def test_without_either_the_checkout_answers(self):
        with mock.patch("subprocess.check_output", return_value=OTHER + "\n") as run:
            self.assertEqual(core.revision(), OTHER)
        self.assertEqual(run.call_args.args[0][:3], ["git", "-C", self.here])

    def test_without_anything_it_is_unknown(self):
        with self.no_git():
            self.assertEqual(core.revision(), "unknown")

    def test_it_is_cached(self):
        os.environ["RELAY_REVISION"] = SHA
        self.assertEqual(core.revision(), SHA)
        os.environ["RELAY_REVISION"] = OTHER
        self.assertEqual(core.revision(), SHA)


class RepoGuardTests(unittest.TestCase):
    """The in-repository guard has a root only in a checkout. The runtime image has none (the guard is off
    there), so nothing about its WORKDIR matters to the guard. The test below pins the two `COPY` lines the
    image build depends on: the entry point and the package it imports."""

    DOCKERFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy", "relay", "Dockerfile")

    def test_the_dockerfile_copies_the_entry_point_and_the_package(self):
        if not os.path.exists(self.DOCKERFILE):
            self.skipTest("deploy/relay/Dockerfile is not in this tree")
        with open(self.DOCKERFILE) as f:
            text = f.read()
        runtime = text[text.index("AS runtime"):]
        self.assertIn("COPY core/server.py ./server.py", runtime)
        self.assertIn("COPY core/lokki_core ./lokki_core", runtime)

    def test_a_state_directory_outside_the_code_tree_is_accepted(self):
        saved = core.REPO_ROOT
        try:
            core.REPO_ROOT = "/app"
            self.assertFalse(core.inside_repo("/home/someone/.local/state/cliproxy-8381/runs.jsonl"))
            self.assertTrue(core.inside_repo("/app/core/runs.jsonl"))
            core.REPO_ROOT = "/"
            self.assertTrue(core.inside_repo("/home/someone/.local/state/cliproxy-8381/runs.jsonl"))
        finally:
            core.REPO_ROOT = saved

    def test_without_a_git_marker_and_without_the_variable_there_is_no_root(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(core._repo_root(os.path.join(d, "core"), env={}))
        saved = core.REPO_ROOT
        try:
            core.REPO_ROOT = None
            self.assertFalse(core.inside_repo("/app/core/runs.jsonl"))
            self.assertFalse(core.inside_repo(os.path.join(os.path.dirname(core.__file__), "runs.jsonl")))
        finally:
            core.REPO_ROOT = saved

    def test_a_git_file_marks_a_worktree_as_a_root(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".git"), "w") as f:
                f.write("gitdir: /elsewhere\n")
            self.assertEqual(core._repo_root(os.path.join(d, "core"), env={}), d)

    def test_the_variable_names_a_root_where_git_is_absent_and_not_where_it_is_present(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(core._repo_root(os.path.join(d, "core"), env={"RELAY_REPO_ROOT": d}), d)
            os.mkdir(os.path.join(d, ".git"))
            self.assertEqual(core._repo_root(os.path.join(d, "core"), env={"RELAY_REPO_ROOT": "/elsewhere"}), d)

    @unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".git"))
                         or os.environ.get("RELAY_REPO_ROOT"), "no .git here (an image test stage sets RELAY_REPO_ROOT)")
    def test_this_checkout_has_a_root(self):
        self.assertIsNotNone(core.REPO_ROOT)

    @unittest.skipUnless(os.name == "nt", "drive letters exist on Windows only")
    def test_a_path_on_another_drive_is_outside(self):
        saved = core.REPO_ROOT
        try:
            core.REPO_ROOT = "C:\\src\\lokki"
            self.assertFalse(core.inside_repo("D:\\state\\runs.jsonl"))
        finally:
            core.REPO_ROOT = saved


if __name__ == "__main__":
    unittest.main()
