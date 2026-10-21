"""`python -m lokki_core`: serve (the default, `--config FILE` as server.py takes
it), `init`, `sync-cliproxy`, `shell-config`, `schema --example`."""
import argparse
import json
import sys


def _port(allow_zero):
    def parse(value):
        from . import init
        try:
            return init.check_port(int(value), allow_zero)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"port must be {'0 or ' if allow_zero else ''}a whole number from 1 to 65535, "
                f"not {value!r}")
    return parse


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-c", "--config"):
        from . import handler
        return handler.main(argv)
    parser = argparse.ArgumentParser(prog="lokki_core")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--data-dir", required=True)
    p.add_argument("--core-port", type=_port(True), default=8381)
    p.add_argument("--cliproxy-port", type=_port(True), default=8317)
    p.add_argument("--cliproxy-version", default="")
    for name in ("sync-cliproxy", "shell-config"):
        sub.add_parser(name).add_argument("--data-dir", required=True)
    p = sub.add_parser("schema")
    p.add_argument("--example", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "init":
        from . import init
        out = init.run(args.data_dir, args.core_port, args.cliproxy_port, args.cliproxy_version)
        note = out.pop("note", "")
        # Ports and paths only: `run` returns no secret, and this prints what it returns.
        print(json.dumps(out))
        if not out["created"]:
            print(note, file=sys.stderr)
    elif args.cmd in ("sync-cliproxy", "shell-config"):
        from . import init
        try:
            if args.cmd == "sync-cliproxy":
                init.sync_cliproxy(args.data_dir)
            else:
                # The one output that carries a secret (the operator token), for the shell's pipe.
                print(json.dumps(init.shell_config(args.data_dir)))
        except init.InitError as e:
            print(f"lokki_core: {e}", file=sys.stderr)
            return 1
    elif args.cmd == "schema":
        from . import schema
        text = schema.example_yaml() if args.example else json.dumps(schema.listing(), indent=1) + "\n"
        # Bytes, not the console encoding: a redirect on Windows would not be UTF-8.
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
