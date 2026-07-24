"""Test helpers, including a dependency-free deterministic PNG writer.

The writer produces byte-for-byte identical output for identical inputs, so the
same ``(width, height, rgb)`` always hashes to the same SHA-256 — exactly what a
visual-regression fixture needs.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_solid_png(
    path: str | Path,
    *,
    width: int = 8,
    height: int = 8,
    rgb: tuple[int, int, int] = (255, 0, 0),
) -> Path:
    """Write a solid-color RGB PNG at ``path`` and return the path.

    Deterministic: identical arguments yield identical bytes (fixed zlib level,
    no timestamp chunks).
    """
    r, g, b = rgb
    raw = bytearray()
    row = bytes([r, g, b]) * width
    for _ in range(height):
        raw.append(0)  # filter type 0 (None) per scanline
        raw.extend(row)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit, truecolor
    idat = zlib.compress(bytes(raw), level=9)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    return out
