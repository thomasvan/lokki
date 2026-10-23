#!/usr/bin/env python3
"""Refuses a Windows executable whose PE header names another subsystem than the one
wanted. `windows` is a GUI program and opens no console window; `console` opens one.

    python3 check_pe_subsystem.py PATH --want windows
"""
import argparse
import struct
import sys

SUBSYSTEMS = {2: "windows", 3: "console"}


def subsystem(data):
    """The subsystem `data`, a PE file, names; a ValueError when it is not one."""
    if data[:2] != b"MZ":
        raise ValueError("not an MZ executable")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("no PE signature")
    optional = pe + 4 + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic not in (0x10B, 0x20B):
        raise ValueError(f"an optional header of unknown magic {magic:#x}")
    # Offset 68 of the optional header in PE32 and PE32+ alike.
    value = struct.unpack_from("<H", data, optional + 68)[0]
    return SUBSYSTEMS.get(value, f"subsystem {value}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path")
    parser.add_argument("--want", required=True, choices=sorted(SUBSYSTEMS.values()))
    args = parser.parse_args(argv)
    with open(args.path, "rb") as f:
        data = f.read()
    try:
        found = subsystem(data)
    except (ValueError, struct.error) as e:
        print(f"{args.path}: {e}", file=sys.stderr)
        return 1
    print(f"{args.path}: {found}")
    if found != args.want:
        print(f"{args.path}: wanted {args.want}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
