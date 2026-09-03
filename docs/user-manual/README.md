# Open-Motion user manuals

Three illustrated user manuals for the bloodflow app, one per feature level. Each
manual documents its level in full and only the *next* level reveals what it adds —
the Open-Motion manual doesn't reference Research features, and neither operator
manual explains how engineering mode is entered.

| Manual | Source | PDF |
|---|---|---|
| Open-Motion | `open-motion-user-manual.md` | `Open-Motion-User-Manual.pdf` |
| Open-Motion Research | `open-motion-user-manual-research.md` | `Open-Motion-User-Manual-Research.pdf` |
| Engineering mode | `open-motion-user-manual-engineering.md` | `Open-Motion-User-Manual-Engineering.pdf` |

- Screenshots in `img/` were captured 2026-07-17 from the app (version 1.4.0) running
  against live bench hardware (console 1.8.1-rc.0, sensors 1.8.2-dev.1), at the default
  1200×800 window size, dark theme.
- The PDFs are generated from the markdown with `python build_pdf.py` (needs
  `pip install markdown` and Microsoft Edge for headless printing). Re-run it after
  editing the markdown so the committed PDFs stay in sync.
- All three documents are marked **draft** — they are documentation previews, not
  validated IFU/controlled documents.
- The engineering manual deliberately does **not** contain the engineering password.
