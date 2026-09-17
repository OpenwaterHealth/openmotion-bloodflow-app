"""One answer to "are we running from a built executable, and where is it?"

The app has three runtime shapes and two of them look alike to the wrong
check:

* source run: nothing is frozen, resources sit in the repo.
* PyInstaller (up to #547): ``sys.frozen`` is True, ``sys._MEIPASS`` is the
  extraction directory, ``sys.executable`` is the launched exe.
* Nuitka (#548): ``sys.frozen`` is **not** set, ``sys._MEIPASS`` does not
  exist, and in onefile mode ``sys.executable`` is the *extracted*
  ``python.exe`` inside the temporary directory, not the exe the user ran.
  Every compiled module instead carries a ``__compiled__`` attribute whose
  ``original_argv0`` is the launched exe and ``containing_dir`` its folder.

Everything that used to ask ``getattr(sys, "frozen", False)`` or reach for
``sys.executable`` goes through here, so the answers stay right under both
bundlers. Getting ``is_frozen`` wrong is not cosmetic: ``main._parse_dev_args``
drops the ``--clinical`` / ``--research`` / ``--data-root`` flags only when
frozen, which is what stops an installed exe from being flipped to another
variant from a shortcut.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _compiled():
    """Nuitka's ``__compiled__`` marker for this module, or None."""
    return globals().get("__compiled__")


def is_frozen() -> bool:
    """True inside any built executable (PyInstaller or Nuitka)."""
    return bool(getattr(sys, "frozen", False)) or _compiled() is not None


def bundler() -> str:
    """'pyinstaller', 'nuitka' or 'source', for logs and the startup report."""
    if _compiled() is not None:
        return "nuitka"
    if getattr(sys, "frozen", False):
        return "pyinstaller"
    return "source"


def executable_path() -> Path:
    """The executable the user launched.

    Under Nuitka onefile that is ``__compiled__.original_argv0`` (the outer
    exe); ``sys.executable`` there would be the extracted interpreter in the
    temp directory, which is the wrong answer for "next to the exe" data,
    for the installed-vs-portable check and for the updater's relaunch.
    """
    original = getattr(_compiled(), "original_argv0", None)
    if original:
        return Path(original)
    return Path(sys.executable)


def bundle_dir() -> Path:
    """Where bundled resources (QML, assets, the sample scan) live at runtime.

    PyInstaller: ``sys._MEIPASS``. Nuitka and source runs: the directory
    this package was unpacked into / the repo root, i.e. the parent of
    ``utils/``; Nuitka lays data files out relative to the same root the
    modules are extracted to, so the layout is identical to a source tree.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent
