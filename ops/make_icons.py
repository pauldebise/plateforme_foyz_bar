"""Génère les icônes PWA (192 et 512 px) sans dépendance externe.

    python -m ops.make_icons [dossier]

Dessine « F&B » (police bitmap 5×7) blanc sur fond violet, aux formats
attendus par le manifeste (`app/static/icons/`). À relancer si le thème
change : les fichiers produits sont versionnés.
"""

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "app" / "static" / "icons"
BACKGROUND = (0x80, 0x4D, 0xB3)  # thème public par défaut
FOREGROUND = (0xFF, 0xFF, 0xFF)

GLYPHS = {
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "&": ("01100", "10010", "10010", "01100", "10101", "10010", "01101"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
}
TEXT = "F&B"


def _png(width, height, rows):
    raw = b"".join(
        b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in rows
    )

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def render(size):
    glyph_width, glyph_height, spacing = 5, 7, 1
    columns = len(TEXT) * glyph_width + (len(TEXT) - 1) * spacing
    scale = max(1, (size * 4 // 5) // columns)  # marge de sécurité de 20 %
    text_width = columns * scale
    text_height = glyph_height * scale
    x0 = (size - text_width) // 2
    y0 = (size - text_height) // 2
    background = tuple(BACKGROUND) + (255,)
    foreground = tuple(FOREGROUND) + (255,)
    rows = [[background] * size for _ in range(size)]
    for index, char in enumerate(TEXT):
        glyph = GLYPHS[char]
        gx = x0 + index * (glyph_width + spacing) * scale
        for gy, line in enumerate(glyph):
            for px, bit in enumerate(line):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        rows[y0 + gy * scale + dy][gx + px * scale + dx] = foreground
    return _png(size, size, rows)


def main(argv=None):
    argv = argv or sys.argv[1:]
    directory = Path(argv[0]) if argv else DEFAULT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        target = directory / f"icon-{size}.png"
        target.write_bytes(render(size))
        print(f"{target} ({target.stat().st_size} o)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
