"""Incremental, shared-read tailing of the per-launch app log.

Backs the engineering "Logs" window (components/LogViewerWindow.qml —
the replacement for the removed console=True second exe, #57): the GUI
polls ``MotionConnector.readAppLog()`` on a QTimer and appends whatever
this module returns. Reads are bounded and the file is opened per call
(ordinary CRT sharing — never an exclusive lock, never a held handle),
so polling from the GUI thread stays cheap and the logging FileHandler
that owns the file for writing is never disturbed.
"""
from __future__ import annotations

import os
from pathlib import Path

# First read returns at most this much from the end of the file (starting
# at a line boundary) so opening the viewer late in a verbose session
# doesn't dump tens of MB into the UI.
MAX_INITIAL_BYTES = 512 * 1024
# Cap per read_new() call; a backlog simply drains over the next polls.
MAX_POLL_BYTES = 256 * 1024

# Matches the per-launch file main.py creates: open-motion-<ts>.log.
LOG_GLOB = "open-motion-*.log"


def latest_log_path(logs_dir) -> str:
    """Newest ``open-motion-*.log`` under ``logs_dir``, or ``''``.

    Newest by filename: the embedded ``YYYYMMDD_HHMMSS`` timestamp
    (see main.py) sorts lexicographically in chronological order.
    """
    try:
        names = sorted(p.name for p in Path(logs_dir).glob(LOG_GLOB))
    except OSError:
        return ""
    if not names:
        return ""
    return str(Path(logs_dir) / names[-1])


class LogTail:
    """Stateful incremental reader for a single log file."""

    def __init__(self, path,
                 max_initial_bytes: int = MAX_INITIAL_BYTES,
                 max_poll_bytes: int = MAX_POLL_BYTES):
        self.path = str(path)
        self._max_initial_bytes = max_initial_bytes
        self._max_poll_bytes = max_poll_bytes
        self._offset: int | None = None  # None until the first read
        # A chunk ending in "\r" is (almost certainly) half of a "\r\n"
        # whose "\n" lands in the next poll; hold it back so newline
        # normalization can't leak a stray "\r" at a chunk boundary.
        self._carry = ""

    def read_new(self) -> str:
        """Text appended since the last call (``''`` when nothing new).

        Newlines are normalized to ``\\n`` (the FileHandler writes the
        log in text mode, so on-disk lines end ``\\r\\n`` on Windows).
        The first call tails the last ``max_initial_bytes`` starting at
        a line boundary; later calls resume from the stored offset and
        return at most ``max_poll_bytes`` (a backlog drains across
        subsequent calls). A shrunken file (truncate/replace) restarts
        from the top. A missing/unreadable file returns ``''`` — the
        caller just polls again later.
        """
        first = self._offset is None
        try:
            with open(self.path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                if first:
                    start = max(0, size - self._max_initial_bytes)
                elif self._offset > size:
                    start = 0  # file was truncated/replaced — start over
                else:
                    start = self._offset
                if size <= start:
                    self._offset = start
                    return ""
                f.seek(start)
                data = f.read(self._max_poll_bytes)
                self._offset = start + len(data)
        except OSError:
            return ""
        text = self._carry + data.decode("utf-8", errors="replace")
        self._carry = ""
        if text.endswith("\r"):
            self._carry = "\r"
            text = text[:-1]
        text = text.replace("\r\n", "\n")
        if first and start > 0:
            # Mid-file entry point: drop the partial first line so the
            # viewer starts on a boundary. The skipped bytes are already
            # consumed (offset advanced) — they are never re-read.
            nl = text.find("\n")
            text = text[nl + 1:] if nl >= 0 else ""
        return text
