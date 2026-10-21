#!/usr/bin/env python3
"""The sidecar naming rule has one home, sidecars.py; its inputs have theirs,
core/VERSION and the CLIProxyAPI lock."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import sidecars  # noqa: E402


def _run(*args):
    out = subprocess.run([sys.executable, os.path.join(HERE, "sidecars.py"), *args],
                         capture_output=True, text=True, check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    return out.stdout


class SidecarsTests(unittest.TestCase):
    def test_names_carry_the_versions_with_dots_as_underscores(self):
        # The versions come from read_versions(), never a literal: core/VERSION and the lock are their homes.
        self.assertEqual(sidecars.token("1.2.3"), "1_2_3")
        core, cliproxy = sidecars.read_versions(REPO)
        with open(os.path.join(REPO, "core", "VERSION"), encoding="utf-8") as f:
            self.assertEqual(core, f.read().strip())
        with open(os.path.join(REPO, "app", "cliproxy.lock"), encoding="utf-8") as f:
            self.assertEqual(cliproxy, json.load(f)["version"])
        names = {"core": f"lokki-core-{core.replace('.', '_')}",
                 "cliproxy": f"cliproxy-{cliproxy.replace('.', '_')}"}
        self.assertEqual(sidecars.names(core, cliproxy), names)
        # build.rs reads these two outputs, so the commands print what names() and read_versions() return.
        self.assertEqual(json.loads(_run("--names")), names)
        self.assertEqual(json.loads(_run("--versions")), {"core": core, "cliproxy": cliproxy})

    def test_config_json_names_the_version_and_both_external_bins(self):
        core, cliproxy = sidecars.read_versions(REPO)
        names = sidecars.names(core, cliproxy)
        bundle = {"externalBin": [f"binaries/{names['core']}", f"binaries/{names['cliproxy']}"]}
        # Only --release asks for signed updater artifacts: without the signing key `tauri build` fails on them.
        rows = [([], {"version": core, "bundle": bundle}),
                (["--release"], {"version": core, "bundle": {**bundle, "createUpdaterArtifacts": True}})]
        for flags, want in rows:
            with self.subTest(flags=flags), tempfile.TemporaryDirectory(prefix="lokki-sidecars-") as tmp:
                out = os.path.join(tmp, "tauri.json")
                _run("--config-json", *flags, "--out", out)
                with open(out, encoding="utf-8") as f:
                    self.assertEqual(json.load(f), want)
        # --release changes only the overlay; on another command it would be silently ignored.
        refused = subprocess.run([sys.executable, os.path.join(HERE, "sidecars.py"), "--names", "--release"],
                                 capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--release", refused.stderr)


if __name__ == "__main__":
    unittest.main()
