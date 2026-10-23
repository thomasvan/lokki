import contextlib
import io
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_pe_subsystem as cps  # noqa: E402


def pe(subsystem, magic=0x20B):
    """The head of a PE file: the MZ stub pointing at 0x40, the signature, a COFF header,
    and an optional header up to its subsystem."""
    stub = bytearray(0x40)
    stub[:2] = b"MZ"
    struct.pack_into("<I", stub, 0x3C, 0x40)
    optional = bytearray(70)
    struct.pack_into("<H", optional, 0, magic)
    struct.pack_into("<H", optional, 68, subsystem)
    return bytes(stub) + b"PE\0\0" + bytes(20) + bytes(optional)


class SubsystemTests(unittest.TestCase):
    def test_only_the_wanted_subsystem_passes(self):
        # (label, file, wanted, exit status)
        rows = [
            ("a GUI program", pe(2), "windows", 0),
            ("a 32-bit GUI program", pe(2, magic=0x10B), "windows", 0),
            ("a console program", pe(3), "windows", 1),
            ("a console program wanted", pe(3), "console", 0),
            ("another subsystem", pe(10), "windows", 1),
            ("not an executable", b"#!/bin/sh\n", "windows", 1),
            ("a cut header", pe(2)[:0x50], "windows", 1),
            ("an unknown optional header", pe(2, magic=0x107), "windows", 1),
        ]
        for label, data, want, status in rows:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "lokki.exe")
                with open(path, "wb") as f:
                    f.write(data)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cps.main([path, "--want", want]), status)


if __name__ == "__main__":
    unittest.main()
