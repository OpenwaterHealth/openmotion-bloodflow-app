"""
stream_proto.py — Raw bloodflow data ingest: CSV → PyQtGraph + SQLite.

Pipeline
--------
  CSVProducer  (Thread)
       │  raw Sample objects, frame-metered at `period_s`
       ▼
  producer.out_q
       │
  DBConsumer  (Thread)  ← all SQLite writes happen here, never in the Qt thread
       │  puts (side, summ, temp) scalars onto plot_q
       ▼
  plot_q  (small, Qt main thread only)
       │
  QTimer → BFPlot.update_plot_data(summ, temp)

No dark-frame correction, no BFI/BVI computation.
All data is stored exactly as read from the CSV."

Usage
-----
    python stream_proto.py <scan_dir> <session_id>

    e.g.
    python stream_proto.py ../scan_data ow98NSF5

The scan_dir must contain files matching:
    scan_{session_id}_{date}_{time}_left_mask{hex}.csv
    scan_{session_id}_{date}_{time}_right_mask{hex}.csv   (optional)
    scan_{session_id}_{date}_{time}_notes.txt             (optional)
"""

import sys
import os
import time
import argparse
import logging
from queue import Queue, Empty
from threading import Thread, Event

from pyqtgraph.Qt import QtCore, QtWidgets
from PySide6.QtWidgets import QApplication

from ui.bfplot import BFPlot
from api.session_samples import (
    Sample, SessionSamples,
    parse_session_csv_filename, find_session_files,
)
from api.bfstorage import (
    open_db, create_session, close_session,
    RawWriter,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLOT_WINDOW  = 500   # rolling plot window (number of samples per camera)
SENTINEL     = object()
PLOT_Q_MAX   = 2_000   # max scalar tuples buffered for the Qt thread


# ---------------------------------------------------------------------------
# Producer: reads left/right CSVs, emits Samples frame-by-frame
# ---------------------------------------------------------------------------

class CSVProducer(Thread):
    """
    Reads one or two scan CSV files and emits Sample objects onto *out_q*,
    sleeping *period_s* between frames (all cameras emitted before the sleep).

    Attributes
    ----------
    out_q         : Queue[Sample | SENTINEL]
    ncams_left    : number of cameras in left CSV (0 if no left file)
    ncams_right   : number of cameras in right CSV (0 if no right file)
    """

    def __init__(
        self,
        left_csv: str | None,
        right_csv: str | None,
        period_s: float,
        stop_event: Event,
    ) -> None:
        super().__init__(daemon=True, name="csv-producer")
        self.period_s   = period_s
        self.stop_event = stop_event
        self.out_q: Queue = Queue(maxsize=20_000)

        self._left_ss  = SessionSamples()
        self._right_ss = SessionSamples()

        self._n_left  = self._left_ss.read_csv(left_csv,   side="left")  if left_csv  else 0
        self._n_right = self._right_ss.read_csv(right_csv, side="right") if right_csv else 0

        self.ncams_left  = self._left_ss.ncams  if self._n_left  else 0
        self.ncams_right = self._right_ss.ncams if self._n_right else 0

    def run(self) -> None:
        try:
            ncl     = max(self.ncams_left,  1)
            ncr     = max(self.ncams_right, 1)
            nfl     = self._n_left  // ncl
            nfr     = self._n_right // ncr
            nframes = max(nfl, nfr)

            for frame_idx in range(nframes):
                if self.stop_event.is_set():
                    break
                for cam_idx in range(self.ncams_left):
                    row_idx = frame_idx * self.ncams_left + cam_idx
                    if row_idx < self._n_left:
                        self.out_q.put(self._left_ss.get(row_idx))
                for cam_idx in range(self.ncams_right):
                    row_idx = frame_idx * self.ncams_right + cam_idx
                    if row_idx < self._n_right:
                        self.out_q.put(self._right_ss.get(row_idx))
                time.sleep(self.period_s)

            self.out_q.put(SENTINEL)
        except Exception as exc:
            log.exception("CSVProducer error: %s", exc)
            self.out_q.put(SENTINEL)


# ---------------------------------------------------------------------------
# DB consumer: reads raw samples, writes to SQLite, feeds plot queue
# ---------------------------------------------------------------------------

class DBConsumer(Thread):
    """
    Reads Sample objects from *raw_q* on its own thread.
    Submits every sample to RawWriter (background batch SQLite writes).
    Puts lightweight ``(side, summ, temp)`` tuples onto *plot_q* for the
    Qt timer — the Qt main thread never touches SQLite.
    """

    def __init__(
        self,
        raw_q: Queue,
        plot_q: Queue,
        db_path: str,
        session_id: str,
        stop_event: Event,
    ) -> None:
        super().__init__(daemon=True, name="db-consumer")
        self.raw_q      = raw_q
        self.plot_q     = plot_q
        self.db_path    = db_path
        self.session_id = session_id
        self.stop_event = stop_event

    def run(self) -> None:
        raw_writer = RawWriter(self.db_path, self.session_id)
        try:
            while not self.stop_event.is_set():
                try:
                    sample = self.raw_q.get(timeout=0.5)
                except Empty:
                    continue

                if sample is SENTINEL:
                    # Forward sentinel so Qt timer knows streaming is done
                    self.plot_q.put(SENTINEL)
                    break

                # DB write (background batch, non-blocking for this thread)
                raw_writer.submit(sample)

                # Put lightweight scalars onto plot_q; drop if Qt is falling behind
                try:
                    self.plot_q.put_nowait((sample.side, float(sample.summ), float(sample.temp)))
                except Exception:
                    pass  # plot_q full — skip this sample, keep streaming

        except Exception as exc:
            log.exception("DBConsumer error: %s", exc)
            self.plot_q.put(SENTINEL)
        finally:
            raw_writer.close()
            log.info("DBConsumer: writer closed.")


# ---------------------------------------------------------------------------
# Qt application / main
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ingest bloodflow CSV → PyQtGraph display + SQLite storage (no processing).",
    )
    p.add_argument(
        "scan_dir",
        help="Directory containing the scan CSV files.",
    )
    p.add_argument(
        "session_id",
        help="Session ID (alphanumeric part of filename, e.g. ow98NSF5).",
    )
    p.add_argument(
        "--db", default="data/sessions.sqlite",
        help="SQLite database path (default: data/sessions.sqlite).",
    )
    p.add_argument(
        "--period", type=float, default=0.025,
        help="Simulated inter-frame delay in seconds (default: 0.025 = 40 Hz).",
    )
    p.add_argument(
        "--plot-size", type=int, default=PLOT_WINDOW,
        help="Number of samples in the rolling plot window per camera.",
    )
    return p


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
    )

    args = build_arg_parser().parse_args()

    # ── Locate session files ────────────────────────────────────────────────
    files = find_session_files(args.scan_dir, args.session_id)
    if not files["left"] and not files["right"]:
        print(
            f"ERROR: no scan files found for session '{args.session_id}' "
            f"in '{args.scan_dir}'",
            file=sys.stderr,
        )
        return 1

    # ── Read notes.txt ──────────────────────────────────────────────────────
    notes_text = ""
    if files["notes"]:
        try:
            with open(files["notes"], "r", encoding="utf-8", errors="replace") as fh:
                notes_text = fh.read()
        except OSError:
            pass

    # ── Build session meta from filenames ──────────────────────────────────
    #   meta format: { "left": {"mask": N}, "right": {"mask": N} }
    #   only the sides that are actually present are included.
    session_meta: dict = {}
    session_start = time.time()

    for side_key in ("left", "right"):
        path = files[side_key]
        if not path:
            continue
        parsed = parse_session_csv_filename(path)
        if parsed:
            session_meta[side_key] = {"mask": parsed["mask"]}
            if side_key == "left":                # use left (or right if no left) for start ts
                session_start = parsed["datetime"].timestamp()
        else:
            session_meta[side_key] = {}

    if not session_meta and files["right"]:
        parsed = parse_session_csv_filename(files["right"])
        if parsed:
            session_start = parsed["datetime"].timestamp()

    # ── Create DB session record (open connection, write, close) ───────────
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    conn = open_db(args.db)
    create_session(
        conn,
        session_id    = args.session_id,
        session_start = session_start,
        session_notes = notes_text or None,
        session_meta  = session_meta,
    )
    conn.close()
    log.info("Session '%s' created in %s  meta=%s", args.session_id, args.db, session_meta)

    # ── Qt window ──────────────────────────────────────────────────────────
    stop_event = Event()
    app        = QtWidgets.QApplication(sys.argv)

    win = QtWidgets.QMainWindow()
    win.setWindowTitle(f"OpenMotion BF — {args.session_id}")
    win.resize(820, 500)

    central      = QtWidgets.QWidget()
    outer_layout = QtWidgets.QVBoxLayout(central)
    outer_layout.setSpacing(2)
    win.setCentralWidget(central)

    plot_layout = QtWidgets.QVBoxLayout()
    plot_layout.setSpacing(0)

    left_plot  = BFPlot("Left  sum/temp",  plot_layout, args.plot_size) if files["left"]  else None
    right_plot = BFPlot("Right sum/temp", plot_layout, args.plot_size) if files["right"] else None

    outer_layout.addLayout(plot_layout)

    status_label = QtWidgets.QLabel("Loading…")
    outer_layout.addWidget(status_label)

    # ── Build pipeline threads ─────────────────────────────────────────────
    producer = CSVProducer(
        left_csv   = files["left"],
        right_csv  = files["right"],
        period_s   = args.period,
        stop_event = stop_event,
    )

    # plot_q carries only lightweight (side, summ, temp) tuples to Qt
    plot_q: Queue = Queue(maxsize=PLOT_Q_MAX)

    db_consumer = DBConsumer(
        raw_q      = producer.out_q,
        plot_q     = plot_q,
        db_path    = args.db,
        session_id = args.session_id,
        stop_event = stop_event,
    )

    frame_counter = [0]
    done          = [False]

    def poll_plot_q() -> None:
        """Qt main thread: drain scalars from plot_q and update plots only."""
        if done[0]:
            return
        try:
            while True:
                item = plot_q.get_nowait()

                if item is SENTINEL:
                    done[0] = True
                    timer.stop()
                    db_conn = open_db(args.db)
                    close_session(db_conn, args.session_id, time.time())
                    db_conn.close()
                    status_label.setText(
                        f"Done — {frame_counter[0]} samples stored in {args.db}"
                    )
                    log.info("Session '%s' complete.", args.session_id)
                    return

                side, summ, temp = item
                if side == "left" and left_plot:
                    left_plot.update_plot_data(summ, temp)
                elif side == "right" and right_plot:
                    right_plot.update_plot_data(summ, temp)
                frame_counter[0] += 1

        except Empty:
            pass

        status_label.setText(
            f"Samples plotted: {frame_counter[0]}  |  "
            f"Producer queue: {producer.out_q.qsize()}"
        )

    timer = QtCore.QTimer()
    timer.timeout.connect(poll_plot_q)
    timer.start(int(args.period * 1000))

    # ── Start threads ──────────────────────────────────────────────────────
    producer.start()
    db_consumer.start()

    win.show()

    try:
        exit_code = app.exec()
    except Exception as exc:
        log.exception("Qt event loop error: %s", exc)
        exit_code = 1
    finally:
        stop_event.set()
        timer.stop()
        try:
            producer.out_q.put_nowait(SENTINEL)
        except Exception:
            pass
        producer.join(timeout=5)
        db_consumer.join(timeout=10)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
