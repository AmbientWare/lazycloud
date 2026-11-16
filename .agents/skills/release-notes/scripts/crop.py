#!/usr/bin/python3
"""Crop a capture by CSS px: /usr/bin/python3 crop.py in.png out.png x y width height [--scale 2]
Captures are taken at 2x, so CSS coordinates are doubled unless --scale says otherwise."""

import sys

from PIL import Image

args = sys.argv[1:]
scale = 2
if "--scale" in args:
    i = args.index("--scale")
    scale = float(args[i + 1])
    del args[i : i + 2]
src, dst, x, y, w, h = args[0], args[1], *map(float, args[2:6])
box = tuple(int(v * scale) for v in (x, y, x + w, y + h))
Image.open(src).crop(box).save(dst)
print(dst, box)
