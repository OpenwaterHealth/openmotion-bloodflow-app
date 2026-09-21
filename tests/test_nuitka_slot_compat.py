"""Nuitka rebuilds every ``@pyqtSlot`` method from source at runtime (#566).

Nuitka's pyqt6 plugin lists ``pyqtSlot`` as an "uncompiled decorator": any
function it decorates is not compiled to C but re-created at import time by
``exec``-ing its unparsed source into the class namespace (filename
``<string>``). A function born that way has no ``__class__`` cell, so a
zero-argument ``super()`` inside it raises

    RuntimeError: super(): __class__ cell not found

the first time the slot runs. That is exactly how the first Nuitka build
crashed on the Scan button: LiveScanSource.value_at is a QML-callable slot
that delegates to its base class. Source runs and PyInstaller builds never
see it, so this guard is the only thing that catches a regression before a
build reaches hardware.

Rule: inside a ``pyqtSlot``-decorated function, spell ``super`` out as
``super(ClassName, self)`` and never reference ``__class__`` directly.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".claude", "build", "dist", "docs", "tests", "__pycache__", "investigations"}


def _app_modules():
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in SKIP_DIRS or any(p in SKIP_DIRS for p in rel.parts[:-1]):
            continue
        yield path


def _decorator_names(node):
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, ast.Attribute):
            yield target.attr


def _slot_functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "pyqtSlot" in set(_decorator_names(node)):
                yield node


def _class_cell_uses(func):
    """Yield (lineno, description) for every zero-arg super() / __class__ use."""
    for sub in ast.walk(func):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "super"
            and not sub.args
        ):
            yield sub.lineno, "zero-argument super()"
        elif isinstance(sub, ast.Name) and sub.id == "__class__":
            yield sub.lineno, "__class__ reference"


def test_no_zero_arg_super_inside_pyqtslot_methods():
    offenders = []
    scanned = 0
    for path in _app_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for func in _slot_functions(tree):
            scanned += 1
            for lineno, what in _class_cell_uses(func):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno} {func.name}: {what}"
                )
    assert scanned > 0, "no @pyqtSlot functions found - scanner is broken"
    assert not offenders, (
        "These @pyqtSlot methods would raise 'super(): __class__ cell not "
        "found' in a Nuitka build; use super(ClassName, self) instead:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_catches_the_original_crash():
    """The scanner must flag the exact shape that crashed the first build."""
    src = (
        "class LiveScanSource(ScanDataSource):\n"
        "    @pyqtSlot(str, int, str, float, result=float)\n"
        "    def value_at(self, side, cam_id, metric, t):\n"
        "        return super().value_at(side, cam_id, metric, t)\n"
    )
    funcs = list(_slot_functions(ast.parse(src)))
    assert [f.name for f in funcs] == ["value_at"]
    assert list(_class_cell_uses(funcs[0])) == [(4, "zero-argument super()")]

    fixed = src.replace("super()", "super(LiveScanSource, self)")
    funcs = list(_slot_functions(ast.parse(fixed)))
    assert list(_class_cell_uses(funcs[0])) == []
