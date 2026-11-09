"""Draws the app icons (a gauge ring on a dark tile) as PNGs with the standard library only. Run: py tools/make_icons.py"""
import math, struct, zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "web" / "icons"
BG, RING, DIM, ACC = (11, 15, 20), (52, 211, 153), (38, 50, 62), (245, 247, 250)


def png(path, size, pixel, height=None):
    h = height or size
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(size):
            raw.extend(pixel(x, y))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def make(size, scale, rounded, height=None, ss=3):
    """scale: share of the tile the ring may use (maskable icons keep it inside the 80% safe zone). height: a wide card."""
    hh = height or size
    cx, c = size / 2, hh / 2
    R = min(size, hh) * scale / 2
    W = R * 0.22
    def sample(px, py):
        if rounded:
            r = size * 0.22
            dx, dy = max(abs(px - cx) - (cx - r), 0), max(abs(py - c) - (c - r), 0)
            if dx * dx + dy * dy > r * r:
                return None
        d = math.hypot(px - cx, py - c)
        if abs(d - (R - W / 2)) <= W / 2:
            ang = (math.degrees(math.atan2(px - cx, -(py - c))) + 360) % 360      # 0 = top, clockwise
            a = (ang - 220) % 360                                                # gauge sweeps 280 deg, open at the bottom
            if a <= 280:
                return RING if a <= 190 else DIM
            return BG
        if d <= R * 0.18:
            return ACC
        return BG
    def pixel(x, y):
        acc, n, vis = [0, 0, 0], 0, 0
        for i in range(ss):
            for j in range(ss):
                v = sample(x + (i + .5) / ss, y + (j + .5) / ss)
                n += 1
                if v is not None:
                    vis += 1
                    acc = [acc[k] + v[k] for k in range(3)]
        if vis == 0:
            return BG                      # outside the rounded corner: dark, never transparent (PNG kept RGB)
        return tuple(round(a / vis) for a in acc)
    return pixel


OUT.mkdir(parents=True, exist_ok=True)
png(OUT / "icon-192.png", 192, make(192, 0.80, True))
png(OUT / "icon-512.png", 512, make(512, 0.80, True))
png(OUT / "icon-maskable-512.png", 512, make(512, 0.56, False))     # full-bleed, ring inside the safe zone
png(OUT / "apple-touch-icon.png", 180, make(180, 0.80, False))      # iOS rounds the corners itself
png(OUT / "social-card.png", 1200, make(1200, 0.62, False, height=630, ss=2), height=630)   # link previews (WhatsApp, Telegram ...)
print("icons written to", OUT)
