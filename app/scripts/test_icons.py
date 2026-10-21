#!/usr/bin/env python3
"""icons.py writes the .ico and .icns containers itself; Tauri's own files are the
reference its reader is checked against, and the checked-in set is what it wrote."""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ICONS = os.path.join(REPO, "app", "src-tauri", "icons")
sys.path.insert(0, HERE)
import icons  # noqa: E402


def _read(*parts):
    with open(os.path.join(*parts), "rb") as f:
        return f.read()


def _png_size(data):
    """The IHDR width and height of a PNG, or None for anything else."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


class IconsTests(unittest.TestCase):
    def test_the_reader_reads_tauris_own_icons(self):
        layers = icons.read_ico(_read(HERE, "testdata", "tauri-2.12.2-icon.ico"))
        self.assertEqual(sorted(layers), [16, 24, 32, 48, 64, 256])
        for size, png in layers.items():
            self.assertEqual(_png_size(png), (size, size), size)
        data = _read(HERE, "testdata", "tauri-2.12.2-icon.icns")
        entries = icons.read_icns(data)
        # tauri icon writes these types in no stable order, so they are compared as a set.
        self.assertEqual({t for t, _ in entries},
                         {"ic14", "is32", "s8mk", "ic12", "ic08", "ic11", "il32", "l8mk",
                          "ic07", "ic13", "ic09", "ic10"})
        self.assertEqual([t for t, _ in entries],
                         ["ic10", "ic07", "is32", "s8mk", "ic14", "ic09", "ic12", "il32",
                          "l8mk", "ic13", "ic11", "ic08"])
        self.assertEqual(8 + sum(8 + len(b) for _, b in entries), len(data))

    def test_the_writer_round_trips_through_the_reader(self):
        layers = icons.read_ico(_read(HERE, "testdata", "tauri-2.12.2-icon.ico"))
        self.assertEqual(icons.read_ico(icons.ico(layers)), layers)
        entries = icons.read_icns(_read(HERE, "testdata", "tauri-2.12.2-icon.icns"))
        self.assertEqual(icons.read_icns(icons.icns(entries)), entries)

    def test_the_reader_refuses_a_broken_icns(self):
        good = icons.icns([("icp4", b"p" * 10), ("ic07", b"q" * 5)])
        second = 8 + 8 + 10
        rows = [
            ("a wrong magic", b"icnX" + good[4:]),
            ("a header length the data disagrees with",
             good[:4] + struct.pack(">I", len(good) + 1) + good[8:]),
            ("an entry that runs past the file",
             good[:second + 4] + struct.pack(">I", 8 + 5 + 1) + good[second + 8:]),
            ("an entry shorter than its own header", good[:12] + struct.pack(">I", 4) + good[16:]),
        ]
        self.assertEqual(icons.read_icns(good), [("icp4", b"p" * 10), ("ic07", b"q" * 5)])
        for name, data in rows:
            with self.assertRaises(ValueError, msg=name):
                icons.read_icns(data)

    def test_32_px_and_under_use_the_small_drawing(self):
        for size, drawing in [(16, "small"), (24, "small"), (32, "small"), (48, "large"),
                              (64, "large"), (128, "large"), (256, "large"), (512, "large"),
                              (1024, "large")]:
            self.assertEqual(icons.layers_for(size), drawing, size)

    def test_the_small_drawing_fills_the_32_px_and_under_layers(self):
        large = {size: f"large-{size}".encode() for size in icons.ICO_SIZES}
        small = {size: f"small-{size}".encode() for size in (16, 24, 32)}
        self.assertEqual(icons.ico_layers(large, small),
                         {16: b"small-16", 24: b"small-24", 32: b"small-32",
                          48: b"large-48", 64: b"large-64", 256: b"large-256"})
        large = {kind: f"large-{kind}".encode() for kind in icons.ICNS_TYPES}
        self.assertEqual(icons.icns_entries(large, small),
                         [("icp4", b"small-16"), ("icp5", b"small-32"), ("ic11", b"small-32"),
                          ("ic12", b"large-ic12"), ("ic07", b"large-ic07"),
                          ("ic13", b"large-ic13"), ("ic08", b"large-ic08"),
                          ("ic14", b"large-ic14"), ("ic09", b"large-ic09"),
                          ("ic10", b"large-ic10")])

    def test_the_checked_in_icons_follow_the_plan(self):
        layers = icons.read_ico(_read(ICONS, "icon.ico"))
        self.assertEqual(sorted(layers), list(icons.ICO_SIZES))
        for size, png in layers.items():
            self.assertEqual(_png_size(png), (size, size), size)
        entries = icons.read_icns(_read(ICONS, "icon.icns"))
        self.assertEqual([t for t, _ in entries], list(icons.ICNS_TYPES))
        for kind, png in entries:
            px = icons.ICNS_PX[kind]
            self.assertEqual(_png_size(png), (px, px), kind)
        for form in ("solid", "line"):
            for ink in ("black", "white"):
                for px in (16, 20, 24, 32):
                    name = f"tray-{form}-{ink}-{px}.png"
                    self.assertEqual(_png_size(_read(ICONS, name)), (px, px), name)


if __name__ == "__main__":
    unittest.main()
