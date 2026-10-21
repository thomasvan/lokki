import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import release_manifest as rm  # noqa: E402
import sidecars  # noqa: E402

SIG = base64.b64encode(b"untrusted comment: signature from tauri secret key\nRWQabc\ntrusted comment: timestamp:1\tfile:x\tversion:0.2.0\nZZZ\n").decode()
NAMES = ["lokki_0.2.0_amd64.AppImage", "lokki_0.2.0_amd64.deb", "lokki_0.2.0_x64-setup.exe", "lokki_0.2.0_x64_en-US.msi",
         "lokki_0.2.0_aarch64.app.tar.gz", "lokki_0.2.0_x64.app.tar.gz", "lokki_0.2.0_aarch64.dmg", "lokki_0.2.0_x64.dmg"]
ALL = {key for key, _ in rm.PLATFORMS.values()}


def sigs_for(*names):
    return {f"{n}.sig": SIG + "\n" for n in names}


def minisign_pubkey(key_id=b"\x01" * 8, key=b"\x02" * 32):
    line = base64.b64encode(b"Ed" + key_id + key).decode()
    return base64.b64encode(f"untrusted comment: minisign public key\n{line}\n".encode()).decode()


class ManifestTests(unittest.TestCase):
    def test_the_manifest_names_each_required_platform_with_its_signature_and_url(self):
        out = rm.manifest("0.2.0", "v0.2.0", "thomasvan/lokki", NAMES, sigs_for(*NAMES), ALL, "2026-10-14T00:00:00Z", "notes")
        self.assertEqual(out["version"], "0.2.0")
        self.assertEqual(out["notes"], "notes")
        self.assertEqual(out["pub_date"], "2026-10-14T00:00:00Z")
        self.assertEqual(set(out["platforms"]), ALL)
        self.assertEqual(out["platforms"]["linux-x86_64"]["url"],
                         "https://github.com/thomasvan/lokki/releases/download/v0.2.0/lokki_0.2.0_amd64.AppImage")
        self.assertEqual(out["platforms"]["darwin-aarch64"]["url"],
                         "https://github.com/thomasvan/lokki/releases/download/v0.2.0/lokki_0.2.0_aarch64.app.tar.gz")
        self.assertEqual(out["platforms"]["linux-x86_64"]["signature"], SIG, "the signature is the .sig's text, stripped")
        self.assertNotIn(".deb", json.dumps(out))
        self.assertNotIn(".msi", json.dumps(out))

    def test_a_missing_required_platform_is_refused_by_name(self):
        # (label, names, signatures, require, missing)
        rows = [
            ("no windows asset", ["lokki_0.2.0_amd64.AppImage"], sigs_for("lokki_0.2.0_amd64.AppImage"), {"linux-x86_64", "windows-x86_64"}, ["windows-x86_64"]),
            ("an asset without its signature", ["lokki_0.2.0_amd64.AppImage"], {}, {"linux-x86_64"}, ["linux-x86_64"]),
            ("an asset of another version does not count", ["lokki_0.1.0_amd64.AppImage"], sigs_for("lokki_0.1.0_amd64.AppImage"), {"linux-x86_64"}, ["linux-x86_64"]),
            ("linux only is enough when linux only is required", ["lokki_0.2.0_amd64.AppImage"], sigs_for("lokki_0.2.0_amd64.AppImage"), {"linux-x86_64"}, []),
            ("every platform missing, named in table order", [], {}, ALL, ["linux-x86_64", "darwin-aarch64", "darwin-x86_64", "windows-x86_64"]),
        ]
        for label, names, sigs, require, missing in rows:
            with self.subTest(label):
                if missing:
                    with self.assertRaises(rm.Missing) as cm:
                        rm.manifest("0.2.0", "v0.2.0", "thomasvan/lokki", names, sigs, require, "2026-10-14T00:00:00Z", "")
                    self.assertEqual(cm.exception.platforms, missing)
                else:
                    rm.manifest("0.2.0", "v0.2.0", "thomasvan/lokki", names, sigs, require, "2026-10-14T00:00:00Z", "")
        # A key the table does not know is a typo, not a platform to skip: it would otherwise write an empty manifest.
        for require in ({"linux-x86-64"}, {""}, {"linux-x86_64", "plan9-mips"}):
            with self.subTest(f"unknown key {sorted(require)}"):
                with self.assertRaises(ValueError):
                    rm.manifest("0.2.0", "v0.2.0", "thomasvan/lokki", NAMES, sigs_for(*NAMES), require, "2026-10-14T00:00:00Z", "")

    def test_the_tag_must_be_the_version(self):
        self.assertIsNone(rm.check_tag("v0.2.0", "0.2.0"))
        self.assertIn("v0.2.0", rm.check_tag("v0.2.1", "0.2.0"))
        self.assertIn("v0.2.0", rm.check_tag("0.2.0", "0.2.0"))

    def test_a_tags_draft_is_reused_and_a_published_release_is_refused(self):
        # (label, the repository's releases, the action or None when refused)
        rows = [
            ("no release", [], "create"),
            ("other tags only", [{"tagName": "v0.0.9", "isDraft": False}, {"tagName": "v0.2.1", "isDraft": True}], "create"),
            ("the tag's draft", [{"tagName": "v0.2.0", "isDraft": True}], "reuse"),
            ("the tag is released", [{"tagName": "v0.2.0", "isDraft": False}], None),
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "releases.json")
            for label, releases, action in rows:
                with self.subTest(label):
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(releases, f)
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        status = rm.main(["--release-action", "v0.2.0", "--releases", path])
                    if action:
                        self.assertEqual((status, out.getvalue()), (0, action + "\n"))
                        self.assertEqual(rm.release_action(releases, "v0.2.0"), action)
                    else:
                        self.assertEqual((status, out.getvalue()), (1, ""))
                        self.assertIn("v0.2.0 is already released", err.getvalue())
                        with self.assertRaises(ValueError):
                            rm.release_action(releases, "v0.2.0")

    def test_only_a_minisign_release_key_can_release(self):
        # (label, pubkey, refused)
        rows = [
            ("the development key", rm.DEV_PUBKEY, True),
            ("no key", None, True),
            ("not base64", "not a key", True),
            ("base64 of one line", base64.b64encode(b"just one line").decode(), True),
            ("the wrong algorithm", base64.b64encode(b"untrusted comment: x\n" + base64.b64encode(b"XX" + b"\x01" * 40) + b"\n").decode(), True),
            ("the wrong length", base64.b64encode(b"untrusted comment: x\n" + base64.b64encode(b"Ed" + b"\x01" * 30) + b"\n").decode(), True),
            ("not a string", 42, True),
            ("a minisign key", minisign_pubkey(), False),
        ]
        for label, key, refused in rows:
            with self.subTest(label):
                config = {"plugins": {"updater": ({"pubkey": key} if key is not None else {})}}
                self.assertEqual(rm.check_pubkey(config) is not None, refused)

    def test_the_cli_writes_the_manifest_from_files(self):
        with tempfile.TemporaryDirectory() as d:
            names = os.path.join(d, "names.txt")
            with open(names, "w", encoding="utf-8") as f:
                f.write("lokki_0.2.0_amd64.AppImage\nlokki_0.2.0_amd64.deb\n")
            os.mkdir(os.path.join(d, "sigs"))
            with open(os.path.join(d, "sigs", "lokki_0.2.0_amd64.AppImage.sig"), "w", encoding="utf-8") as f:
                f.write(SIG + "\n")
            notes = os.path.join(d, "notes.md")
            with open(notes, "w", encoding="utf-8") as f:
                f.write("notes\n")
            out = os.path.join(d, "latest.json")
            common = ["--version", "0.2.0", "--tag", "v0.2.0", "--repo", "thomasvan/lokki", "--names", names,
                      "--signatures-dir", os.path.join(d, "sigs"), "--pub-date", "2026-10-14T00:00:00Z", "--notes", notes, "--out", out]
            self.assertEqual(rm.main(common + ["--require", "linux-x86_64"]), 0)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(json.load(f), rm.manifest("0.2.0", "v0.2.0", "thomasvan/lokki", ["lokki_0.2.0_amd64.AppImage", "lokki_0.2.0_amd64.deb"],
                                                           {"lokki_0.2.0_amd64.AppImage.sig": SIG + "\n"}, {"linux-x86_64"}, "2026-10-14T00:00:00Z", "notes\n"))
            os.unlink(out)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(rm.main(common + ["--require", "linux-x86_64,windows-x86_64"]), 1)
            self.assertIn("windows-x86_64", err.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(rm.main(common + ["--require", ""]), 1)
            self.assertIn("unknown platform", err.getvalue())
            self.assertFalse(os.path.exists(out))
            # The two checks the release job runs before building.
            core, _ = sidecars.read_versions()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(rm.main(["--check-tag", f"v{core}"]), 0)
                self.assertEqual(rm.main(["--check-tag", f"v{core}.1"]), 1)
                for key, status in ((minisign_pubkey(), 0), ("not a key", 1)):
                    config = os.path.join(d, "tauri.conf.json")
                    with open(config, "w", encoding="utf-8") as f:
                        json.dump({"plugins": {"updater": {"pubkey": key}}}, f)
                    self.assertEqual(rm.main(["--check-pubkey", config]), status)
            self.assertFalse(os.path.exists(out))
