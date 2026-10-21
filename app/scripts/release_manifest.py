#!/usr/bin/env python3
"""The updater's manifest (`latest.json`) from a draft release's asset names, and the two
refusals the release job makes before building: a tag that is not core/VERSION, and a
public key that cannot have signed a release. PLATFORMS is the one home of the platform
table."""
import argparse
import base64
import binascii
import json
import os
import sys

# triple -> (the updater's platform key, the asset the updater downloads)
PLATFORMS = {
    "x86_64-unknown-linux-gnu": ("linux-x86_64", "lokki_{version}_amd64.AppImage"),
    "aarch64-apple-darwin": ("darwin-aarch64", "lokki_{version}_aarch64.app.tar.gz"),
    "x86_64-apple-darwin": ("darwin-x86_64", "lokki_{version}_x64.app.tar.gz"),
    "x86_64-pc-windows-msvc": ("windows-x86_64", "lokki_{version}_x64-setup.exe"),
}

# The development key; its private half is gone, and the release job refuses it.
DEV_PUBKEY = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDhBRDc2NUNBNjNFMEQwRTEKUldUaDBPQmp5bVhYaW4xeWFZYUF4dzRpOVJlUFB3N2cyeUNtdXArRnYxVGgwbkRQUmdJVHNXZHEK"


class Missing(Exception):
    def __init__(self, platforms):
        super().__init__("no signed asset for: " + ", ".join(platforms))
        self.platforms = platforms


def asset_url(repo, tag, name):
    return f"https://github.com/{repo}/releases/download/{tag}/{name}"


def manifest(version, tag, repo, names, signatures, require, pub_date, notes):
    unknown = sorted(set(require) - {key for key, _ in PLATFORMS.values()})
    if unknown:
        raise ValueError("unknown platform in --require: " + ", ".join(repr(k) for k in unknown))
    present = set(names)
    platforms = {}
    missing = []
    for key, pattern in PLATFORMS.values():
        name = pattern.format(version=version)
        sig = signatures.get(f"{name}.sig")
        if name in present and sig and sig.strip():
            platforms[key] = {"signature": sig.strip(), "url": asset_url(repo, tag, name)}
        elif key in require:
            missing.append(key)
    if missing:
        raise Missing(missing)
    return {"version": version, "notes": notes, "pub_date": pub_date, "platforms": platforms}


def check_tag(tag, version):
    want = f"v{version}"
    return None if tag == want else f"the tag is {tag!r} but core/VERSION says {want!r}"


def release_action(releases, tag):
    """What the release job does with `tag`, given `gh release list --json tagName,isDraft`:
    "create" the draft, or "reuse" the one an earlier run left. A published release is refused."""
    for release in releases:
        if release.get("tagName") == tag:
            if release.get("isDraft"):
                return "reuse"
            raise ValueError(f"{tag} is already released")
    return "create"


def _is_minisign_pubkey(text):
    try:
        lines = base64.b64decode(text, validate=True).decode("utf-8").splitlines()
        raw = base64.b64decode(lines[1], validate=True)
    except (binascii.Error, UnicodeDecodeError, IndexError, TypeError, ValueError):
        return False
    return len(raw) == 42 and raw[:2] == b"Ed"


def check_pubkey(config):
    key = ((config.get("plugins") or {}).get("updater") or {}).get("pubkey")
    if not key or key == DEV_PUBKEY:
        return "tauri.conf.json carries no release public key (plugins.updater.pubkey is unset or the development key)"
    if not _is_minisign_pubkey(key):
        return "plugins.updater.pubkey is not a minisign public key as `tauri signer generate` prints it"
    return None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check-tag")
    p.add_argument("--check-pubkey")
    p.add_argument("--release-action", metavar="TAG", help="print create or reuse for TAG, given --releases")
    p.add_argument("--releases", help="a file with `gh release list --json tagName,isDraft`")
    p.add_argument("--version")
    p.add_argument("--tag")
    p.add_argument("--repo")
    p.add_argument("--names", help="a file with one asset name per line")
    p.add_argument("--signatures-dir")
    p.add_argument("--require")
    p.add_argument("--pub-date")
    p.add_argument("--notes")
    p.add_argument("--out")
    a = p.parse_args(argv)
    if a.check_tag:
        import sidecars
        core, _ = sidecars.read_versions()
        problem = check_tag(a.check_tag, core)
    elif a.release_action:
        if not a.releases:
            p.error("--release-action needs --releases FILE")
        with open(a.releases, encoding="utf-8") as f:
            try:
                print(release_action(json.load(f), a.release_action))
            except ValueError as e:
                print(f"release_manifest: {e}", file=sys.stderr)
                return 1
        return 0
    elif a.check_pubkey:
        with open(a.check_pubkey, encoding="utf-8") as f:
            problem = check_pubkey(json.load(f))
    else:
        for name in ("version", "tag", "repo", "names", "signatures_dir", "require", "pub_date", "notes", "out"):
            if getattr(a, name) is None:
                p.error(f"--{name.replace('_', '-')} is required to write the manifest")
        with open(a.names, encoding="utf-8") as f:
            names = [line.strip() for line in f if line.strip()]
        signatures = {}
        for name in os.listdir(a.signatures_dir):
            if name.endswith(".sig"):
                with open(os.path.join(a.signatures_dir, name), encoding="utf-8") as f:
                    signatures[name] = f.read()
        with open(a.notes, encoding="utf-8") as f:
            notes = f.read()
        try:
            out = manifest(a.version, a.tag, a.repo, names, signatures, set(a.require.split(",")), a.pub_date, notes)
        except (Missing, ValueError) as e:
            print(f"release_manifest: {e}", file=sys.stderr)
            return 1
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
            f.write("\n")
        return 0
    if problem:
        print(f"release_manifest: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
