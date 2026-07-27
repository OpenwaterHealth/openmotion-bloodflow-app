#!/usr/bin/env python3
"""Regenerate assets/images/favicon.ico from assets/images/favicon.png.

Writes classic BMP/DIB frames for 16..128 px and a single PNG-compressed
256 px frame, which is the conventional layout Microsoft's icon guidance
describes. Run this rather than a plain `Image.save(..., format="ICO")`:
Pillow's default ICO writer emits PNG-compressed frames at *every* size,
so a casual regen silently changes the frame encoding of a shipped asset.

Note on history: this script was written for issue #223 on the theory
that the all-PNG icon was why the taskbar showed the generic Windows
icon. That theory was wrong — Windows 10/11 loads PNG frames fine at
every size, and the real cause was the Win32 window-class icon (see
scripts/win_icon_resource.py). The BMP layout is kept because it is what
now ships and is byte-for-byte reproducible from this script, not
because the shell requires it. Regenerating the icon is not a fix for
any icon bug; don't reach for it as one.

Usage:  python scripts/make_app_icon.py
"""

import io
import struct
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PNG = REPO_ROOT / "assets" / "images" / "favicon.png"
TARGET_ICO = REPO_ROOT / "assets" / "images" / "favicon.ico"

BMP_SIZES = [16, 24, 32, 48, 64, 128]  # classic DIB frames
PNG_SIZE = 256  # the one size where PNG compression is supported


def main() -> None:
    src = Image.open(SOURCE_PNG).convert("RGBA")

    # BMP-frame ICO for the small sizes (Pillow honors bitmap_format="bmp").
    bmp_buf = io.BytesIO()
    src.save(
        bmp_buf,
        format="ICO",
        sizes=[(s, s) for s in BMP_SIZES],
        bitmap_format="bmp",
    )
    bmp_ico = bmp_buf.getvalue()

    # 256px frame as PNG (LANCZOS to match Pillow's ICO downscaling).
    png_buf = io.BytesIO()
    src.resize((PNG_SIZE, PNG_SIZE), Image.Resampling.LANCZOS).save(
        png_buf, format="PNG"
    )
    png_frame = png_buf.getvalue()

    # Splice the PNG frame into the BMP-frame ICO: bump the directory entry
    # count, shift every image-data offset by one 16-byte directory entry,
    # and append the 256px entry + data at the end.
    count = struct.unpack("<H", bmp_ico[4:6])[0]
    out = bytearray(bmp_ico[:4])
    out += struct.pack("<H", count + 1)
    data_shift = 16  # one extra directory entry before all image data
    for i in range(count):
        entry = bytearray(bmp_ico[6 + i * 16 : 6 + (i + 1) * 16])
        offset = struct.unpack("<I", entry[12:16])[0]
        entry[12:16] = struct.pack("<I", offset + data_shift)
        out += entry
    # 256 encodes as 0 in the byte-wide width/height fields.
    out += struct.pack(
        "<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png_frame), len(bmp_ico) + data_shift
    )
    out += bmp_ico[6 + count * 16 :]
    out += png_frame

    TARGET_ICO.write_bytes(out)
    print(f"wrote {TARGET_ICO} ({len(out)} bytes, {count + 1} frames)")


if __name__ == "__main__":
    main()
