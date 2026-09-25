"""Render the Open-Motion Research user manual to a styled PDF.

The user manual is Research-only (#371): this script renders exactly one
source, ``open-motion-research-user-manual.md``. Clinical and engineering
manuals do not belong in this public repository.

Usage:
    python build_pdf.py
        Rebuild the committed PDF next to the markdown. Do this in the same
        commit as any change to the markdown, img/ or this script — the
        user-manual workflow fails a PR that changes them without the PDF.
    python build_pdf.py --version 1.6.0 --out build/manual/Manual-1.6.0.pdf
        CI build: stamp a "Built from" row on the cover and write elsewhere.
    python build_pdf.py --check-version 1.6.0 [--strict] [--check-only]
        Compare the manual's documented "Application version" with a release
        tag (dev/rc suffixes ignored). A mismatch is a warning, or an error
        with --strict (release-build.yml uses --strict on production tags).

Needs ``markdown`` (pip-installed on demand when rendering) and a Chromium
browser for headless printing: Microsoft Edge (preinstalled on Windows and on
GitHub's Windows runners) or Google Chrome / Chromium.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

DOCS = os.path.dirname(os.path.abspath(__file__))
MANUAL = "open-motion-research-user-manual.md"
PDF_NAME = "Open-Motion-Research-User-Manual.pdf"

BROWSER_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
BROWSER_NAMES = ["msedge", "microsoft-edge", "google-chrome", "google-chrome-stable",
                 "chromium", "chromium-browser"]

# The cover row that states which app version the manual's content describes.
VERSION_ROW = re.compile(r"^\| \*\*Application version\*\* \| *([^|]+?) *\|$", re.M)

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
.cover .subtitle { font-size: 15pt; font-weight: 600; color: #e67e22; margin: 0 0 14mm 0; }
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
/* The image and its caption share one paragraph; keep them on one page. */
p:has(> img) { break-inside: avoid; page-break-inside: avoid; }
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
  white-space: nowrap;
}
/* Section rules in the markdown are redundant with the page break before each
   h2, and the one after the TOC would otherwise land on a page of its own. */
hr { display: none; }
p { margin: 2.5mm 0; }
li { margin: 1.2mm 0; }
strong { color: #16202e; }
"""

TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title><style>{css}</style></head>
<body>{body}</body></html>"""


def find_browser():
    for path in BROWSER_PATHS:
        if os.path.exists(path):
            return path
    for name in BROWSER_NAMES:
        path = shutil.which(name)
        if path:
            return path
    raise SystemExit("No Microsoft Edge, Google Chrome or Chromium found for headless PDF printing.")


def documented_version(text):
    m = VERSION_ROW.search(text)
    if not m:
        raise SystemExit(f"{MANUAL}: the cover has no '| **Application version** | X.Y.Z |' row.")
    return m.group(1).strip()


def base_version(version):
    """'v1.6.0-rc.2' -> '1.6.0'."""
    version = version.strip()
    if version.startswith("v"):
        version = version[1:]
    return re.split(r"[-+]", version, maxsplit=1)[0]


def check_version(text, tag, strict):
    """True when the manual documents the tag's version (or the mismatch is only a warning)."""
    documented = documented_version(text)
    if base_version(documented) == base_version(tag):
        print(f"User manual documents {documented}; matches {tag}.")
        return True
    level = "error" if strict else "warning"
    message = (
        f"The user manual documents version {documented} but this build is {tag}. "
        f"Review docs/user-manual/{MANUAL} against the app, update its "
        "'Application version' row, and rebuild the PDF (python docs/user-manual/build_pdf.py)."
    )
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}")
    else:
        print(f"{level.upper()}: {message}")
    return not strict


def render(text, pdf_path, built_from=None):
    try:
        import markdown
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "markdown"])
        import markdown

    if built_from:
        text = VERSION_ROW.sub(
            lambda m: m.group(0) + f"\n| **Built from** | {built_from} |", text, count=1)
    # enable markdown processing inside the cover div
    text = text.replace('<div class="cover">', '<div class="cover" markdown="1">')
    # inject a TOC after the cover (first --- after the closing div)
    text = text.replace("</div>\n\n---\n", "</div>\n\n[TOC]\n\n---\n", 1)
    html_body = markdown.markdown(
        text,
        extensions=["extra", "toc", "sane_lists"],
        extension_configs={"toc": {"toc_depth": "2-2", "title": "Contents"}},
    )
    # The HTML sits next to the markdown so img/ paths resolve.
    html_path = os.path.join(DOCS, MANUAL.replace(".md", ".tmp.html"))
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(TEMPLATE.format(title="Open-Motion Research User Manual", css=CSS, body=html_body))

    pdf_path = os.path.abspath(pdf_path)
    os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
    if os.path.exists(pdf_path):
        os.remove(pdf_path)
    url = "file:///" + html_path.replace("\\", "/").lstrip("/")
    cmd = [find_browser(), "--headless", "--disable-gpu", "--no-pdf-header-footer",
           f"--print-to-pdf={pdf_path}", url]
    if sys.platform.startswith("linux"):
        cmd.insert(1, "--no-sandbox")
    try:
        subprocess.run(cmd, check=True, timeout=120)
        for _ in range(20):
            if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 10000:
                break
            time.sleep(0.5)
    finally:
        os.remove(html_path)
    if not os.path.exists(pdf_path):
        raise SystemExit(f"PDF was not written: {pdf_path}")
    print(f"{pdf_path}: {os.path.getsize(pdf_path) // 1024} KB")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help=f"PDF path (default: {PDF_NAME} next to the markdown)")
    ap.add_argument("--version", help="stamp a 'Built from' row on the cover")
    ap.add_argument("--check-version", metavar="TAG",
                    help="compare the documented 'Application version' with TAG")
    ap.add_argument("--strict", action="store_true",
                    help="a version mismatch is an error instead of a warning")
    ap.add_argument("--check-only", action="store_true", help="check, do not render")
    args = ap.parse_args(argv)

    with open(os.path.join(DOCS, MANUAL), encoding="utf-8") as fh:
        text = fh.read()
    ok = check_version(text, args.check_version, args.strict) if args.check_version else True
    if not ok:
        return 1
    if not args.check_only:
        render(text, args.out or os.path.join(DOCS, PDF_NAME), args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
