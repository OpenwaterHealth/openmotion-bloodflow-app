"""Operator preferences persisted in the ``settings`` table of scans.db (#546).

Replaces the plaintext ``app_config.local.json`` overrides file. Only the
PREFERENCE and STATE tiers of ``config.app_config`` are ever written here;
constants are compiled in and session keys are never persisted at all.

The table lives in the same SQLite file as the scan data and the audit
``logs`` table, opened through the SDK's ``db_open`` so that on a clinical
build it is SQLCipher-encrypted with the keyring-held key and every page is
HMAC-protected: the file cannot be hand-edited and tampering is detected.

Fail-soft by contract, like AuditLog: a missing path or a database error
degrades the store to a no-op (preferences then last for the session only)
and is logged at WARNING. It never raises into the app.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger("openmotion.bloodflow-app.settings")


class SettingsStore:
    """Key/value store for operator preferences inside scans.db."""

    TABLE = "settings"

    def __init__(self, db_path: Optional[str | Path]) -> None:
        self._lock = threading.Lock()
        self._conn = None
        self._path = str(db_path) if db_path else None
        if not self._path:
            logger.info("SettingsStore: no db_path; preferences are session-only.")
            return
        try:
            from omotion import db_open

            self._conn = db_open.connect(self._path)
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self.TABLE} ("
                " key TEXT PRIMARY KEY,"
                " value TEXT NOT NULL,"
                " updated_epoch REAL NOT NULL)"
            )
            self._conn.commit()
        except Exception:
            logger.warning(
                "SettingsStore: failed to open %s; preferences are session-only.",
                self._path, exc_info=True,
            )
            self._conn = None

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    @property
    def path(self) -> Optional[str]:
        return self._path

    def load(self) -> Dict[str, Any]:
        """Every saved key with its JSON-decoded value ({} when disabled).

        A row that fails to decode is skipped and logged, never fatal.
        """
        if self._conn is None:
            return {}
        out: Dict[str, Any] = {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT key, value FROM {self.TABLE}"
                ).fetchall()
        except Exception:
            logger.warning("SettingsStore: read failed", exc_info=True)
            return {}
        for key, raw in rows:
            try:
                out[str(key)] = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("SettingsStore: skipping undecodable value for %r", key)
        return out

    def save(self, values: Dict[str, Any]) -> bool:
        """Upsert ``values``. Returns True when every row was written."""
        if self._conn is None or not values:
            return False
        now = time.time()
        rows = []
        for key, value in values.items():
            try:
                rows.append((str(key), json.dumps(value, default=str), now))
            except (TypeError, ValueError):
                # A non-serializable value (a QJSValue leaking through the
                # QML bridge, #446) loses that one write, not the app.
                logger.error("SettingsStore: cannot serialize %r; not saved", key)
        if not rows:
            return False
        try:
            with self._lock:
                self._conn.executemany(
                    f"INSERT INTO {self.TABLE} (key, value, updated_epoch)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET"
                    " value = excluded.value, updated_epoch = excluded.updated_epoch",
                    rows,
                )
                self._conn.commit()
            return len(rows) == len(values)
        except Exception:
            logger.warning("SettingsStore: write failed", exc_info=True)
            return False

    def delete(self, keys: Iterable[str]) -> bool:
        """Remove the given keys (a key back at its compiled value)."""
        keys = [str(k) for k in keys]
        if self._conn is None or not keys:
            return False
        try:
            with self._lock:
                self._conn.executemany(
                    f"DELETE FROM {self.TABLE} WHERE key = ?", [(k,) for k in keys]
                )
                self._conn.commit()
            return True
        except Exception:
            logger.warning("SettingsStore: delete failed", exc_info=True)
            return False

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
