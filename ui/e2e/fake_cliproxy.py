#!/usr/bin/env python3
"""A fake CLIProxyAPI management API for the browser run: the core's own test fake
(core/test_quota_reader.py) serving two made-up codex accounts, `a1` at 80 % used and
`b2` at 10 %, with a reset an hour ahead, and two made-up model ids on `/v1/models`. Prints `port=N` and serves until killed or
until stdin closes (the run that started it is gone). The management secret is read
from a file, never from argv, which `ps` shows to everyone on the host."""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))
from test_quota_reader import FakeManagement, usage  # noqa: E402

ACCOUNTS = [("a1", "one@example.invalid", 80), ("b2", "two@example.invalid", 10)]
MODELS = ["model-alpha", "model-beta"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret-file", required=True)
    args = parser.parse_args()
    with open(args.secret_file, encoding="utf-8") as f:
        secret = f.read().strip()
    reset_at = int(time.time()) + 3600
    fake = FakeManagement(
        credentials=[{"provider": "codex", "auth_index": index, "email": email, "disabled": False}
                     for index, email, _ in ACCOUNTS],
        usage={index: usage(used, reset_at=reset_at) for index, _, used in ACCOUNTS},
        secret=secret)
    fake.models = MODELS
    print(f"port={fake.server.server_address[1]}", flush=True)
    sys.stdin.read()          # blocks until the parent closes the pipe or dies
    fake.close()


if __name__ == "__main__":
    main()
