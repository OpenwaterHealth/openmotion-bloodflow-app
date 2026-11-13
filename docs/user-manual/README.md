# Open-Motion Research user manual

Illustrated operator manual for **Open-Motion Research** (#371).

| Source | PDF (committed) | Build script |
|---|---|---|
| `open-motion-research-user-manual.md` + `img/` | `Open-Motion-Research-User-Manual.pdf` | `build_pdf.py` |

## Rules

1. **Research only.** This repository is public. The manual describes the Research
   build and never covers clinical or engineering mode; `build_pdf.py` renders this
   one source and nothing else. `tests/test_user_manual.py` enforces both.
2. **Keep it current with the app.**
   - Any change operators can see (components/, pages/, main.qml, error codes) should
     update the manual — and its screenshots if they change — in the same PR. The
     *User manual* workflow warns on a UI change that leaves the manual untouched.
   - The cover's **Application version** row names the release the content describes.
     Before a production tag, review the manual, update that row and rebuild the PDF:
     `release-build.yml` fails a production tag whose manual documents a different
     version (dev/rc tags only warn), before anything is signed. See `AGENTS.md`.
3. **Rebuild the PDF with its sources.** After editing the markdown, `img/` or
   `build_pdf.py`, run `python docs/user-manual/build_pdf.py` and commit the PDF in the
   same PR. The *User manual* workflow fails a PR that changes the sources without it.

## Build artifacts

- Every PR and push to `next` / `main` renders the PDF from the checked-out sources
  (`.github/workflows/user-manual.yml`) and uploads it as the workflow artifact
  `Open-Motion-Research-User-Manual-<git describe>`.
- Every release build uploads `Open-Motion-Research-User-Manual-<tag>` as a workflow
  artifact, and tagged releases attach `Open-Motion-Research-User-Manual-<tag>.pdf`
  to the GitHub Release. CI builds stamp a **Built from** row on the cover.

## Rebuilding locally

`python docs/user-manual/build_pdf.py` — needs `markdown` (installed on demand) and
Microsoft Edge, Google Chrome or Chromium for headless printing. Options:
`--out PATH`, `--version TEXT` (the *Built from* row), `--check-version TAG [--strict]`,
`--check-only`.

## Screenshots

`img/` holds 1200×800 captures of the Research build in its default **Liquid Glass** theme
against bench hardware, plus four crops (`rs-patterns`, `rs-custom`, `rs-continuous`,
`rs-dots-menu`). They cannot be generated in CI (they need the device), so refresh the
affected ones by hand when the UI changes. The current set was captured on 2026-09-30 from
the 1.5.4-dev.1 tag run from source (`python main.py --research`, fresh data folder) on a
bench running development firmware (console 1.8.2-dev.2, sensors 1.8.2-rc.4), which is what
`rs-settings-4.png` shows. The Output Folder field in `rs-settings-1.png` /
`rs-settings-2.png` is covered by an annotation because that run used a scratch data folder.
`rs-ready.png` is the unannotated source of `rs-anatomy.png`. Bench captures: the BFI/BVI
values are for illustration only.

The document is marked **draft**: a documentation preview, not a validated controlled
document.
