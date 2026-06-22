# MSI Installer + In-App Updater Implementation Plan

> **Phasing (2026-06-18):** this campaign is split into slices.
> **Phase 1 — installer + ProgramData relocation** (Tasks 1–5, 10–16, minus the
> updater) ships first on `feature/msi-installer`. **Phase 1.5 — the in-app
> updater** (Tasks 6–9 + the updater hardening) ships next on
> `feature/in-app-updater`, stacked on Phase 1. **Phase 2 — tamper-resistance**
> follows (separate workstream). This document is the original combined plan;
> the task numbers below still apply, just delivered across those slices.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the bloodflow app as a signed WiX Burn bundle that installs the WinUSB driver + the app into Program Files in one run, with an in-app "Update" button that performs an in-place upgrade.

**Architecture:** PyInstaller is unchanged — `openwater.spec` still produces `dist\OpenWaterApp\`. WiX v5 wraps that folder into an app MSI; a Burn bundle chains the existing driver MSI → app MSI into one `OpenWater-Setup-X.Y.Z.exe`. Because Program Files is read-only at runtime, writable state (config overrides, logs, scan data) relocates to `%PROGRAMDATA%\OpenWater\`. The in-app updater (RUO build only — clinical builds suppress it per issue #96) downloads the matching bundle, verifies its Authenticode signature, and runs it for an in-place upgrade.

**Tech Stack:** Python 3.13 / PyQt6 / pytest, PyInstaller 6.11, WiX Toolset v5 (`wix` .NET CLI), WiX Burn + BAL extension, PowerShell, GitHub Actions.

**Spec:** [docs/superpowers/specs/2026-06-17-msi-installer-in-app-update-design.md](../specs/2026-06-17-msi-installer-in-app-update-design.md)

**Conventions for this plan:**
- **Symbol names (verified at tip-of-`next`):** the connector class is `MotionConnector` and the QML singleton is `MotionInterface` (registered in `main.py` via `qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", connector)`). Use these exact names in Python imports and QML.
- **Line numbers are approximate.** `motion_connector.py` is ~4500 lines and grows; locate every edit site by the quoted symbol/code, not the line number.
- New unit tests are marked `@pytest.mark.unit` so conftest's autouse fixtures skip app launch (see `tests/conftest.py`). Run them with `python -m pytest -m unit <path> -v`.
- Tests redirect the writable root via the `OPENWATER_DATA_ROOT` env var (added in Task 1) to a `tmp_path`, never the real ProgramData.
- WiX/installer/CI tasks cannot be unit-tested; their "test" is a successful build + the manual VM verification in Task 16.
- Commit after every task.

---

## Phase A — Writable-state relocation (app code)

### Task 1: `utils/app_paths.py` — writable-root resolver

**Files:**
- Create: `utils/app_paths.py`
- Test: `tests/test_app_paths.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_paths.py
import os
import pytest
from pathlib import Path
from utils import app_paths


@pytest.mark.unit
def test_writable_root_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "ow"))
    root = app_paths.writable_root()
    assert root == tmp_path / "ow"
    assert root.is_dir()  # created on access


@pytest.mark.unit
def test_local_config_and_data_dir_under_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "ow"))
    assert app_paths.local_config_path() == tmp_path / "ow" / "app_config.local.json"
    assert app_paths.default_data_dir() == tmp_path / "ow" / "data"
    assert (tmp_path / "ow" / "data").is_dir()


@pytest.mark.unit
def test_dev_root_is_cwd_when_not_frozen(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    # not frozen in test → root is cwd, behavior unchanged for local dev
    assert app_paths.writable_root() == tmp_path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -m unit tests/test_app_paths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'utils.app_paths'`

- [ ] **Step 3: Write minimal implementation**

```python
# utils/app_paths.py
"""Resolve writable, user-data locations outside the (read-only) install dir.

When the app is installed to Program Files, its bundled files are read-only.
Runtime-writable state (config overrides, logs, scan data) lives under
%PROGRAMDATA%\\OpenWater\\ instead. In a dev (non-frozen) run, everything stays
under the cwd so local development is unchanged.

Override the root with the OPENWATER_DATA_ROOT env var (used by tests and as a
power-user escape hatch).
"""
from pathlib import Path
import os
import sys

_APP_DIRNAME = "OpenWater"


def writable_root() -> Path:
    """Return the writable data root, creating it if necessary."""
    env = os.environ.get("OPENWATER_DATA_ROOT")
    if env:
        root = Path(env)
    elif getattr(sys, "frozen", False):
        base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        root = Path(base) / _APP_DIRNAME
    else:
        # dev: keep everything under the cwd, unchanged from before
        root = Path.cwd()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_config_path() -> Path:
    """Path to the writable config-overrides file."""
    return writable_root() / "app_config.local.json"


def default_data_dir() -> Path:
    """Default scan-data / logs root when dataDirectory is unset."""
    d = writable_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -m unit tests/test_app_paths.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add utils/app_paths.py tests/test_app_paths.py
git commit -m "feat: add app_paths writable-root resolver for ProgramData relocation"
```

---

### Task 2: `utils/config_store.py` — layered load + overrides-diff save

**Files:**
- Create: `utils/config_store.py`
- Test: `tests/test_config_store.py`

The store layers config: code `defaults` → read-only bundled `config/app_config.json` → writable `app_config.local.json`. Runtime changes save as a **diff** against the first two layers so future shipped-default changes still reach untouched keys.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_store.py
import json
import pytest
from utils import config_store


DEFAULTS = {"developerMode": False, "reducedMode": False, "leftMask": 0x66, "bfiMax": 10.0}


@pytest.mark.unit
def test_load_merges_overrides_over_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    (tmp_path / "app_config.local.json").write_text(
        json.dumps({"developerMode": True}), encoding="utf-8"
    )
    baseline, merged = config_store.load_app_config(DEFAULTS)
    assert baseline["developerMode"] is False      # baseline untouched
    assert merged["developerMode"] is True          # override wins
    assert merged["bfiMax"] == 10.0                 # untouched key flows through


@pytest.mark.unit
def test_save_writes_only_diff_against_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    baseline = dict(DEFAULTS)
    current = {**DEFAULTS, "developerMode": True}
    config_store.save_overrides(current, baseline)
    written = json.loads((tmp_path / "app_config.local.json").read_text(encoding="utf-8"))
    assert written == {"developerMode": True}       # only the changed key


@pytest.mark.unit
def test_load_with_no_override_file_returns_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    baseline, merged = config_store.load_app_config(DEFAULTS)
    assert merged == baseline
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -m unit tests/test_config_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'utils.config_store'`

- [ ] **Step 3: Write minimal implementation**

```python
# utils/config_store.py
"""Load app config from layered sources and persist runtime overrides.

Layers (lowest to highest precedence):
  1. code defaults (passed in by the caller)
  2. shipped, read-only config/app_config.json (PyInstaller bundle)
  3. writable overrides: %PROGRAMDATA%\\OpenWater\\app_config.local.json

Runtime changes are written as a *diff* against layers 1+2 so future changes to
shipped defaults still reach keys the operator never touched.
"""
import json
import logging

from utils.resource_path import resource_path
from utils import app_paths

logger = logging.getLogger(__name__)

_INT_KEYS = ("leftMask", "rightMask", "reducedModeLeftMask", "reducedModeRightMask")


def _coerce_ints(cfg: dict) -> dict:
    for key in _INT_KEYS:
        if cfg.get(key) is not None:
            cfg[key] = int(cfg[key])
    return cfg


def shipped_baseline(defaults: dict) -> dict:
    """defaults merged with the read-only bundled app_config.json."""
    base = dict(defaults)
    path = resource_path("config", "app_config.json")
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            base.update({k: v for k, v in loaded.items() if k in defaults})
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load %s: %s; using defaults", path, e)
    return _coerce_ints(base)


def load_overrides() -> dict:
    """Read the writable overrides file (empty dict if absent/invalid)."""
    path = app_paths.local_config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load overrides %s: %s", path, e)
        return {}


def load_app_config(defaults: dict):
    """Return (baseline, merged); merged = baseline + writable overrides."""
    baseline = shipped_baseline(defaults)
    overrides = load_overrides()
    merged = {
        **baseline,
        **{k: v for k, v in overrides.items() if k in baseline},
    }
    return baseline, _coerce_ints(merged)


def save_overrides(current: dict, baseline: dict) -> None:
    """Persist only keys whose value differs from the baseline."""
    diff = _coerce_ints({k: v for k, v in current.items() if baseline.get(k) != v})
    path = app_paths.local_config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(diff, f, indent=2)
    except OSError as e:
        logger.warning("Could not write overrides %s: %s", path, e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -m unit tests/test_config_store.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add utils/config_store.py tests/test_config_store.py
git commit -m "feat: add config_store with layered load and overrides-diff save"
```

---

### Task 3: Wire `main.py` to the layered store + ProgramData log/data root

**Files:**
- Modify: `main.py` — `_load_app_config` (delegate to the store), the `_data_dir` default block in `main()`, and the `MotionConnector(...)` construction
- Test: `tests/test_main_config_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main_config_wiring.py
import json
import importlib
import pytest


@pytest.mark.unit
def test_load_app_config_applies_local_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    (tmp_path / "app_config.local.json").write_text(
        json.dumps({"developerMode": True}), encoding="utf-8"
    )
    main = importlib.import_module("main")
    cfg = main._load_app_config()
    assert cfg["developerMode"] is True              # override applied over baseline
    # baseline is stashed for the connector (value comes from the shipped
    # config file, so assert presence, not a specific value).
    assert "developerMode" in main._APP_CONFIG_BASELINE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -m unit tests/test_main_config_wiring.py -v`
Expected: FAIL — `_load_app_config` does not consult overrides / `_APP_CONFIG_BASELINE` does not exist.

- [ ] **Step 3: Write minimal implementation**

In `main.py`, add the import near the other `utils` imports (next to `from utils.resource_path import resource_path`):

```python
from utils import app_paths, config_store
```

Add a module-level baseline stash (just below the imports, before `_load_app_config`):

```python
# Shipped baseline (defaults + read-only bundled config), captured at load so
# the connector can diff runtime changes against it when saving overrides.
_APP_CONFIG_BASELINE: dict = {}
```

Replace the body of `_load_app_config` from the `config_path = resource_path("config", "app_config.json")` line through its `return` statements with delegation to the store. Keep the big `defaults = {...}` dict exactly as-is; only the tail changes:

```python
    # (defaults = { ... } stays unchanged above this point)
    baseline, merged = config_store.load_app_config(defaults)
    _APP_CONFIG_BASELINE.clear()
    _APP_CONFIG_BASELINE.update(baseline)
    logger.info("Loaded app config (overrides from %s)", app_paths.local_config_path())
    return merged
```

Change the data-dir default so a frozen install logs/scans under ProgramData instead of cwd. In `main()`, find the block that sets `_data_dir` (currently `_data_dir = app_config.get("dataDirectory")` followed by the `if not _data_dir:` fallback to `os.getcwd()`) and change the fallback line:

```python
    _data_dir = app_config.get("dataDirectory")
    if not _data_dir:
        # Installed (frozen) build → ProgramData; dev run → cwd (unchanged),
        # falling back to ~/Documents if cwd is not writable.
        candidate = str(app_paths.writable_root()) if getattr(sys, "frozen", False) else os.getcwd()
        if os.access(candidate, os.W_OK):
            _data_dir = candidate
        else:
            _data_dir = os.path.join(
                os.path.expanduser("~"), "Documents", "OpenWater Bloodflow"
            )
```

Pass the baseline into the connector. Find the `connector = MotionConnector(...)` construction and add the `baseline_config` kwarg, preserving the existing `data_dir`, `app_version`, and `log_path` kwargs:

```python
    connector = MotionConnector(
        motion_interface, app_config=app_config, data_dir=_data_dir,
        baseline_config=_APP_CONFIG_BASELINE,
        app_version=APP_VERSION, log_path=logfile_path,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -m unit tests/test_main_config_wiring.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_config_wiring.py
git commit -m "feat: load config via layered store; route logs/data to ProgramData when frozen"
```

---

### Task 4: Connector saves overrides instead of overwriting bundled config

**Files:**
- Modify: `motion_connector.py` — the `MotionConnector.__init__` signature + baseline capture, and the `_save_app_config` method
- Test: `tests/test_connector_save_overrides.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connector_save_overrides.py
import json
import pytest
import motion_connector
from motion_connector import MotionConnector


@pytest.mark.unit
def test_save_app_config_delegates_diff_to_store(tmp_path, monkeypatch):
    # Redirect resource_path("config", ...) to a throwaway dir so the pre-impl
    # ("red") version of _save_app_config can't clobber the repo's real
    # config/app_config.json when this test first runs.
    (tmp_path / "app_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENWATER_CONFIG_DIR", str(tmp_path))

    # MotionConnector.__init__ wires hardware/telemetry, so bypass it with
    # __new__ and set only the attributes _save_app_config reads.
    conn = MotionConnector.__new__(MotionConnector)
    conn._app_config = {"developerMode": True, "reducedMode": False}
    conn._baseline_config = {"developerMode": False, "reducedMode": False}

    captured = {}
    monkeypatch.setattr(
        motion_connector.config_store,
        "save_overrides",
        lambda current, baseline: captured.update(current=current, baseline=baseline),
    )
    conn._save_app_config()

    assert captured["current"] == conn._app_config
    assert captured["baseline"] == conn._baseline_config
```

This proves the connector delegates to `config_store.save_overrides` with the right args; the *diff* behavior itself is already covered by `tests/test_config_store.py` (Task 2), and the end-to-end ProgramData write is verified manually in Task 16 Step 2.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -m unit tests/test_connector_save_overrides.py -v`
Expected: FAIL — `__init__` has no `baseline_config` kwarg.

- [ ] **Step 3: Write minimal implementation**

Add the import near the top of `motion_connector.py` (next to `from utils.resource_path import resource_path`):

```python
from utils import config_store
```

In `MotionConnector.__init__`, add the `baseline_config` parameter and capture it. The current signature is `def __init__(self, interface, app_config=None, data_dir=None, config_dir="config", parent=None, log_level=logging.INFO, app_version="", log_path="")` — add `baseline_config=None` after `app_config=None`. Then, right after `self._app_config = dict(cfg)`, add:

```python
        self._baseline_config = dict(baseline_config or {})
```

Replace the `_save_app_config` method (the one that does `config_path = resource_path("config", "app_config.json")` then `json.dump`) with:

```python
    def _save_app_config(self):
        """Persist runtime config changes as a diff against the shipped baseline.

        Writes only changed keys to %PROGRAMDATA%\\OpenWater\\app_config.local.json
        — never the read-only bundled config under Program Files.
        """
        config_store.save_overrides(self._app_config, self._baseline_config)
```

The class attribute `_INT_CONFIG_KEYS = {"leftMask", "rightMask"}` is now superseded by `config_store._INT_KEYS`; delete it if no other reference remains (grep `_INT_CONFIG_KEYS` first — if referenced elsewhere, leave it).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -m unit tests/test_connector_save_overrides.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add motion_connector.py tests/test_connector_save_overrides.py
git commit -m "feat: connector persists config as ProgramData overrides diff"
```

---

### Task 5: Gitignore the dev-mode overrides file

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Add the ignore entry**

Append to `.gitignore`:

```
# Runtime config overrides (written by setConfig; ProgramData in prod, cwd in dev)
app_config.local.json
data/
```

- [ ] **Step 2: Verify nothing is already tracked**

Run: `git status --porcelain app_config.local.json`
Expected: no output (file is not tracked).

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore runtime config overrides + dev data dir"
```

---

## Phase B — In-app updater (app code)

### Task 6: Variant-aware update-asset selector

**Files:**
- Modify: `motion_connector.py` (add module-level `_select_update_asset` + a `_REQUIRE_SIGNED_UPDATES` flag near the other module functions, e.g. just after `developer_password_matches`), and the `.zip` asset loop inside `_check_for_updates_worker`
- Test: `tests/test_update_asset_select.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_update_asset_select.py
import pytest
from motion_connector import _select_update_asset


CLINICAL = {"name": "OpenWater-Setup-1.2.3.exe", "browser_download_url": "u/clinical"}
RUO = {"name": "OpenWater-Setup-1.2.3_RUO.exe", "browser_download_url": "u/ruo"}
ZIP = {"name": "OpenMotionDriver-x64.zip", "browser_download_url": "u/zip"}


@pytest.mark.unit
def test_selects_ruo_bundle_for_ruo_variant():
    assert _select_update_asset([CLINICAL, RUO, ZIP], is_ruo=True) == "u/ruo"


@pytest.mark.unit
def test_selects_clinical_bundle_for_clinical_variant():
    assert _select_update_asset([CLINICAL, RUO, ZIP], is_ruo=False) == "u/clinical"


@pytest.mark.unit
def test_returns_none_when_no_matching_exe():
    assert _select_update_asset([ZIP], is_ruo=True) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -m unit tests/test_update_asset_select.py -v`
Expected: FAIL — `cannot import name '_select_update_asset'`

- [ ] **Step 3: Write minimal implementation**

Add near the other module-level helpers in `motion_connector.py` (e.g. just after the `developer_password_matches` function):

```python
# Flip to True once release builds are Authenticode-signed; until then an
# unsigned (NotSigned) update bundle is allowed through with a logged warning.
_REQUIRE_SIGNED_UPDATES = False


def _select_update_asset(assets: list, is_ruo: bool):
    """Return the download URL of the Setup bundle matching the running variant.

    RUO builds match ``OpenWater-Setup-*_RUO.exe``; clinical builds match the
    non-RUO ``OpenWater-Setup-*.exe``. Returns None if no matching .exe asset.
    """
    for asset in assets:
        name = (asset.get("name") or "")
        low = name.lower()
        if not low.endswith(".exe"):
            continue
        asset_is_ruo = low.endswith("_ruo.exe")
        if asset_is_ruo == is_ruo:
            return asset.get("browser_download_url")
    return None
```

Replace the `.zip` asset loop in `_check_for_updates_worker` (the `# Find the .zip asset download URL` block that loops over `data.get("assets", [])` and breaks on `asset["name"].endswith(".zip")`) with:

```python
            # Find the Setup bundle matching this build's variant.
            # reducedMode True = clinical build; False = RUO/full build.
            is_ruo = not bool(self._app_config.get("reducedMode", False))
            download_url = _select_update_asset(data.get("assets", []), is_ruo) or data.get(
                "html_url", ""
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -m unit tests/test_update_asset_select.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add motion_connector.py tests/test_update_asset_select.py
git commit -m "feat: select update bundle asset by build variant"
```

---

### Task 7: Authenticode status helper

**Files:**
- Modify: `motion_connector.py` (add `_authenticode_status` near `_select_update_asset`)
- Test: `tests/test_authenticode_status.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_authenticode_status.py
import subprocess
import types
import pytest
import motion_connector


@pytest.mark.unit
def test_authenticode_status_parses_powershell_output(monkeypatch):
    def fake_run(*a, **k):
        return types.SimpleNamespace(stdout="Valid\n", returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert motion_connector._authenticode_status("C:/x.exe") == "Valid"


@pytest.mark.unit
def test_authenticode_status_returns_error_on_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("no powershell")
    monkeypatch.setattr(subprocess, "run", boom)
    assert motion_connector._authenticode_status("C:/x.exe") == "Error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -m unit tests/test_authenticode_status.py -v`
Expected: FAIL — `module 'motion_connector' has no attribute '_authenticode_status'`

- [ ] **Step 3: Write minimal implementation**

Add to `motion_connector.py` next to `_select_update_asset`:

```python
def _authenticode_status(path: str) -> str:
    """Return the Authenticode signature status of ``path``.

    Uses PowerShell's Get-AuthenticodeSignature (always present on Windows).
    Returns one of 'Valid', 'NotSigned', 'HashMismatch', 'UnknownError', ... or
    'Error' if the check itself could not run.
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-AuthenticodeSignature -LiteralPath '{path}').Status",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() or "Error"
    except Exception:
        return "Error"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -m unit tests/test_authenticode_status.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add motion_connector.py tests/test_authenticode_status.py
git commit -m "feat: add Authenticode status helper for update verification"
```

---

### Task 8: `applyUpdate` slot — download, verify, launch, quit

**Files:**
- Modify: `motion_connector.py:1` (add `QCoreApplication` to the QtCore import), `motion_connector.py` (add slot near `openDownloadUrl` ~line 3945)
- Test: `tests/test_apply_update.py`

The installer spawn + app-quit is integration-only (verified in Task 16). The unit-testable part is the **signature decision**: extract it into a pure `_update_decision(status, require_signed) -> (should_launch, error)` function and test that. `_apply_update_worker` then just acts on the decision.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_apply_update.py
import pytest
from motion_connector import _update_decision


@pytest.mark.unit
def test_valid_signature_launches():
    assert _update_decision("Valid", require_signed=True) == (True, None)


@pytest.mark.unit
def test_invalid_signature_aborts_with_message():
    launch, err = _update_decision("HashMismatch", require_signed=False)
    assert launch is False
    assert "HashMismatch" in err


@pytest.mark.unit
def test_unsigned_allowed_when_not_required():
    assert _update_decision("NotSigned", require_signed=False) == (True, None)


@pytest.mark.unit
def test_unsigned_rejected_when_required():
    launch, err = _update_decision("NotSigned", require_signed=True)
    assert launch is False
    assert "not signed" in err.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -m unit tests/test_apply_update.py -v`
Expected: FAIL — `cannot import name '_update_decision'`

- [ ] **Step 3: Write minimal implementation**

Add the pure decision function next to `_authenticode_status` (module level):

```python
def _update_decision(status: str, require_signed: bool):
    """Decide whether to launch the downloaded bundle given its signature status.

    Returns (should_launch: bool, error_message: str | None).
    """
    if status == "Valid":
        return True, None
    if status == "NotSigned":
        if require_signed:
            return False, "Update is not signed; refusing to install."
        return True, None  # transition period: allow with a warning logged by caller
    return False, f"Update signature check failed: {status}"
```

Add `QCoreApplication` to the QtCore import (lines 1-10):

```python
from PyQt6.QtCore import (
    QObject,
    pyqtSignal,
    pyqtProperty,
    pyqtSlot,
    QVariant,
    QThread,
    QTimer,
    QRecursiveMutex,
    QCoreApplication,
)
```

Add the slot + worker near `openDownloadUrl` (~line 3945). Keep `openDownloadUrl` for now (still referenced until Task 9 switches the QML):

```python
    @pyqtSlot(str)
    def applyUpdate(self, download_url: str):
        """Download the update bundle, verify it, and launch the in-place upgrade."""
        t = threading.Thread(
            target=self._apply_update_worker, args=(download_url,), daemon=True
        )
        t.start()

    def _apply_update_worker(self, download_url: str):
        import urllib.request
        import subprocess
        from utils import app_paths

        try:
            updates_dir = app_paths.writable_root() / "updates"
            updates_dir.mkdir(parents=True, exist_ok=True)
            dest = updates_dir / download_url.rsplit("/", 1)[-1]
            logger.info("Downloading update %s -> %s", download_url, dest)
            urllib.request.urlretrieve(download_url, str(dest))

            status = _authenticode_status(str(dest))
            should_launch, error = _update_decision(status, _REQUIRE_SIGNED_UPDATES)
            if not should_launch:
                self.updateCheckFailed.emit(error)
                return
            if status == "NotSigned":
                logger.warning("Update bundle is not signed (transition period) — proceeding")

            # Launch the Burn bundle detached, then quit so our files unlock and
            # the in-place major upgrade can replace them. Burn relaunches us.
            logger.info("Launching installer; quitting app for in-place upgrade")
            subprocess.Popen(
                [str(dest)],
                close_fds=True,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            )
            QCoreApplication.quit()
        except Exception as e:
            logger.error("applyUpdate failed: %s", e)
            self.updateCheckFailed.emit(str(e))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -m unit tests/test_apply_update.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add motion_connector.py tests/test_apply_update.py
git commit -m "feat: applyUpdate slot downloads, verifies, and runs in-place upgrade"
```

---

### Task 9: Point `UpdateBanner.qml` at the in-place updater

**Files:**
- Modify: `components/UpdateBanner.qml` (button label + `onClicked`)

QML has no hot-reload — verify by restarting the app (Task 16). No automated test.

- [ ] **Step 1: Change the button label and action**

In `components/UpdateBanner.qml`, change the download button text from `"Download"` to `"Update"`:

```qml
            Text {
                id: downloadBtn
                anchors.centerIn: parent
                text: "Update"
                color: theme.accentBlue
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
```

And change the click handler from `openDownloadUrl` to `applyUpdate` (note the singleton is `MotionInterface`, the name the existing line already uses):

```qml
                onClicked: MotionInterface.applyUpdate(banner.downloadUrl)
```

- [ ] **Step 2: Verify the app still loads the QML**

Run (from a conda env with the app deps; hardware not required to reach the banner code path, but the window must open):
`python main.py`
Expected: app launches with no QML error for `UpdateBanner.qml` in the console/log. Close the app.

- [ ] **Step 3: Commit**

```bash
git add components/UpdateBanner.qml
git commit -m "feat: UpdateBanner triggers in-place update instead of browser download"
```

---

## Phase C — WiX installer sources

### Task 10: App MSI source (`installer/app.wxs`)

**Files:**
- Create: `installer/app.wxs`

Built and verified in Task 13 (needs a `dist\` to harvest). This task just authors the source. Uses WiX v5 syntax; `$(var.X)` values are supplied by `wix build -d` flags in Task 13.

- [ ] **Step 1: Author the app MSI source**

```xml
<!-- installer/app.wxs — packages dist\OpenWaterApp\ into Program Files. -->
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package Name="$(var.ProductName)"
           Manufacturer="Openwater"
           Version="$(var.Version)"
           UpgradeCode="$(var.UpgradeCode)"
           Scope="perMachine"
           Compressed="yes">

    <MajorUpgrade AllowSameVersionUpgrades="yes"
                  DowngradeErrorMessage="A newer version of [ProductName] is already installed." />
    <MediaTemplate EmbedCab="yes" />

    <StandardDirectory Id="ProgramFiles64Folder">
      <Directory Id="VENDORFOLDER" Name="OpenWater">
        <Directory Id="APPFOLDER" Name="Bloodflow" />
      </Directory>
    </StandardDirectory>

    <StandardDirectory Id="ProgramMenuFolder">
      <Directory Id="AppShortcutFolder" Name="$(var.ProductName)" />
    </StandardDirectory>

    <Feature Id="Main" Title="$(var.ProductName)" Level="1">
      <ComponentGroupRef Id="AppFiles" />
      <ComponentRef Id="AppShortcut" />
    </Feature>

    <!-- Harvest the entire PyInstaller one-folder output. -->
    <ComponentGroup Id="AppFiles" Directory="APPFOLDER">
      <Files Include="$(var.SourceDir)\**" />
    </ComponentGroup>

    <Component Id="AppShortcut" Directory="AppShortcutFolder" Guid="*">
      <Shortcut Id="StartMenuShortcut"
                Name="$(var.ProductName)"
                Target="[APPFOLDER]OpenWaterApp.exe"
                WorkingDirectory="APPFOLDER" />
      <RemoveFolder Id="RemoveAppShortcutFolder" On="uninstall" />
      <RegistryValue Root="HKLM" Key="Software\Openwater\Bloodflow"
                     Name="installed" Type="integer" Value="1" KeyPath="yes" />
    </Component>
  </Package>
</Wix>
```

- [ ] **Step 2: Commit**

```bash
git add installer/app.wxs
git commit -m "feat: WiX app MSI source (Program Files, major-upgrade)"
```

---

### Task 11: Burn bundle source (`installer/bundle.wxs`)

**Files:**
- Create: `installer/bundle.wxs`

Chains the driver MSI → app MSI. Burn auto-detects the driver MSI's install state from its ProductCode, so it is skipped when already current — no manual `DetectCondition` needed. The BAL extension provides the standard UI.

- [ ] **Step 1: Author the bundle source**

```xml
<!-- installer/bundle.wxs — one setup.exe: driver MSI then app MSI. -->
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs"
     xmlns:bal="http://wixtoolset.org/schemas/v4/wxs/bal">
  <Bundle Name="$(var.ProductName)"
          Manufacturer="Openwater"
          Version="$(var.Version)"
          UpgradeCode="$(var.BundleUpgradeCode)">

    <BootstrapperApplication>
      <bal:WixStandardBootstrapperApplication Theme="hyperlinkLicense"
                                              LicenseUrl="" />
    </BootstrapperApplication>

    <Chain>
      <!-- WinUSB driver. Vital but its absence on uninstall is fine (Permanent). -->
      <MsiPackage Id="DriverMsi"
                  SourceFile="$(var.DriverMsi)"
                  Vital="yes"
                  Permanent="yes"
                  Visible="no" />
      <!-- The app itself. -->
      <MsiPackage Id="AppMsi"
                  SourceFile="$(var.AppMsi)"
                  Vital="yes" />
    </Chain>
  </Bundle>
</Wix>
```

- [ ] **Step 2: Commit**

```bash
git add installer/bundle.wxs
git commit -m "feat: WiX Burn bundle chaining driver MSI + app MSI"
```

---

### Task 12: Skippable signing helper (`installer/sign.ps1`)

**Files:**
- Create: `installer/sign.ps1`

A no-op until `CODESIGN_THUMBPRINT` is set; signs the given files when it is. Bundle signing (insignia detach/reattach) lives in the build script (Task 13) because it is multi-step.

- [ ] **Step 1: Author the helper**

```powershell
# installer/sign.ps1 — Authenticode-sign the given files, or skip if no cert.
param([Parameter(Mandatory = $true)][string[]]$Files)

$thumb = $env:CODESIGN_THUMBPRINT
if (-not $thumb) {
    Write-Host "signing skipped (CODESIGN_THUMBPRINT not set)" -ForegroundColor Yellow
    return
}

foreach ($f in $Files) {
    Write-Host "signing $f" -ForegroundColor Cyan
    & signtool sign /sha1 $thumb /fd SHA256 `
        /tr http://timestamp.digicert.com /td SHA256 $f
    if ($LASTEXITCODE -ne 0) { throw "signtool failed for $f" }
}
```

- [ ] **Step 2: Verify it no-ops cleanly**

Run: `powershell -NoProfile -File installer/sign.ps1 -Files installer/sign.ps1`
Expected: prints `signing skipped (CODESIGN_THUMBPRINT not set)` and exits 0.

- [ ] **Step 3: Commit**

```bash
git add installer/sign.ps1
git commit -m "feat: skippable Authenticode signing helper"
```

---

### Task 13: Installer build orchestrator (`installer/build_installer.ps1`)

**Files:**
- Create: `installer/build_installer.ps1`

Resolves the numeric `X.Y.Z`, extracts the driver MSI from the zip, builds the app MSI + bundle for the requested variant, signs the launcher/MSI, then signs the bundle via the insignia dance. Constant GUIDs are stored here (distinct per variant).

- [ ] **Step 1: Author the orchestrator**

```powershell
# installer/build_installer.ps1 — build the app MSI + Burn bundle for one variant.
param(
    [ValidateSet("clinical", "ruo")][string]$Variant = "clinical",
    [string]$DistDir = "dist\OpenWaterApp"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
Set-Location $root

# ── constant, never-changing GUIDs (distinct per variant so they never
#    cross-upgrade). Generate once with [guid]::NewGuid() and paste here. ──
$guids = @{
    clinical = @{
        ProductName       = "OpenWater Bloodflow"
        UpgradeCode       = "11111111-1111-1111-1111-111111111111"
        BundleUpgradeCode = "22222222-2222-2222-2222-222222222222"
        Suffix            = ""
    }
    ruo = @{
        ProductName       = "OpenWater Bloodflow (RUO)"
        UpgradeCode       = "33333333-3333-3333-3333-333333333333"
        BundleUpgradeCode = "44444444-4444-4444-4444-444444444444"
        Suffix            = "_RUO"
    }
}
$g = $guids[$Variant]

# ── numeric X.Y.Z from the git tag/describe (drop any -dev/-rc suffix) ──
try {
    $desc = (git describe --tags --always 2>$null).Trim()
} catch { $desc = "" }
if ($desc -match '(\d+)\.(\d+)\.(\d+)') {
    $version = "$($matches[1]).$($matches[2]).$($matches[3])"
} else {
    $version = "0.0.0"
}
Write-Host "Variant=$Variant  Version=$version  Product='$($g.ProductName)'" -ForegroundColor Green

# ── extract the driver MSI from the bundled zip ──
$drvZip = "resources\OpenMotionDriver-x64.zip"
$drvDir = "build\driver"
Remove-Item -Recurse -Force $drvDir -ErrorAction SilentlyContinue
Expand-Archive -Path $drvZip -DestinationPath $drvDir -Force
$driverMsi = Join-Path $drvDir "OpenMotionDriver-x64.msi"
if (-not (Test-Path $driverMsi)) { throw "driver MSI not found in $drvZip" }

# ── output names ──
$outDir = "build\installer"
New-Item -ItemType Directory -Force $outDir | Out-Null
$appMsi    = Join-Path $outDir "OpenWaterApp$($g.Suffix).msi"
$bundleExe = Join-Path $outDir "OpenWater-Setup-$version$($g.Suffix).exe"

# ── build the app MSI ──
wix build installer\app.wxs -o $appMsi `
    -d "ProductName=$($g.ProductName)" `
    -d "Version=$version" `
    -d "UpgradeCode=$($g.UpgradeCode)" `
    -d "SourceDir=$DistDir"
if ($LASTEXITCODE -ne 0) { throw "app MSI build failed" }

# sign the app MSI (skippable)
powershell -NoProfile -File installer\sign.ps1 -Files $appMsi

# ── build the Burn bundle ──
wix build installer\bundle.wxs -o $bundleExe -ext WixToolset.Bal.wixext `
    -d "ProductName=$($g.ProductName)" `
    -d "Version=$version" `
    -d "BundleUpgradeCode=$($g.BundleUpgradeCode)" `
    -d "DriverMsi=$driverMsi" `
    -d "AppMsi=$appMsi"
if ($LASTEXITCODE -ne 0) { throw "bundle build failed" }

# ── sign the bundle (insignia detach → sign engine → reattach → sign) ──
if ($env:CODESIGN_THUMBPRINT) {
    $engine = Join-Path $outDir "engine.exe"
    wix burn detach $bundleExe -engine $engine
    powershell -NoProfile -File installer\sign.ps1 -Files $engine
    wix burn reattach $bundleExe -engine $engine -o $bundleExe
    powershell -NoProfile -File installer\sign.ps1 -Files $bundleExe
} else {
    Write-Host "bundle signing skipped (no cert)" -ForegroundColor Yellow
}

Write-Host "Built $bundleExe" -ForegroundColor Green
```

- [ ] **Step 2: Install WiX v5 and the BAL extension locally**

Run:
```
dotnet tool install --global wix
wix extension add -g WixToolset.Bal.wixext
```
Expected: both succeed (or report "already installed").

- [ ] **Step 3: Build a fresh `dist\` then build the clinical installer**

Run:
```
python -m PyInstaller -y openwater.spec
powershell -NoProfile -File installer\build_installer.ps1 -Variant clinical
```
Expected: `build\installer\OpenWater-Setup-<ver>.exe` is produced; "bundle signing skipped (no cert)" printed.

- [ ] **Step 4: Build the RUO installer**

Flip `reducedMode` in the built config, then build the RUO bundle:
```
$cfg = "dist\OpenWaterApp\_internal\config\app_config.json"
(Get-Content -Raw $cfg) -replace '"reducedMode"\s*:\s*true', '"reducedMode": false' | Set-Content -NoNewline -Encoding UTF8 $cfg
powershell -NoProfile -File installer\build_installer.ps1 -Variant ruo
```
Expected: `build\installer\OpenWater-Setup-<ver>_RUO.exe` produced. (Restore the config afterward or rebuild `dist\` for the clinical artifact.)

- [ ] **Step 5: Commit**

```bash
git add installer/build_installer.ps1
git commit -m "feat: installer build orchestrator (app MSI + Burn bundle, both variants)"
```

---

## Phase D — CI

### Task 14: Build installers in `release-build.yml`

**Files:**
- Modify: `.github/workflows/release-build.yml`

Add WiX install + installer builds after the existing PyInstaller step, and publish the two `setup.exe` files as release assets alongside the driver zip. The existing RUO config-flip step (lines 132-151) is reused to produce the RUO `dist\` before building the RUO bundle.

- [ ] **Step 1: Add a WiX setup step after "Build with PyInstaller" (after line 99)**

```yaml
      - name: Install WiX v5
        shell: pwsh
        run: |
          dotnet tool install --global wix
          wix extension add -g WixToolset.Bal.wixext
          echo "$env:USERPROFILE\.dotnet\tools" | Out-File -FilePath $env:GITHUB_PATH -Append

      - name: Build clinical installer
        shell: pwsh
        env:
          CODESIGN_THUMBPRINT: ${{ secrets.CODESIGN_THUMBPRINT }}
        run: |
          powershell -NoProfile -File installer\build_installer.ps1 -Variant clinical

      - name: Build RUO installer
        shell: pwsh
        env:
          CODESIGN_THUMBPRINT: ${{ secrets.CODESIGN_THUMBPRINT }}
        run: |
          $cfg = "dist\OpenWaterApp\_internal\config\app_config.json"
          $orig = Get-Content -Raw $cfg
          $ruo = $orig -replace '"reducedMode"\s*:\s*true', '"reducedMode": false'
          if ($ruo -eq $orig) { throw "RUO build: did not find reducedMode: true to flip" }
          Set-Content -Path $cfg -Value $ruo -Encoding UTF8 -NoNewline
          try {
            powershell -NoProfile -File installer\build_installer.ps1 -Variant ruo
          } finally {
            Set-Content -Path $cfg -Value $orig -Encoding UTF8 -NoNewline
          }
```

Note: `CODESIGN_THUMBPRINT` is an as-yet-unset repo secret; until it exists the signing steps no-op (Task 12). When a cloud signing service is chosen instead of a local thumbprint, swap `sign.ps1`'s body accordingly — the workflow wiring stays.

- [ ] **Step 2: Add the installers to the release `files:` list (lines 202-205)**

```yaml
          files: |
            ${{ steps.meta.outputs.ARTIFACT_ZIP }}
            ${{ steps.meta.outputs.ARTIFACT_ZIP_RUO }}
            build/installer/OpenWater-Setup-*.exe
            resources/OpenMotionDriver-x64.zip
```

(The legacy `.zip` artifacts stay for one transition release per the spec; remove those two lines in a later release.)

- [ ] **Step 3: Validate the workflow YAML**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/release-build.yml'))"`
Expected: no exception (valid YAML).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/release-build.yml
git commit -m "ci: build and publish WiX installers for both variants"
```

---

## Phase E — Full-suite check + manual verification

### Task 15: Run the unit suite

**Files:** none

- [ ] **Step 1: Run all new unit tests together**

Run: `python -m pytest -m unit tests/test_app_paths.py tests/test_config_store.py tests/test_main_config_wiring.py tests/test_connector_save_overrides.py tests/test_update_asset_select.py tests/test_authenticode_status.py tests/test_apply_update.py -v`
Expected: all PASS, no app launch (unit-marked).

- [ ] **Step 2: Lint touched Python**

Run: `python -m flake8 utils/app_paths.py utils/config_store.py main.py motion_connector.py`
Expected: no errors (match existing style; the repo pins flake8 7.1.1).

- [ ] **Step 3: Commit any lint fixes**

```bash
git add -A
git commit -m "chore: lint installer/updater changes" || echo "nothing to commit"
```

---

### Task 16: Manual clean-VM verification (per spec verification plan)

**Files:** none — this is the acceptance gate for the installer/updater that cannot be unit-tested.

- [ ] **Step 1: Fresh install on a clean Windows VM (no prior app, no driver)**

Copy `build\installer\OpenWater-Setup-<ver>.exe` to a clean VM and run it.
Expected: single UAC prompt; on completion the WinUSB driver is present (Device Manager), the app is under `C:\Program Files\OpenWater\Bloodflow\`, and a Start-menu shortcut exists.

- [ ] **Step 2: Verify writable-state relocation**

Launch the app, double-click the logo, enter the developer password to unlock developer mode, close and relaunch.
Expected: `C:\ProgramData\OpenWater\app_config.local.json` exists and contains `{"developerMode": true}` (only the changed key); the bundled `Program Files\...\_internal\config\app_config.json` is unchanged; developer mode persists across the restart.

- [ ] **Step 3: Verify scan data + logs land in ProgramData**

Run any scan / let the app log.
Expected: `C:\ProgramData\OpenWater\app-logs\ow-bloodflowapp-*.log` and scan output / `scans.db` appear under `C:\ProgramData\OpenWater\` (not under Program Files).

- [ ] **Step 4: Verify the in-place update flow (RUO build)**

Install the **RUO** bundle (its banner is the one that appears — clinical suppresses it per issue #96). Publish a newer release (higher tag) to the GitHub repo, then in the running app wait for / trigger the update banner and click **Update**.
Expected: bundle downloads to `C:\ProgramData\OpenWater\updates\`, one UAC prompt, in-place upgrade, app relaunches reporting the new version; ProgramData state preserved.

- [ ] **Step 5: Verify RUO ↔ clinical do not cross-upgrade**

On a box with the clinical product installed, run the RUO bundle.
Expected: both appear as separate entries in "Apps & features" (distinct ProductCodes); neither replaced the other.

- [ ] **Step 6: Record results**

Note the outcomes of Steps 1-5 in the PR description. Any failure → return to the relevant task before merging.

---

## Self-Review notes (for the implementer)

- **Signing is intentionally unfinished:** every artifact gets a *skippable* signing pass (Tasks 12-14) and the updater allows `NotSigned` bundles through with a warning (`_REQUIRE_SIGNED_UPDATES = False`, Task 6). When the EV cert lands: set the `CODESIGN_THUMBPRINT` secret (or swap `sign.ps1` for the chosen cloud signer) and flip `_REQUIRE_SIGNED_UPDATES = True`.
- **GUIDs in Task 13 are placeholders to replace once:** generate four real GUIDs with `[guid]::NewGuid()` and paste them in before the first real release; thereafter they are constant forever.
- **Hand-off to workstream #2 (tamper):** the overrides-file hardening (ACLs / signing), `developerMode` persistence policy, and password-constant handling are out of scope here (spec §Out of scope).
- **Clinical builds never auto-update** by existing design (issue #96) — the in-app updater is exercised only on the RUO build (Task 16 Step 4).
