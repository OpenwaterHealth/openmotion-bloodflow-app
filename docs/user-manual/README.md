# Open-Motion user manual

Illustrated operator manual for the **Clinical build of Open-Motion 1.5.3** (#371).

| Source | PDF |
|---|---|
| `open-motion-user-manual.md` | `Open-Motion-User-Manual.pdf` |

- Written against the 1.5.3 tag. The Clinical build hides Scan Settings, Check, autoscale,
  raw-CSV output and app/firmware updates, so none of those appear; engineering mode is not
  mentioned at all.
- Screenshots in `img/` were captured on 2026-09-25 from the installed Clinical build
  running against bench hardware (console fw 1.8.1, sensor fw 1.8.2, SDK 1.12.0), at the
  default 1200×800 window size, dark theme unless noted. The installed build was
  **1.5.3-rc.2**, which differs from 1.5.3 only in the data folder (#581). Two
  screenshots show that: the Output Folder in `cl-settings-1.png` and the Application
  version in `cl-settings-3.png`. Both captions say so.
- The bench sensors were not on a subject: the left module had no contact, so contact
  warnings appear during scans and BFI/BVI values are not physiological.
- Rebuild the PDF after editing the markdown: `python build_pdf.py` (needs
  `pip install markdown` and Microsoft Edge for headless printing).
- The document is marked **draft**. It is a documentation preview, not a validated
  IFU/controlled document.
