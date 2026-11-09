"""History "Aggregate sessions" grouping and group checkboxes.

Loads the real components/HistoryModal.qml in a bare offscreen QQmlEngine
with a stubbed MotionInterface (same harness as
test_history_delete_confirm.py) and drives the grouping functions directly
by setting `view`, so no scan DB is involved.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import sys
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QObject, QUrl, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QGuiApplication

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
HISTORY_QML = REPO_ROOT / "components" / "HistoryModal.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"


class _StubMotionInterface(QObject):
    _neverEmitted = pyqtSignal()

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        return {"darkMode": True, "clinicalMode": False, "engineeringMode": False}

    @pyqtProperty(str, notify=_neverEmitted)
    def directory(self):
        return ""

    @pyqtProperty(int, notify=_neverEmitted)
    def state(self):
        return 0


def _scan(sid, label, date, dur=60, cfg=("Near", "Far")):
    return {"sessionId": sid, "userLabel": label, "label": f"scan_{sid}",
            "dateTime": date, "durationSec": dur,
            "configL": cfg[0], "configR": cfg[1], "interrupted": False}


VIEW = [
    _scan(1, "A", "2026-11-08 10:00", 60),
    _scan(2, "B", "2026-11-07 10:00", 30),
    _scan(3, "A", "2026-11-05 10:00", 90, ("Near", "Near")),
    _scan(4, "", "2026-11-04 10:00"),
    _scan(5, "", "2026-11-03 10:00"),
]


@pytest.fixture
def history():
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0, "AppTheme",
    )
    engine = QQmlEngine()
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(HISTORY_QML)))
    obj = component.create()
    if obj is None:
        raise RuntimeError("\n".join(e.toString() for e in component.errors()))
    obj.setProperty("view", VIEW)
    obj.rebuildRows()
    yield obj
    obj.deleteLater()
    del engine


def _rows(history):
    return history.property("rows").toVariant()


def test_flat_mode_lists_every_scan(history):
    rows = _rows(history)
    assert [r["kind"] for r in rows] == ["scan"] * 5
    assert [r["scan"]["sessionId"] for r in rows] == [1, 2, 3, 4, 5]


def test_aggregate_groups_repeated_labels_in_view_order(history):
    history.setProperty("aggregate", True)
    rows = _rows(history)
    # "A" (2 scans) is a collapsed group; "B" has one scan so stays a plain
    # row; the two unlabeled scans group under the empty key.
    assert [(r["kind"], r.get("key")) for r in rows] == [
        ("group", "A"), ("scan", None), ("group", "")]
    a = rows[0]
    assert a["ids"] == [1, 3] and a["count"] == 2 and a["totalSec"] == 150
    assert a["dateText"] == "2026-11-05 – 11-08"
    assert a["configText"] == "Mixed"
    assert rows[2]["configText"] == "Near / Far"
    assert rows[2]["dateText"] == "2026-11-03 – 11-04"


def test_expanding_a_group_nests_its_scans(history):
    history.setProperty("aggregate", True)
    history.toggleGroup("A")
    rows = _rows(history)
    assert [r["kind"] for r in rows[:3]] == ["group", "scan", "scan"]
    assert rows[0]["open"] is True
    assert [r["scan"]["sessionId"] for r in rows[1:3]] == [1, 3]
    assert all(r["nested"] for r in rows[1:3])
    history.toggleGroup("A")
    assert _rows(history)[1]["kind"] == "scan"
    assert _rows(history)[1]["scan"]["sessionId"] == 2


def test_group_checkbox_checks_and_clears_its_scans(history):
    history.setProperty("aggregate", True)
    history.toggleCheck(2)                     # outside the group
    history.setGroupChecked([1, 3], True)
    assert history.property("checkedCount") == 3
    assert history.groupCheckedCount([1, 3]) == 2
    history.setGroupChecked([1, 3], False)
    assert history.property("checkedCount") == 1
    assert history.groupCheckedCount([1, 3]) == 0
