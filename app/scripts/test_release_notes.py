import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import release_notes as rn  # noqa: E402


class NotesTests(unittest.TestCase):
    def test_notes_list_the_subjects_and_name_a_cliproxy_change(self):
        # (label, subjects, lock_before, lock_after, body, expected)
        rows = [
            ("subjects only, lock unchanged", ["app: a", "core: b"], {"version": "8.0.23"}, {"version": "8.0.23"}, "ignored: the lock did not change\n",
             "## Changes\n\n- app: a\n- core: b\n"),
            ("lock changed: the versions and the upstream body", ["app: bump"], {"version": "8.0.23"}, {"version": "8.1.0"},
             "fixed things\n", "## Changes\n\n- app: bump\n\n## CLIProxyAPI 8.0.23 -> 8.1.0\n\nfixed things\n"),
            # Before the first tag every commit is "since the previous tag", most of it history no user reads.
            ("first release: no previous lock, no subjects", ["core: first"], None, {"version": "8.0.23"}, "",
             "## Changes\n\nThe first release of lokki.\n\n## CLIProxyAPI 8.0.23\n"),
            ("no subjects", [], {"version": "8.0.23"}, {"version": "8.0.23"}, "", "## Changes\n\n(no commits since the previous tag)\n"),
        ]
        for label, subjects, before, after, body, expected in rows:
            with self.subTest(label):
                self.assertEqual(rn.notes(subjects, before, after, body), expected)
