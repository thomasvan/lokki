#!/usr/bin/env python3
"""The PyInstaller command line the core sidecar is built with, and the entry it builds from."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import build_core  # noqa: E402
import sidecars  # noqa: E402


def _values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


class BuildCoreTests(unittest.TestCase):
    def test_pyinstaller_args_carry_the_paths_the_data_and_the_name(self):
        triple = "x86_64-unknown-linux-gnu"
        core_dir, out, rev = "core", os.path.abspath("dist-out"), os.path.abspath("rev-tmp")
        for sep in (":", ";"):  # --add-data joins with os.pathsep, which is ';' on Windows
            with self.subTest(pathsep=sep), mock.patch.object(os, "pathsep", sep):
                argv = build_core.args(core_dir, "1.2.3", triple, out, rev)
                core = os.path.abspath(core_dir)
                self.assertIn("--onefile", argv)
                self.assertEqual(_values(argv, "--name"), [f"{sidecars.core_name('1.2.3')}-{triple}"])
                self.assertEqual(_values(argv, "--paths"), [core])
                self.assertEqual(_values(argv, "--add-data"), [
                    os.path.join(core, "lokki_core", "static") + sep + "lokki_core/static",
                    os.path.join(core, "VERSION") + sep + ".",
                    os.path.join(rev, "REVISION") + sep + "."])
                self.assertEqual(_values(argv, "--hidden-import"), ["yaml"])
                self.assertEqual(_values(argv, "--distpath"), [out])
                self.assertEqual(os.path.basename(argv[-1]), "core_entry.py")

    def test_the_entry_imports_without_running(self):
        sys.path.insert(0, os.path.join(REPO, "core"))
        self.addCleanup(sys.path.remove, os.path.join(REPO, "core"))
        import core_entry  # a SystemExit here fails the test
        from lokki_core.__main__ import main
        self.assertIs(core_entry.main, main)


if __name__ == "__main__":
    unittest.main()
