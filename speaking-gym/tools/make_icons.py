#!/usr/bin/env python3
"""生成 PWA 图标（纯标准库画一个麦克风图标）。用法：python3 tools/make_icons.py"""
import math
import os
import struct
import zlib

TEAL = (15, 118, 110)
WHITE = (255, 255, 255)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "icons")


def png_chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png(path, size, pixel_fn):
    rows = bytearray()
    for y in range(size):
        rows += b"\x00"
        for x in range(size):
            rows += bytes(pixel_fn(x, y))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)))
        f.write(png_chunk(b"IDAT", zlib.compress(bytes(rows), 9)))
        f.write(png_chunk(b"IEND", b""))


def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def glyph(u, v):
    """返回该归一化坐标是否属于白色麦克风图形。"""
    if seg_dist(u, v, 0.5, 0.34, 0.5, 0.46) < 0.095:  # 话筒头（胶囊）
        return True
    d = math.hypot(u - 0.5, v - 0.47)
    if abs(d - 0.155) < 0.016 and v >= 0.47:  # U 型支架
        return True
    if abs(u - 0.5) < 0.015 and 0.625 <= v <= 0.70:  # 立杆
        return True
    if abs(u - 0.5) < 0.085 and 0.70 <= v <= 0.729:  # 底座
        return True
    return False


def rounded_bg(u, v, radius):
    dx = max(abs(u - 0.5) - (0.5 - radius), 0.0)
    dy = max(abs(v - 0.5) - (0.5 - radius), 0.0)
    return math.hypot(dx, dy) <= radius


def make(path, size, corner, pad):
    ss = 3  # 3x3 超采样抗锯齿
    def px(x, y):
        acc_bg = acc_g = 0
        for i in range(ss):
            for j in range(ss):
                u = (x + (i + 0.5) / ss) / size
                v = (y + (j + 0.5) / ss) / size
                if rounded_bg(u, v, corner):
                    acc_bg += 1
                    gu = (u - 0.5) * (1 + 2 * pad) + 0.5
                    gv = (v - 0.5) * (1 + 2 * pad) + 0.5
                    if glyph(gu, gv):
                        acc_g += 1
        n = ss * ss
        if acc_bg == 0:
            return (0, 0, 0, 0)
        t = acc_g / n
        r = int(TEAL[0] + (WHITE[0] - TEAL[0]) * t)
        g = int(TEAL[1] + (WHITE[1] - TEAL[1]) * t)
        b = int(TEAL[2] + (WHITE[2] - TEAL[2]) * t)
        return (r, g, b, int(255 * acc_bg / n))
    write_png(path, size, px)
    print("生成", path)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    make(os.path.join(OUT, "icon-192.png"), 192, 0.20, 0.0)
    make(os.path.join(OUT, "icon-512.png"), 512, 0.20, 0.0)
    make(os.path.join(OUT, "icon-maskable-512.png"), 512, 0.0, 0.12)
    make(os.path.join(OUT, "apple-touch-icon.png"), 180, 0.0, 0.0)
