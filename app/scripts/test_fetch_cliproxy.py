#!/usr/bin/env python3
"""The CLIProxyAPI sidecar is fetched, hash-checked against the lock and unpacked to one
executable. No test reaches the network: `download` is injected."""
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import fetch_cliproxy  # noqa: E402
import sidecars  # noqa: E402

ELF = b"\x7fELF\x02\x01\x01" + b"\0" * 32
PE = b"MZ\x90\0" + b"\0" * 32
VERSION = "1.2.3"
LINUX = "x86_64-unknown-linux-gnu"
WINDOWS = "x86_64-pc-windows-msvc"


def _tar_gz(binary):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("cli-proxy-api", binary), ("README.md", b"readme"), ("LICENSE", b"mit"),
                           ("config.example.yaml", b"port: 1")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip(binary):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cli-proxy-api.exe", binary)
        z.writestr("README.md", b"readme")
    return buf.getvalue()


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lokki-fetch-")
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.out = os.path.join(self.root, "out")
        os.mkdir(self.out)
        self.calls = []

    def _lock(self, target, asset, archive):
        path = os.path.join(self.root, "cliproxy.lock")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "assets": {target: {
                "name": asset, "sha256": hashlib.sha256(archive).hexdigest()}}}, f)
        return path

    def _download(self, payload):
        def download(url, dest):
            self.calls.append((url, dest))
            with open(dest, "wb") as f:
                f.write(payload)
        return download

    def test_a_wrong_hash_is_refused_before_extraction(self):
        asset = f"CLIProxyAPI_{VERSION}_linux_amd64.tar.gz"
        lock = self._lock(LINUX, asset, _tar_gz(ELF))
        # Bytes that are no archive: opening them first would raise a tar error, not HashMismatch.
        with self.assertRaises(fetch_cliproxy.HashMismatch):
            fetch_cliproxy.fetch(lock, LINUX, self.out, self._download(b"not the archive"))
        self.assertEqual(os.listdir(self.out), [])

    def test_the_right_hash_extracts_the_binary_only(self):
        asset = f"CLIProxyAPI_{VERSION}_linux_amd64.tar.gz"
        lock = self._lock(LINUX, asset, _tar_gz(ELF))
        path = fetch_cliproxy.fetch(lock, LINUX, self.out, self._download(_tar_gz(ELF)))
        want = f"{sidecars.cliproxy_name(VERSION)}-{LINUX}"
        self.assertEqual(os.listdir(self.out), [want])
        self.assertEqual(path, os.path.join(self.out, want))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o755)
        (url, dest), = self.calls
        self.assertEqual(url, f"https://github.com/router-for-me/CLIProxyAPI/releases/download/v{VERSION}/{asset}")
        out = os.path.abspath(self.out)
        self.assertNotEqual(os.path.commonpath([out, os.path.abspath(dest)]), out)  # beside out, never inside

    def test_windows_assets_are_zip_with_an_exe(self):
        self.assertEqual(fetch_cliproxy.member_name(f"CLIProxyAPI_{VERSION}_windows_amd64.zip"), "cli-proxy-api.exe")
        self.assertEqual(fetch_cliproxy.member_name(f"CLIProxyAPI_{VERSION}_darwin_aarch64.tar.gz"), "cli-proxy-api")
        asset = f"CLIProxyAPI_{VERSION}_windows_amd64.zip"
        lock = self._lock(WINDOWS, asset, _zip(PE))
        path = fetch_cliproxy.fetch(lock, WINDOWS, self.out, self._download(_zip(PE)))
        self.assertEqual(os.listdir(self.out), [f"{sidecars.cliproxy_name(VERSION)}-{WINDOWS}.exe"])
        self.assertEqual(os.path.dirname(path), self.out)

    def test_only_elf_macho_and_pe_images_are_executables(self):
        rows = [("elf", ELF, True), ("pe", PE, True),
                ("mach-o 64 little-endian", b"\xcf\xfa\xed\xfe" + b"\0" * 8, True),
                ("mach-o 32 little-endian", b"\xce\xfa\xed\xfe" + b"\0" * 8, True),
                ("mach-o 64 big-endian", b"\xfe\xed\xfa\xcf" + b"\0" * 8, True),
                ("mach-o 32 big-endian", b"\xfe\xed\xfa\xce" + b"\0" * 8, True),
                ("mach-o universal", b"\xca\xfe\xba\xbe" + b"\0" * 8, True),
                ("gzip", gzip.compress(b"x"), False),
                ("an html error page", b"<!doctype html><title>404</title>", False),
                ("empty", b"", False)]
        for label, data, want in rows:
            with self.subTest(label), tempfile.TemporaryDirectory(prefix="lokki-image-") as tmp:
                path = os.path.join(tmp, "f")
                with open(path, "wb") as f:
                    f.write(data)
                self.assertIs(fetch_cliproxy.is_executable_image(path), want)

    def test_a_member_that_is_not_an_executable_image_is_refused(self):
        # gzip bytes under the binary's name
        archive = _tar_gz(gzip.compress(b"not a binary"))
        lock = self._lock(LINUX, f"CLIProxyAPI_{VERSION}_linux_amd64.tar.gz", archive)
        with self.assertRaises(fetch_cliproxy.NotExecutable):
            fetch_cliproxy.fetch(lock, LINUX, self.out, self._download(archive))
        self.assertEqual(os.listdir(self.out), [])

    def test_the_lock_names_the_six_targets_with_plain_assets(self):
        # The six targets are a boundary the release must not move quietly, so the test holds its own copy.
        targets = {"x86_64-unknown-linux-gnu": ("linux", "amd64", "tar.gz"),
                   "aarch64-unknown-linux-gnu": ("linux", "aarch64", "tar.gz"),
                   "x86_64-apple-darwin": ("darwin", "amd64", "tar.gz"),
                   "aarch64-apple-darwin": ("darwin", "aarch64", "tar.gz"),
                   "x86_64-pc-windows-msvc": ("windows", "amd64", "zip"),
                   "aarch64-pc-windows-msvc": ("windows", "aarch64", "zip")}
        with open(os.path.join(REPO, "app", "cliproxy.lock"), encoding="utf-8") as f:
            lock = json.load(f)
        self.assertEqual(set(lock["assets"]), set(targets))
        for triple, (os_, arch, ext) in targets.items():
            with self.subTest(triple):
                entry = lock["assets"][triple]
                self.assertEqual(entry["name"], f"CLIProxyAPI_{lock['version']}_{os_}_{arch}.{ext}")  # no _no-plugin variant
                self.assertRegex(entry["sha256"], re.compile(r"^[0-9a-f]{64}$"))


if __name__ == "__main__":
    unittest.main()
