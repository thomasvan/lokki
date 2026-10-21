#!/usr/bin/env python3
"""The icon set, from the drawings in docs/design/ux/: the app icon (app-icon.svg, and
app-icon-small.svg at 32 px and under, so each wing keeps a full pixel), the tray's two
forms in black and white at each bar size, and the favicon PNGs from ui/src/assets/.

`npx tauri icon` (the CLI app/package.json pins) rasterizes every PNG, in a scratch
directory this removes. It cannot put two drawings in one container, so this writes
icon.ico and icon.icns itself, holding PNG layers as Tauri's do.

Run from anywhere: `python3 app/scripts/icons.py`, after `cd app && npm ci`.
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
APP = os.path.join(REPO, "app")
DESIGN = os.path.join(REPO, "docs", "design", "ux")
ICONS = os.path.join(APP, "src-tauri", "icons")
ASSETS = os.path.join(REPO, "ui", "src", "assets")

ICO_SIZES = (16, 24, 32, 48, 64, 256)
# Each .icns type and its pixel size, in the order build() writes them. The small drawing is
# icp4 and icp5 (16 and 32 px) and ic11 (16 px at 2x), in place of the old is32/s8mk and
# il32/l8mk bitmaps tauri icon writes.
ICNS_PX = {"icp4": 16, "icp5": 32, "ic11": 32, "ic12": 64, "ic07": 128, "ic13": 256,
           "ic08": 256, "ic14": 512, "ic09": 512, "ic10": 1024}
ICNS_TYPES = tuple(ICNS_PX)
TRAY_PX = (16, 20, 24, 32)


def layers_for(size):
    """Which drawing of the app icon a size uses."""
    return "small" if size <= 32 else "large"


def ico_layers(large, small):
    """The .ico's layers by size: the small drawing's PNG at 32 px and under, the large
    drawing's above."""
    return {size: (small if layers_for(size) == "small" else large)[size] for size in ICO_SIZES}


def icns_entries(large, small):
    """The .icns entries in ICNS_TYPES' order: `small` by pixel size at 32 px and under,
    `large` by type above."""
    return [(kind, small[px] if layers_for(px) == "small" else large[kind])
            for kind, px in ICNS_PX.items()]


def ico(layers):
    """An .ico of PNG layers, keyed by size, in ascending order."""
    sizes = sorted(layers)
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = len(head) + 16 * len(sizes)
    entries, body = b"", b""
    for size in sizes:
        png = layers[size]
        # A width or height of 0 means 256.
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        body += png
        offset += len(png)
    return head + entries + body


def read_ico(data):
    """An .ico's layers, keyed by size."""
    _, kind, count = struct.unpack_from("<HHH", data, 0)
    if kind != 1:
        raise ValueError("not an .ico")
    layers = {}
    for i in range(count):
        width, _, _, _, _, _, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        layers[width or 256] = data[offset:offset + length]
    return layers


def icns(entries):
    """An .icns of (type, data) entries, in the order given."""
    body = b"".join(kind.encode("ascii") + struct.pack(">I", 8 + len(data)) + data
                    for kind, data in entries)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def read_icns(data):
    """An .icns's (type, data) entries, in the file's order."""
    if data[:4] != b"icns" or struct.unpack_from(">I", data, 4)[0] != len(data):
        raise ValueError("not an .icns")
    entries, at = [], 8
    while at < len(data):
        length = struct.unpack_from(">I", data, at + 4)[0]
        if length < 8 or at + length > len(data):
            raise ValueError(f"an .icns entry at {at} runs past the file")
        entries.append((data[at:at + 4].decode("ascii"), data[at + 8:at + length]))
        at += length
    return entries


def _rasterize(svg, out, sizes=None):
    """`tauri icon` of one drawing into `out`: the whole set, or only `sizes` as PNGs."""
    cmd = ["npx", "--no-install", "tauri", "icon", svg, "-o", out]
    if sizes:
        cmd[5:5] = ["-p", ",".join(map(str, sizes))]
    subprocess.run(cmd, cwd=APP, check=True, stdout=subprocess.DEVNULL)


def _png(directory, size):
    with open(os.path.join(directory, f"{size}x{size}.png"), "rb") as f:
        return f.read()


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def build(scratch):
    large, small = os.path.join(scratch, "large"), os.path.join(scratch, "small")
    _rasterize(os.path.join(DESIGN, "app-icon.svg"), large)
    # Every PNG of 32 px and under in the set: the ico's three, and the Square30x30 logo.
    _rasterize(os.path.join(DESIGN, "app-icon-small.svg"), small, (16, 24, 30, 32))

    # The desktop set tauri icon writes, as the tree carries it; never android/ or ios/.
    for name in sorted(os.listdir(large)):
        source = os.path.join(large, name)
        if not name.endswith(".png") or not os.path.isfile(source):
            continue
        with open(source, "rb") as f:
            size = struct.unpack(">I", f.read(24)[16:20])[0]
        if layers_for(size) == "small":
            source = os.path.join(small, f"{size}x{size}.png")
        shutil.copyfile(source, os.path.join(ICONS, name))

    smalls = {px: _png(small, px) for px in (16, 24, 32)}
    with open(os.path.join(large, "icon.ico"), "rb") as f:
        _write(os.path.join(ICONS, "icon.ico"), ico(ico_layers(read_ico(f.read()), smalls)))
    with open(os.path.join(large, "icon.icns"), "rb") as f:
        entries = icns_entries(dict(read_icns(f.read())), smalls)
    _write(os.path.join(ICONS, "icon.icns"), icns(entries))

    # The tray: black as drawn, and white with the fill or stroke turned white.
    for form in ("solid", "line"):
        with open(os.path.join(DESIGN, f"logo-template-{form}.svg"), encoding="utf-8") as f:
            drawing = f.read()
        if '"black"' not in drawing:
            raise SystemExit(f"logo-template-{form}.svg draws in no \"black\" this can turn white")
        for ink in ("black", "white"):
            svg = os.path.join(scratch, f"tray-{form}-{ink}.svg")
            with open(svg, "w", encoding="utf-8") as f:
                f.write(drawing.replace('"black"', f'"{ink}"'))
            out = os.path.join(scratch, f"tray-{form}-{ink}")
            _rasterize(svg, out, TRAY_PX)
            for px in TRAY_PX:
                shutil.copyfile(os.path.join(out, f"{px}x{px}.png"),
                                os.path.join(ICONS, f"tray-{form}-{ink}-{px}.png"))

    for svg, px in (("favicon.svg", 32), ("favicon-16.svg", 16)):
        out = os.path.join(scratch, svg)
        _rasterize(os.path.join(ASSETS, svg), out, (px,))
        shutil.copyfile(os.path.join(out, f"{px}x{px}.png"),
                        os.path.join(ASSETS, f"favicon-{px}.png"))


def main():
    with tempfile.TemporaryDirectory(prefix="lokki-icons-") as scratch:
        build(scratch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
