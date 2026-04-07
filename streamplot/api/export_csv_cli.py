#!/usr/bin/env python3
"""
export_csv_cli.py — Export session data from the bloodflow SQLite database.

Subcommands
-----------
  list                         List all sessions in the database.
  raw   <session_id> <out>     Export session_raw (histogram frames) to CSV.
  data  <session_id> <out>     Export session_data (BF values) to CSV.

Examples
--------
  python api/export_csv_cli.py sessions.sqlite list
  python api/export_csv_cli.py sessions.sqlite raw  ow98NSF5 raw_out.csv
  python api/export_csv_cli.py sessions.sqlite raw  ow98NSF5 raw_out.csv --no-hist
  python api/export_csv_cli.py sessions.sqlite data ow98NSF5 data_out.csv
  python api/export_csv_cli.py sessions.sqlite data ow98NSF5 data_out.csv --side left
"""

import argparse
import sys

from api.bfstorage import (
    export_raw_to_csv,
    export_data_to_csv,
    list_sessions,
    open_db,
)


def _cmd_list(db_path: str) -> int:
    conn = open_db(db_path)
    try:
        sessions = list_sessions(conn)
    finally:
        conn.close()

    if not sessions:
        print("No sessions found.")
        return 0

    hdr = f"{'session_id':>12}  {'session_start':>20}  {'raw':>8}  {'data':>8}  notes"
    print(hdr)
    print("-" * len(hdr))
    for s in sessions:
        import datetime
        ts = datetime.datetime.fromtimestamp(s["session_start"]).strftime("%Y-%m-%d %H:%M:%S")
        notes = (s.get("session_notes") or "")[:30].replace("\n", " ")
        print(
            f"{s['session_id']:>12}  {ts:>20}  "
            f"{s['raw_count']:>8}  {s['data_count']:>8}  {notes}"
        )
    return 0


def _cmd_raw(db_path: str, session_id: str, out_csv: str,
             include_hist: bool, side: str | None) -> int:
    # Side filtering is done post-export by re-reading; for simplicity we
    # export all and let the user filter, OR we could pass side to the exporter.
    # Here we extend export_raw_to_csv with an optional side column filter via
    # a simple post-filter on the CSV — but to keep things clean we add side
    # support directly.
    from api.bfstorage import open_db as _open, _unpack_hist, _HIST_BINS
    import csv, os

    conn = _open(db_path)
    rows_written = 0
    fieldnames = [
        "session_id", "side", "cam_id", "frame_id", "timestamp_s",
        "temp", "sum", "tcm", "tcl", "pdc",
    ]
    if include_hist:
        fieldnames += [f"hist[{i}]" for i in range(_HIST_BINS)]

    try:
        sql = (
            "SELECT side, cam_id, frame_id, timestamp_s, hist, temp, sum, tcm, tcl, pdc "
            "FROM session_raw WHERE session_id=?"
        )
        params: list = [session_id]
        if side:
            sql += " AND side=?"
            params.append(side)
        sql += " ORDER BY rowid"

        cursor = conn.execute(sql, params)
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in cursor:
                side_v, cam_id, frame_id, ts, hist_blob, temp, summ, tcm, tcl, pdc = row
                d: dict = {
                    "session_id": session_id, "side": side_v,
                    "cam_id": int(cam_id), "frame_id": int(frame_id),
                    "timestamp_s": float(ts), "temp": float(temp),
                    "sum": int(summ), "tcm": float(tcm),
                    "tcl": float(tcl), "pdc": float(pdc),
                }
                if include_hist:
                    hist = _unpack_hist(hist_blob)
                    for i, v in enumerate(hist):
                        d[f"hist[{i}]"] = int(v)
                writer.writerow(d)
                rows_written += 1
    finally:
        conn.close()

    if rows_written == 0:
        print(
            f"Warning: no raw data found for session '{session_id}'.",
            file=sys.stderr,
        )
        return 1

    print(f"Exported {rows_written} raw row(s) to {out_csv}")
    return 0


def _cmd_data(db_path: str, session_id: str, out_csv: str,
              side: str | None) -> int:
    import csv

    conn = open_db(db_path)
    rows_written = 0
    fieldnames = ["session_id", "cam_id", "side", "time_s",
                  "bfi", "bvi", "contrast", "mean"]
    try:
        sql = (
            "SELECT cam_id, side, time_s, bfi, bvi, contrast, mean "
            "FROM session_data WHERE session_id=?"
        )
        params: list = [session_id]
        if side:
            sql += " AND side=?"
            params.append(side)
        sql += " ORDER BY time_s, cam_id"

        cursor = conn.execute(sql, params)
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in cursor:
                cam_id, side_v, time_s, bfi, bvi, contrast, mean = row
                writer.writerow({
                    "session_id": session_id, "cam_id": int(cam_id),
                    "side": side_v, "time_s": float(time_s),
                    "bfi": float(bfi), "bvi": float(bvi),
                    "contrast": float(contrast), "mean": float(mean),
                })
                rows_written += 1
    finally:
        conn.close()

    if rows_written == 0:
        print(
            f"Warning: no computed data found for session '{session_id}'.",
            file=sys.stderr,
        )
        return 1

    print(f"Exported {rows_written} data row(s) to {out_csv}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export bloodflow session data from SQLite to CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("db_path", help="Path to the SQLite database file.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── list ────────────────────────────────────────────────────────────────
    sub.add_parser("list", help="List all sessions.")

    # ── raw ─────────────────────────────────────────────────────────────────
    p_raw = sub.add_parser("raw", help="Export session_raw (histogram frames).")
    p_raw.add_argument("session_id", help="Alphanumeric session ID.")
    p_raw.add_argument("out_csv",    help="Output CSV file path.")
    p_raw.add_argument(
        "--no-hist", action="store_true", default=False,
        help="Omit the 1024 histogram columns (≈30× smaller file).",
    )
    p_raw.add_argument(
        "--side", choices=["left", "right"], default=None,
        help="Filter to one side only.",
    )

    # ── data ────────────────────────────────────────────────────────────────
    p_data = sub.add_parser("data", help="Export session_data (computed BF values).")
    p_data.add_argument("session_id", help="Alphanumeric session ID.")
    p_data.add_argument("out_csv",    help="Output CSV file path.")
    p_data.add_argument(
        "--side", choices=["left", "right"], default=None,
        help="Filter to one side only.",
    )

    args = parser.parse_args()

    if args.cmd == "list":
        return _cmd_list(args.db_path)

    if args.cmd == "raw":
        return _cmd_raw(
            args.db_path, args.session_id, args.out_csv,
            include_hist=not args.no_hist,
            side=args.side,
        )

    if args.cmd == "data":
        return _cmd_data(
            args.db_path, args.session_id, args.out_csv,
            side=args.side,
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
