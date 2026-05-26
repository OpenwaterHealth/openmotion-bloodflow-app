# Dark Correction Research

| Field | Value |
|-------|-------|
| **Status** | `prototype` |
| **Owner** | Ethan |
| **Created** | 2026-05-25 |
| **Target graduation** | exploratory |

## Description

Offline research comparing the current moment-based dark frame correction against full-histogram subtraction and deconvolution methods for OpenMotion histogram scans. The first target dataset is `scan_data/20260520_191204_owEENEJ6_left_maskF0_raw.csv`, with cameras 4-7 and camera 7 as the high-gain stress case.

## Run

```powershell
python sandbox/dark-correction-research/analyze_dark_correction.py `
  --csv scan_data/20260520_191204_owEENEJ6_left_maskF0_raw.csv `
  --cameras 4 5 6 7 `
  --dark-interval 600 `
  --output-dir sandbox/dark-correction-research/outputs/20260520_191204
```

## Outputs

The script writes compact CSV summaries, PNG plots, and `report.md` under the selected output directory. Raw multi-GB scan CSVs are inputs only and must not be copied or committed into this sandbox folder.
