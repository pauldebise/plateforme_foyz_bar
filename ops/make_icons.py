"""Génère les icônes PWA (192 et 512 px) sans dépendance externe.

    python -m ops.make_icons [dossier]

Dessine le « F négatif » de Brest : F violet sur fond lavande, aux formats
attendus par le manifeste (`app/static/icons/`). À relancer si le thème
change : les fichiers produits sont versionnés.
"""

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "app" / "static" / "icons"
BACKGROUND = (0xF5, 0xF0, 0xFB)
FOREGROUND = (0x80, 0x4D, 0xB3)
REFERENCE_SIZE = 512
# Géométrie du F dans une grille 512×512, en rectangles (x, y, largeur, hauteur).
F_RECTS = (
    (144, 112, 224, 112),  # barre supérieure
    (144, 224, 112, 176),  # hampe
    (256, 256, 64, 96),  # barre médiane
)


def _png(width, height, rows):
    raw = b"".join(b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in rows)

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
    scale = size / REFERENCE_SIZE
    background = (*BACKGROUND, 255)
    foreground = (*FOREGROUND, 255)
    rows = [[background] * size for _ in range(size)]
    for x, y, width, height in F_RECTS:
        x0 = round(x * scale)
        y0 = round(y * scale)
        x1 = round((x + width) * scale)
        y1 = round((y + height) * scale)
        for py in range(y0, y1):
            row = rows[py]
            for px in range(x0, x1):
                row[px] = foreground
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
