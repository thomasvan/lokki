#!/usr/bin/env python3
"""The release notes: the subjects of main's own commits since the previous tag, a line per
merged pull request (`git log --first-parent` in app.yml; the first release says so instead) and, when app/cliproxy.lock changed, CLIProxyAPI's versions and that release's
body (handed in; no network here)."""
import argparse
import json
import sys


def notes(subjects, lock_before, lock_after, cliproxy_body):
    lines = ["## Changes", ""]
    # No previous lock means no previous tag: every commit would be "since", and most of them are history.
    if lock_before is None:
        lines.append("The first release of lokki.")
    else:
        lines += [f"- {s}" for s in subjects] or ["(no commits since the previous tag)"]
    after = lock_after["version"]
    before = lock_before["version"] if lock_before else None
    if before != after:
        lines += ["", f"## CLIProxyAPI {before} -> {after}" if before else f"## CLIProxyAPI {after}"]
        if cliproxy_body.strip():
            lines += ["", cliproxy_body.rstrip("\n")]
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lock-before")
    p.add_argument("--lock-after", required=True)
    p.add_argument("--cliproxy-notes")
    a = p.parse_args(argv)
    subjects = [line.rstrip("\n") for line in sys.stdin if line.strip()]
    before = None
    if a.lock_before:
        with open(a.lock_before, encoding="utf-8") as f:
            before = json.load(f)
    with open(a.lock_after, encoding="utf-8") as f:
        after = json.load(f)
    body = ""
    if a.cliproxy_notes:
        with open(a.cliproxy_notes, encoding="utf-8") as f:
            body = f.read()
    sys.stdout.write(notes(subjects, before, after, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
