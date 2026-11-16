#!/usr/bin/python3
"""Build a dark PDF from a notes file: /usr/bin/python3 build.py notes.md out.pdf [--font path.woff2]

Notes format, one item per line:
  # Title                      document title
  plain text                   lead paragraph (under the title) or a paragraph
  ## Section                   section heading
  - bullet                     bullet; `F` in backticks renders as a key
  ![caption](/abs/shot.png)    full-width screenshot with caption
  ::: row  ...  :::            screenshots side by side (crops)
Images are converted to JPEG and embedded, so the PDF and HTML stand alone.
"""

import argparse
import base64
import html
import io
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).parent


def jpeg(path):
    im = Image.open(path).convert("RGB")
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=86)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def inline(text):
    text = html.escape(text)
    return re.sub(r"`([^`]+)`", r"<kbd>\1</kbd>", text)


def figure(m):
    return f'<figure><img src="{jpeg(m.group(2))}"><figcaption>{inline(m.group(1))}</figcaption></figure>'


def render(lines):
    out, bullets, row, seen_h1 = [], [], None, False

    def flush():
        if bullets:
            out.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for raw in lines:
        line = raw.strip()
        img = re.match(r"!\[(.*)\]\((.+)\)$", line)
        if line == "::: row":
            flush()
            row = []
            continue
        if line == ":::" and row is not None:
            out.append('<div class="row">' + "".join(row) + "</div>")
            row = None
            continue
        if row is not None:
            if img:
                row.append(figure(img))
            continue
        if not line:
            flush()
            continue
        if line.startswith("- "):
            bullets.append(inline(line[2:]))
            continue
        flush()
        if line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
            seen_h1 = True
        elif img:
            out.append(figure(img))
        else:
            out.append(
                f'<p class="{"lead" if seen_h1 and len(out) == 1 else ""}">{inline(line)}</p>'
            )
    flush()
    return "\n".join(out)


CSS = """
@page{size:Letter;margin:.5in .55in}
html,body{background:#0b0b0f;color:#e7e7ea;-webkit-print-color-adjust:exact;margin:0}
body{font:10.5pt/1.55 FONT,system-ui,sans-serif}
h1{font-size:22pt;color:#fff;margin:0 0 4pt}
h2{font-size:15pt;color:#fff;margin:26pt 0 6pt;padding-top:12pt;border-top:1px solid #2a2a30;break-after:avoid}
p{margin:0 0 10pt}p.lead{color:#a1a1aa}
ul{margin:6pt 0 12pt;padding-left:18pt;break-inside:avoid}li{margin:2pt 0}
figure{margin:10pt 0 16pt;break-inside:avoid}
img{display:block;max-width:100%;max-height:7.2in;border:1px solid #2a2a33}
figcaption{font-size:9pt;color:#a1a1aa;margin-top:5pt}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14pt;align-items:start}
kbd{font:8.5pt ui-monospace,monospace;border:1px solid #3a3a40;background:#16161c;padding:0 3pt}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("notes")
    ap.add_argument("pdf")
    ap.add_argument("--font")
    a = ap.parse_args()
    font = ""
    css = CSS.replace("FONT", "NotesFont" if a.font else "system-ui")
    if a.font:
        font = f"@font-face{{font-family:NotesFont;src:url('file://{Path(a.font).resolve()}') format('woff2-variations');font-weight:400 800}}"
    lines = Path(a.notes).read_text().splitlines()
    title = next((line[2:] for line in lines if line.startswith("# ")), "Release notes")
    page = f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title><style>{font}{css}</style></head><body>{render(lines)}</body></html>'
    html_path = Path(a.pdf).with_suffix(".html")
    html_path.write_text(page)
    subprocess.run(["node", str(HERE / "pdf.mjs"), str(html_path), a.pdf], check=True)
    print(a.pdf, html_path)


if __name__ == "__main__":
    sys.exit(main())
