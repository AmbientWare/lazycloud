#!/usr/bin/python3
"""Contact sheet for review: /usr/bin/python3 contact.py out.png <images or one pdf> [--cols N]
A PDF is rasterised page by page first, so one image shows the whole layout."""

import glob
import subprocess
import sys
import tempfile

from PIL import Image

args = sys.argv[1:]
cols = 2
if "--cols" in args:
    i = args.index("--cols")
    cols = int(args[i + 1])
    del args[i : i + 2]
out, inputs = args[0], args[1:]
if len(inputs) == 1 and inputs[0].endswith(".pdf"):
    tmp = tempfile.mkdtemp()
    subprocess.run(["pdftoppm", "-r", "40", "-png", inputs[0], f"{tmp}/p"], check=True)
    inputs = sorted(glob.glob(f"{tmp}/p-*.png"))
ims = [Image.open(p).convert("RGB") for p in inputs]
for im in ims:
    im.thumbnail((900 // cols, 1200))
w = max(i.width for i in ims)
h = max(i.height for i in ims)
rows = (len(ims) + cols - 1) // cols
sheet = Image.new("RGB", (cols * (w + 10), rows * (h + 10)), "white")
for k, im in enumerate(ims):
    sheet.paste(im, ((k % cols) * (w + 10), (k // cols) * (h + 10)))
sheet.save(out)
print(out, sheet.size)
