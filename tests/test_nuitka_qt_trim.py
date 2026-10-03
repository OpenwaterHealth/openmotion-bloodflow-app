"""The Nuitka build bundles only the Qt modules and plugins the app uses
(#680). The qt-trim user plugin (scripts/nuitka_qt_trim.py) applies
allow-lists to the installed PyQt6 wheel. A wrong list fails only in the
shipped exe, at launch or when a modal first opens, so it is pinned here
against the same wheel: the app's imports must be kept, the kept set must
be closed under Qt's own dependencies, the drop decisions must hit exactly
the dropped files, and the app's QML must compile against a tree that holds
nothing but the kept modules. The plugin's Nuitka hooks only run inside a
real build, so their behaviour is pinned here too."""

import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
pytest.importorskip("nuitka", reason="the plugin subclasses Nuitka's plugin base")
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import nuitka_qt_trim as trim  # noqa: E402

_IMPORT = re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)", re.MULTILINE)


def _app_qml_files():
    return (
        [REPO_ROOT / "main.qml"]
        + sorted((REPO_ROOT / "pages").rglob("*.qml"))
        + sorted((REPO_ROOT / "components").rglob("*.qml"))
    )


def _qt_root():
    pytest.importorskip("PyQt6")
    return trim.qt_root()


def _nuitka_dest(*parts):
    """A destination path the way Nuitka spells it on this OS."""
    return os.path.normpath("/".join(parts))


def _qml_files_by_owner(qml_dir):
    """[(file, directory of the module that owns it or None)]."""
    module_dirs = list(trim.qml_modules(qml_dir).values())
    out = []
    for f in qml_dir.rglob("*"):
        if f.is_file():
            rel = f.relative_to(qml_dir).as_posix()
            owner = max((d for d in module_dirs if rel.startswith(d + "/")), key=len, default=None)
            out.append((f, owner))
    return out


def _write_module(qml_dir, rel, qmldir_lines=(), files=None):
    d = qml_dir / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "qmldir").write_text(
        "\n".join([f"module {rel.replace('/', '.')}", *qmldir_lines]) + "\n", encoding="utf-8"
    )
    for name, text in (files or {}).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text, encoding="utf-8")


def _fake_qt(tmp_path, modules=("QtQuick",), plugins=("platforms/qwindows", "platforms/qminimal")):
    for m in modules:
        _write_module(tmp_path / "qml", m.replace(".", "/"))
    for p in plugins:
        (tmp_path / "plugins" / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "plugins" / f"{p}.dll").write_bytes(b"MZ")
    return tmp_path


# --- the allow-list against the app ----------------------------------------


def test_every_module_the_app_qml_imports_is_kept():
    imported = set()
    for f in _app_qml_files():
        imported.update(_IMPORT.findall(f.read_text(encoding="utf-8")))
    imported.discard("OpenMotion")  # registered by main.py, not a Qt module
    assert imported, "found no QML imports at all"
    assert sorted(imported - set(trim.QML_MODULES)) == []


def test_the_kept_style_is_the_one_main_py_pins():
    main_py = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    pinned = re.search(r'"QT_QUICK_CONTROLS_STYLE":\s*"(\w+)"', main_py)
    assert pinned, "main.py no longer pins QT_QUICK_CONTROLS_STYLE"
    assert pinned.group(1) == trim.CONTROLS_STYLE
    assert f"QtQuick.Controls.{trim.CONTROLS_STYLE}" in trim.QML_MODULES


# --- the allow-list against the installed Qt --------------------------------


def test_kept_modules_need_no_module_that_is_dropped():
    assert trim.closure_violations(_qt_root() / "qml") == []


def test_no_file_of_a_kept_module_is_dropped():
    root = _qt_root()
    decisions = trim.Trim(root)
    kept = {trim.qml_modules(root / "qml")[m] for m in trim.QML_MODULES}
    hits = [
        f for f, owner in _qml_files_by_owner(root / "qml")
        if owner in kept
        and decisions.drops_dll(_nuitka_dest(trim.QML_DEST, f.relative_to(root / "qml").as_posix()))
    ]
    assert hits == []


def test_every_file_of_a_dropped_module_is_dropped():
    root = _qt_root()
    decisions = trim.Trim(root)
    kept = {trim.qml_modules(root / "qml")[m] for m in trim.QML_MODULES}
    missed = [
        f for f, owner in _qml_files_by_owner(root / "qml")
        if owner is not None and owner not in kept
        and not decisions.drops_qml(_nuitka_dest(trim.QML_DEST, f.relative_to(root / "qml").as_posix()))
    ]
    assert missed == []


def test_the_big_unused_modules_are_dropped():
    qml_dir = _qt_root() / "qml"
    dropped = set(trim.dropped_qml_dirs(qml_dir))
    present = set(trim.qml_modules(qml_dir).values())
    for d in (
        "QtQuick3D", "QtQuick/Pdf", "QtMultimedia", "QtWebSockets", "QtWebChannel",
        "QtRemoteObjects", "QtSensors", "QtPositioning", "QtTextToSpeech", "QtTest",
        "QtCharts", "QtQuick/Particles", "QtQuick/Controls/Fusion",
    ):
        if d in present:
            assert d in dropped, d


def test_only_the_kept_qt_plugins_survive():
    root = _qt_root()
    decisions = trim.Trim(root)
    survivors = sorted(
        f"{dll.parent.name}/{dll.stem}"
        for dll in (root / "plugins").glob("*/*.dll")
        if not decisions.drops_plugin(_nuitka_dest(trim.PLUGINS_DEST, dll.parent.name, dll.name))
    )
    expected = sorted(f"{fam}/{name}" for fam, names in trim.QT_PLUGINS.items() for name in names)
    assert survivors == expected


def test_the_app_qml_compiles_against_only_the_kept_modules(tmp_path):
    """Builds the QML tree the exe ships and compiles every app file against
    it in a fresh process. The second half proves the probe can fail: drop
    one kept module and the files that import it no longer compile."""
    qml_dir = _qt_root() / "qml"
    dropped = trim.dropped_qml_dirs(qml_dir)

    def tree(dest, drop=()):
        skip = set(dropped) | set(drop)

        def ignore(d, names):
            rel = Path(d).relative_to(qml_dir).as_posix()
            prefix = "" if rel == "." else rel + "/"
            return [n for n in names if prefix + n in skip]

        shutil.copytree(qml_dir, dest, ignore=ignore)
        return dest

    def probe(d):
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "tests" / "qml_import_probe.py"), str(d), str(REPO_ROOT)],
            capture_output=True, text=True, timeout=120,
        )

    ok = probe(tree(tmp_path / "kept"))
    assert ok.returncode == 0, ok.stdout + ok.stderr

    broken = probe(tree(tmp_path / "no-dialogs", drop=["QtQuick/Dialogs"]))
    assert broken.returncode == 1, broken.stdout + broken.stderr
    assert 'module "QtQuick.Dialogs" is not installed' in broken.stdout


# --- the decisions on synthetic trees ---------------------------------------


def test_a_qmldir_dependency_outside_the_list_is_a_violation(tmp_path):
    _write_module(tmp_path, "A", ["depends B auto", "optional import C auto"])
    _write_module(tmp_path, "B")
    _write_module(tmp_path, "C")
    assert trim.closure_violations(tmp_path, keep=("A",)) == [("A", "B")]
    assert trim.closure_violations(tmp_path, keep=("A", "B")) == []


def test_qml_imports_count_except_other_styles_file_selectors(tmp_path):
    _write_module(tmp_path, "A", files={
        "Button.qml": "import B\nItem {}\n",
        "+Material/Menu.qml": "import C\n",
        "+Fusion/Menu.qml": "import D\n",
    })
    for m in ("B", "C", "D"):
        _write_module(tmp_path, m)
    assert trim.closure_violations(tmp_path, keep=("A",)) == [("A", "B"), ("A", "C")]


def test_a_nested_module_belongs_to_itself_not_its_parent(tmp_path):
    _write_module(tmp_path, "A")
    _write_module(tmp_path, "A/impl", files={"X.qml": "import B\n"})
    _write_module(tmp_path, "B")
    assert trim.closure_violations(tmp_path, keep=("A",)) == []
    assert trim.closure_violations(tmp_path, keep=("A", "A.impl")) == [("A.impl", "B")]
    assert trim.dropped_qml_dirs(tmp_path, keep=("A",)) == ["A/impl", "B"]


def test_a_kept_module_inside_a_dropped_one_is_refused(tmp_path):
    _write_module(tmp_path, "A")
    _write_module(tmp_path, "A/impl")
    with pytest.raises(trim.TrimError, match="A/impl"):
        trim.dropped_qml_dirs(tmp_path, keep=("A.impl",))


def test_a_kept_module_missing_from_qt_is_a_violation(tmp_path):
    _write_module(tmp_path, "A")
    assert trim.closure_violations(tmp_path, keep=("A", "Gone")) == [("Gone", "(not in this Qt)")]


def test_a_kept_plugin_missing_from_qt_is_refused(tmp_path):
    root = _fake_qt(tmp_path, plugins=("platforms/qminimal",))
    with pytest.raises(trim.TrimError, match="platforms/qwindows"):
        trim.Trim(root, keep_qml=("QtQuick",), keep_plugins={"platforms": ("qwindows",)})


def test_decisions_match_any_separator_and_case(tmp_path):
    root = _fake_qt(tmp_path, modules=("QtQuick", "QtQuick3D"))
    decisions = trim.Trim(root, keep_qml=("QtQuick",), keep_plugins={"platforms": ("qwindows",)})
    assert decisions.drops_dll(r"PyQt6\qml\QtQuick3D\qquick3dplugin.dll")
    assert decisions.drops_dll("pyqt6/qml/qtquick3d/qquick3dplugin.dll")
    assert not decisions.drops_dll(r"PyQt6\qml\QtQuick\qtquick2plugin.dll")
    assert not decisions.drops_dll(r"PyQt6\qml\QtQuick3DX\x.dll")  # a prefix is not a parent
    assert decisions.drops_dll(r"PyQt6\Qt6\plugins\platforms\qminimal.dll")
    assert not decisions.drops_dll(r"PyQt6\Qt6\plugins\platforms\qwindows.dll")
    assert not decisions.drops_dll("qt6quick3d.dll")  # libraries go via their users
    assert not decisions.drops_dll(r"PyQt6\QtCore.pyd")


# --- the Nuitka hooks -------------------------------------------------------


def _plugin(tmp_path, monkeypatch):
    root = _fake_qt(tmp_path, modules=("QtQuick", "QtQuick3D"))
    monkeypatch.setattr(trim, "qt_root", lambda: root)
    monkeypatch.setattr(trim, "QML_MODULES", ("QtQuick",))
    monkeypatch.setattr(trim, "QT_PLUGINS", {"platforms": ("qwindows",)})
    plugin = trim.NuitkaPluginQtTrim()
    said = []
    monkeypatch.setattr(plugin, "info", said.append, raising=False)

    def sysexit(message):
        raise SystemExit(message)

    monkeypatch.setattr(plugin, "sysexit", sysexit, raising=False)
    return plugin, said


def _entry(dest, source):
    return types.SimpleNamespace(dest_path=dest, source_path=source, tags={"copy"})


def test_a_dropped_dll_is_not_copied_and_brings_no_dependencies(tmp_path, monkeypatch):
    plugin, said = _plugin(tmp_path, monkeypatch)
    plugin.onCompilationStartChecks()
    assert "QtQuick3D" in said[0]
    dropped = _entry(r"PyQt6\qml\QtQuick3D\qquick3dplugin.dll", r"C:\qt\qml\QtQuick3D\qquick3dplugin.dll")
    kept = _entry(r"PyQt6\qml\QtQuick\qtquick2plugin.dll", r"C:\qt\qml\QtQuick\qtquick2plugin.dll")
    plugin.onDllTags(dropped)
    plugin.onDllTags(kept)
    assert "copy" not in dropped.tags and "copy" in kept.tags
    deps = (r"C:\qt\bin\Qt6Quick3D.dll", r"C:\qt\bin\Qt6Quick.dll")
    assert plugin.removeDllDependencies(dropped.source_path, deps) == deps
    assert plugin.removeDllDependencies(kept.source_path, deps) == ()


def test_a_dropped_qml_data_file_is_inhibited(tmp_path, monkeypatch):
    plugin, _said = _plugin(tmp_path, monkeypatch)
    dropped = types.SimpleNamespace(dest_path=r"PyQt6\qml\QtQuick3D\qmldir", tags={"copy", "qml"})
    kept = types.SimpleNamespace(dest_path=r"PyQt6\qml\QtQuick\qmldir", tags={"copy", "qml"})
    plugin.onDataFileTags(dropped)
    plugin.onDataFileTags(kept)
    assert dropped.tags == {"inhibit"} and kept.tags == {"copy", "qml"}


def test_a_closure_violation_stops_the_build(tmp_path, monkeypatch):
    plugin, _said = _plugin(tmp_path, monkeypatch)
    _write_module(tmp_path / "qml", "QtQuick", ["depends QtQuick3D auto"])
    with pytest.raises(SystemExit, match="QtQuick -> QtQuick3D"):
        plugin.onCompilationStartChecks()


# --- the build script -------------------------------------------------------


BUILD_NUITKA = (REPO_ROOT / "scripts" / "build_nuitka.ps1").read_text(encoding="utf-8-sig")


def test_build_nuitka_loads_the_plugin():
    assert '"--user-plugin=scripts\\nuitka_qt_trim.py"' in BUILD_NUITKA
    assert '"--include-qt-plugins=sensible,qml"' in BUILD_NUITKA


@pytest.mark.parametrize(
    "dest, dropped",
    [
        ("omotion/dfu-util/linux-amd64/dfu-util", True),
        ("omotion/dfu-util/linux-amd64/dfu-util-static", True),
        ("omotion/dfu-util/darwin-x86_64/dfu-util", True),
        ("omotion/dfu-util/darwin-x86_64/libusb-1.0.a", True),
        ("omotion/dfu-util/win64/libusb-1.0.a", True),
        ("omotion/dfu-util/win64/libusb-1.0.dll.a", True),
        ("omotion/dfu-util/win32/libusb-1.0.la", True),
        ("omotion/dfu-util/win64/dfu-util.exe", False),
        ("omotion/dfu-util/win64/libusb-1.0.dll", False),
        ("omotion/dfu-util/win32/dfu-util.exe", False),
        ("omotion/dfu-util/COPYING", False),
        ("omotion/dfu-util/README-bin.txt", False),
        ("omotion/models/10K3CG_R-T.csv", False),
        ("omotion/nvcm/impl1_data.ied", False),
    ],
)
def test_sdk_data_exclusions(dest, dropped):
    """--noinclude-data-files matches with fnmatch on Nuitka's destination
    path, normcased on Windows, so "*" also crosses directory separators."""
    import fnmatch

    patterns = re.findall(r'"--noinclude-data-files=(omotion/[^"]+)"', BUILD_NUITKA)
    assert patterns, "build_nuitka.ps1 has no omotion data exclusions"
    assert any(fnmatch.fnmatch(os.path.normpath(dest), p) for p in patterns) is dropped
