"""
verify_data.py — Compare a session stored in the database against its
                  source CSV files.

Usage
-----
    python verify_data.py <session_id> <scan_dir>
                          [--db data/sessions.sqlite]
                          [--tol 1e-4]
                          [--no-hist]
                          [--verbose]

What is checked
---------------
For each side that is present (left / right):

  1. ROW COUNT
     CSV rows  ==  DB session_raw rows for that side

  2. PER-ROW SCALAR FIELDS
     cam_id, frame_id, timestamp_s, temp, sum, tcm, tcl, pdc
     checked in CSV-row order (DB rows are fetched ordered by cam_id,
     frame_id to match the CSV layout).

  3. HISTOGRAM BLOBS (unless --no-hist)
     Every 1024-bin histogram from the CSV is compared to the unpacked
     BLOB in the DB.  Any mismatch is reported with row/bin detail.

  4. SESSION META
     The session_meta JSON must contain a key for each side that is
     present, and its "mask" value must match the mask decoded from the
     filename.

Exit codes
----------
  0  all checks passed
  1  one or more checks failed
  2  usage / file-not-found error
"""

import argparse
import json
import math
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from api.bfstorage import open_db, _unpack_hist
from api.session_samples import (
    SessionSamples,
    find_session_files,
    parse_session_csv_filename,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_pct(n, total):
    if total == 0:
        return "0/0"
    return f"{n}/{total} ({100*n/total:.1f}%)"


class _Reporter:
    """Accumulates PASS / FAIL lines and prints a final summary."""

    def __init__(self, verbose: bool):
        self.verbose  = verbose
        self.failures : list[str] = []
        self.checks   : int = 0

    def ok(self, msg: str):
        self.checks += 1
        if self.verbose:
            print(f"  PASS  {msg}")

    def fail(self, msg: str):
        self.checks += 1
        self.failures.append(msg)
        print(f"  FAIL  {msg}")

    def info(self, msg: str):
        print(f"        {msg}")

    @property
    def passed(self) -> bool:
        return len(self.failures) == 0


# ---------------------------------------------------------------------------
# Core comparison for one side
# ---------------------------------------------------------------------------

def _verify_side(
    conn,
    session_id: str,
    csv_path: str,
    side: str,
    tol: float,
    check_hist: bool,
    rep: _Reporter,
):
    label = f"[{side}]"

    # ── Load CSV ──────────────────────────────────────────────────────────
    ss = SessionSamples()
    n_csv = ss.read_csv(csv_path, side=side)
    rep.info(f"{label} CSV rows: {n_csv}, cameras: {ss.ncams}")

    # ── Load DB rows in insertion order (= CSV row order) ────────────────
    # frame_id is a hardware counter that wraps at 255, so sorting by it
    # would misalign rows.  rowid is the insertion sequence and always
    # matches the original CSV order.
    db_rows = conn.execute(
        """
        SELECT cam_id, frame_id, timestamp_s, hist, temp, sum, tcm, tcl, pdc
        FROM   session_raw
        WHERE  session_id = ? AND side = ?
        ORDER  BY rowid
        """,
        (session_id, side),
    ).fetchall()
    n_db = len(db_rows)

    # ── 1. Row count ──────────────────────────────────────────────────────
    if n_csv == n_db:
        rep.ok(f"{label} row count matches: {n_csv}")
    else:
        rep.fail(f"{label} row count mismatch: CSV={n_csv}, DB={n_db}")
        # Can't compare further if counts differ
        return

    # ── 2. Scalar fields + 3. Histograms ─────────────────────────────────
    scalar_mismatches = 0
    hist_mismatches   = 0
    first_scalar_fail = None
    first_hist_fail   = None

    FIELDS = ("cam_id", "frame_id", "timestamp_s", "temp", "sum", "tcm", "tcl", "pdc")

    for i in range(n_csv):
        csv_s  = ss.get(i)
        db_row = db_rows[i]

        # DB columns: cam_id[0] frame_id[1] timestamp_s[2] hist[3] temp[4] sum[5] tcm[6] tcl[7] pdc[8]
        csv_vals = [
            float(csv_s.cam_id),
            float(csv_s.frame_id),
            float(csv_s.timestamp),
            float(csv_s.temp),
            float(csv_s.summ),
            float(csv_s.tcm),
            float(csv_s.tcl),
            float(csv_s.pdc),
        ]
        db_vals = [
            float(db_row[0]),  # cam_id
            float(db_row[1]),  # frame_id
            float(db_row[2]),  # timestamp_s
            float(db_row[4]),  # temp
            float(db_row[5]),  # sum
            float(db_row[6]),  # tcm
            float(db_row[7]),  # tcl
            float(db_row[8]),  # pdc
        ]

        row_ok = True
        for fname, cv, dv in zip(FIELDS, csv_vals, db_vals):
            abs_diff = abs(cv - dv)
            # Use relative tolerance for large values, absolute for small
            ref = max(abs(cv), abs(dv), 1.0)
            if abs_diff / ref > tol:
                row_ok = False
                if first_scalar_fail is None:
                    first_scalar_fail = (i, fname, cv, dv, abs_diff)
        if not row_ok:
            scalar_mismatches += 1

        if check_hist:
            csv_hist = csv_s.hist.astype(np.uint32)
            db_hist  = _unpack_hist(db_row[3])
            if not np.array_equal(csv_hist, db_hist):
                hist_mismatches += 1
                if first_hist_fail is None:
                    bad_bins = np.where(csv_hist != db_hist)[0]
                    first_hist_fail = (i, bad_bins[:5], csv_hist[bad_bins[:5]], db_hist[bad_bins[:5]])

    # Report scalars
    if scalar_mismatches == 0:
        rep.ok(f"{label} all {n_csv} rows: scalar fields match (tol={tol})")
    else:
        rep.fail(
            f"{label} scalar mismatch in {_fmt_pct(scalar_mismatches, n_csv)} rows"
        )
        if first_scalar_fail:
            idx, fname, cv, dv, diff = first_scalar_fail
            rep.info(f"  first mismatch @ row {idx}: field={fname} CSV={cv} DB={dv} diff={diff:.3g}")

    # Report histograms
    if check_hist:
        if hist_mismatches == 0:
            rep.ok(f"{label} all {n_csv} histograms match exactly")
        else:
            rep.fail(
                f"{label} histogram mismatch in {_fmt_pct(hist_mismatches, n_csv)} rows"
            )
            if first_hist_fail:
                idx, bins, cvals, dvals = first_hist_fail
                rep.info(f"  first mismatch @ row {idx}: bins={list(bins)} CSV={list(cvals)} DB={list(dvals)}")


# ---------------------------------------------------------------------------
# Session meta check
# ---------------------------------------------------------------------------

def _verify_meta(conn, session_id: str, files: dict, rep: _Reporter):
    row = conn.execute(
        "SELECT session_meta FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()

    if row is None:
        rep.fail(f"session '{session_id}' not found in sessions table")
        return

    try:
        meta = json.loads(row[0] or "{}")
    except json.JSONDecodeError as exc:
        rep.fail(f"session_meta is not valid JSON: {exc}")
        return

    rep.info(f"session_meta = {json.dumps(meta)}")

    for side in ("left", "right"):
        csv_path = files[side]
        if csv_path is None:
            continue
        parsed = parse_session_csv_filename(csv_path)
        if side not in meta:
            rep.fail(f"session_meta missing key '{side}'")
            continue

        if parsed and "mask" in meta[side]:
            expected_mask = parsed["mask"]
            stored_mask   = meta[side]["mask"]
            if int(expected_mask) == int(stored_mask):
                rep.ok(f"[{side}] meta mask matches: {stored_mask} (0x{int(stored_mask):02X})")
            else:
                rep.fail(
                    f"[{side}] meta mask mismatch: "
                    f"filename={expected_mask} (0x{int(expected_mask):02X}), "
                    f"stored={stored_mask} (0x{int(stored_mask):02X})"
                )
        else:
            rep.ok(f"[{side}] meta key present")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Verify that a session in the DB matches its source CSV files.",
    )
    p.add_argument("session_id", help="Session ID, e.g. ow98NSF5")
    p.add_argument("scan_dir",   help="Directory containing the source CSV files")
    p.add_argument("--db",       default="data/sessions.sqlite",
                   help="SQLite database path (default: data/sessions.sqlite)")
    p.add_argument("--tol",      type=float, default=1e-4,
                   help="Relative tolerance for floating-point comparisons (default: 1e-4)")
    p.add_argument("--no-hist",  action="store_true",
                   help="Skip histogram blob comparison (faster)")
    p.add_argument("--verbose",  action="store_true",
                   help="Print PASS lines in addition to FAIL lines")
    return p


def main() -> int:
    args = build_parser().parse_args()

    # ── Sanity checks ─────────────────────────────────────────────────────
    if not os.path.isfile(args.db):
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2

    files = find_session_files(args.scan_dir, args.session_id)
    if not files["left"] and not files["right"]:
        print(
            f"ERROR: no CSV files found for session '{args.session_id}' "
            f"in '{args.scan_dir}'",
            file=sys.stderr,
        )
        return 2

    # ── Open DB ───────────────────────────────────────────────────────────
    conn = open_db(args.db)
    rep  = _Reporter(verbose=args.verbose)

    print(f"\n=== verify_data  session={args.session_id}  db={args.db} ===\n")

    # ── Session meta ──────────────────────────────────────────────────────
    print("--- Session meta ---")
    _verify_meta(conn, args.session_id, files, rep)
    print()

    # ── Per-side data ─────────────────────────────────────────────────────
    for side in ("left", "right"):
        csv_path = files[side]
        if csv_path is None:
            continue
        print(f"--- Side: {side}  ({os.path.basename(csv_path)}) ---")
        _verify_side(
            conn,
            session_id  = args.session_id,
            csv_path    = csv_path,
            side        = side,
            tol         = args.tol,
            check_hist  = not args.no_hist,
            rep         = rep,
        )
        print()

    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────
    total   = rep.checks
    n_fail  = len(rep.failures)
    n_pass  = total - n_fail

    print(f"=== Result: {n_pass}/{total} checks passed ===")
    if rep.passed:
        print("ALL CHECKS PASSED\n")
        return 0
    else:
        print(f"{n_fail} check(s) FAILED:\n")
        for f in rep.failures:
            print(f"  • {f}")
        print()
        return 1


if __name__ == "__main__":
    sys.exit(main())
