# Open-Motion Research user manual

Illustrated operator manual for **Open-Motion Research 1.5.3** (#371).

| Source | PDF |
|---|---|
| `open-motion-research-user-manual.md` | `Open-Motion-Research-User-Manual.pdf` |

- A standalone manual for the Research build: Scan Settings (label, camera patterns,
  timed / continuous), Check, the per-camera plot grid and display options, History,
  Settings (raw CSV output, plot display, updates) and the full error reference.
  Engineering mode is not covered.
- Screenshots in `img/` were captured on 2026-09-25 from the Research build of the 1.5.3
  tag, run from source (`python main.py --research`) against openmotion-sdk 1.12.0 and
  bench hardware (console fw 1.8.1, sensor fw 1.8.2), at the default 1200×800 window
  size, dark theme. The run used a scratch data folder, so the Output Folder field in
  `rs-settings-1.png` / `rs-settings-2.png` is covered by an annotation label instead of a
  developer path.
- The bench sensors were not on a subject: scans were recorded on the right sensor's Far
  cameras only, and BFI/BVI values are not physiological.
- Rebuild the PDF after editing the markdown: `python build_pdf.py` (needs
  `pip install markdown` and Microsoft Edge for headless printing).
- The document is marked **draft**. It is a documentation preview, not a validated
  controlled document.
