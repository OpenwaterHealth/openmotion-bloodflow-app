#!/usr/bin/env python3
"""
export_csv_cli.py - Command-line wrapper for export_session_to_csv.

Usage
-----
  python export_csv_cli.py <db_path> <session_id> <out_csv> [--no-hist]

Examples
--------
  python export_csv_cli.py sessions.sqlite 1 session_1.csv
  python export_csv_cli.py sessions.sqlite 2 session_2.csv --no-hist
  python export_csv_cli.py sessions.sqlite --list
"""

import argparse
import sys

from api.bfstorage import export_session_to_csv, open_db


def _list_sessions(db_path: str) -> None:
    """Print a table of all sessions in the database."""
    conn = open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT session_id, uid, started_at, notes, "
            "       (SELECT COUNT(*) FROM samples s WHERE s.session_id = ss.session_id) "
            "       AS sample_count "
            "FROM sessions ss "
            "ORDER BY session_id"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No sessions found.")
        return

    print(f"{'ID':>4}  {'Started at':>27}  {'Samples':>8}  Notes")
    print("-" * 70)
    for sid, uid, started_at, notes, count in rows:
        note_str = (notes or "")[:30]
        print(f"{sid:>4}  {started_at:>27}  {count:>8}  {note_str}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a session from the bloodflow SQLite database to CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("db_path", help="Path to the SQLite database file.")
    parser.add_argument(
        "session_id",
        nargs="?",
        type=int,
        help="session_id to export.  Omit when using --list.",
    )
    parser.add_argument(
        "out_csv",
        nargs="?",
        help="Output CSV file path.  Omit when using --list.",
    )
    parser.add_argument(
        "--no-hist",
        action="store_true",
        default=False,
        help="Exclude histogram columns (hist[0]..hist[1023]) from the output.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        default=False,
        help="List all sessions in the database and exit.",
    )

    args = parser.parse_args()

    if args.list:
        _list_sessions(args.db_path)
        return 0

    if args.session_id is None or args.out_csv is None:
        parser.error("session_id and out_csv are required unless --list is given.")

    rows = export_session_to_csv(
        db_path=args.db_path,
        session_id=args.session_id,
        out_csv_path=args.out_csv,
        include_hist=not args.no_hist,
    )

    if rows == 0:
        print(
            f"Warning: no samples found for session_id={args.session_id}.",
            file=sys.stderr,
        )
        return 1

    print(f"Exported {rows} row(s) to {args.out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
