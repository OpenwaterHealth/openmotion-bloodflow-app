# Debug Log Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Send Debug Logs to Openwater" button to the LogsModal that zips the last 48h of app logs (+ config + system info), reveals the zip in the file explorer, and tells the user to email it to support@openwater.cc.

**Architecture:** A pure `debug_bundle.py` module (`build_debug_bundle`) does the file-gathering/zipping (unit-testable against a temp dir). A thin `prepareDebugLogBundle()` connector slot calls it, reveals the file, toasts, and records an audit event. One QML button wires it up.

**Tech Stack:** Python 3.13, PyQt6, stdlib `zipfile`/`glob`/`subprocess`, pytest. QML 6.

**Design spec:** [docs/superpowers/specs/2026-06-16-debug-log-bundle-design.md](../specs/2026-06-16-debug-log-bundle-design.md)

---

## File Structure

- **Create** `debug_bundle.py` — `build_debug_bundle(...)` + `WINDOW_HOURS`. Pure file logic, no Qt.
- **Create** `tests/test_debug_bundle.py` — unit tests for the module.
- **Modify** `audit_log.py` — add `EV_DEBUG_BUNDLE_CREATED` constant.
- **Modify** `motion_connector.py` — add `prepareDebugLogBundle()` slot + `_reveal_in_explorer()` helper.
- **Modify** `tests/test_audit_connector.py` — slot test.
- **Modify** `components/LogsModal.qml` — "Send Debug Logs" toolbar button.
- **Modify** `docs/audit-log.md` — document the button + the new event.

---

## Task 1: debug_bundle module

**Files:**
- Create: `debug_bundle.py`
- Test: `tests/test_debug_bundle.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_debug_bundle.py`:

```python
"""Unit tests for the debug-log bundle builder (debug_bundle.py)."""
import os
import zipfile

import pytest

from debug_bundle import build_debug_bundle, WINDOW_HOURS

pytestmark = pytest.mark.unit

_NOW = 1_750_000_000.0  # fixed epoch for deterministic tests


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_window_hours_default_is_48():
    assert WINDOW_HOURS == 48


def test_includes_recent_logs_excludes_old(tmp_path):
    data = tmp_path / "data"
    logs = data / "app-logs"
    for name in ("ow-bloodflowapp-A.log", "ow-bloodflowapp-B.log"):
        _write(logs / name, "log")
        os.utime(logs / name, (_NOW - 3600, _NOW - 3600))   # 1h ago
    old = logs / "ow-bloodflowapp-OLD.log"
    _write(old, "old")
    os.utime(old, (_NOW - 49 * 3600, _NOW - 49 * 3600))     # 49h ago
    _write(data / "app_config.json", '{"k":1}')

    meta = build_debug_bundle(
        str(data), str(tmp_path / "out"), now_epoch=_NOW,
        extra_info={"app_version": "1.2.3", "sdk_version": "9.9"},
    )

    names = zipfile.ZipFile(meta["path"]).namelist()
    assert "app-logs/ow-bloodflowapp-A.log" in names
    assert "app-logs/ow-bloodflowapp-B.log" in names
    assert "app-logs/ow-bloodflowapp-OLD.log" not in names
    assert "app_config.json" in names
    assert "system_info.txt" in names
    assert meta["log_count"] == 2
    assert meta["bytes"] == os.path.getsize(meta["path"])
    base = os.path.basename(meta["path"])
    assert base.startswith("debug-bundle-") and base.endswith(".zip")


def test_empty_window_still_writes_system_info(tmp_path):
    data = tmp_path / "data"
    (data / "app-logs").mkdir(parents=True)
    meta = build_debug_bundle(str(data), str(tmp_path / "out"), now_epoch=_NOW)
    names = zipfile.ZipFile(meta["path"]).namelist()
    assert names == ["system_info.txt"]   # no logs, no config present
    assert meta["log_count"] == 0
    assert meta["file_count"] == 1


def test_system_info_contains_versions_and_host(tmp_path):
    data = tmp_path / "data"
    (data / "app-logs").mkdir(parents=True)
    meta = build_debug_bundle(
        str(data), str(tmp_path / "out"), now_epoch=_NOW,
        extra_info={"app_version": "1.2.3", "sdk_version": "9.9"},
    )
    txt = zipfile.ZipFile(meta["path"]).read("system_info.txt").decode("utf-8")
    assert "app_version: 1.2.3" in txt
    assert "sdk_version: 9.9" in txt
    assert "hostname:" in txt
    assert "generated:" in txt


def test_explicit_config_path_is_used(tmp_path):
    data = tmp_path / "data"
    (data / "app-logs").mkdir(parents=True)
    cfg = tmp_path / "elsewhere" / "app_config.json"
    _write(cfg, '{"x":2}')
    meta = build_debug_bundle(
        str(data), str(tmp_path / "out"), now_epoch=_NOW, config_path=str(cfg),
    )
    names = zipfile.ZipFile(meta["path"]).namelist()
    assert "app_config.json" in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_debug_bundle.py -p no:cacheprovider -q`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'debug_bundle'`.

- [ ] **Step 3: Write the module**

Create `debug_bundle.py`:

```python
"""Build a zip bundle of recent app logs for emailing to support.

Collects the last N hours of app log files plus app_config.json and a
generated system_info.txt into a single zip. Pure file logic — no Qt, no
hardware — so it is unit-testable against a temp directory.
"""

from __future__ import annotations

import datetime
import logging
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

from audit_log import gather_host_info

logger = logging.getLogger("openmotion.bloodflow-app.debug-bundle")

WINDOW_HOURS = 48


def _system_info_text(now_epoch: float, extra_info: Optional[Dict[str, Any]]) -> str:
    """Render host info (+ caller-supplied extras) as sorted key: value
    lines, with a leading local-time 'generated' stamp."""
    info = gather_host_info()
    if extra_info:
        info.update(extra_info)
    generated = (
        datetime.datetime.fromtimestamp(now_epoch)
        .astimezone()
        .isoformat(timespec="seconds")
    )
    lines = [f"generated: {generated}"]
    lines += [f"{k}: {info[k]}" for k in sorted(info)]
    return "\n".join(lines) + "\n"


def build_debug_bundle(
    data_dir: str | Path,
    dest_dir: str | Path,
    now_epoch: float,
    *,
    window_hours: int = WINDOW_HOURS,
    config_path: str | Path | None = None,
    extra_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Zip recent app logs + config + system info into dest_dir.

    Includes <data_dir>/app-logs/*.log with mtime within window_hours,
    the app_config.json at config_path (default <data_dir>/app_config.json)
    if present, and a generated system_info.txt. Returns
    {"path", "file_count", "log_count", "bytes"}.
    """
    data_dir = Path(data_dir)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    cutoff = now_epoch - window_hours * 3600
    log_dir = data_dir / "app-logs"
    recent_logs = []
    if log_dir.is_dir():
        for p in sorted(log_dir.glob("*.log")):
            try:
                if p.stat().st_mtime >= cutoff:
                    recent_logs.append(p)
            except OSError:
                logger.warning("debug_bundle: stat failed for %s", p, exc_info=True)

    if config_path is None:
        config_path = data_dir / "app_config.json"
    config_path = Path(config_path)

    stamp = datetime.datetime.fromtimestamp(now_epoch).strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"debug-bundle-{stamp}.zip"

    file_count = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in recent_logs:
            try:
                zf.write(p, arcname=f"app-logs/{p.name}")
                file_count += 1
            except OSError:
                logger.warning("debug_bundle: could not add %s", p, exc_info=True)
        if config_path.is_file():
            try:
                zf.write(config_path, arcname=config_path.name)
                file_count += 1
            except OSError:
                logger.warning(
                    "debug_bundle: could not add config %s", config_path,
                    exc_info=True,
                )
        zf.writestr("system_info.txt", _system_info_text(now_epoch, extra_info))
        file_count += 1

    return {
        "path": str(dest),
        "file_count": file_count,
        "log_count": len(recent_logs),
        "bytes": dest.stat().st_size,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_debug_bundle.py -p no:cacheprovider -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Lint**

Run: `python -m flake8 debug_bundle.py tests/test_debug_bundle.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add debug_bundle.py tests/test_debug_bundle.py
git commit -m "feat: add debug_bundle module (zip recent app logs)"
```
(Commit message must end with the trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`)

---

## Task 2: Connector slot + reveal helper + event constant

**Files:**
- Modify: `audit_log.py` (event-type constants block)
- Modify: `motion_connector.py` (add slot + helper after `exportAuditLogCsv`)
- Test: `tests/test_audit_connector.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_audit_connector.py`:

```python
def test_prepare_debug_bundle_creates_zip_and_logs(tmp_path):
    import os
    import zipfile
    db = str(tmp_path / "scans.db")
    logs = tmp_path / "app-logs"
    logs.mkdir()
    (logs / "ow-bloodflowapp-x.log").write_text("hello", encoding="utf-8")
    c = _connector(tmp_path, scan_db_path=db)
    # Don't spawn a real file-explorer process during the test.
    c._reveal_in_explorer = lambda p: None
    path = c.prepareDebugLogBundle()
    assert path and os.path.exists(path)
    assert path.endswith(".zip")
    names = zipfile.ZipFile(path).namelist()
    assert any(n.startswith("app-logs/") for n in names)
    assert "system_info.txt" in names
    assert "debug_bundle_created" in _types(c)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_audit_connector.py::test_prepare_debug_bundle_creates_zip_and_logs -p no:cacheprovider -q`
Expected: FAIL — `AttributeError: 'MotionConnector' object has no attribute 'prepareDebugLogBundle'`.

- [ ] **Step 3: Add the event constant**

In `audit_log.py`, find:

```python
EV_AUDIT_LOG_EXPORTED = "audit_log_exported"
```

Insert immediately after it:

```python
EV_DEBUG_BUNDLE_CREATED = "debug_bundle_created"
```

- [ ] **Step 4: Add the slot + helper to the connector**

In `motion_connector.py`, find the end of `exportAuditLogCsv` (it returns `""` on failure / the path on success — the method directly above is the audit-log block added earlier). Immediately after the end of `exportAuditLogCsv`, insert:

```python
    @pyqtSlot(result=str)
    def prepareDebugLogBundle(self) -> str:
        """Zip the last 48h of app logs (+ config + system info) into
        app-logs/debug-bundles/, reveal it in the file explorer, and toast
        the support address. Returns the zip path, or '' on failure."""
        try:
            from debug_bundle import build_debug_bundle, WINDOW_HOURS
            try:
                from version import get_version as _gv
                app_version = _gv()
            except Exception:
                app_version = ""
            try:
                sdk_version = self._interface.get_sdk_version()
                sdk_version = sdk_version if isinstance(sdk_version, str) else ""
            except Exception:
                sdk_version = ""
            dest_dir = os.path.join(self._directory, "app-logs", "debug-bundles")
            meta = build_debug_bundle(
                self._directory,
                dest_dir,
                time.time(),
                config_path=resource_path("config", "app_config.json"),
                extra_info={"app_version": app_version, "sdk_version": sdk_version},
            )
        except Exception:
            logger.exception("prepareDebugLogBundle: failed to build bundle")
            self.errorOccurred.emit("Could not create the debug log bundle.")
            return ""

        path = meta["path"]
        self._reveal_in_explorer(path)
        from audit_log import EV_DEBUG_BUNDLE_CREATED
        self._audit.log(EV_DEBUG_BUNDLE_CREATED, {
            "dest": path,
            "file_count": meta["file_count"],
            "log_count": meta["log_count"],
            "bytes": meta["bytes"],
            "window_hours": WINDOW_HOURS,
        })
        self.notify(
            "Debug logs saved to " + path
            + ". Please email this file to support@openwater.cc.",
            "success", 0, True, "debug-bundle",
        )
        return path

    def _reveal_in_explorer(self, path: str) -> None:
        """Best-effort: open the OS file browser with the file selected.
        Never raises — a failed reveal must not lose the bundle."""
        try:
            import subprocess
            import sys
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            logger.warning(
                "could not reveal %s in file explorer", path, exc_info=True
            )
```

(`os`, `time`, `resource_path`, `logger`, `pyqtSlot`, and the `errorOccurred`/`notify` members all already exist in this module — do not re-import or redefine them.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_audit_connector.py -k debug_bundle -p no:cacheprovider -q`
Expected: PASS.

- [ ] **Step 6: Lint**

Run: `python -m flake8 audit_log.py tests/test_audit_connector.py`
Expected: clean (the test file must be clean; `motion_connector.py` has pre-existing violations — confirm the slot's added lines introduce none by checking the violation count is unchanged: `python -m flake8 motion_connector.py | wc -l` before/after should both be the same baseline).

- [ ] **Step 7: Commit**

```bash
git add audit_log.py motion_connector.py tests/test_audit_connector.py
git commit -m "feat: prepareDebugLogBundle connector slot + reveal + audit event"
```
(Trailer required.)

---

## Task 3: LogsModal "Send Debug Logs" button

**Files:**
- Modify: `components/LogsModal.qml` (toolbar)

> **No automated test:** QML is not unit-tested in this repo. Verified by `python -c "import main"` and visually in Task 4.

- [ ] **Step 1: Add the button**

In `components/LogsModal.qml`, find the toolbar spacer immediately before the Export CSV button:

```qml
                Item { Layout.fillWidth: true }

                Button {
                    text: "Export CSV"
```

Replace with (inserts the new button between the spacer and Export CSV):

```qml
                Item { Layout.fillWidth: true }

                Button {
                    text: "Send Debug Logs"
                    Layout.preferredWidth: 140; Layout.preferredHeight: 32
                    hoverEnabled: true
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13; color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.accentBlue : theme.bgInput
                        border.color: parent.hovered ? theme.textPrimary : theme.textSecondary; radius: 4
                    }
                    // Connector builds the zip, reveals it, and toasts the
                    // support address; refresh so the debug_bundle_created
                    // entry shows up in the list.
                    onClicked: {
                        MotionInterface.prepareDebugLogBundle()
                        root.refresh()
                    }
                }

                Button {
                    text: "Export CSV"
```

- [ ] **Step 2: Verify QML parses + balance**

Run: `python -c "import main; print('import ok')"`
Expected: `import ok` (no traceback).
Run: `python -c "s=open('components/LogsModal.qml',encoding='utf-8').read(); print('braces',s.count('{')-s.count('}'),'parens',s.count('(')-s.count(')'),'brackets',s.count('[')-s.count(']'))"`
Expected: `braces 0 parens 0 brackets 0`.

- [ ] **Step 3: Commit**

```bash
git add components/LogsModal.qml
git commit -m "feat: add Send Debug Logs button to LogsModal"
```
(Trailer required.)

---

## Task 4: Docs + verification

**Files:**
- Modify: `docs/audit-log.md`

- [ ] **Step 1: Document the button + event**

In `docs/audit-log.md`, find the event-types table row:

```
| `audit_log_exported` | The audit log is exported to CSV. | `dest`, `row_count` |
```

Insert immediately after it:

```
| `debug_bundle_created` | The "Send Debug Logs" button is used. | `dest`, `file_count`, `log_count`, `bytes`, `window_hours` |
```

Then find the `## Exporting to CSV` heading and insert this new section immediately before it:

```markdown
## Sending debug logs to Openwater

The **Send Debug Logs** button (top of the audit-log viewer) packages the
app's diagnostic logs for support. It writes a zip to
`<dataDirectory>/app-logs/debug-bundles/debug-bundle-<timestamp>.zip`
containing the app log files from the last 48 hours, `app_config.json`,
and a `system_info.txt` (app/SDK version + host details). The file
explorer opens with the zip selected, and a message shows the path —
**email that zip to support@openwater.cc**. No data is sent automatically,
and the bundle contains no scan data or patient information.

```

- [ ] **Step 2: Run the feature's full test set**

Run: `python -m pytest tests/test_debug_bundle.py tests/test_audit_connector.py tests/test_audit_log.py -p no:cacheprovider -q`
Expected: all PASS.

- [ ] **Step 3: Lint touched Python**

Run: `python -m flake8 debug_bundle.py tests/test_debug_bundle.py tests/test_audit_connector.py`
Expected: clean.

- [ ] **Step 4: End-to-end smoke (no hardware)**

Run this script to confirm the slot produces a real zip with the expected members:

```bash
python - <<'PY'
import sys, os, tempfile, zipfile
sys.path.insert(0, os.getcwd())
from unittest.mock import MagicMock
from PyQt6.QtCore import QCoreApplication
app = QCoreApplication(sys.argv)
from motion_connector import MotionConnector
d = tempfile.mkdtemp()
os.makedirs(os.path.join(d, "app-logs"))
open(os.path.join(d, "app-logs", "ow-bloodflowapp-demo.log"), "w").write("demo log\n")
iface = MagicMock()
iface.is_device_connected.return_value = (False, False, False)
iface.scan_workflow.running = False
iface.scan_workflow.config_running = False
iface.scan_db_path = os.path.join(d, "scans.db")
iface.get_sdk_version.return_value = "1.5.8"
c = MotionConnector(interface=iface, app_config={"developerMode": False},
                    data_dir=d, config_dir="config")
c._reveal_in_explorer = lambda p: None
path = c.prepareDebugLogBundle()
print("bundle:", path)
print("members:", zipfile.ZipFile(path).namelist())
PY
```
Expected: prints a `debug-bundle-*.zip` path under `app-logs/debug-bundles/` whose members include `app-logs/ow-bloodflowapp-demo.log` and `system_info.txt`.

- [ ] **Step 5: Commit docs**

```bash
git add docs/audit-log.md
git commit -m "docs: document Send Debug Logs button + debug_bundle_created event"
```
(Trailer required.)

- [ ] **Step 6: Manual/visual check (controller does this after execution)**

Launch `python main.py`, open Settings → Audit Log → View Logs (password `OpenwaterHealth`), click **Send Debug Logs**: confirm the file explorer opens with the zip selected, a success toast shows the path + support@openwater.cc, and a `debug_bundle_created` row appears after the list refreshes.

---

## Self-Review (completed by plan author)

**Spec coverage:**
- Zip last 48h of `app-logs/*.log` → Task 1 (`build_debug_bundle`, mtime filter) ✓
- Include `app_config.json` + `system_info.txt` (host + app/SDK version) → Task 1 ✓
- Output to `app-logs/debug-bundles/debug-bundle-<ts>.zip` → Task 2 (`dest_dir`) ✓
- Reveal in explorer (Windows `/select`, best-effort) → Task 2 (`_reveal_in_explorer`) ✓
- Toast with support@openwater.cc (sticky) → Task 2 ✓
- `debug_bundle_created` audit event with `{dest,file_count,log_count,bytes,window_hours}` → Task 2 ✓
- Fail-soft (build/reveal never crash) → Task 2 (try/except around build; `_reveal_in_explorer` never raises) ✓
- Button in LogsModal → Task 3 ✓
- Pure-module unit tests + connector slot test → Tasks 1, 2 ✓
- Docs → Task 4 ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type/name consistency:** `build_debug_bundle(data_dir, dest_dir, now_epoch, *, window_hours, config_path, extra_info)` and its return keys (`path`/`file_count`/`log_count`/`bytes`) are used identically in Tasks 1, 2, and 4. `prepareDebugLogBundle`, `_reveal_in_explorer`, `EV_DEBUG_BUNDLE_CREATED`, and the `debug_bundle_created` string are consistent across tasks.

**Known limitation (intentional):** the QML button (Task 3) and the reveal/toast side effects have no automated test (QML isn't unit-tested here; the reveal spawns an OS process) — verified via import smoke + the Task 4 end-to-end script + the Task 4 Step 6 manual check.
