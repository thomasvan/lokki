#!/usr/bin/env python3
"""The one home of the sidecar file-name rule. A dot in a name reads as an extension to
Tauri's Windows path helper, so versions go into names with dots as underscores."""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def token(version):
    return version.replace(".", "_")


def core_name(version):
    return f"lokki-core-{token(version)}"


def cliproxy_name(version):
    return f"cliproxy-{token(version)}"


def names(core_version, cliproxy_version):
    return {"core": core_name(core_version), "cliproxy": cliproxy_name(cliproxy_version)}


def read_versions(repo_root=REPO):
    """(core version, CLIProxyAPI version) from core/VERSION and app/cliproxy.lock."""
    with open(os.path.join(repo_root, "core", "VERSION"), encoding="utf-8") as f:
        core = f.read().strip()
    with open(os.path.join(repo_root, "app", "cliproxy.lock"), encoding="utf-8") as f:
        return core, json.load(f)["version"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--names", action="store_true", help="print both names as JSON")
    group.add_argument("--versions", action="store_true", help="print both versions as JSON")
    group.add_argument("--config-json", action="store_true", help="write the `tauri build --config` override")
    parser.add_argument("--out", help="the file --config-json writes")
    parser.add_argument("--release", action="store_true",
                        help="with --config-json: also ask for signed updater artifacts (needs the signing key)")
    args = parser.parse_args(argv)
    if args.release and not args.config_json:
        parser.error("--release only goes with --config-json")
    core, cliproxy = read_versions()
    n = names(core, cliproxy)
    if args.names:
        print(json.dumps(n))
        return 0
    if args.versions:
        print(json.dumps({"core": core, "cliproxy": cliproxy}))
        return 0
    if not args.out:
        parser.error("--config-json needs --out FILE")
    conf = {"version": core, "bundle": {"externalBin": [f"binaries/{n['core']}", f"binaries/{n['cliproxy']}"]}}
    if args.release:
        conf["bundle"]["createUpdaterArtifacts"] = True
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(conf, f)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
