"""A portable build's export Save dialogs open in its own folder (#723).

Qt's Windows file dialog keeps only the file name of a QML FileDialog's
``selectedFile``; the folder comes from ``currentFolder`` alone, else from
the last folder Windows remembers for ``Open-Motion.exe``. Every build
ships under that name, so several portable zips side by side opened each
other's data folders. A portable build therefore sets ``currentFolder`` to
``MotionInterface.directory`` before each open; an installed build leaves
it unset so Windows keeps the user's last export folder (e.g. a USB stick).

Loads the real components/LogsModal.qml (Audit Log export) and
components/HistoryModal.qml (single-scan export) in a bare offscreen
QQmlEngine with a stubbed MotionInterface and clicks Export. Offscreen Qt
has no native file dialog: ``open()`` builds a fallback helper that fails
without a window, and from then on ``currentFolder`` reads back empty, so
the real dialog cannot report what it was opened with. This engine
therefore shadows ``QtQuick.Dialogs`` with a stand-in that records
``currentFolder`` at ``open()``, the value the Windows helper hands to
``IFileDialog::SetFolder``. The multi-scan FolderDialog binds its folder
already and is not covered here.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import contextlib
import os
import sys
from pathlib import Path

# A QGuiApplication must exist before any QML Quick item is created (see
# test_logs_modal_filters.py for why it is made at import time, via argv).
from PyQt6.QtCore import (  # noqa: E402
    QCoreApplication,
    QMetaObject,
    QObject,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QGuiApplication  # noqa: E402

if QCoreApplication.instance() is None:
    _qt_app = QGuiApplication([sys.argv[0], "-platform", "offscreen"])
else:
    _qt_app = QCoreApplication.instance()

import pytest  # noqa: E402
from PyQt6.QtQml import (  # noqa: E402
    QQmlComponent,
    QQmlEngine,
    qmlRegisterSingletonInstance,
    qmlRegisterSingletonType,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
LOGS_MODAL_QML = REPO_ROOT / "components" / "LogsModal.qml"
HISTORY_QML = REPO_ROOT / "components" / "HistoryModal.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"

# Stand-in for the QtQuick.Dialogs types the two modals use (FileDialog,
# FolderDialog, MessageDialog): their members, plus what the dialog held
# when it was opened.
_FAKE_DIALOGS_QMLDIR = """\
module QtQuick.Dialogs
FileDialog 6.0 Dialog.qml
FolderDialog 6.0 Dialog.qml
MessageDialog 6.0 Dialog.qml
"""
_FAKE_DIALOG_QML = """\
import QtQuick
QtObject {
    enum FileMode { OpenFile, OpenFiles, SaveFile }
    property string title
    property string text
    property int fileMode
    property var nameFilters
    property url currentFolder
    property url selectedFile
    property url selectedFolder
    property url openedInFolder
    property int openCount: 0
    signal accepted()
    signal rejected()
    function open() { openedInFolder = currentFolder; openCount += 1 }
}
"""

_SESSION_ID = 7
_SCANS = [
    {"sessionId": _SESSION_ID, "label": "owABC", "userLabel": "Subject 1",
     "timestamp": "2026-10-23T09:00:00", "clinicalMode": True,
     "interrupted": False},
]


class _StubMotionInterface(QObject):
    """What LogsModal, HistoryModal and AppTheme read, with the derived
    ``portableMode`` switchable per test."""

    directoryChanged = pyqtSignal()
    appConfigChanged = pyqtSignal()
    pastScanLoadFinished = pyqtSignal(str, bool)
    scanCsvExportFinished = pyqtSignal(bool, str)
    scansExportFinished = pyqtSignal(int, int, str)
    errorOccurred = pyqtSignal(str)
    _neverEmitted = pyqtSignal()

    def __init__(self, directory, parent=None):
        super().__init__(parent)
        self._directory = directory
        self._portable = False

    def set_portable(self, portable):
        self._portable = portable
        self.appConfigChanged.emit()

    @pyqtProperty("QVariantMap", notify=appConfigChanged)
    def appConfig(self):
        return {"darkMode": True, "clinicalMode": True,
                "engineeringMode": False, "portableMode": self._portable}

    @pyqtProperty(str, notify=directoryChanged)
    def directory(self):
        return self._directory

    @pyqtProperty(int, notify=_neverEmitted)
    def state(self):
        return 0

    @pyqtSlot()
    def recordAuditLogViewed(self):
        pass

    @pyqtSlot(result="QVariantList")
    def auditEventTypes(self):
        return []

    @pyqtSlot("QVariantMap", result="QVariantList")
    def filteredAuditLogEntries(self, filters):
        return []

    @pyqtSlot(result="QVariantList")
    def get_scan_sessions(self):
        return list(_SCANS)

    @pyqtSlot(int, result="QVariantMap")
    def get_session_stats(self, session_id):
        return {"sampleCount": 0}

    @pyqtSlot(str, str)
    def notify(self, message, level):
        pass


@contextlib.contextmanager
def _basic_controls_style():
    prev = os.environ.get("QT_QUICK_CONTROLS_STYLE")
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Basic"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("QT_QUICK_CONTROLS_STYLE", None)
        else:
            os.environ["QT_QUICK_CONTROLS_STYLE"] = prev


@pytest.fixture(scope="module")
def qml(tmp_path_factory):
    """Compile both modals once against one stub; hand out fresh instances."""
    # A real folder, held as the str(Path) the connector holds (backslashes
    # on Windows).
    directory = str(tmp_path_factory.mktemp("Open-Motion-portable"))
    imports = tmp_path_factory.mktemp("qml-imports")
    fake = imports / "QtQuick" / "Dialogs"
    fake.mkdir(parents=True)
    (fake / "qmldir").write_text(_FAKE_DIALOGS_QMLDIR)
    (fake / "Dialog.qml").write_text(_FAKE_DIALOG_QML)

    stub = _StubMotionInterface(directory)
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0, "AppTheme",
    )
    engine = QQmlEngine()
    engine.addImportPath(str(imports))   # first in the list: shadows Qt's
    components = {}
    with _basic_controls_style():
        for name, path in (("logs", LOGS_MODAL_QML), ("history", HISTORY_QML)):
            component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
            if component.isError():
                raise RuntimeError(
                    f"{path.name} failed to compile:\n"
                    + "\n".join(e.toString() for e in component.errors())
                )
            components[name] = component

    created = []

    def make(name):
        with _basic_controls_style():
            obj = components[name].create()
        if obj is None:
            raise RuntimeError(
                "\n".join(e.toString() for e in components[name].errors()))
        created.append(obj)
        return obj

    make.stub = stub
    yield make

    for obj in created:
        obj.deleteLater()
    del engine


def _export_button(modal):
    buttons = [o for o in modal.findChildren(QObject)
               if o.inherits("QQuickAbstractButton")
               and str(o.property("text")).startswith("Export CSV")]
    assert len(buttons) == 1, f"expected one Export CSV button, got {buttons}"
    return buttons[0]


def _dialog(modal, object_name):
    d = modal.findChild(QObject, object_name)
    assert d is not None, f"{object_name!r} not found"
    # The stand-in, not Qt's dialog: the import path shadowing held.
    assert d.metaObject().indexOfProperty("openedInFolder") >= 0
    return d


def _local(url):
    return os.path.normcase(os.path.normpath(QUrl(url).toLocalFile()))


def _audit_log(qml):
    modal = qml("logs")
    QMetaObject.invokeMethod(modal, "open")
    return modal, _dialog(modal, "auditExportDialog"), "audit_log.csv"


def _history_single_scan(qml):
    modal = qml("history")
    QMetaObject.invokeMethod(modal, "open")
    # One checked row: the single-scan Save As path, not the FolderDialog.
    modal.setProperty("checked", {str(_SESSION_ID): True})
    modal.setProperty("checkedCount", 1)
    return modal, _dialog(modal, "scanExportDialog"), "owABC_export.csv"


def _click_export(modal, dialog):
    opens = dialog.property("openCount")
    QMetaObject.invokeMethod(_export_button(modal), "click")
    assert dialog.property("openCount") == opens + 1, "Export did not open"


_EXPORTS = [
    pytest.param(_audit_log, id="audit-log"),
    pytest.param(_history_single_scan, id="history-single-scan"),
]


@pytest.mark.parametrize("setup", _EXPORTS)
def test_portable_build_opens_dialog_in_its_own_folder(qml, setup):
    qml.stub.set_portable(True)
    modal, dialog, file_name = setup(qml)
    own_folder = _local(QUrl.fromLocalFile(qml.stub.directory))

    _click_export(modal, dialog)
    assert _local(dialog.property("openedInFolder")) == own_folder
    assert QUrl(dialog.property("selectedFile")).fileName() == file_name

    # Every time: after the user browsed elsewhere, the next export
    # still opens in this build's folder.
    dialog.setProperty("currentFolder", QUrl.fromLocalFile(
        str(Path(qml.stub.directory).parent)))
    _click_export(modal, dialog)
    assert _local(dialog.property("openedInFolder")) == own_folder


@pytest.mark.parametrize("setup", _EXPORTS)
def test_installed_build_leaves_the_dialog_folder_to_windows(qml, setup):
    qml.stub.set_portable(False)
    modal, dialog, file_name = setup(qml)

    _click_export(modal, dialog)
    # Never set: Windows opens the folder the user last exported to.
    assert QUrl(dialog.property("openedInFolder")).isEmpty()
    assert QUrl(dialog.property("selectedFile")).fileName() == file_name
