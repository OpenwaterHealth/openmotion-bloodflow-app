"""
db_viewer.py — Interactive viewer for the OpenMotion bloodflow SQLite database.

Usage
-----
    python ui/db_viewer.py                          # prompts for DB via file dialog
    python ui/db_viewer.py --db data/sessions.sqlite

Layout
------
  Left panel   : session list table (session_id, start, end, raw rows, data rows, notes)
  Right top    : BFI / BVI time-series plot (session_data) — per camera, left/right tabs
  Right middle : histogram viewer (session_raw) — single frame, camera selector
  Right bottom : raw data table (paged, 500 rows at a time)

Toolbar actions: Open DB, Export Raw CSV, Export Data CSV, Refresh.
"""

import argparse
import datetime
import json
import os
import sys

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets, QtGui

# ---------------------------------------------------------------------------
# Resolve project root so imports work however the script is invoked
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from api.bfstorage import open_db, list_sessions, export_raw_to_csv, export_data_to_csv
from api.bfstorage import _unpack_hist  # internal helper, safe to use here


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts):
    if ts is None:
        return ""
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _load_sessions(conn):
    return list_sessions(conn)


def _load_data_rows(conn, session_id, side=None):
    sql = (
        "SELECT cam_id, side, time_s, bfi, bvi, contrast, mean "
        "FROM session_data WHERE session_id=?"
    )
    params = [session_id]
    if side:
        sql += " AND side=?"
        params.append(side)
    sql += " ORDER BY time_s, cam_id"
    return conn.execute(sql, params).fetchall()


def _load_raw_page(conn, session_id, side=None, offset=0, limit=500):
    sql = (
        "SELECT side, cam_id, frame_id, timestamp_s, temp, sum, tcm, tcl, pdc "
        "FROM session_raw WHERE session_id=?"
    )
    params = [session_id]
    if side:
        sql += " AND side=?"
        params.append(side)
    sql += f" ORDER BY rowid LIMIT {limit} OFFSET {offset}"
    return conn.execute(sql, params).fetchall()


def _raw_count(conn, session_id, side=None):
    sql = "SELECT COUNT(*) FROM session_raw WHERE session_id=?"
    params = [session_id]
    if side:
        sql += " AND side=?"
        params.append(side)
    return conn.execute(sql, params).fetchone()[0]


def _load_hist_frame(conn, session_id, cam_id, side, frame_idx):
    """Return the hist BLOB for a specific frame index (by rowid ordering)."""
    sql = (
        "SELECT hist FROM session_raw "
        "WHERE session_id=? AND cam_id=? AND side=? "
        "ORDER BY rowid LIMIT 1 OFFSET ?"
    )
    row = conn.execute(sql, (session_id, cam_id, side, frame_idx)).fetchone()
    if row is None:
        return None
    return _unpack_hist(row[0])


def _cameras_for_session(conn, session_id, side=None):
    sql = "SELECT DISTINCT cam_id FROM session_raw WHERE session_id=?"
    params = [session_id]
    if side:
        sql += " AND side=?"
        params.append(side)
    sql += " ORDER BY cam_id"
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def _raw_frame_count(conn, session_id, cam_id, side):
    row = conn.execute(
        "SELECT COUNT(*) FROM session_raw WHERE session_id=? AND cam_id=? AND side=?",
        (session_id, cam_id, side),
    ).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Session list widget
# ---------------------------------------------------------------------------

class SessionTable(QtWidgets.QTableWidget):
    session_selected = QtCore.Signal(str)   # emits session_id

    _HEADERS = ["Session ID", "Start", "End", "Raw rows", "Data rows", "Notes"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(self._HEADERS))
        self.setHorizontalHeaderLabels(self._HEADERS)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.horizontalHeader().setStretchLastSection(True)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.itemSelectionChanged.connect(self._on_selection)

    def populate(self, sessions):
        self.blockSignals(True)
        self.setRowCount(0)
        for s in sessions:
            row = self.rowCount()
            self.insertRow(row)
            self.setItem(row, 0, QtWidgets.QTableWidgetItem(str(s["session_id"])))
            self.setItem(row, 1, QtWidgets.QTableWidgetItem(_fmt_ts(s["session_start"])))
            self.setItem(row, 2, QtWidgets.QTableWidgetItem(_fmt_ts(s["session_end"])))
            self.setItem(row, 3, QtWidgets.QTableWidgetItem(str(s["raw_count"])))
            self.setItem(row, 4, QtWidgets.QTableWidgetItem(str(s["data_count"])))
            notes = (s.get("session_notes") or "").replace("\n", " ")[:80]
            self.setItem(row, 5, QtWidgets.QTableWidgetItem(notes))
        self.resizeColumnsToContents()
        self.blockSignals(False)

    def _on_selection(self):
        rows = self.selectedItems()
        if rows:
            sid = self.item(rows[0].row(), 0).text()
            self.session_selected.emit(sid)


# ---------------------------------------------------------------------------
# BFI / BVI plot panel
# ---------------------------------------------------------------------------

class BFPlotPanel(QtWidgets.QWidget):
    """Plots BFI and BVI traces for all cameras in a session."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Side:"))
        self._side_combo = QtWidgets.QComboBox()
        self._side_combo.addItems(["left", "right", "both"])
        self._side_combo.currentTextChanged.connect(self._refresh)
        controls.addWidget(self._side_combo)

        controls.addWidget(QtWidgets.QLabel("Metric:"))
        self._metric_combo = QtWidgets.QComboBox()
        self._metric_combo.addItems(["bfi", "bvi", "contrast", "mean"])
        self._metric_combo.currentTextChanged.connect(self._refresh)
        controls.addWidget(self._metric_combo)
        controls.addStretch()
        layout.addLayout(controls)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("k")
        self._plot.showGrid(x=True, y=True)
        self._plot.addLegend()
        layout.addWidget(self._plot)

        self._conn = None
        self._session_id = None
        self._data = []

    _COLORS = [
        "r", "g", "b", "y", "c", "m", "w",
        (255, 128, 0), (0, 255, 128), (128, 0, 255),
        (255, 64, 64), (64, 255, 64), (64, 64, 255), (200, 200, 0),
        (0, 200, 200), (200, 0, 200),
    ]

    def load(self, conn, session_id):
        self._conn = conn
        self._session_id = session_id
        self._refresh()

    def _refresh(self):
        if self._conn is None or self._session_id is None:
            return
        side_sel = self._side_combo.currentText()
        metric   = self._metric_combo.currentText()
        side_filter = None if side_sel == "both" else side_sel
        rows = _load_data_rows(self._conn, self._session_id, side_filter)
        self._plot.clear()
        self._plot.setLabel("left", metric.upper())
        self._plot.setLabel("bottom", "time (s)")

        # Group by (cam_id, side)
        groups: dict = {}
        for cam_id, side, time_s, bfi, bvi, contrast, mean in rows:
            key = (int(cam_id), side)
            if key not in groups:
                groups[key] = ([], [])
            val = {"bfi": bfi, "bvi": bvi, "contrast": contrast, "mean": mean}[metric]
            groups[key][0].append(float(time_s))
            groups[key][1].append(float(val))

        for idx, ((cam_id, side), (xs, ys)) in enumerate(sorted(groups.items())):
            color = self._COLORS[idx % len(self._COLORS)]
            pen = pg.mkPen(color, width=1)
            label = f"cam{cam_id} {side}"
            self._plot.plot(xs, ys, pen=pen, name=label)


# ---------------------------------------------------------------------------
# Histogram viewer
# ---------------------------------------------------------------------------

class HistogramPanel(QtWidgets.QWidget):
    """Shows a single raw histogram frame as a bar chart."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Side:"))
        self._side_combo = QtWidgets.QComboBox()
        self._side_combo.addItems(["left", "right"])
        self._side_combo.currentTextChanged.connect(self._on_side_changed)
        controls.addWidget(self._side_combo)

        controls.addWidget(QtWidgets.QLabel("Camera:"))
        self._cam_combo = QtWidgets.QComboBox()
        self._cam_combo.currentIndexChanged.connect(self._refresh)
        controls.addWidget(self._cam_combo)

        controls.addWidget(QtWidgets.QLabel("Frame:"))
        self._frame_spin = QtWidgets.QSpinBox()
        self._frame_spin.setMinimum(0)
        self._frame_spin.valueChanged.connect(self._refresh)
        controls.addWidget(self._frame_spin)

        self._count_label = QtWidgets.QLabel("")
        controls.addWidget(self._count_label)
        controls.addStretch()
        layout.addLayout(controls)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("k")
        self._plot.setLabel("bottom", "bin")
        self._plot.setLabel("left", "count")
        self._bars = pg.BarGraphItem(x=np.arange(1024), height=np.zeros(1024), width=1,
                                     brush="steelblue")
        self._plot.addItem(self._bars)
        layout.addWidget(self._plot)

        self._conn = None
        self._session_id = None

    def load(self, conn, session_id):
        self._conn = conn
        self._session_id = session_id
        self._on_side_changed()

    def _on_side_changed(self):
        if self._conn is None or self._session_id is None:
            return
        side = self._side_combo.currentText()
        cams = _cameras_for_session(self._conn, self._session_id, side)
        self._cam_combo.blockSignals(True)
        self._cam_combo.clear()
        for c in cams:
            self._cam_combo.addItem(str(c), userData=c)
        self._cam_combo.blockSignals(False)
        self._refresh()

    def _refresh(self):
        if self._conn is None or self._session_id is None:
            return
        side = self._side_combo.currentText()
        idx  = self._cam_combo.currentIndex()
        if idx < 0:
            return
        cam_id = self._cam_combo.itemData(idx)
        nframes = _raw_frame_count(self._conn, self._session_id, cam_id, side)
        self._frame_spin.setMaximum(max(0, nframes - 1))
        self._count_label.setText(f"/ {nframes} frames")
        frame_idx = self._frame_spin.value()
        hist = _load_hist_frame(self._conn, self._session_id, cam_id, side, frame_idx)
        if hist is not None:
            self._bars.setOpts(x=np.arange(1024), height=hist.astype(float))


# ---------------------------------------------------------------------------
# Raw data table (paged)
# ---------------------------------------------------------------------------

class RawTablePanel(QtWidgets.QWidget):
    _HEADERS = ["side", "cam_id", "frame_id", "timestamp_s",
                "temp", "sum", "tcm", "tcl", "pdc"]
    _PAGE = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Side:"))
        self._side_combo = QtWidgets.QComboBox()
        self._side_combo.addItems(["both", "left", "right"])
        self._side_combo.currentTextChanged.connect(self._reset_page)
        controls.addWidget(self._side_combo)

        self._prev_btn = QtWidgets.QPushButton("< Prev")
        self._next_btn = QtWidgets.QPushButton("Next >")
        self._prev_btn.clicked.connect(self._prev_page)
        self._next_btn.clicked.connect(self._next_page)
        self._page_label = QtWidgets.QLabel("rows 0–0 of 0")
        controls.addWidget(self._prev_btn)
        controls.addWidget(self._page_label)
        controls.addWidget(self._next_btn)
        controls.addStretch()
        layout.addLayout(controls)

        self._table = QtWidgets.QTableWidget()
        self._table.setColumnCount(len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table)

        self._conn = None
        self._session_id = None
        self._offset = 0
        self._total = 0

    def load(self, conn, session_id):
        self._conn = conn
        self._session_id = session_id
        self._reset_page()

    def _reset_page(self):
        self._offset = 0
        self._load_page()

    def _prev_page(self):
        self._offset = max(0, self._offset - self._PAGE)
        self._load_page()

    def _next_page(self):
        if self._offset + self._PAGE < self._total:
            self._offset += self._PAGE
        self._load_page()

    def _load_page(self):
        if self._conn is None or self._session_id is None:
            return
        side_sel = self._side_combo.currentText()
        side = None if side_sel == "both" else side_sel
        self._total = _raw_count(self._conn, self._session_id, side)
        rows = _load_raw_page(self._conn, self._session_id, side,
                              self._offset, self._PAGE)
        self._table.setRowCount(0)
        for row_data in rows:
            r = self._table.rowCount()
            self._table.insertRow(r)
            for col, val in enumerate(row_data):
                text = f"{val:.4g}" if isinstance(val, float) else str(val)
                self._table.setItem(r, col, QtWidgets.QTableWidgetItem(text))
        self._table.resizeColumnsToContents()
        end = min(self._offset + self._PAGE, self._total)
        self._page_label.setText(f"rows {self._offset + 1}–{end} of {self._total}")
        self._prev_btn.setEnabled(self._offset > 0)
        self._next_btn.setEnabled(end < self._total)


# ---------------------------------------------------------------------------
# Session detail panel (tabs: plot, histogram, raw table, meta)
# ---------------------------------------------------------------------------

class SessionDetailPanel(QtWidgets.QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._bf_plot = BFPlotPanel()
        self._hist    = HistogramPanel()
        self._raw_tbl = RawTablePanel()
        self._meta_edit = QtWidgets.QPlainTextEdit()
        self._meta_edit.setReadOnly(True)

        self.addTab(self._bf_plot,    "BFI / BVI")
        self.addTab(self._hist,       "Histogram")
        self.addTab(self._raw_tbl,    "Raw data")
        self.addTab(self._meta_edit,  "Session info")

    def load(self, conn, session_id, session_row):
        self._bf_plot.load(conn, session_id)
        self._hist.load(conn, session_id)
        self._raw_tbl.load(conn, session_id)
        # Populate meta tab
        try:
            meta = json.loads(session_row.get("session_meta") or "{}")
            meta_str = json.dumps(meta, indent=2)
        except Exception:
            meta_str = str(session_row.get("session_meta", ""))
        notes = session_row.get("session_notes") or ""
        text = (
            f"Session ID  : {session_row['session_id']}\n"
            f"Start       : {_fmt_ts(session_row['session_start'])}\n"
            f"End         : {_fmt_ts(session_row['session_end'])}\n"
            f"Raw rows    : {session_row['raw_count']}\n"
            f"Data rows   : {session_row['data_count']}\n"
            f"\nNotes\n-----\n{notes}\n"
            f"\nMeta (JSON)\n-----------\n{meta_str}\n"
        )
        self._meta_edit.setPlainText(text)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class DBViewer(QtWidgets.QMainWindow):
    def __init__(self, db_path=None):
        super().__init__()
        self.setWindowTitle("OpenMotion BF — Database Viewer")
        self.resize(1400, 820)
        self._conn = None
        self._db_path = None
        self._sessions = []

        self._build_ui()
        self._build_toolbar()

        if db_path:
            self._open_db(db_path)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        # Left: session list
        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(QtWidgets.QLabel("<b>Sessions</b>"))
        self._session_table = SessionTable()
        self._session_table.session_selected.connect(self._on_session_selected)
        left_layout.addWidget(self._session_table)

        splitter.addWidget(left_widget)

        # Right: detail panel
        self._detail = SessionDetailPanel()
        splitter.addWidget(self._detail)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)

        self._status = QtWidgets.QStatusBar()
        self.setStatusBar(self._status)

    def _build_toolbar(self):
        tb = self.addToolBar("Main")
        tb.setMovable(False)

        act_open = QtGui.QAction("Open DB", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self._action_open)
        tb.addAction(act_open)

        tb.addSeparator()

        act_export_raw = QtGui.QAction("Export Raw CSV", self)
        act_export_raw.triggered.connect(self._action_export_raw)
        tb.addAction(act_export_raw)

        act_export_data = QtGui.QAction("Export Data CSV", self)
        act_export_data.triggered.connect(self._action_export_data)
        tb.addAction(act_export_data)

        tb.addSeparator()

        act_refresh = QtGui.QAction("Refresh", self)
        act_refresh.setShortcut("F5")
        act_refresh.triggered.connect(self._refresh)
        tb.addAction(act_refresh)

        # DB path label
        self._db_label = QtWidgets.QLabel("  No database open")
        tb.addWidget(self._db_label)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _action_open(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open bloodflow database", "",
            "SQLite databases (*.sqlite *.db);;All files (*)"
        )
        if path:
            self._open_db(path)

    def _action_export_raw(self):
        sid = self._current_session_id()
        if sid is None:
            self._status.showMessage("Select a session first.", 3000)
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export raw data", f"{sid}_raw.csv",
            "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return
        include = QtWidgets.QMessageBox.question(
            self, "Include histograms?",
            "Include histogram columns (1024 per row)?\n"
            "The file will be ~30× larger if included.",
            QtWidgets.QMessageBox.StandardButton.Yes |
            QtWidgets.QMessageBox.StandardButton.No,
        ) == QtWidgets.QMessageBox.StandardButton.Yes
        n = export_raw_to_csv(self._db_path, sid, path, include_hist=include)
        self._status.showMessage(f"Exported {n} rows to {path}", 5000)

    def _action_export_data(self):
        sid = self._current_session_id()
        if sid is None:
            self._status.showMessage("Select a session first.", 3000)
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export computed data", f"{sid}_data.csv",
            "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return
        n = export_data_to_csv(self._db_path, sid, path)
        self._status.showMessage(f"Exported {n} rows to {path}", 5000)

    def _refresh(self):
        if self._conn is None:
            return
        self._sessions = _load_sessions(self._conn)
        self._session_table.populate(self._sessions)
        self._status.showMessage("Refreshed.", 2000)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_db(self, db_path):
        try:
            from api.bfstorage import open_db
            if self._conn is not None:
                self._conn.close()
            self._conn = open_db(db_path)
            self._db_path = db_path
            self._db_label.setText(f"  {db_path}")
            self.setWindowTitle(f"OpenMotion BF — {os.path.basename(db_path)}")
            self._refresh()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Error", f"Could not open database:\n{exc}")

    def _current_session_id(self):
        items = self._session_table.selectedItems()
        if not items:
            return None
        return self._session_table.item(items[0].row(), 0).text()

    def _on_session_selected(self, session_id):
        row_dict = next((s for s in self._sessions if s["session_id"] == session_id), None)
        if row_dict is None or self._conn is None:
            return
        self._detail.load(self._conn, session_id, row_dict)
        self._status.showMessage(f"Session: {session_id}", 3000)

    def closeEvent(self, event):
        if self._conn is not None:
            self._conn.close()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="OpenMotion bloodflow SQLite database viewer.",
    )
    parser.add_argument(
        "--db", default=None,
        help="Path to the SQLite database file.  "
             "If omitted a file-open dialog is shown on startup.",
    )
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    viewer = DBViewer(db_path=args.db)
    viewer.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
