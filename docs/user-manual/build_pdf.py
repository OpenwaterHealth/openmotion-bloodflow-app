"""Render the user-manual markdown files in this folder to styled PDFs.

Usage:  python build_pdf.py
Needs:  pip install markdown   (plus Microsoft Edge, used headless for printing)

The PDFs land next to the markdown sources. Screenshots live in img/ and were
captured from the app running against bench hardware (app 1.4.0).
"""
import os
import re
import subprocess
import sys
import time

try:
    import markdown
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "markdown"])
    import markdown

DOCS = os.path.dirname(os.path.abspath(__file__))
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
EDGE = next(p for p in EDGE_CANDIDATES if os.path.exists(p))

FILES = {
    "open-motion-user-manual-clinical.md": "Open-Motion-User-Manual-Clinical.pdf",
    "open-motion-user-manual-research.md": "Open-Motion-User-Manual-Research.pdf",
    "open-motion-user-manual-engineering.md": "Open-Motion-User-Manual-Engineering.pdf",
}

CSS = """
* { -webkit-print-color-adjust: exact; print-color-adjust: exact; box-sizing: border-box; }
@page { size: Letter; margin: 15mm 14mm 16mm 14mm; }
html { font-size: 10.5pt; }
body {
  font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
  color: #1c2430; line-height: 1.5; margin: 0;
}
/* ---------- cover ---------- */
.cover { text-align: center; padding-top: 46mm; page-break-after: always; }
.cover img.cover-logo {
  width: 300px; margin-bottom: 14mm; background: #16202e;
  padding: 20px 30px; border-radius: 12px; border: none;
}
.cover h1 { font-size: 30pt; margin: 0 0 4mm 0; color: #16202e; letter-spacing: 0.5px; border: none; }
.cover h2 {
  font-size: 15pt; font-weight: 600; color: #e67e22; margin: 0 0 14mm 0;
  page-break-before: avoid; border: none; padding: 0;
}
.cover table { margin: 0 auto 10mm auto; width: auto; min-width: 65%; }
.cover th { display: none; }
.cover td { text-align: left; }
.cover blockquote { max-width: 150mm; margin-left: auto; margin-right: auto; text-align: left; }
/* ---------- headings ---------- */
h1 { font-size: 20pt; color: #16202e; }
h2 {
  page-break-before: always; font-size: 15pt; color: #16202e;
  border-bottom: 2.5px solid #e67e22; padding-bottom: 2mm; margin-top: 2mm;
}
h3 { font-size: 12pt; color: #2b3a52; margin-top: 7mm; }
h2, h3 { page-break-after: avoid; }
/* ---------- toc ---------- */
.toc { page-break-after: always; padding-top: 8mm; }
.toc .toctitle { font-size: 16pt; font-weight: 700; color: #16202e; display: block; margin-bottom: 5mm; border-bottom: 2.5px solid #e67e22; padding-bottom: 2mm; }
.toc ul { list-style: none; padding-left: 0; margin: 0; }
.toc li { margin: 2.2mm 0; font-size: 11pt; }
.toc a { color: #1c2430; text-decoration: none; }
/* ---------- tables ---------- */
table { border-collapse: collapse; width: 100%; margin: 3mm 0 5mm 0; font-size: 9.5pt; }
th { background: #16202e; color: #ffffff; text-align: left; padding: 2mm 2.5mm; }
td { border-bottom: 1px solid #d8dde5; padding: 1.8mm 2.5mm; vertical-align: top; }
tr:nth-child(even) td { background: #f4f6f9; }
tr { page-break-inside: avoid; }
/* ---------- images & captions ---------- */
img { max-width: 100%; display: block; margin: 4mm auto 1.5mm auto; border: 1px solid #c9d0da; border-radius: 6px; page-break-inside: avoid; }
p > img:only-child { margin-bottom: 2mm; }
img + em, p em:only-child { display: block; text-align: center; }
p:has(> em:only-child) { text-align: center; color: #5a6675; font-size: 9pt; margin: 0 8mm 5mm 8mm; }
/* ---------- callouts ---------- */
blockquote {
  border-left: 4px solid #e67e22; background: #fdf3e7; color: #4a3a25;
  margin: 4mm 0; padding: 2.5mm 4mm; border-radius: 0 6px 6px 0; page-break-inside: avoid;
}
blockquote p { margin: 1mm 0; }
code {
  font-family: Consolas, 'Courier New', monospace; font-size: 9pt;
  background: #eef1f5; border-radius: 3px; padding: 0.3mm 1.2mm; color: #23415f;
}
hr { border: none; border-top: 1px solid #d8dde5; margin: 6mm 0; }
p { margin: 2.5mm 0; }
li { margin: 1.2mm 0; }
strong { color: #16202e; }
"""

TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title><style>{css}</style></head>
<body>{body}</body></html>"""


def build(md_name, pdf_name):
    path = os.path.join(DOCS, md_name)
    text = open(path, encoding="utf-8").read()
    # enable markdown processing inside the cover div
    text = text.replace('<div class="cover">', '<div class="cover" markdown="1">')
    # inject a TOC after the cover (first --- after the closing div)
    text = text.replace("</div>\n\n---\n", "</div>\n\n[TOC]\n\n---\n", 1)
    html_body = markdown.markdown(
        text,
        extensions=["extra", "toc", "sane_lists"],
        extension_configs={"toc": {"toc_depth": "2-2", "title": "Contents"}},
    )
    title = re.sub(r"open-motion-user-manual-(\w+).md", r"Open-Motion User Manual \1", md_name)
    html_path = os.path.join(DOCS, md_name.replace(".md", ".tmp.html"))
    open(html_path, "w", encoding="utf-8").write(TEMPLATE.format(title=title, css=CSS, body=html_body))

    pdf_path = os.path.join(DOCS, pdf_name)
    if os.path.exists(pdf_path):
        os.remove(pdf_path)
    url = "file:///" + html_path.replace("\\", "/")
    subprocess.run(
        [EDGE, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_path}", url],
        check=True, timeout=120,
    )
    for _ in range(20):
        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 10000:
            break
        time.sleep(0.5)
    os.remove(html_path)
    print(f"{pdf_name}: {os.path.getsize(pdf_path) // 1024} KB")


if __name__ == "__main__":
    for md, pdf in FILES.items():
        build(md, pdf)
    print("done")
