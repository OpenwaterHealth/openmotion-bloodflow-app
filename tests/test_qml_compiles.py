"""Every app QML file compiles against the installed Qt (#778).

tests/test_nuitka_qt_trim.py runs the same check against the trimmed Qt
tree, but that module skips without Nuitka, so a dev environment without
Nuitka never compiled the QML at all. #773 shipped a WindowMenu.qml that
used a QtQuick 6.7 property under `import QtQuick 6.0`, and the app could
not start until it was reverted. This needs only PyQt6."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_app_qml_compiles_against_the_installed_qt():
    PyQt6 = pytest.importorskip("PyQt6")
    qml_dir = Path(PyQt6.__file__).parent / "Qt6" / "qml"
    if not qml_dir.is_dir():
        pytest.skip(f"no QML import directory at {qml_dir}")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" / "qml_import_probe.py"), str(qml_dir), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
