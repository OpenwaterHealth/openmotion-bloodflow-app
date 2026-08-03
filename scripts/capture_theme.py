#!/usr/bin/env python3
"""Render the app once and save a PNG of its window — no screen-recording grant.

macOS only hands ``screencapture`` real pixels when the calling process holds a
Screen Recording grant, which a CI box or a fresh dev machine will not have. Qt
has no such problem rendering *itself*: ``QQuickWindow.grabWindow()`` reads back
the scene graph from inside the process. So this drives the real ``main.py``
(never a stand-in QML harness — the point is to catch a theme breaking the
actual app) and grabs the window from within.

    python scripts/capture_theme.py --theme aqua --dark --out shot.png

``main.py`` is executed unmodified; the grab is installed by wrapping
``exec()`` so the timer is armed on the running event loop.
"""
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _find_modal(root, label: str):
    """Depth-first search the QQuickItem tree for a modal by its `label`.

    Every modal in components/ declares `readonly property string label`, so
    that is a stabler handle than an objectName nothing sets.
    """
    pending = [root]
    while pending:
        item = pending.pop()
        try:
            if item.property("label") == label:
                return item
        except (AttributeError, RuntimeError):
            pass
        try:
            pending.extend(item.childItems())
        except (AttributeError, RuntimeError):
            pass
    return None


def _open_modal(win, label: str) -> bool:
    from PyQt6.QtCore import QMetaObject
    item = _find_modal(win.contentItem(), label)
    if item is None:
        print(f"CAPTURE-WARN: no modal labelled {label!r} found", file=sys.stderr)
        return False
    # QML-declared open() does the config load a bare visible=true skips, so
    # prefer it and fall back only if the invoke fails.
    if not QMetaObject.invokeMethod(item, "open"):
        item.setProperty("visible", True)
    return True


def _install_grab(delay_ms: int, out_path: Path, settle_ms: int = 900,
                  modal: str | None = None) -> None:
    """Arm a one-shot grab on the event loop, then quit."""
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QGuiApplication

    def _grab() -> None:
        windows = [w for w in QGuiApplication.topLevelWindows() if w.isVisible()]
        if not windows:
            print("CAPTURE-FAIL: no visible top-level window", file=sys.stderr)
            QGuiApplication.instance().exit(2)
            return
        # The largest visible window is the app proper; Qt also reports
        # offscreen/utility windows for popups and tooltips.
        win = max(windows, key=lambda w: w.width() * w.height())
        # topLevelWindows() hands back base QWindow wrappers; grabWindow() only
        # exists on QQuickWindow, so cast down to reach the scene-graph readback.
        if not hasattr(win, "grabWindow"):
            from PyQt6 import sip
            from PyQt6.QtQuick import QQuickWindow
            win = sip.cast(win, QQuickWindow)
        if modal and not _grab.opened:
            # Open, then come back a beat later so the modal has laid out and
            # painted before readback.
            _grab.opened = True
            _open_modal(win, modal)
            QTimer.singleShot(1200, _grab)
            return
        image = win.grabWindow()
        if image.isNull():
            print("CAPTURE-FAIL: grabWindow returned a null image", file=sys.stderr)
            QGuiApplication.instance().exit(3)
            return
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not image.save(str(out_path)):
            print(f"CAPTURE-FAIL: could not write {out_path}", file=sys.stderr)
            QGuiApplication.instance().exit(4)
            return
        print(f"CAPTURE-OK: {out_path} {image.width()}x{image.height()}")
        QGuiApplication.instance().exit(0)

    _grab.opened = False

    def _bail() -> None:
        print("CAPTURE-FAIL: watchdog expired, app never became grabbable",
              file=sys.stderr)
        QGuiApplication.instance().exit(5)

    # PyQt6 gives QApplication its own ``exec`` binding, so patching the
    # QGuiApplication base is silently shadowed — patch the most-derived class
    # main.py actually instantiates.
    from PyQt6.QtWidgets import QApplication
    target = QApplication
    orig_exec = target.exec

    def patched_exec(*args, **kwargs):
        # Two-stage: the first timer waits for QML to load and lay out, the
        # second gives the compositor a frame to actually paint before readback.
        QTimer.singleShot(delay_ms, lambda: QTimer.singleShot(settle_ms, _grab))
        # Never let a broken theme turn into a hung capture job.
        QTimer.singleShot(delay_ms + settle_ms + 20000, _bail)
        # Qt's exec takes no arguments even when reached as a bound method.
        return orig_exec()

    target.exec = patched_exec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theme", help="themeName config value to force")
    parser.add_argument("--dark", action="store_true", help="force dark mode")
    parser.add_argument("--light", action="store_true", help="force light mode")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--delay-ms", type=int, default=6000)
    parser.add_argument("--modal", help="open a modal by its QML `label` "
                                        "before capturing, e.g. Settings")
    args = parser.parse_args()

    if args.theme:
        os.environ["OPENMOTION_THEME"] = args.theme
    if args.dark:
        os.environ["OPENMOTION_DARK"] = "1"
    if args.light:
        os.environ["OPENMOTION_DARK"] = "0"
    # Research variant: clinical hides most of the chrome worth looking at.
    os.environ.setdefault("OPENMOTION_CLINICAL", "0")

    _install_grab(args.delay_ms, args.out.resolve(), modal=args.modal)

    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(PROJECT_ROOT)
    sys.argv = ["main.py"]
    try:
        runpy.run_path(str(PROJECT_ROOT / "main.py"), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
