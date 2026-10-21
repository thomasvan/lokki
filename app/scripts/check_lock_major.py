#!/usr/bin/env python3
"""Refuse a tree whose CLIProxyAPI lock pins a major version the reader's route prefix
(MANAGEMENT_MAJOR in core/lokki_core/quota/reader.py) does not speak (spec 7). Run on the
head alone: a lock bump across a major without the reader fails here on that PR."""
import argparse
import json
import re
import sys

_MAJOR = re.compile(r'^MANAGEMENT_MAJOR\s*=\s*"(\d+)"', re.M)


def refuse(lock, reader):
    lock_major = int(lock["version"].split(".")[0])
    m = _MAJOR.search(reader)
    if not m:
        return "core/lokki_core/quota/reader.py has no MANAGEMENT_MAJOR constant"
    reader_major = int(m.group(1))
    if reader_major != lock_major:
        return (f"the reader's MANAGEMENT_MAJOR is {reader_major} but app/cliproxy.lock pins "
                f"{lock['version']}: change both in one PR")
    return None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lock", required=True)
    p.add_argument("--reader", required=True)
    a = p.parse_args(argv)
    with open(a.lock, encoding="utf-8") as f:
        lock = json.load(f)
    with open(a.reader, encoding="utf-8") as f:
        reader = f.read()
    problem = refuse(lock, reader)
    if problem:
        print(f"check_lock_major: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
