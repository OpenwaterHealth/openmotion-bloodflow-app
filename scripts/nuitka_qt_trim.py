"""Nuitka user plugin: bundle only the Qt modules and plugins the app uses
(#680).

    python -m nuitka ... --user-plugin=scripts/nuitka_qt_trim.py

Nuitka's pyqt6 plugin is coarse. ``--include-qt-plugins=qml`` copies Qt's
whole QML tree: Quick 3D, Qt PDF, Multimedia, WebSockets, WebChannel,
RemoteObjects, Sensors, Positioning, TextToSpeech, Qt Test, every Controls
style, and the Qt libraries those QML plugins link. The "sensible" plugin
families add image formats the app never loads (ICNS, TIFF, TGA, WebP, PDF)
and the TLS backends. All of that is code an attacker could reach in the
shipped exe and that we would have to patch.

The keep-lists below are allow-lists. The plugin reads the Qt tree of the
installed PyQt6 wheel and drops everything not listed, so a Qt upgrade that
adds a module leaves it out without anyone touching this file. The other
direction stops the build: if an allowed QML module needs one that is not
allowed (a Qt upgrade added a dependency), the plugin exits and names it.
Add the dependency below after checking why it is needed.

Why a plugin and not ``--noinclude-dlls``: Nuitka 4.2 still scans the
dependencies of a DLL that option excludes, so every excluded QML plugin
kept pulling in its Qt library (Qt6Quick3D*.dll, Qt6Pdf.dll,
Qt6Multimedia.dll, ...), and the onefile step then warned about each
excluded file as "missing". Here a dropped DLL loses its "copy" tag, which
is what Nuitka checks before copying a binary into the distribution and
before packing it, and it reports no dependencies, so the Qt libraries that
only dropped plugins link are never added. Data files are inhibited the way
``--noinclude-data-files`` does it.

The guard test is tests/test_nuitka_qt_trim.py.
"""

import importlib.util
import re
from pathlib import Path

from nuitka.plugins.PluginBase import NuitkaPluginBase

# QML modules the app needs. The first block is what the app's own QML
# imports. tests/test_nuitka_qt_trim.py fails when a QML file imports a
# module that is not listed here.
QML_MODULES = (
    "QtQuick",
    "QtQuick.Controls",
    "QtQuick.Dialogs",
    "QtQuick.Effects",
    "QtQuick.Layouts",
    "QtQuick.Window",
    # The Controls style. main.py pins QT_QUICK_CONTROLS_STYLE=Material, and
    # QtQuick.Controls loads the style at runtime, so no import names it.
    "QtQuick.Controls.Material",
    "QtQuick.Controls.Material.impl",
    # What the modules above depend on: the qmldir depends/import lines and
    # the imports in the modules' own QML files. Basic is Material's
    # fallback and the Controls default.
    "QtQml",
    "QtQml.Models",
    "QtQml.WorkerScript",
    "QtQuick.Controls.impl",
    "QtQuick.Controls.Basic",
    "QtQuick.Controls.Basic.impl",
    "QtQuick.Templates",
    "QtQuick.Dialogs.quickimpl",
    "QtQuick.Shapes",
)

# The Controls style the app runs with (main.py, QT_QUICK_CONTROLS_STYLE).
# QML under a "+Fusion/", "+Imagine/", ... file-selector directory is used
# only under that style, so its imports are not dependencies here.
CONTROLS_STYLE = "Material"

# Qt plugins the app needs, per plugin family (DLL names without .dll).
# Every other plugin DLL is dropped, whole families included.
#   platforms: qwindows draws the window. qoffscreen backs a headless
#     `-platform offscreen` run.
#   imageformats: qico, for the window and taskbar icon (favicon.ico).
#     PNG is built into QtGui. The app ships no other image format, and the
#     Qt Quick modules above embed none (no SVG either, so no qsvg and no
#     iconengines/qsvgicon).
#   styles: the native widget style, for the QMessageBox dialogs main.py
#     shows before QML loads.
# There is no "tls" family here: nothing in the app or the SDK uses
# QtNetwork (updates and firmware downloads go through requests).
QT_PLUGINS = {
    "platforms": ("qwindows", "qoffscreen"),
    "imageformats": ("qico",),
    "styles": ("qmodernwindowsstyle",),
}

# Nuitka's destination paths for the PyQt6 wheel's Qt tree.
QML_DEST = "PyQt6/qml"
PLUGINS_DEST = "PyQt6/Qt6/plugins"

_IMPORT = re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)", re.MULTILINE)


class TrimError(Exception):
    pass


def qt_root():
    """The Qt6 directory of the installed PyQt6 wheel. Found without
    importing PyQt6, so no Qt DLL is loaded into the compiling process."""
    spec = importlib.util.find_spec("PyQt6")
    if spec is None or not spec.submodule_search_locations:
        raise TrimError("PyQt6 is not installed in the build environment")
    root = Path(list(spec.submodule_search_locations)[0]) / "Qt6"
    if not (root / "qml").is_dir() or not (root / "plugins").is_dir():
        raise TrimError(f"no Qt qml/plugins tree under {root}")
    return root


def qml_modules(qml_dir):
    """{module name: directory relative to qml_dir} for every QML module
    in the tree, read from each qmldir's ``module`` line."""
    found = {}
    for qmldir in sorted(Path(qml_dir).rglob("qmldir")):
        rel = qmldir.parent.relative_to(qml_dir).as_posix()
        name = rel.replace("/", ".")
        for line in qmldir.read_text(encoding="utf-8", errors="replace").splitlines():
            words = line.split()
            if len(words) >= 2 and words[0] == "module":
                name = words[1]
                break
        found[name] = rel
    return found


def module_requirements(qml_dir, rel, module_dirs, style=CONTROLS_STYLE):
    """Modules the module in ``rel`` needs: its qmldir's ``depends``,
    ``import`` and ``default import`` lines (not ``optional import``), plus
    the imports in its own QML files. Nested modules and the file-selector
    variants of other Controls styles are left out."""
    base = Path(qml_dir) / rel
    needs = set()
    for line in (base / "qmldir").read_text(encoding="utf-8", errors="replace").splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] in ("depends", "import"):
            needs.add(words[1])
        elif len(words) >= 3 and words[0] == "default" and words[1] == "import":
            needs.add(words[2])
    nested = {Path(qml_dir) / d for d in module_dirs if d != rel and d.startswith(rel + "/")}
    for qml in base.rglob("*.qml"):
        parents = qml.relative_to(base).parents
        if any(base / p in nested for p in parents):
            continue
        if any(p.name.startswith("+") and p.name != "+" + style for p in parents):
            continue
        needs.update(_IMPORT.findall(qml.read_text(encoding="utf-8", errors="replace")))
    return needs


def closure_violations(qml_dir, keep=QML_MODULES):
    """[(kept module, module it needs that is not kept)]. Needs with no
    directory in the tree are skipped: they are resolved elsewhere (the
    ``QML`` pseudo-module, or a module whose qmldir is embedded in its
    library's resources, such as Qt.labs.folderlistmodel), so there is
    nothing to bundle or drop."""
    modules = qml_modules(qml_dir)
    missing = [(m, "(not in this Qt)") for m in keep if m not in modules]
    if missing:
        return missing
    out = []
    for module in keep:
        for need in sorted(module_requirements(qml_dir, modules[module], modules.values())):
            if need in modules and need not in keep:
                out.append((module, need))
    return out


def dropped_qml_dirs(qml_dir, keep=QML_MODULES):
    """Directories (relative to qml_dir) of the modules not kept. A module
    nested in a dropped one goes with its parent; a kept module nested in a
    dropped one is a configuration error."""
    modules = qml_modules(qml_dir)
    kept = {modules[m] for m in keep if m in modules}
    out = []
    for d in sorted(d for d in modules.values() if d not in kept):
        if any(d.startswith(o + "/") for o in out):
            continue
        swallowed = sorted(k for k in kept if k.startswith(d + "/"))
        if swallowed:
            raise TrimError(f"dropping {d} would also drop kept module dir(s) {swallowed}")
        out.append(d)
    return out


def missing_qt_plugins(plugins_dir, keep=QT_PLUGINS):
    """Kept plugins the wheel does not carry, as "family/name"."""
    return sorted(
        f"{family}/{name}"
        for family, names in keep.items()
        for name in names
        if not (Path(plugins_dir) / family / f"{name}.dll").is_file()
    )


def _dest(path):
    return str(path).replace("\\", "/").lower()


class Trim:
    """The drop decisions for one Qt tree, on Nuitka destination paths
    (``PyQt6\\qml\\...``, ``PyQt6\\Qt6\\plugins\\...``; any separator, any
    case)."""

    def __init__(self, root=None, keep_qml=None, keep_plugins=None):
        root = Path(root) if root else qt_root()
        keep_qml = QML_MODULES if keep_qml is None else keep_qml
        keep_plugins = QT_PLUGINS if keep_plugins is None else keep_plugins
        qml_dir, plugins_dir = root / "qml", root / "plugins"
        violations = closure_violations(qml_dir, keep_qml)
        if violations:
            raise TrimError(
                "kept QML module(s) need modules that are not kept (add them to "
                "QML_MODULES in scripts/nuitka_qt_trim.py after checking why): "
                + ", ".join(f"{m} -> {n}" for m, n in violations)
            )
        missing = missing_qt_plugins(plugins_dir, keep_plugins)
        if missing:
            raise TrimError(f"kept Qt plugin(s) not found under {plugins_dir}: {missing}")
        self.dropped_dirs = dropped_qml_dirs(qml_dir, keep_qml)
        self._qml_prefixes = tuple(_dest(f"{QML_DEST}/{d}/") for d in self.dropped_dirs)
        self._plugins_prefix = _dest(PLUGINS_DEST + "/")
        self._kept_plugins = {
            _dest(f"{PLUGINS_DEST}/{family}/{name}.dll")
            for family, names in keep_plugins.items()
            for name in names
        }

    def drops_qml(self, dest_path):
        return _dest(dest_path).startswith(self._qml_prefixes)

    def drops_plugin(self, dest_path):
        dest = _dest(dest_path)
        return dest.startswith(self._plugins_prefix) and dest not in self._kept_plugins

    def drops_dll(self, dest_path):
        return self.drops_qml(dest_path) or self.drops_plugin(dest_path)


class NuitkaPluginQtTrim(NuitkaPluginBase):
    plugin_name = "qt-trim"
    plugin_desc = "Bundle only the Qt modules and plugins the app uses (#680)."

    def __init__(self):
        self._trim = None
        self._dropped_sources = set()
        self._dropped_dlls = 0
        self._dropped_data = 0

    def _decisions(self):
        if self._trim is None:
            try:
                self._trim = Trim()
            except TrimError as exc:
                self.sysexit(f"qt-trim: {exc}")
            self.info(
                "keeping %d QML modules and Qt plugins %s; dropping QML module dirs: %s"
                % (
                    len(QML_MODULES),
                    ", ".join(f"{f}/{n}" for f, names in QT_PLUGINS.items() for n in names),
                    ", ".join(self._trim.dropped_dirs),
                )
            )
        return self._trim

    def onCompilationStartChecks(self):
        self._decisions()

    def onDllTags(self, included_entry_point):
        if self._decisions().drops_dll(included_entry_point.dest_path):
            included_entry_point.tags.discard("copy")
            self._dropped_sources.add(_dest(included_entry_point.source_path))
            self._dropped_dlls += 1

    def removeDllDependencies(self, dll_filename, dll_filenames):
        # A dropped DLL never loads, so nothing it links is needed on its
        # account. Whatever a kept binary links is still added for that one.
        if _dest(dll_filename) in self._dropped_sources:
            return tuple(dll_filenames)
        return ()

    def onDataFileTags(self, included_datafile):
        if self._decisions().drops_qml(included_datafile.dest_path):
            included_datafile.tags.clear()
            included_datafile.tags.add("inhibit")
            self._dropped_data += 1

    def onStandaloneDistributionFinished(self, dist_dir):
        self.info(
            "dropped %d Qt plugin / QML DLLs and %d QML data files"
            % (self._dropped_dlls, self._dropped_data)
        )
