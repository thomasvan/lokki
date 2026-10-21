#!/usr/bin/env python3
"""Fetch the CLIProxyAPI sidecar the lock names for one target: download, verify the hash
before the archive is opened, extract the one binary under its versioned name."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sidecars  # noqa: E402

URL = "https://github.com/router-for-me/CLIProxyAPI/releases/download/v{version}/{asset}"
# ELF; PE ("MZ"); Mach-O 32/64 in both byte orders; universal.
_MAGICS = (b"\x7fELF", b"MZ", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


class HashMismatch(Exception):
    pass


class NotExecutable(Exception):
    pass


def verify(path, sha256):
    """Raises HashMismatch unless the file's SHA-256 is `sha256`."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != sha256.lower():
        raise HashMismatch(f"{os.path.basename(path)} does not match the lock's sha256")


def member_name(asset):
    return "cli-proxy-api.exe" if asset.endswith(".zip") else "cli-proxy-api"


def is_executable_image(path):
    with open(path, "rb") as f:
        head = f.read(4)
    return any(head.startswith(m) for m in _MAGICS)


def output_name(version, target):
    """The file `fetch` writes and `--check` runs: the versioned name, the triple, `.exe` on Windows."""
    return f"{sidecars.cliproxy_name(version)}-{target}" + (".exe" if "windows" in target else "")


def download_url(url, dest):
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _extract(archive, asset, member, dest):
    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as z, z.open(member) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    else:
        with tarfile.open(archive, "r:gz") as t:
            src = t.extractfile(member)
            if src is None:
                raise NotExecutable(f"{asset} has no file {member}")
            with src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def fetch(lock_path, target, out_dir, download=download_url):
    """Returns the path of the unpacked binary in `out_dir`. `download(url, dest)` writes
    the archive; the temp file lives beside `out_dir`, never in it, and out_dir gains
    the binary only once it has passed the hash and the executable check."""
    with open(lock_path, encoding="utf-8") as f:
        lock = json.load(f)
    entry = lock["assets"][target]
    asset = entry["name"]
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix=".fetch-cliproxy-", dir=os.path.dirname(out_dir))
    try:
        archive = os.path.join(scratch, asset)
        download(URL.format(version=lock["version"], asset=asset), archive)
        verify(archive, entry["sha256"])
        member = member_name(asset)
        staged = os.path.join(scratch, member)
        _extract(archive, asset, member, staged)
        if not is_executable_image(staged):
            raise NotExecutable(f"{member} in {asset} is not an ELF, Mach-O or PE file")
        dest = os.path.join(out_dir, output_name(lock["version"], target))
        os.chmod(staged, 0o755)
        shutil.move(staged, dest)
        return dest
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def check(path, version):
    """Runs the binary with -h (exit 0; --version exits 2 on this binary) and expects the
    version in the first line."""
    out = subprocess.run([path, "-h"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise SystemExit(f"{path} -h exited {out.returncode}")
    text = (out.stdout or out.stderr).splitlines()
    first = text[0] if text else ""
    if f"Version: {version}" not in first:
        raise SystemExit(f"{path} -h says {first!r}, not version {version}")
    return first


def host_triple():
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    return {"linux": f"{arch}-unknown-linux-gnu", "darwin": f"{arch}-apple-darwin",
            "win32": f"{arch}-pc-windows-msvc"}[sys.platform]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lock", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--check", action="store_true", help="run the file already in --out instead of fetching")
    args = p.parse_args(argv)
    with open(args.lock, encoding="utf-8") as f:
        version = json.load(f)["version"]
    if args.check:
        if args.target != host_triple():
            raise SystemExit(f"--check runs the file, so --target must be this host's {host_triple()}")
        print(check(os.path.join(args.out, output_name(version, args.target)), version))
        return 0
    print(fetch(args.lock, args.target, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
