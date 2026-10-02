"""Compile the app's QML against one QML import directory (#680).

    python tests/qml_import_probe.py <qml import dir> <repo root>

Run in a fresh process by tests/test_nuitka_qt_trim.py. A module that is
already loaded in a process can resolve without its directory, so an
in-process check would pass a trimmed tree that the shipped exe cannot use.
The default import paths are replaced by the given directory (the Qt
resource paths stay, they are compiled into the Qt DLLs). Every .qml file
of the app is compiled, not created, with the Controls style main.py pins.
That resolves every import and type without running app logic. Prints one
line per file that fails, "<file>: <error>", and exits 1 if any did.
"""

import os
import sys
from pathlib import Path

qml_dir, repo = Path(sys.argv[1]), Path(sys.argv[2])
os.environ["QT_QUICK_CONTROLS_STYLE"] = "Material"

from PyQt6.QtCore import QObject, QUrl  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtQml import (  # noqa: E402
    QQmlComponent,
    QQmlEngine,
    qmlRegisterSingletonInstance,
    qmlRegisterSingletonType,
)

app = QGuiApplication([sys.argv[0], "-platform", "offscreen"])
engine = QQmlEngine()
engine.setImportPathList(
    [p for p in engine.importPathList() if p.startswith(("qrc:", ":"))] + [str(qml_dir)]
)
# The OpenMotion module that main.py registers. Compiling needs the names,
# not the connector's API.
stub = QObject()
qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
qmlRegisterSingletonType(
    QUrl.fromLocalFile(str(repo / "components" / "AppTheme.qml")), "OpenMotion", 1, 0, "AppTheme"
)

files = [repo / "main.qml"] + sorted((repo / "pages").rglob("*.qml")) + sorted(
    (repo / "components").rglob("*.qml")
)
failed = 0
for f in files:
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(f)), QQmlComponent.CompilationMode.PreferSynchronous)
    if component.isError():
        failed += 1
        for error in component.errors():
            print(f"{f.relative_to(repo).as_posix()}: {error.toString()}")
print(f"compiled {len(files) - failed}/{len(files)} files", file=sys.stderr)
sys.exit(1 if failed else 0)
